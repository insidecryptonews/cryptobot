import os
import time
import json
import logging
import datetime as dt
from dataclasses import dataclass, asdict
from typing import List, Optional

import requests
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ============================================================
#                     LOGGING BÁSICO
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("news_trading_bot")

# ============================================================
#                     CONFIGURACIÓN GLOBAL
# ============================================================

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default

def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v is not None else default


LIVE_TRADING = env_bool("LIVE_TRADING", False)
USE_MARGIN = env_bool("USE_MARGIN", False)
MARGIN_MODE = env_str("MARGIN_MODE", "cross")
MARGIN_LEVERAGE = env_float("MARGIN_LEVERAGE", 2.0)

QUOTE_ASSET = "USDC"
STATE_FILE = "state.json"

CYCLE_SECONDS = 60  # ya lo has puesto a 60s

CONFIG = {
    "quote_asset": QUOTE_ASSET,
    "underperform_threshold_pct": -2.0,
    "better_threshold_pct": 2.0,
    "weight_price": 0.7,
    "min_score_gap_to_switch": 0.3,
    "taker_fee_pct": 0.1,          # 0.1% taker
    "slippage_buffer_pct": 0.05,   # 0.05% de colchón
    "min_edge_factor": 1.5,
    "rebalance_fraction": 0.2,
    "max_trades_per_day": 30,
    "max_daily_loss_pct": 20.0,
    "max_position_pct": 0.9,
    "trade_cooldown_minutes": 10,
}


# ============================================================
#                          STATE
# ============================================================

@dataclass
class BotState:
    current_symbol: str = "BTCUSDC"
    last_trade_time: Optional[str] = None
    last_equity_usdc: float = 0.0
    today_date: Optional[str] = None
    today_trades: int = 0
    today_pl_pct: float = 0.0


def load_state() -> BotState:
    if not os.path.exists(STATE_FILE):
        logger.info("No existe state.json, usando estado por defecto")
        return BotState()
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        return BotState(**data)
    except Exception as e:
        logger.warning(f"No se pudo leer state.json, estado por defecto: {e}")
        return BotState()


def save_state(state: BotState):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(state), f, indent=2)
    except Exception as e:
        logger.error(f"Error guardando state.json: {e}")


# ============================================================
#                      WRAPPER BINANCE
# ============================================================

class BinanceWrapper:
    def __init__(self):
        api_key = os.getenv("BINANCE_API_KEY")
        api_secret = os.getenv("BINANCE_API_SECRET")
        use_testnet = env_bool("BINANCE_TESTNET", False)

        # Debug muy claro de lo que ve Railway
        logger.info(
            f"[DEBUG BINANCE ENVS] KEY_SET={bool(api_key)} "
            f"SECRET_SET={bool(api_secret)} TESTNET={use_testnet}"
        )

        if not api_key or not api_secret:
            raise RuntimeError(
                "Faltan claves API de Binance dentro del entorno de ejecución. "
                "Asegúrate de tener BINANCE_API_KEY y BINANCE_API_SECRET en las "
                "Variables del SERVICIO de Railway y vuelve a desplegar."
            )

        try:
            self.client = Client(api_key, api_secret, testnet=use_testnet)
            if use_testnet:
                logger.info("Usando Binance TESTNET")
            else:
                logger.info("Usando Binance MAINNET")
        except Exception as e:
            logger.error(f"Error inicializando cliente de Binance: {e}")
            raise

    # ---------- Universo dinámico de símbolos ----------

    def get_universe(self) -> List[str]:
        try:
            info = self.client.get_exchange_info()
            symbols = []
            for s in info["symbols"]:
                if s.get("status") != "TRADING":
                    continue
                if s.get("quoteAsset") != QUOTE_ASSET:
                    continue
                if "SPOT" not in s.get("permissions", []):
                    continue
                symbols.append(s["symbol"])
            # limitar a 30 para ir suave
            symbols = symbols[:30]
            logger.info(f"Universo dinámico ({len(symbols)} símbolos): {', '.join(symbols)}")
            return symbols
        except Exception as e:
            logger.error(f"Error obteniendo universo: {e}")
            # por si peta, al menos BTCUSDC
            return [f"BTC{QUOTE_ASSET}"]

    # ---------- Precio / cambio 1h ----------

    def get_price(self, symbol: str) -> float:
        try:
            t = self.client.get_symbol_ticker(symbol=symbol)
            return float(t["price"])
        except Exception as e:
            logger.error(f"Error precio {symbol}: {e}")
            return 0.0

    def get_1h_change(self, symbol: str) -> float:
        try:
            kl = self.client.get_klines(symbol=symbol, interval="1h", limit=2)
            if len(kl) < 2:
                return 0.0
            prev_close = float(kl[0][4])
            last_close = float(kl[1][4])
            if prev_close == 0:
                return 0.0
            ch = (last_close - prev_close) / prev_close * 100
            return ch
        except Exception as e:
            logger.error(f"Error cambio 1h {symbol}: {e}")
            return 0.0

    # ---------- Balances SPOT ----------

    def get_spot_balance(self, asset: str) -> float:
        try:
            acc = self.client.get_account()
            for b in acc["balances"]:
                if b["asset"] == asset:
                    return float(b["free"]) + float(b["locked"])
        except Exception as e:
            logger.error(f"Error balance spot {asset}: {e}")
        return 0.0

    def get_total_equity_usdc(self) -> float:
        total = self.get_spot_balance(QUOTE_ASSET)
        universe = self.get_universe()
        for sym in universe:
            base = sym.replace(QUOTE_ASSET, "")
            bal = self.get_spot_balance(base)
            if bal > 0:
                px = self.get_price(sym)
                total += bal * px
        return total

    # ---------- Órdenes SPOT ----------

    def spot_sell(self, symbol: str, qty: float):
        if qty <= 0:
            return None
        if not LIVE_TRADING:
            logger.info(f"[SIMULACIÓN SPOT] SELL {qty} {symbol}")
            return None
        try:
            return self.client.order_market_sell(symbol=symbol, quantity=qty)
        except BinanceAPIException as e:
            logger.error(f"Error SPOT SELL {symbol}: {e}")
        return None

    def spot_buy(self, symbol: str, usdc_amount: float):
        px = self.get_price(symbol)
        if px <= 0:
            return None
        qty = float(f"{usdc_amount / px:.6f}")
        if qty <= 0:
            return None
        if not LIVE_TRADING:
            logger.info(f"[SIMULACIÓN SPOT] BUY {qty} {symbol}")
            return None
        try:
            return self.client.order_market_buy(symbol=symbol, quantity=qty)
        except BinanceAPIException as e:
            logger.error(f"Error SPOT BUY {symbol}: {e}")
        return None

    # ---------- Órdenes MARGEN CROSS ----------

    def margin_sell(self, symbol: str, qty: float):
        if qty <= 0:
            return None
        if not LIVE_TRADING:
            logger.info(f"[SIMULACIÓN MARGIN] SELL {qty} {symbol}")
            return None
        try:
            return self.client.create_margin_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=qty,
                sideEffectType="AUTO_REPAY"
            )
        except BinanceAPIException as e:
            logger.error(f"Error MARGIN SELL {symbol}: {e}")
        return None

    def margin_buy(self, symbol: str, usdc_amount: float):
        px = self.get_price(symbol)
        if px <= 0:
            return None
        qty = float(f"{usdc_amount / px:.6f}")
        if qty <= 0:
            return None
        if not LIVE_TRADING:
            logger.info(f"[SIMULACIÓN MARGIN] BUY {qty} {symbol}")
            return None
        try:
            return self.client.create_margin_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quantity=qty,
                sideEffectType="MARGIN_BUY"
            )
        except BinanceAPIException as e:
            logger.error(f"Error MARGIN BUY {symbol}: {e}")
        return None


