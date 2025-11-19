import os
import time
import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("cryptobot")

# ============================================================
# ENV HELPERS
# ============================================================

def env_bool(name, default=False):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

def env_float(name, default):
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v)
    except:
        return default

def env_str(name, default=""):
    v = os.getenv(name)
    return v if v else default


# ============================================================
# CONFIG (MARGEN X5)
# ============================================================

QUOTE_ASSET = "USDC"
CYCLE_SECONDS = 60

LIVE_TRADING = env_bool("LIVE_TRADING", False)

# 🔥 MARGEN ACTIVADO Y FIJO A X5
USE_MARGIN = True
MARGIN_LEVERAGE = 5
MARGIN_MODE = "cross"

# ============================================================
# BINANCE WRAPPER
# ============================================================

class BinanceWrapper:
    def __init__(self):

        api_key = os.getenv("BINANCE_API_KEY")
        api_secret = os.getenv("BINANCE_API_SECRET")
        use_testnet = env_bool("BINANCE_TESTNET", False)

        logger.info(
            f"[DEBUG ENVS] KEY_SET={bool(api_key)} "
            f"SECRET_SET={bool(api_secret)} "
            f"TESTNET={use_testnet} "
            f"USE_MARGIN={USE_MARGIN} "
            f"LEVERAGE={MARGIN_LEVERAGE}"
        )

        if not api_key or not api_secret:
            raise RuntimeError("FALTAN CLAVES DE BINANCE EN RAILWAY.")

        self.client = Client(api_key, api_secret, testnet=use_testnet)

        if use_testnet:
            logger.info("Usando TESTNET")
        else:
            logger.info("Usando MAINNET")

    # -----------------------------
    # PRECIOS
    # -----------------------------

    def price(self, symbol):
        try:
            d = self.client.get_symbol_ticker(symbol=symbol)
            return float(d["price"])
        except:
            return 0.0

    # -----------------------------
    # BALANCES
    # -----------------------------

    def spot_balance(self, asset):
        try:
            acc = self.client.get_account()
            for b in acc["balances"]:
                if b["asset"] == asset:
                    return float(b["free"])
        except:
            pass
        return 0.0

    def margin_balance(self, asset):
        try:
            acc = self.client.get_margin_account()
            for a in acc["userAssets"]:
                if a["asset"] == asset:
                    return float(a["free"])
        except:
            pass
        return 0.0

    # -----------------------------
    # CHANGE 1H
    # -----------------------------

    def change_1h(self, symbol):
        try:
            kl = self.client.get_klines(symbol=symbol, interval="1h", limit=2)
            prev = float(kl[0][4])
            last = float(kl[1][4])
            return (last - prev) / prev * 100
        except:
            return 0.0

    # -----------------------------
    # BUY
    # -----------------------------

    def buy(self, symbol, amount_usdc):
        px = self.price(symbol)
        if px == 0:
            return

        # x5 de margen
        qty = round(amount_usdc / px * MARGIN_LEVERAGE, 6)

        if USE_MARGIN:
            if LIVE_TRADING:
                try:
                    self.client.create_margin_order(
                        symbol=symbol,
                        side="BUY",
                        type="MARKET",
                        quantity=qty,
                        sideEffectType="MARGIN_BUY"
                    )
                except BinanceAPIException as e:
                    logger.error(e)
            else:
                logger.info(f"[SIM MARGIN X5] BUY {qty} {symbol}")
        else:
            if LIVE_TRADING:
                try:
                    self.client.order_market_buy(symbol=symbol, quantity=qty)
                except BinanceAPIException as e:
                    logger.error(e)
            else:
                logger.info(f"[SIM SPOT] BUY {qty} {symbol}")

    # -----------------------------
    # SELL ALL
    # -----------------------------

    def sell_all(self, symbol):
        asset = symbol.replace(QUOTE_ASSET, "")

        if USE_MARGIN:
            qty = self.margin_balance(asset)
            if qty <= 0:
                return
            if LIVE_TRADING:
                try:
                    self.client.create_margin_order(
                        symbol=symbol,
                        side="SELL",
                        type="MARKET",
                        quantity=qty,
                        sideEffectType="AUTO_REPAY"
                    )
                except BinanceAPIException as e:
                    logger.error(e)
            else:
                logger.info(f"[SIM MARGIN X5] SELL {qty} {symbol}")
        else:
            qty = self.spot_balance(asset)
            if qty <= 0:
                return
            if LIVE_TRADING:
                try:
                    self.client.order_market_sell(symbol=symbol, quantity=qty)
                except BinanceAPIException as e:
                    logger.error(e)
            else:
                logger.info(f"[SIM SPOT] SELL {qty} {symbol}")


# ============================================================
# BOT PRINCIPAL
# ============================================================

class CryptoBot:
    def __init__(self):
        self.binance = BinanceWrapper()
        self.current = f"BTC{QUOTE_ASSET}"

    def run(self):
        logger.info("=== CICLO NUEVO ===")

        # 🔥 Universo seguro de monedas USDC con buen margin
        universe = [
            f"BTC{QUOTE_ASSET}",
            f"ETH{QUOTE_ASSET}",
            f"BNB{QUOTE_ASSET}",
            f"SOL{QUOTE_ASSET}",
            f"XRP{QUOTE_ASSET}",
            f"LINK{QUOTE_ASSET}",
            f"ADA{QUOTE_ASSET}",
            f"DOGE{QUOTE_ASSET}",
            f"AVAX{QUOTE_ASSET}",
            f"NEAR{QUOTE_ASSET}",
            f"ATOM{QUOTE_ASSET}",
            f"DOT{QUOTE_ASSET}",
            f"FIL{QUOTE_ASSET}",
        ]

        best = None
        best_ch = -999

        for s in universe:
            ch = self.binance.change_1h(s)
            logger.info(f"{s}: {ch:.2f}%")
            if ch > best_ch:
                best_ch = ch
                best = s

        logger.info(f"Actual: {self.current} | Mejor: {best}")

        if best != self.current:
            logger.info("CAMBIO DE MONEDA!")
            self.binance.sell_all(self.current)
            # Usar balance según margen o spot
            bal = self.binance.margin_balance(QUOTE_ASSET) if USE_MARGIN else self.binance.spot_balance(QUOTE_ASSET)
            self.binance.buy(best, bal)
            self.current = best


# ============================================================
# MAIN LOOP
# ============================================================

if __name__ == "__main__":
    logger.info("Iniciando CryptoBot con MARGEN X5 y universo seguro USDC")
    logger.info(f"Trading real: {LIVE_TRADING}")
    logger.info(f"MARGEN ACTIVADO = {USE_MARGIN}, LEVERAGE = {MARGIN_LEVERAGE}")

    bot = CryptoBot()

    while True:
        try:
            bot.run()
        except Exception as e:
            logger.error(e)

        time.sleep(CYCLE_SECONDS)
