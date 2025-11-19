import os
import time
import json
import threading
import traceback
from decimal import Decimal
from binance.client import Client
from binance.streams import ThreadedWebsocketManager

# ============================================================
# CONFIGURACIÓN DEL BOT
# ============================================================

SYMBOLS = [
    "BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC", "LINKUSDC",
    "ADAUSDC", "DOGEUSDC", "AVAXUSDC", "NEARUSDC", "ATOMUSDC", "DOTUSDC", "FILUSDC"
]

TIMEFRAME = os.getenv("TIMEFRAME", "15m")
USE_MARGIN = os.getenv("USE_MARGIN", "true").lower() == "true"
LEVERAGE = int(os.getenv("MARGIN_LEVERAGE", "5"))
REAL = os.getenv("REAL_TRADING", "false").lower() == "true"

EDGE_MIN_DIFF = Decimal("0.20")  # mínimo edge para cambiar

# Precio en vivo desde WebSocket
price_live = {}

# ============================================================
# WRAPPER BINANCE — solo 1 request por orden, NINGUNA MÁS
# ============================================================

class BinanceAPI:
    def __init__(self):
        try:
            self.client = Client(
                os.getenv("BINANCE_API_KEY"),
                os.getenv("BINANCE_SECRET_KEY")
            )
        except Exception as e:
            print("Error al iniciar cliente:", e)
            raise

        if USE_MARGIN:
            try:
                self.client.futures_change_leverage(symbol="BTCUSDT", leverage=LEVERAGE)
            except:
                pass

    # Funcion para operar
    def buy(self, symbol, qty):
        if not REAL:
            print(f"[SIMULACIÓN] BUY {qty} {symbol}")
            return

        try:
            order = self.client.create_margin_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quantity=qty
            )
            print("BUY OK:", order)
        except Exception:
            traceback.print_exc()

    def sell(self, symbol, qty):
        if not REAL:
            print(f"[SIMULACIÓN] SELL {qty} {symbol}")
            return

        try:
            order = self.client.create_margin_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=qty
            )
            print("SELL OK:", order)
        except Exception:
            traceback.print_exc()

    def get_balance(self):
        try:
            data = self.client.get_margin_account()
            total = Decimal(data["totalAssetOfBtc"])
            return total
        except:
            return Decimal("0")


# ============================================================
# WEBSOCKET STREAMS — PRECIOS EN VIVO SIN PESAR LA API
# ============================================================

class PriceStreamer:
    def __init__(self):
        self.twm = ThreadedWebsocketManager(
            api_key=os.getenv("BINANCE_API_KEY"),
            api_secret=os.getenv("BINANCE_SECRET_KEY")
        )
        self.twm.start()

    def stream_price(self, symbol):
        def handle(msg):
            if "c" in msg:
                price_live[symbol] = Decimal(msg["c"])

        self.twm.start_kline_socket(
            callback=handle,
            symbol=symbol.lower(),
            interval="1m"
        )

    def start_all(self):
        for s in SYMBOLS:
            self.stream_price(s)


# ============================================================
# BOT PRINCIPAL
# ============================================================

class CryptoBot:
    def __init__(self):
        self.api = BinanceAPI()
        self.current_symbol = "USDC"
        self.position_amount = Decimal("0")

        self.streamer = PriceStreamer()
        self.streamer.start_all()

        print("STREAMS ACTIVOS. PRECIOS EN TIEMPO REAL.")
        print("Bot listo. Esperando datos...")

    def get_best_symbol(self):
        """Elige la moneda cuyo precio sube más rápido en los últimos 15m."""
        if len(price_live) < len(SYMBOLS):
            return None, Decimal("0")

        # Calculamos diferencias usando precios actuales y precios hace 15 minutos (cache)
        diffs = {}

        for symbol in SYMBOLS:
            now = price_live[symbol]
            if symbol not in self.history:
                self.history[symbol] = now
            old = self.history[symbol]
            diff = (now - old) / old * 100
            diffs[symbol] = diff

        best = max(diffs, key=lambda s: diffs[s])
        return best, diffs[best]

    def loop(self):
        self.history = {}
        print("Esperando 1 minuto para cargar precios iniciales...")
        time.sleep(60)

        while True:
            try:
                best_symbol, diff = self.get_best_symbol()
                if not best_symbol:
                    print("Aún no hay stream completo...")
                    time.sleep(5)
                    continue

                print(f"Mejor moneda ahora: {best_symbol} ({diff:.2f}%)")

                # Condición cambio
                if self.current_symbol != best_symbol and abs(diff) >= EDGE_MIN_DIFF:
                    print(f"CAMBIO → {best_symbol} (diff {diff:.2f}%)")

                    if self.position_amount > 0 and self.current_symbol != "USDC":
                        self.api.sell(self.current_symbol, float(self.position_amount))

                    balance = self.api.get_balance()
                    qty = float(balance * LEVERAGE)

                    self.api.buy(best_symbol, qty)

                    self.position_amount = qty
                    self.current_symbol = best_symbol
                else:
                    print("Sin cambio. Mercado estable.")

            except Exception:
                traceback.print_exc()

            time.sleep(60)  # 1 ciclo por minuto usando websockets (ligero)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("Iniciando CRYPTOBOT WEBSOCKET EDITION (ANTI-BAN)")
    bot = CryptoBot()
    bot.loop()