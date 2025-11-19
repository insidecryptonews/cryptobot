import os
import time
import logging
from decimal import Decimal, ROUND_DOWN

from binance.client import Client
from binance.websockets import BinanceSocketManager
from binance.exceptions import BinanceAPIException, BinanceRequestException

# ===================== CONFIG =====================

SYMBOLS = [
    "BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC",
    "LINKUSDC", "ADAUSDC", "DOGEUSDC", "AVAXUSDC", "NEARUSDC",
    "ATOMUSDC", "DOTUSDC", "FILUSDC",
]

USDC_ASSET = "USDC"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)

API_KEY = os.getenv("BINANCE_API_KEY") or os.getenv("BINANCE_KEY") or ""
API_SECRET = os.getenv("BINANCE_API_SECRET") or os.getenv("BINANCE_SECRET") or ""

TRADE_REAL = os.getenv("TRADING_REAL", "false").lower() == "true"
USE_MARGIN = os.getenv("USE_MARGIN", "true").lower() == "true"
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
EDGE_MIN_DIFF = float(os.getenv("EDGE_MIN_DIFF", "0.2"))  # diferencia mínima en %
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "60"))

# Para no liarla con deuda, por ahora NO usamos auto-borrow.
# Operamos solo con el saldo libre que tengas en margen.
MIN_NOTIONAL_USD = float(os.getenv("MIN_NOTIONAL_USD", "5"))  # si hay menos que esto, no operamos


# ===================== STREAM DE PRECIOS =====================

class PriceStream:
    """
    Mantiene en memoria el % de cambio del último cierre de vela para cada símbolo,
    usando WebSocket de kline. Anti-ban porque no hacemos peticiones REST para el precio.
    """

    def __init__(self, client: Client, symbols, interval: str):
        self.client = client
        self.symbols = symbols
        self.interval = interval
        self._bsm = BinanceSocketManager(client)
        self.last_change = {}  # symbol -> pct_change
        self._conns = []

    def _handle_kline(self, msg):
        """
        msg['k'] es la vela. Usamos el cierre de la vela anterior (cuando x == True).
        """
        if msg.get("e") == "error":
            logging.error("WebSocket error: %s", msg)
            return

        data = msg.get("k") or {}
        is_closed = data.get("x")
        if not is_closed:
            return  # solo cuando la vela se cierra, para que sea estable

        symbol = msg.get("s")
        try:
            o = float(data["o"])
            c = float(data["c"])
        except (KeyError, ValueError, TypeError):
            return

        pct = (c - o) / o * 100.0
        self.last_change[symbol] = pct

    def start(self):
        for sym in self.symbols:
            conn_key = self._bsm.start_kline_socket(sym, self._handle_kline, interval=self.interval)
            self._conns.append(conn_key)
        self._bsm.start()
        logging.info("WebSocket de precios iniciado para %d símbolos (%s, timeframe=%s)",
                     len(self.symbols), ", ".join(self.symbols), self.interval)

    def stop(self):
        for key in self._conns:
            try:
                self._bsm.stop_socket(key)
            except Exception:
                pass
        self._bsm.close()
        logging.info("WebSocket de precios detenido")


# ===================== LÓGICA DE TRADING =====================