# ============================================================
#                         ESTRATEGIA
# ============================================================

@dataclass
class SymbolStats:
    symbol: str
    change_1h: float
    score: float


class Strategy:
    def __init__(self, binance: BinanceWrapper):
        self.binance = binance

    def analyze(self) -> List[SymbolStats]:
        stats: List[SymbolStats] = []
        for sym in self.binance.get_universe():
            ch = self.binance.get_1h_change(sym)
            sc = ch * CONFIG["weight_price"]
            stats.append(SymbolStats(sym, ch, sc))
        return stats

    def pick_best(self, stats: List[SymbolStats]) -> SymbolStats:
        return sorted(stats, key=lambda x: x.score, reverse=True)[0]

    def should_switch(self, current: SymbolStats, best: SymbolStats, state: BotState) -> bool:
        if current.symbol == best.symbol:
            return False

        underperforming = current.change_1h < CONFIG["underperform_threshold_pct"]
        better = best.change_1h > CONFIG["better_threshold_pct"]

        score_gap = best.score - current.score
        if score_gap < CONFIG["min_score_gap_to_switch"]:
            return False

        delta = best.change_1h - current.change_1h
        trade_cost = (
            CONFIG["taker_fee_pct"] * 2 * CONFIG["rebalance_fraction"]
            + CONFIG["slippage_buffer_pct"]
        )
        required_edge = trade_cost * CONFIG["min_edge_factor"]

        if delta <= required_edge:
            logger.info(
                f"Mejora insuficiente: Δ={delta:.2f}% vs req {required_edge:.2f}%"
            )
            return False

        if not (underperforming and better):
            return False

        if state.last_trade_time:
            last = dt.datetime.fromisoformat(state.last_trade_time)
            diff = dt.datetime.utcnow() - last
            if diff.total_seconds() < CONFIG["trade_cooldown_minutes"] * 60:
                logger.info("Cooldown activo, no se cambia aún")
                return False

        return True


# ============================================================
#                       RISK MANAGER
# ============================================================

