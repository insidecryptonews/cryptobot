import os
import time
import logging
from decimal import Decimal, ROUND_DOWN

from binance.client import Client
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
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
EDGE_MIN_DIFF = float(os.getenv("EDGE_MIN_DIFF", "0.2"))
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "60"))
MIN_NOTIONAL_USD = float(os.getenv("MIN_NOTIONAL_USD", "5"))

# ===================== FUNCIÓN DE VELAS (REST) =====================

def get_last_closed_candle_pct(client, symbol, interval):
    """
    Usa REST (no WebSocket) para obtener la vela cerrada anterior y calcular %.
    Simbolos en USDT para precio, pero operamos en USDC.
    """
    try:
        candles = client.get_klines(symbol=symbol, interval=interval, limit=2)
        o = float(candles[-2][1])
        c = float(candles[-2][4])
        return (c - o) / o * 100.0
    except Exception as e:
        logging.error("Error obteniendo vela de %s: %s", symbol, e)
        return None

# ===================== BOT =====================

class CryptoBot:
    def __init__(self):
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Faltan claves de Binance")

        self.client = Client(API_KEY, API_SECRET)
        self.symbols = SYMBOLS[:]  # copia
        self.current_symbol = None

        logging.info("Iniciando CryptoBot (REST anti-ban + auto-ajuste LOT_SIZE/minQty)")

    # ---------- BALANCES ----------

    def _get_margin_balances(self):
        try:
            acc = self.client.get_margin_account()
        except BinanceAPIException as e:
            logging.error("Error al obtener cuenta de margen: %s", e)
            return 0.0, {}

        usdc_free = 0.0
        posiciones = {}

        for a in acc.get("userAssets", []):
            asset = a.get("asset")
            free = float(a.get("free", 0))
            if asset == USDC_ASSET:
                usdc_free = free
            else:
                if free > 0:
                    posiciones[asset] = free

        return usdc_free, posiciones

    # ---------- FILTROS ----------

    def _round_step(self, qty, step):
        return float(Decimal(qty).quantize(Decimal(step), rounding=ROUND_DOWN).normalize())

    def _get_filters(self, symbol):
        """
        Devuelve: (stepSize, minNotional, minQty)
        """
        info = self.client.get_symbol_info(symbol)
        lot_step = "0.000001"
        min_notional = 10.0
        min_qty = 0.0

        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                lot_step = f.get("stepSize", lot_step)
                try:
                    min_qty = float(f.get("minQty", "0"))
                except:
                    min_qty = 0.0
            if f.get("filterType") == "MIN_NOTIONAL":
                try:
                    min_notional = float(f.get("minNotional", min_notional))
                except:
                    pass

        return lot_step, min_notional, min_qty

    # ---------- ORDEN DE VENTA ----------

    def _sell_all(self, symbol, positions):
        base_asset = symbol.replace("USDC", "")
        base_qty = positions.get(base_asset, 0.0)

        if base_qty <= 0:
            return

        lot_step, _, _ = self._get_filters(symbol)
        qty = self._round_step(base_qty, lot_step)

        if qty <= 0:
            return

        if not TRADE_REAL:
            logging.info("[SIM] SELL %s %.6f", symbol, qty)
            return

        try:
            logging.info("VENDIENDO %s %.6f", symbol, qty)
            self.client.create_margin_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=qty,
            )
        except Exception as e:
            logging.error("Error SELL %s: %s", symbol, e)

    # ---------- ORDEN DE COMPRA (CON AUTO-FILTRO) ----------

    def _buy_with_all(self, symbol, usdc_amount):
        """
        Intenta comprar con TODO el USDC disponible.
        Si no cumple minNotional o minQty, devuelve False y NO lanza error.
        """
        if usdc_amount <= 0:
            logging.info("Sin USDC para comprar %s", symbol)
            return False

        try:
            price = float(self.client.get_symbol_ticker(symbol=symbol)["price"])
        except Exception as e:
            logging.error("No se pudo obtener precio para %s: %s", symbol, e)
            return False

        lot_step, min_notional, min_qty = self._get_filters(symbol)

        # chequeo mínimo en notional (USDC)
        needed_notional = max(min_notional, MIN_NOTIONAL_USD)
        if usdc_amount < needed_notional:
            logging.info("⛔ %s requiere al menos %.2f USDC (tienes %.2f) — saltado",
                         symbol, needed_notional, usdc_amount)
            return False

        # cantidad base que se podría comprar
        raw_qty = usdc_amount / price

        # chequeo mínimo en cantidad base (minQty)
        if raw_qty < min_qty:
            logging.info("⛔ %s requiere minQty %.6f (tú llegarías a %.6f) — saltado",
                         symbol, min_qty, raw_qty)
            return False

        # aplicar stepSize
        qty = self._round_step(raw_qty, lot_step)
        if qty <= 0:
            logging.info("⛔ Cantidad redondeada a 0 en %s — saltado", symbol)
            return False

        if not TRADE_REAL:
            logging.info("[SIM] BUY %s %.6f (≈ %.2f USDC)", symbol, qty, usdc_amount)
            return True

        try:
            logging.info("COMPRANDO %s %.6f (≈ %.2f USDC)", symbol, qty, usdc_amount)
            self.client.create_margin_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quantity=qty,
            )
            return True
        except Exception as e:
            logging.error("Error BUY %s: %s", symbol, e)
            return False

    # ===================== LOOP PRINCIPAL =====================

    def run(self):

        while True:
            try:
                if not self.symbols:
                    logging.warning("No quedan símbolos operables con el capital actual. Se queda en USDC.")
                    time.sleep(CYCLE_SECONDS)
                    continue

                # 1) elegir mejor símbolo por % de cambio (precio en USDT)
                best_symbol = None
                best_change = -999.0

                for sym in self.symbols:
                    ws_sym = sym.replace("USDC", "USDT")
                    pct = get_last_closed_candle_pct(self.client, ws_sym, TIMEFRAME)
                    if pct is None:
                        continue
                    if pct > best_change:
                        best_change = pct
                        best_symbol = sym

                if best_symbol is None:
                    logging.info("Sin datos de velas todavía…")
                    time.sleep(5)
                    continue

                # 2) cambio actual de la moneda en la que estamos (si hay)
                curr_change = 0.0
                if self.current_symbol:
                    ws_curr = self.current_symbol.replace("USDC", "USDT")
                    curr_change = get_last_closed_candle_pct(self.client, ws_curr, TIMEFRAME) or 0.0

                edge = best_change - curr_change

                logging.info(
                    "Actual: %s (%.3f%%) | Mejor: %s (%.3f%%) | Diff=%.3f%%",
                    self.current_symbol or "USDC",
                    curr_change,
                    best_symbol,
                    best_change,
                    edge,
                )

                # si ya estamos en la mejor y el edge no mejora, no hacemos nada
                if edge < EDGE_MIN_DIFF and self.current_symbol == best_symbol:
                    time.sleep(CYCLE_SECONDS)
                    continue

                # si no estamos en nada y el edge es pequeño/negativo, esperamos
                if edge < EDGE_MIN_DIFF and self.current_symbol is None:
                    time.sleep(CYCLE_SECONDS)
                    continue

                # 3) mirar balances
                usdc_free, posiciones = self._get_margin_balances()

                # vender posición actual si existe
                if self.current_symbol:
                    self._sell_all(self.current_symbol, posiciones)
                    time.sleep(1)
                    usdc_free, posiciones = self._get_margin_balances()

                # 4) intentar comprar la nueva moneda con casi todo el saldo
                capital = usdc_free * 0.98
                ok = self._buy_with_all(best_symbol, capital)

                if ok:
                    self.current_symbol = best_symbol
                else:
                    logging.info("⛔ No se pudo comprar %s con el capital actual. Se queda en USDC.", best_symbol)
                    # eliminar este símbolo de la lista para no insistir
                    self.symbols = [s for s in self.symbols if s != best_symbol]
                    self.current_symbol = None

                time.sleep(CYCLE_SECONDS)

            except KeyboardInterrupt:
                logging.info("Bot detenido manualmente.")
                break
            except Exception as e:
                logging.exception("Error inesperado en el loop: %s", e)
                time.sleep(10)


if __name__ == "__main__":
    bot = CryptoBot()
    bot.run()