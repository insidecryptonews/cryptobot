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
USE_MARGIN = os.getenv("USE_MARGIN", "true").lower() == "true"
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
EDGE_MIN_DIFF = float(os.getenv("EDGE_MIN_DIFF", "0.2"))
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "60"))

MIN_NOTIONAL_USD = float(os.getenv("MIN_NOTIONAL_USD", "5"))

# ===================== FUNCIÓN PARA PEDIR VELAS (REST ANTI-BAN) =====================

def get_last_closed_candle_pct(client, symbol, interval):
    try:
        candles = client.get_klines(symbol=symbol, interval=interval, limit=2)
        o = float(candles[-2][1])
        c = float(candles[-2][4])
        pct = (c - o) / o * 100.0
        return pct
    except Exception as e:
        logging.error("Error obteniendo vela %s: %s", symbol, e)
        return None

# ===================== BOT =====================

class CryptoBot:
    def __init__(self):
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Faltan claves de Binance")

        self.client = Client(API_KEY, API_SECRET)

        self.symbols = SYMBOLS
        self.current_symbol = None

        logging.info("Iniciando CryptoBot MODO RAILWAY (REST-ANTIBAN)")

    # ----- UTILS -----

    def _get_margin_balances(self):
        try:
            acc = self.client.get_margin_account()
        except:
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

    def _round_step(self, quantity, step):
        step_dec = Decimal(step)
        q = Decimal(quantity)
        return float(q.quantize(step_dec, rounding=ROUND_DOWN).normalize())

    def _get_symbol_filters(self, symbol):
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

    # ----- MARGIN ORDERS -----

    def _place_margin_market_sell(self, symbol, base_qty):
        if base_qty <= 0: return
        try:
            lot_step, _ = self._get_symbol_filters(symbol)
            qty_rounded = self._round_step(base_qty, lot_step)
            if qty_rounded <= 0: return

            if not TRADE_REAL:
                logging.info("[SIM] SELL %s %.6f", symbol, qty_rounded)
                return

            logging.info("VENDIENDO %s %.6f", symbol, qty_rounded)
            self.client.create_margin_order(
                symbol=symbol, side="SELL", type="MARKET", quantity=qty_rounded
            )
        except Exception as e:
            logging.error("Error en SELL %s: %s", symbol, e)

    def _place_margin_market_buy(self, symbol, usdc_amount):
        if usdc_amount <= 0: return
        try:
            price = float(self.client.get_symbol_ticker(symbol=symbol)["price"])
            base_qty = usdc_amount / price

            lot_step, min_notional = self._get_symbol_filters(symbol)

            if usdc_amount < max(min_notional, MIN_NOTIONAL_USD):
                logging.warning("Capital insuficiente para %s", symbol)
                return

            qty_rounded = self._round_step(base_qty, lot_step)

            if not TRADE_REAL:
                logging.info("[SIM] BUY %s %.6f", symbol, qty_rounded)
                return

            logging.info("COMPRANDO %s %.6f", symbol, qty_rounded)
            self.client.create_margin_order(
                symbol=symbol, side="BUY", type="MARKET", quantity=qty_rounded
            )

        except Exception as e:
            logging.error("Error en BUY %s: %s", symbol, e)

    # ===================== LOOP PRINCIPAL =====================

    def run(self):

        while True:
            try:
                best_symbol = None
                best_change = -999

                # Pedimos velas vía REST (anti-ban)
                for sym in self.symbols:
                    pct = get_last_closed_candle_pct(self.client, sym.replace("USDC","USDT"), TIMEFRAME)
                    if pct is None: continue
                    if pct > best_change:
                        best_change = pct
                        best_symbol = sym

                if best_symbol is None:
                    logging.info("Sin datos aún…")
                    time.sleep(5)
                    continue

                # Calculamos cambio actual
                curr_change = 0.0
                if self.current_symbol:
                    curr_change = get_last_closed_candle_pct(self.client, self.current_symbol.replace("USDC","USDT"), TIMEFRAME) or 0.0

                edge = best_change - curr_change

                logging.info(
                    "Actual: %s (%.3f%%) | Mejor: %s (%.3f%%) | Diff=%.3f%%",
                    self.current_symbol or "USDC",
                    curr_change,
                    best_symbol,
                    best_change,
                    edge,
                )

                if edge < EDGE_MIN_DIFF and self.current_symbol == best_symbol:
                    time.sleep(CYCLE_SECONDS)
                    continue

                if best_symbol != self.current_symbol:

                    usdc_free, posiciones = self._get_margin_balances()

                    if self.current_symbol:
                        base_asset = self.current_symbol.replace("USDC","")
                        base_qty = posiciones.get(base_asset, 0.0)
                        self._place_margin_market_sell(self.current_symbol, base_qty)
                        time.sleep(1)
                        usdc_free, posiciones = self._get_margin_balances()

                    capital = usdc_free * 0.98
                    if capital >= MIN_NOTIONAL_USD:
                        self._place_margin_market_buy(best_symbol, capital)
                        self.current_symbol = best_symbol

                time.sleep(CYCLE_SECONDS)

            except Exception as e:
                logging.exception("Error inesperado: %s", e)
                time.sleep(10)


if __name__ == "__main__":
    bot = CryptoBot()
    bot.run()