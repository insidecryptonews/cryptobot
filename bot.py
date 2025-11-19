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
EDGE_MIN_DIFF = float(os.getenv("EDGE_MIN_DIFF", "0.2"))
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "60"))

MIN_NOTIONAL_USD = float(os.getenv("MIN_NOTIONAL_USD", "5"))

# ===================== STREAM =====================

class PriceStream:
    """
    FIX 1: WebSocket coge velas de USDT (SPOT) porque USDC no envía.
    FIX 2: Guardamos los símbolos tal cual vengan (USDT).
    """

    def __init__(self, client: Client, symbols, interval: str):
        self.client = client
        self.symbols = symbols
        self.interval = interval
        self._bsm = BinanceSocketManager(client)
        self.last_change = {}
        self._conns = []

    def _handle_kline(self, msg):

        if msg.get("e") == "error":
            logging.error("WebSocket error: %s", msg)
            return

        data = msg.get("k") or {}
        if not data.get("x"):
            return

        symbol = msg.get("s")
        try:
            o = float(data["o"])
            c = float(data["c"])
        except:
            return

        pct = (c - o) / o * 100.0
        self.last_change[symbol] = pct

    def start(self):
        for sym in self.symbols:
            ws_symbol = sym.replace("USDC", "USDT")  # <- FIX PRINCIPAL
            conn_key = self._bsm.start_kline_socket(
                ws_symbol, self._handle_kline, interval=self.interval
            )
            self._conns.append(conn_key)

        self._bsm.start()
        logging.info("WebSocket iniciado con FIX para USDC/USDT")

    def stop(self):
        for key in self._conns:
            try:
                self._bsm.stop_socket(key)
            except:
                pass
        self._bsm.close()


# ===================== BOT =====================

class CryptoBot:
    def __init__(self):
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Faltan claves de Binance")

        self.client = Client(API_KEY, API_SECRET)
        self.symbols = SYMBOLS
        self.current_symbol = None
        self.stream = PriceStream(self.client, self.symbols, TIMEFRAME)

        logging.info("Iniciando CryptoBot ANTI-BAN + FIX USDC")

    def _get_margin_balances(self):
        try:
            acc = self.client.get_margin_account()
        except BinanceAPIException as e:
            logging.error("Error al obtener margin: %s", e)
            return 0.0, {}

        usdc_free = 0.0
        posiciones = {}
        for asset in acc.get("userAssets", []):
            a = asset.get("asset")
            free = float(asset.get("free", 0))
            if a == USDC_ASSET:
                usdc_free = free
            else:
                if free > 0:
                    posiciones[a] = free
        return usdc_free, posiciones

    def _round_step(self, quantity: float, step: str) -> float:
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
                except:
                    pass
        return lot_step, min_notional

    def _place_margin_market_sell(self, symbol, base_qty):
        if base_qty <= 0:
            return
        try:
            lot_step, _ = self._get_symbol_filters(symbol)
            qty_rounded = self._round_step(base_qty, lot_step)

            if qty_rounded <= 0:
                return

            if not TRADE_REAL:
                logging.info("[SIM] VENDER %s %.6f", symbol, qty_rounded)
                return

            logging.info("VENDIENDO %s %.6f", symbol, qty_rounded)
            self.client.create_margin_order(
                symbol=symbol, side="SELL", type="MARKET", quantity=qty_rounded
            )
        except Exception as e:
            logging.error("Error al vender %s: %s", symbol, e)

    def _place_margin_market_buy(self, symbol, usdc_amount):
        if usdc_amount <= 0:
            return

        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            price = float(ticker["price"])

            base_qty = usdc_amount / price
            lot_step, min_notional = self._get_symbol_filters(symbol)

            if usdc_amount < max(min_notional, MIN_NOTIONAL_USD):
                logging.warning("Capital insuficiente para %s", symbol)
                return

            qty_rounded = self._round_step(base_qty, lot_step)

            if qty_rounded <= 0:
                return

            if not TRADE_REAL:
                logging.info("[SIM] COMPRAR %s %.6f", symbol, qty_rounded)
                return

            logging.info("COMPRANDO %s %.6f", symbol, qty_rounded)
            self.client.create_margin_order(
                symbol=symbol, side="BUY", type="MARKET", quantity=qty_rounded
            )

        except Exception as e:
            logging.error("Error al comprar %s: %s", symbol, e)

    # ===================== LOOP =====================

    def run(self):

        self.stream.start()
        logging.info("Calentando 10s...")
        time.sleep(10)

        while True:
            try:
                # FIX 2 → convertir el símbolo actual a USDT para buscar velas
                if self.current_symbol:
                    ws_curr = self.current_symbol.replace("USDC", "USDT")
                else:
                    ws_curr = None

                if not self.stream.last_change:
                    logging.info("Esperando datos...")
                    time.sleep(5)
                    continue

                # Buscar mejor moneda (basada en USDT)
                best_symbol = None
                best_change = -999

                for sym in self.symbols:
                    ws_sym = sym.replace("USDC", "USDT")
                    pct = self.stream.last_change.get(ws_sym, None)
                    if pct is not None and pct > best_change:
                        best_change = pct
                        best_symbol = sym  # <- devolvemos USDC porque queremos operar USDC

                if best_symbol is None:
                    logging.info("No hay velas aún…")
                    time.sleep(5)
                    continue

                curr_change = 0.0
                if ws_curr:
                    curr_change = self.stream.last_change.get(ws_curr, 0.0)

                edge = best_change - curr_change

                logging.info(
                    "Actual: %s (%.3f%%) | Mejor: %s (%.3f%%) | Diff=%.3f%%",
                    self.current_symbol or "USDC", curr_change,
                    best_symbol, best_change, edge
                )

                # reglas de cambio
                if edge < EDGE_MIN_DIFF and self.current_symbol == best_symbol:
                    time.sleep(CYCLE_SECONDS)
                    continue

                if edge < EDGE_MIN_DIFF and self.current_symbol is None:
                    time.sleep(CYCLE_SECONDS)
                    continue

                # --- CAMBIO REAL ---
                if best_symbol != self.current_symbol:

                    usdc_free, posiciones = self._get_margin_balances()

                    if self.current_symbol:
                        base_asset = self.current_symbol.replace("USDC", "")
                        base_qty = posiciones.get(base_asset, 0.0)
                        if base_qty > 0:
                            self._place_margin_market_sell(self.current_symbol, base_qty)

                        time.sleep(2)
                        usdc_free, posiciones = self._get_margin_balances()

                    capital = usdc_free * 0.98
                    if capital >= MIN_NOTIONAL_USD:
                        self._place_margin_market_buy(best_symbol, capital)
                        self.current_symbol = best_symbol

                time.sleep(CYCLE_SECONDS)

            except KeyboardInterrupt:
                logging.info("Bot detenido manualmente.")
                break
            except Exception as e:
                logging.exception("Error inesperado: %s", e)
                time.sleep(10)


if __name__ == "__main__":
    bot = CryptoBot()
    bot.run()