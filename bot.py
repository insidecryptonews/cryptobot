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
# CONFIG (MARGEN X5 + TIMEFRAME + UMBRAL)
# ============================================================

QUOTE_ASSET = "USDC"
CYCLE_SECONDS = 60

LIVE_TRADING = env_bool("LIVE_TRADING", False)

# 🔥 MARGEN ACTIVADO Y FIJO A X5
USE_MARGIN = True
MARGIN_LEVERAGE = 5
MARGIN_MODE = "cross"

# ⏱️ TIMEFRAME para las velas de análisis (1h por defecto)
# Valores típicos válidos: "1m", "5m", "15m", "30m", "1h", "4h"...
TIMEFRAME = env_str("TIMEFRAME", "1h")

# 🔍 UMBRAL MÍNIMO PARA CAMBIAR DE MONEDA (en % de diferencia)
# Si la mejor moneda no mejora al menos este % respecto a la actual, NO cambiamos.
EDGE_MIN_DIFF = 0.20  # 0.20%


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
            f"LEVERAGE={MARGIN_LEVERAGE} "
            f"TIMEFRAME={TIMEFRAME} "
            f"EDGE_MIN_DIFF={EDGE_MIN_DIFF}"
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
    # CHANGE PCT (TIMEFRAME CONFIGURABLE)
    # -----------------------------

    def change_pct(self, symbol, interval=None):
        if interval is None:
            interval = TIMEFRAME
        try:
            kl = self.client.get_klines(symbol=symbol, interval=interval, limit=2)
            if len(kl) < 2:
                return 0.0
            prev = float(kl[0][4])
            last = float(kl[1][4])
            return (last - prev) / prev * 100
        except Exception as e:
            logger.error(f"Error obteniendo cambio para {symbol} en {interval}: {e}")
            return 0.0

    # -----------------------------
    # BUY
    # -----------------------------

    def buy(self, symbol, amount_usdc):
        px = self.price(symbol)
        if px == 0:
            logger.warning(f"Precio 0 en {symbol}, no compro.")
            return

        # x5 de margen
        qty = round(amount_usdc / px * MARGIN_LEVERAGE, 6)

        if qty <= 0:
            logger.warning(f"Cantidad calculada 0 en BUY {symbol}, amount_usdc={amount_usdc}")
            return

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
                    logger.info(f"[REAL MARGIN X5] BUY {qty} {symbol}")
                except BinanceAPIException as e:
                    logger.error(e)
            else:
                logger.info(f"[SIM MARGIN X5] BUY {qty} {symbol}")
        else:
            if LIVE_TRADING:
                try:
                    self.client.order_market_buy(symbol=symbol, quantity=qty)
                    logger.info(f"[REAL SPOT] BUY {qty} {symbol}")
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
                logger.warning(f"No hay balance margin en {asset} para vender.")
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
                    logger.info(f"[REAL MARGIN X5] SELL {qty} {symbol}")
                except BinanceAPIException as e:
                    logger.error(e)
            else:
                logger.info(f"[SIM MARGIN X5] SELL {qty} {symbol}")
        else:
            qty = self.spot_balance(asset)
            if qty <= 0:
                logger.warning(f"No hay balance spot en {asset} para vender.")
                return
            if LIVE_TRADING:
                try:
                    self.client.order_market_sell(symbol=symbol, quantity=qty)
                    logger.info(f"[REAL SPOT] SELL {qty} {symbol}")
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
        logger.info(f"=== CICLO NUEVO (TIMEFRAME={TIMEFRAME}) ===")

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
        current_ch = None

        for s in universe:
            ch = self.binance.change_pct(s)
            logger.info(f"{s}: {ch:.2f}% ({TIMEFRAME})")

            if s == self.current:
                current_ch = ch

            if ch > best_ch:
                best_ch = ch
                best = s

        # Si por alguna razón no se ha calculado current_ch (por ejemplo, current no está en universe),
        # asumimos que es muy malo para obligar al cambio si best es decente.
        if current_ch is None:
            current_ch = -999

        diff = best_ch - current_ch
        logger.info(
            f"Actual: {self.current} ({current_ch:.2f}%) | Mejor: {best} ({best_ch:.2f}%) | Diff={diff:.2f}%"
        )

        # 🔍 Aplicamos el UMBRAL: solo cambiamos si la mejor mejora al menos EDGE_MIN_DIFF
        if best != self.current and diff >= EDGE_MIN_DIFF:
            logger.info(f"CAMBIO DE MONEDA! (Diff={diff:.2f}% >= {EDGE_MIN_DIFF:.2f}%)")
            self.binance.sell_all(self.current)
            # Usar balance según margen o spot
            bal = self.binance.margin_balance(QUOTE_ASSET) if USE_MARGIN else self.binance.spot_balance(QUOTE_ASSET)
            self.binance.buy(best, bal)
            self.current = best
        else:
            logger.info(
                f"NO cambiamos de moneda. Diff={diff:.2f}% < {EDGE_MIN_DIFF:.2f}% "
                f"o best == current."
            )


# ============================================================
# MAIN LOOP
# ============================================================

if __name__ == "__main__":
    logger.info("Iniciando CryptoBot con MARGEN X5, universo seguro USDC y UMBRAL de cambio")
    logger.info(f"Trading real: {LIVE_TRADING}")
    logger.info(
        f"MARGEN ACTIVADO = {USE_MARGIN}, LEVERAGE = {MARGIN_LEVERAGE}, "
        f"TIMEFRAME={TIMEFRAME}, EDGE_MIN_DIFF={EDGE_MIN_DIFF}"
    )

    bot = CryptoBot()

    while True:
        try:
            bot.run()
        except Exception as e:
            logger.error(e)

        time.sleep(CYCLE_SECONDS)