class CryptoBot:
    def __init__(self):
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Faltan BINANCE_API_KEY / BINANCE_API_SECRET en variables de entorno")

        self.client = Client(API_KEY, API_SECRET)

        self.symbols = SYMBOLS
        self.current_symbol = None  # símbolo en el que estamos posicionados
        self.stream = PriceStream(self.client, self.symbols, TIMEFRAME)

        logging.info("Iniciando CryptoBot ANTI-BAN")
        logging.info("Trading real: %s", TRADE_REAL)
        logging.info("Margen activado: %s (x%s)", USE_MARGIN, LEVERAGE)
        logging.info("Timeframe: %s | EDGE_MIN_DIFF: %.3f%%", TIMEFRAME, EDGE_MIN_DIFF)

    # ---------- UTILIDADES DE MARGEN ----------

    def _get_margin_balances(self):
        """
        Devuelve:
          usdc_free (float),
          posiciones dict: base_asset -> free_qty
        """
        try:
            acc = self.client.get_margin_account()
        except BinanceAPIException as e:
            logging.error("Error al obtener cuenta de margen: %s", e)
            return 0.0, {}

        usdc_free = 0.0
        posiciones = {}
        for asset in acc.get("userAssets", []):
            asset_name = asset.get("asset")
            free = float(asset.get("free", "0"))
            if asset_name == USDC_ASSET:
                usdc_free = free
            else:
                if free > 0:
                    posiciones[asset_name] = free
        return usdc_free, posiciones

    def _round_step(self, quantity: float, step: str) -> float:
        """
        Redondea la cantidad a múltiplos del 'stepSize' que viene en los filtros de LOT_SIZE.
        """
        step_dec = Decimal(step)
        q = Decimal(quantity)
        return float(q.quantize(step_dec, rounding=ROUND_DOWN).normalize())

    def _get_symbol_filters(self, symbol: str):
        info = self.client.get_symbol_info(symbol)
        lot_step = "0.000001"
        min_notional = 10.0
        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                lot_step = f.get("stepSize", lot_step)
            if f.get("filterType") == "MIN_NOTIONAL":
                try:
                    min_notional = float(f.get("minNotional", min_notional))
                except (TypeError, ValueError):
                    pass
        return lot_step, min_notional

    def _place_margin_market_sell(self, symbol: str, base_qty: float):
        if base_qty <= 0:
            return
        try:
            lot_step, _ = self._get_symbol_filters(symbol)
            qty_rounded = self._round_step(base_qty, lot_step)
            if qty_rounded <= 0:
                logging.warning("Cantidad a vender demasiado pequeña en %s", symbol)
                return
            if not TRADE_REAL:
                logging.info("[SIM] VENDER %s %.6f", symbol, qty_rounded)
                return
            logging.info("VENDIENDO %s %.6f (margen)", symbol, qty_rounded)
            self.client.create_margin_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=qty_rounded,
            )
        except (BinanceAPIException, BinanceRequestException) as e:
            logging.error("Error al vender %s: %s", symbol, e)

    def _place_margin_market_buy(self, symbol: str, usdc_amount: float):
        if usdc_amount <= 0:
            return
        try:
            # Obtenemos precio de mercado rápido (1 llamada REST por cambio de moneda).
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            price = float(ticker["price"])
            base_qty = usdc_amount / price

            lot_step, min_notional = self._get_symbol_filters(symbol)
            if usdc_amount < max(min_notional, MIN_NOTIONAL_USD):
                logging.warning(
                    "Capital insuficiente para comprar %s: %.3f USDC (min %.3f)",
                    symbol, usdc_amount, max(min_notional, MIN_NOTIONAL_USD),
                )
                return

            qty_rounded = self._round_step(base_qty, lot_step)
            if qty_rounded <= 0:
                logging.warning("Cantidad a comprar demasiado pequeña en %s", symbol)
                return

            if not TRADE_REAL:
                logging.info("[SIM] COMPRAR %s %.6f (≈ %.2f USDC)", symbol, qty_rounded, usdc_amount)
                return

            logging.info("COMPRANDO %s %.6f (≈ %.2f USDC, margen sin deuda)",
                         symbol, qty_rounded, usdc_amount)
            self.client.create_margin_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quantity=qty_rounded,
            )
        except (BinanceAPIException, BinanceRequestException) as e:
            logging.error("Error al comprar %s: %s", symbol, e)

    # ---------- CORE LOOP ----------

    def run(self):
        # Iniciar WebSocket
        self.stream.start()

        # Pequeño warm-up
        logging.info("Calentando stream de precios...")
        time.sleep(10)

        while True:
            try:
                if not self.stream.last_change:
                    logging.info("Aún no hay datos de precios, esperamos 5s...")
                    time.sleep(5)
                    continue

                # Buscar mejor símbolo según % de cambio de la última vela cerrada
                best_symbol = None
                best_change = -999.0
                for sym, pct in self.stream.last_change.items():
                    if pct > best_change:
                        best_change = pct
                        best_symbol = sym

                if best_symbol is None:
                    logging.info("Sin datos suficientes todavía, esperamos...")
                    time.sleep(5)
                    continue

                current_change = self.stream.last_change.get(self.current_symbol, 0.0)
                edge = best_change - current_change

                logging.info(
                    "Actual: %s (%.3f%%) | Mejor: %s (%.3f%%) | Diff=%.3f%%",
                    self.current_symbol or "USDC",
                    current_change,
                    best_symbol,
                    best_change,
                    edge,
                )

                # Si no supera el umbral, no hacemos nada
                if edge < EDGE_MIN_DIFF and self.current_symbol == best_symbol:
                    logging.info("No cambiamos: edge %.3f%% < %.3f%% o ya estamos en %s",
                                 edge, EDGE_MIN_DIFF, best_symbol)
                    time.sleep(CYCLE_SECONDS)
                    continue

                if edge < EDGE_MIN_DIFF and self.current_symbol is None:
                    logging.info("Aún no merece la pena entrar; edge=%.3f%% < %.3f%%", edge, EDGE_MIN_DIFF)
                    time.sleep(CYCLE_SECONDS)
                    continue

                # Si llegamos aquí y best_symbol es diferente, planteamos cambio
                if best_symbol != self.current_symbol:
                    logging.info("Posible cambio de moneda: %s -> %s (edge=%.3f%%)",
                                 self.current_symbol or "USDC", best_symbol, edge)

                    usdc_free, posiciones = self._get_margin_balances()
                    logging.info("Saldo margen: %.4f %s | posiciones: %s",
                                 usdc_free, USDC_ASSET, posiciones)

                    # 1) vender posición anterior si existe
                    if self.current_symbol:
                        base_asset = self.current_symbol.replace(USDC_ASSET, "")
                        base_qty = posiciones.get(base_asset, 0.0)
                        if base_qty > 0:
                            self._place_margin_market_sell(self.current_symbol, base_qty)
                        else:
                            logging.info("No hay %s para vender", base_asset)

                        # refrescar balances tras venta
                        time.sleep(2)
                        usdc_free, posiciones = self._get_margin_balances()

                    # 2) comprar nueva moneda con prácticamente todo el USDC
                    capital = usdc_free * 0.98  # dejamos un pequeño margen para comisiones
                    if capital < MIN_NOTIONAL_USD:
                        logging.warning("Capital en USDC demasiado bajo (%.3f). No operamos.", capital)
                    else:
                        self._place_margin_market_buy(best_symbol, capital)
                        self.current_symbol = best_symbol

                time.sleep(CYCLE_SECONDS)

            except KeyboardInterrupt:
                logging.info("Bot detenido manualmente (Ctrl+C).")
                break
            except Exception as e:
                logging.exception("Error inesperado en el loop principal: %s", e)
                time.sleep(10)


if __name__ == "__main__":
    bot = CryptoBot()
    bot.run()