class RiskManager:
    def __init__(self, binance: BinanceWrapper):
        self.binance = binance

    def update_day(self, state: BotState) -> BotState:
        today = dt.date.today().isoformat()
        if state.today_date != today:
            state.today_date = today
            state.today_trades = 0
            state.today_pl_pct = 0.0
            logger.info("Nuevo día → reseteo de métricas diarias")
        return state

    def allowed_today(self, state: BotState) -> bool:
        if CONFIG["max_trades_per_day"] > 0 and state.today_trades >= CONFIG["max_trades_per_day"]:
            logger.info("Límite de trades diario alcanzado")
            return False
        if CONFIG["max_daily_loss_pct"] > 0 and state.today_pl_pct <= -CONFIG["max_daily_loss_pct"]:
            logger.info("Límite de pérdidas diario alcanzado")
            return False
        return True

    def position_ok(self, symbol: str) -> bool:
        total = self.binance.get_total_equity_usdc()
        px = self.binance.get_price(symbol)
        base = symbol.replace(QUOTE_ASSET, "")
        pos = self.binance.get_spot_balance(base) * px
        if total <= 0:
            return True
        if pos / total > CONFIG["max_position_pct"]:
            logger.info(f"Posición demasiado grande en {symbol}")
            return False
        return True

    def rebalance(self, from_sym: str, to_sym: str, state: BotState) -> BotState:
        equity_before = self.binance.get_total_equity_usdc()
        quote = QUOTE_ASSET

        base_from = from_sym.replace(quote, "")
        bal = self.binance.get_spot_balance(base_from)
        sell_qty = bal * CONFIG["rebalance_fraction"]

        if USE_MARGIN:
            self.binance.margin_sell(from_sym, sell_qty)
        else:
            self.binance.spot_sell(from_sym, sell_qty)

        quote_bal = self.binance.get_spot_balance(quote)
        buy_amount = quote_bal * CONFIG["rebalance_fraction"]

        if USE_MARGIN:
            self.binance.margin_buy(to_sym, buy_amount)
        else:
            self.binance.spot_buy(to_sym, buy_amount)

        equity_after = self.binance.get_total_equity_usdc()
        if state.last_equity_usdc > 0:
            pl = (equity_after - state.last_equity_usdc) / state.last_equity_usdc * 100
        else:
            pl = 0.0

        state.today_trades += 1
        state.today_pl_pct += pl
        state.last_equity_usdc = equity_after
        state.last_trade_time = dt.datetime.utcnow().isoformat()
        state.current_symbol = to_sym

        logger.info(
            f"Trade completado | Equity {equity_before:.2f} → {equity_after:.2f} USDC | PnL={pl:.2f}%"
        )
        return state


# ============================================================
#                      BOT PRINCIPAL
# ============================================================

class NewsTradingBot:
    def __init__(self):
        self.binance = BinanceWrapper()
        self.strategy = Strategy(self.binance)
        self.risk = RiskManager(self.binance)
        self.state = load_state()

        if self.state.last_equity_usdc <= 0:
            eq = self.binance.get_total_equity_usdc()
            self.state.last_equity_usdc = eq
            save_state(self.state)
            logger.info(f"Equity inicial: {eq:.2f} USDC")

    def run_cycle(self):
        logger.info("===== NUEVO CICLO =====")

        self.state = self.risk.update_day(self.state)

        if not self.risk.allowed_today(self.state):
            logger.info("No se opera por límites de riesgo, solo análisis")
            self.log_analysis()
            return

        stats = self.strategy.analyze()
        for s in stats:
            logger.info(
                f"{s.symbol}: cambio 1h={s.change_1h:.2f}%, score={s.score:.2f}"
            )

        current_stats = None
        for s in stats:
            if s.symbol == self.state.current_symbol:
                current_stats = s
                break

        if current_stats is None:
            current_stats = self.strategy.pick_best(stats)
            self.state.current_symbol = current_stats.symbol

        best = self.strategy.pick_best(stats)

        logger.info(
            f"Moneda actual: {self.state.current_symbol} | Mejor opción ahora: {best.symbol}"
        )

        if not self.strategy.should_switch(current_stats, best, self.state):
            logger.info("La estrategia dice que NO hay que cambiar de moneda en este ciclo.")
            save_state(self.state)
            return

        if not self.risk.position_ok(best.symbol):
            logger.info("Riesgo de posición demasiada alta, no se cambia.")
            save_state(self.state)
            return

        logger.info(f"CAMBIO: {self.state.current_symbol} → {best.symbol}")
        self.state = self.risk.rebalance(
            from_sym=self.state.current_symbol,
            to_sym=best.symbol,
            state=self.state,
        )
        save_state(self.state)

    def log_analysis(self):
        stats = self.strategy.analyze()
        for s in stats:
            logger.info(
                f"[ANÁLISIS] {s.symbol}: cambio 1h={s.change_1h:.2f}%, score={s.score:.2f}"
            )


# ============================================================
#                       MAIN LOOP
# ============================================================

def main():
    logger.info("Iniciando NewsTradingBot")
    logger.info(f"LIVE_TRADING={LIVE_TRADING}")
    logger.info(f"USE_MARGIN={USE_MARGIN} | MARGIN_MODE={MARGIN_MODE} | x{MARGIN_LEVERAGE}")
    bot = NewsTradingBot()

    while True:
        try:
            bot.run_cycle()
        except Exception as e:
            logger.exception(f"Error en ciclo principal: {e}")
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
