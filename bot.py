#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CryptoBot MARGIN x5 (REST anti-ban + auto-ajuste LOT_SIZE/minQty)

- Opera en MARGIN CROSS (no spot).
- Usa una sola posición a la vez: USDC o una alt (DOTUSDC, AVAXUSDC, etc.).
- Compra en margin con sideEffectType='MARGIN_BUY' (apalancamiento según tengas configurado, p.ej. x5).
- Vende con sideEffectType='AUTO_REPAY' (repaga automáticamente el préstamo).
- Ajusta la cantidad a LOT_SIZE / minQty / minNotional para evitar errores -1013.

REQUISITOS:
    pip install python-binance

IMPORTANTE:
    - Activa la cuenta de Margin en Binance.
    - Activa los pares en Margin y configura el apalancamiento (x5).
    - Rellena API_KEY y API_SECRET con tus claves reales (SPOT/MARGIN).
"""

import time
import math
import logging
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, KLINE_INTERVAL_15MINUTE
from binance.exceptions import BinanceAPIException

# ============================
# CONFIGURACIÓN
# ============================

API_KEY = "rbjXqPIinPOyCKfFJMBSgGjL642UENDnvcJDRpXx7u97z1AEmNeUXqQg1NnnxbOP"
API_SECRET = "e61MGR7vDZqvbOJtJryqGjjAYsqcVkFOn39GOEZOqBvIFobHmeJbmn8aMx3k7bK2"

QUOTE_ASSET = "USDC"

SIMBOLOS = [
    "DOTUSDC",
    "AVAXUSDC",
    "NEARUSDC",
    "ATOMUSDC",
    "SOLUSDC",
    "XRPUSDC",
    "ADAUSDC",
    "DOGEUSDC",
    "FILUSDC",
]

CAPITAL_PORCENTAJE = 0.98

TAKE_PROFIT = 0.40
STOP_LOSS  = 0.80

REST_SECONDS = 8

# ============================
# LOGGING
# ============================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("CryptoBotMargin")

# ============================
# FUNC AUXILIARES
# ============================

def get_symbol_filters(client: Client, symbol: str):
    info = client.get_symbol_info(symbol)
    lot_filter = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
    min_qty = float(lot_filter["minQty"])
    step_size = float(lot_filter["stepSize"])

    notional_filter = next((f for f in info["filters"] if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL")), None)
    if notional_filter:
        min_notional = float(notional_filter.get("minNotional", 0.0))
    else:
        min_notional = 0.0

    return min_qty, step_size, min_notional


def obtener_margin_account(client: Client):
    return client.get_margin_account()


def obtener_saldo_margin(client: Client, asset: str) -> float:
    acc = obtener_margin_account(client)
    for a in acc["userAssets"]:
        if a["asset"] == asset:
            return float(a["free"])
    return 0.0


def obtener_precio(client: Client, symbol: str) -> float:
    t = client.get_symbol_ticker(symbol=symbol)
    return float(t["price"])


def obtener_score_15m(client: Client, symbol: str) -> float:
    """
    Devuelve el % de cambio en los ÚLTIMOS 15 MINUTOS.
    (close - open) / open * 100 del último candle 15m.
    """
    klines = client.get_klines(symbol=symbol, interval=KLINE_INTERVAL_15MINUTE, limit=2)

    ultimo = klines[-1]
    open_price = float(ultimo[1])
    close_price = float(ultimo[4])

    if open_price <= 0:
        return 0.0

    return (close_price / open_price - 1.0) * 100.0


def ajustar_qty_desde_capital(client: Client, symbol: str, capital_usdc: float, price: float):
    min_qty, step_size, min_notional = get_symbol_filters(client, symbol)

    if capital_usdc <= 0:
        return None

    raw_qty = capital_usdc / price
    if raw_qty <= 0:
        return None

    qty = math.floor(raw_qty / step_size) * step_size
    qty = float(f"{qty:.8f}")

    if qty < min_qty:
        return None
    if qty * price < min_notional:
        return None

    return qty


def ajustar_qty_desde_balance_margin(client: Client, symbol: str, balance_qty: float, price: float):
    min_qty, step_size, min_notional = get_symbol_filters(client, symbol)

    if balance_qty <= 0:
        return None

    qty = math.floor(balance_qty / step_size) * step_size
    qty = float(f"{qty:.8f}")

    if qty < min_qty:
        return None
    if qty * price < min_notional:
        return None

    return qty


def detectar_posicion_actual_margin(client: Client):
    saldo_usdc_margin = obtener_saldo_margin(client, QUOTE_ASSET)

    mejor_symbol = "USDC"
    mejor_valor_usdc = 0.0

    for symbol in SIMBOLOS:
        base = symbol.replace(QUOTE_ASSET, "")
        saldo_base_margin = obtener_saldo_margin(client, base)

        if saldo_base_margin <= 0:
            continue

        price = obtener_precio(client, symbol)
        valor_usdc = saldo_base_margin * price

        if valor_usdc > mejor_valor_usdc:
            mejor_valor_usdc = valor_usdc
            mejor_symbol = symbol

    if mejor_symbol == "USDC":
        logger.info(f"Iniciando en USDC (MARGIN) con saldo {saldo_usdc_margin:.4f} USDC")
        return "USDC", 1.0
    else:
        entry_price = obtener_precio(client, mejor_symbol)
        logger.info(f"Iniciando detectando posición MARGIN en {mejor_symbol} (≈ {mejor_valor_usdc:.2f} USDC)")
        return mejor_symbol, entry_price


def elegir_mejor_symbol(client: Client):
    """
    🔥 ELIGE LA MEJOR MONEDA EN LOS ÚLTIMOS 15 MINUTOS.
    (score más alto)
    """
    mejor_symbol = None
    mejor_score = -999999.0

    for symbol in SIMBOLOS:
        try:
            score = obtener_score_15m(client, symbol)
            logger.info(f"Score 15m {symbol}: {score:.3f}%")

            if score > mejor_score:  # AHORA ELIGE LA MEJOR
                mejor_score = score
                mejor_symbol = symbol

        except Exception as e:
            logger.warning(f"No se pudo obtener score 15m de {symbol}: {e}")
            continue

    return mejor_symbol, mejor_score


def comprar_symbol_margin(client: Client, symbol: str, saldo_usdc_margin_free: float):
    price = obtener_precio(client, symbol)

    capital_usar = saldo_usdc_margin_free * CAPITAL_PORCENTAJE
    qty = ajustar_qty_desde_capital(client, symbol, capital_usar, price)

    if qty is None:
        logger.warning(f"No se puede comprar {symbol} (LOT_SIZE/minNotional).")
        return None, None

    logger.info(f"COMPRANDO MARGIN {symbol} qty={qty:.8f} (≈ {qty*price:.4f} USDC)")

    try:
        order = client.create_margin_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=qty,
            sideEffectType="MARGIN_BUY"
        )

        fills = order.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            total_quote = sum(float(f["price"]) * float(f["qty"]) for f in fills)
            avg_price = total_quote / total_qty
        else:
            avg_price = price

        logger.info(f"BUY ejecutada. Precio medio ≈ {avg_price:.8f}")
        return order, avg_price

    except Exception as e:
        logger.error(f"Error BUY {symbol}: {e}")
        return None, None


def vender_symbol_margin(client: Client, symbol: str):
    base = symbol.replace(QUOTE_ASSET, "")
    saldo_base_margin = obtener_saldo_margin(client, base)

    if saldo_base_margin <= 0:
        return None

    price = obtener_precio(client, symbol)
    qty = ajustar_qty_desde_balance_margin(client, symbol, saldo_base_margin, price)

    if qty is None:
        return None

    logger.info(f"VENDIENDO MARGIN {symbol} qty={qty:.8f} (≈ {qty*price:.4f} USDC)")

    try:
        order = client.create_margin_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=qty,
            sideEffectType="AUTO_REPAY"
        )
        return order

    except Exception as e:
        logger.error(f"Error SELL {symbol}: {e}")
        return None


# ============================
# LOOP PRINCIPAL
# ============================

def main():
    logger.info("Iniciando CryptoBot MARGIN x5 (versión fuerza 15m)")

    client = Client(API_KEY, API_SECRET)
    symbol_actual, entry_price = detectar_posicion_actual_margin(client)

    while True:
        try:
            saldo_usdc_margin_free = obtener_saldo_margin(client, QUOTE_ASSET)

            if symbol_actual == "USDC":

                mejor_symbol, mejor_score = elegir_mejor_symbol(client)
                logger.info(f"Actual: USDC | Mejor (15m): {mejor_symbol} ({mejor_score:.3f}%)")

                order, avg_price = comprar_symbol_margin(client, mejor_symbol, saldo_usdc_margin_free)

                if order and avg_price:
                    symbol_actual = mejor_symbol
                    entry_price = avg_price

            else:
                price_now = obtener_precio(client, symbol_actual)
                pnl_pct = (price_now / entry_price - 1.0) * 100.0

                logger.info(f"Actual {symbol_actual} | Entry={entry_price:.8f} | Now={price_now:.8f} | PnL={pnl_pct:.3f}%")

                if pnl_pct >= TAKE_PROFIT:
                    vender_symbol_margin(client, symbol_actual)
                    symbol_actual = "USDC"
                    entry_price = 1.0

                elif pnl_pct <= -STOP_LOSS:
                    vender_symbol_margin(client, symbol_actual)
                    symbol_actual = "USDC"
                    entry_price = 1.0

            time.sleep(REST_SECONDS)

        except Exception as e:
            logger.error(f"Error en loop: {e}")
            time.sleep(REST_SECONDS * 2)


if __name__ == "__main__":
    main()