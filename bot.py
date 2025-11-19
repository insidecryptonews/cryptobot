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
    try:
        candles = client.get_klines(symbol=symbol, interval=interval, limit=2)
        o = float(candles[-2][1])
        c = float(candles[-2][4])
        return (c - o) / o * 100
    except:
        return None

# ===================== BOT =====================

class CryptoBot:
    def __init__(self):
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Faltan claves de Binance")

        self.client = Client(API_KEY, API_SECRET)
        self.symbols = SYMBOLS
        self.current_symbol = None

        logging.info("Iniciando CryptoBot (REST anti-ban + auto-ajuste LOT_SIZE)")

    # ---------- BALANCES ----------

    def _get_margin_balances(self):
        try:
            acc = self.client.get_margin_account()
        except:
            return 0.0, {}

        usdc_free = 0.0
        posiciones = {}

        for a in acc.get("userAssets", []):
            asset = a["asset"]
            free = float(a["free"])
            if asset == USDC_ASSET:
                usdc_free = free
            else:
                if free > 0:
                    posiciones[asset] = free

        return usdc_free, posiciones

    # ---------- FILTROS ----------

    def _round_step(self, qty, step):
        return float(Decimal(qty).quantize(Decimal(step), rounding=ROUND_DOWN))

    def _get_filters(self, symbol):
        info = self.client.get_symbol_info(symbol)
        lot_step = "0.000001"
        min_notional = 10.0

        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                lot_step = f["stepSize"]
            if f["filterType"] == "MIN_NOTIONAL":
                min_notional = float(f["minNotional"])

        return lot_step, min_notional

    # ---------- ORDEN DE VENTA ----------

    def _sell_all(self, symbol, positions):
        base_asset = symbol.replace("USDC", "")
        qty = positions.get(base_asset, 0)

        if qty <= 0:
            return

        lot_step, _ = self._get_filters(symbol)
        qty = self._round_step(qty, lot_step)

        if qty <= 0:
            return

        if not TRADE_REAL:
            logging.info("[SIM] SELL %s %.6f", symbol, qty)
            return

        try:
            logging.info("VENDIENDO %s %.6f", symbol, qty)
            self.client.create_margin_order(
                symbol=symbol, side="SELL", type="MARKET", quantity=qty
            )
        except Exception as e:
            logging.error("Error SELL %s: %s", symbol, e)

    # ---------- ORDEN DE COMPRA (CON AUTO-FILTRO LOT_SIZE) ----------

    def _buy_with_all(self, symbol, usdc_amount):
        # Obtener precio
        try:
            price = float(self.client.get_symbol_ticker(symbol=symbol)["price"])
        except:
            logging.error("Precio no disponible para %s", symbol)
            return False

        # Buscar lot_size y min_notional
        lot_step, min_notional = self._get_filters(symbol)

        # ¿Cumple el mínimo en USDC?
        if usdc_amount < max(min_notional, MIN_NOTIONAL_USD):
            logging.info("⛔ %s necesita %.2f USDC mínimo — saltado", symbol, max(min_notional, MIN_NOTIONAL_USD))
            return False

        # Cantidad base a comprar
        qty = usdc_amount / price
        qty = self._round_step(qty, lot_step)

        if qty <= 0:
            logging.info("⛔ Cantidad muy pequeña para %s — saltado", symbol)
            return False

        # Modo simulación
        if not TRADE_REAL:
            logging.info("[SIM] BUY %s %.6f", symbol, qty)
            return True

        # Compra real
        try:
            logging.info("COMPRANDO %s %.6f (≈ %.2f USDC)", symbol, qty, usdc_amount)
            self.client.create_margin_order(
                symbol=symbol, side="BUY", type="MARKET", quantity=qty
            )
            return True
        except Exception as e:
            logging.error("Error BUY %s: %s", symbol, e)
            return False

    # ===================== LOOP PRINCIPAL =====================

    def run(self):

        while True:
            try:

                # 1) Obtener mejor símbolo por % cambio
                best_symbol = None
                best_change = -999

                for sym in self.symbols:
                    pct = get_last_closed_candle_pct(self.client, sym.replace("USDC","USDT"), TIMEFRAME)
                    if pct is None:
                        continue
                    if pct > best_change:
                        best_change = pct
                        best_symbol = sym

                if not best_symbol:
                    logging.info("Sin datos…")
                    time.sleep(5)
                    continue

                # 2) Cambio actual
                curr_change = 0
                if self.current_symbol:
                    curr_change = get_last_closed_candle_pct(
                        self.client, self.current_symbol.replace("USDC","USDT"), TIMEFRAME
                    ) or 0

                edge = best_change - curr_change

                logging.info("Actual: %s (%.3f%%) | Mejor: %s (%.3f%%) | Diff=%.3f%%",
                             self.current_symbol or "USDC",
                             curr_change,
                             best_symbol,
                             best_change,
                             edge)

                if edge < EDGE_MIN_DIFF and self.current_symbol == best_symbol:
                    time.sleep(CYCLE_SECONDS)
                    continue

                usdc_free, posiciones = self._get_margin_balances()

                # 3) Venta si hay símbolo anterior
                if self.current_symbol:
                    self._sell_all(self.current_symbol, posiciones)
                    time.sleep(1)
                    usdc_free, posiciones = self._get_margin_balances()

                # 4) Intentar comprar la nueva moneda
                capital = usdc_free * 0.98

                ok = self._buy_with_all(best_symbol, capital)

                if ok:
                    self.current_symbol = best_symbol
                else:
                    logging.info(f"⛔ No se pudo comprar {best_symbol}, se queda en USDC")
                    self.current_symbol = None

                time.sleep(CYCLE_SECONDS)

            except Exception as e:
                logging.exception("Error inesperado: %s", e)
                time.sleep(10)


if __name__ == "__main__":
    bot = CryptoBot()
    bot.run()