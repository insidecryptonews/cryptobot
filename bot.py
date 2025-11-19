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
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
from binance.exceptions import BinanceAPIException

# ============================
# CONFIGURACIÓN
# ============================

API_KEY = "rbjXqPIinPOyCKfFJMBSgGjL642UENDnvcJDRpXx7u97z1AEmNeUXqQg1NnnxbOP"
API_SECRET = "e61MGR7vDZqvbOJtJryqGjjAYsqcVkFOn39GOEZOqBvIFobHmeJbmn8aMx3k7bK2"

QUOTE_ASSET = "USDC"

# PARES QUE VA A OPERAR EN MARGIN (DEBEN ESTAR HABILITADOS EN MARGIN EN BINANCE)
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

# Porcentaje del "capital usdc margin" a usar como base de cálculo por operación
# (Realmente con MARGIN_BUY puede usar apalancamiento según tu configuración de margin)
CAPITAL_PORCENTAJE = 0.98

# Take Profit y Stop Loss en porcentaje sobre el precio de entrada
TAKE_PROFIT = 0.40   # +0.40%
STOP_LOSS  = 0.80    # -0.80%

# Tiempo entre iteraciones
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
# FUNCIONES AUXILIARES
# ============================

def get_symbol_filters(client: Client, symbol: str):
    """Obtiene LOT_SIZE y MIN_NOTIONAL del símbolo (de la info spot, que es válida también para margin)."""
    info = client.get_symbol_info(symbol)
    lot_filter = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
    min_qty = float(lot_filter["minQty"])
    step_size = float(lot_filter["stepSize"])

    notional_filter = next(
        (f for f in info["filters"] if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL")),
        None
    )
    if notional_filter:
        min_notional = float(notional_filter.get("minNotional", 0.0))
    else:
        min_notional = 0.0

    return min_qty, step_size, min_notional


def obtener_margin_account(client: Client):
    """Devuelve el margin account completo (CROSS)."""
    return client.get_margin_account()


def obtener_saldo_margin(client: Client, asset: str) -> float:
    """
    Obtiene el saldo 'free' de un asset en MARGIN CROSS (no spot).
    """
    acc = obtener_margin_account(client)
    for a in acc["userAssets"]:
        if a["asset"] == asset:
            # 'free' es el saldo disponible, sin préstamos.
            return float(a["free"])
    return 0.0


def obtener_saldo_margin_total(client: Client, asset: str) -> float:
    """
    Obtiene el saldo total (free + borrowed - interest) de un asset en margin, por si quieres verlo.
    No lo usamos para operar directamente, pero puede servir como info.
    """
    acc = obtener_margin_account(client)
    for a in acc["userAssets"]:
        if a["asset"] == asset:
            free = float(a["free"])
            borrowed = float(a["borrowed"])
            interest = float(a.get("interest", 0.0))
            return free + borrowed - interest
    return 0.0


def obtener_precio(client: Client, symbol: str) -> float:
    t = client.get_symbol_ticker(symbol=symbol)
    return float(t["price"])


def obtener_score_24h(client: Client, symbol: str) -> float:
    t = client.get_ticker(symbol=symbol)
    return float(t["priceChangePercent"])


def ajustar_qty_desde_capital(client: Client, symbol: str, capital_usdc: float, price: float):
    """
    Devuelve qty ajustada para operar en margin, a partir de una cantidad 'equivalente' en USDC.
    Aunque uses apalancamiento, la qty base se calcula así; el sideEffectType='MARGIN_BUY'
    se encarga de pedir prestado si hace falta según tu configuración de margin.
    """
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
    """
    Ajusta la cantidad a vender en margin, según el balance de la alt en margin.
    """
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
    """
    Detecta si estamos básicamente en USDC (sin alts relevantes)
    o si tenemos alguna alt de SIMBOLOS con valor apreciable en margin.

    Devuelve (symbol_actual, entry_price_aproximado)
    - "USDC", 1.0  si no hay alts relevantes
    - "XXXUSDC", entry_price si detecta posición en alt
    """
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
        logger.info(f"Iniciando en USDC (MARGIN) con saldo {saldo_usdc_margin:.4f} {QUOTE_ASSET} (free)")
        return "USDC", 1.0
    else:
        entry_price = obtener_precio(client, mejor_symbol)
        logger.info(
            f"Iniciando detectando posición MARGIN en {mejor_symbol} "
            f"(≈ {mejor_valor_usdc:.2f} {QUOTE_ASSET} al precio {entry_price:.8f})"
        )
        return mejor_symbol, entry_price


def elegir_mejor_symbol(client: Client):
    """
    Elige el símbolo con peor rendimiento 24h (score más bajo)
    para intentar comprar "lo más castigado".
    """
    mejor_symbol = None
    mejor_score = 999999.0

    for symbol in SIMBOLOS:
        try:
            score = obtener_score_24h(client, symbol)
            logger.info(f"Score {symbol}: {score:.3f}% (24h)")
            if score < mejor_score:
                mejor_score = score
                mejor_symbol = symbol
        except Exception as e:
            logger.warning(f"No se pudo obtener score de {symbol}: {e}")
            continue

    return mejor_symbol, mejor_score


def comprar_symbol_margin(client: Client, symbol: str, saldo_usdc_margin_free: float):
    """
    Compra en margin:
    - side='BUY'
    - type='MARKET'
    - sideEffectType='MARGIN_BUY' (puede pedir prestado y usar apalancamiento)
    """
    price = obtener_precio(client, symbol)

    # Capital base que tomamos como referencia: free USDC en margin (sin contar lo prestado)
    capital_usar = saldo_usdc_margin_free * CAPITAL_PORCENTAJE

    qty = ajustar_qty_desde_capital(client, symbol, capital_usar, price)
    if qty is None:
        logger.warning(
            f"⛔ No se puede comprar {symbol} con capital base ≈ {capital_usar:.4f} {QUOTE_ASSET} "
            f"(LOT_SIZE / minQty / minNotional)."
        )
        return None, None

    logger.info(
        f"COMPRANDO MARGIN {symbol} qty={qty:.8f} "
        f"(≈ {qty * price:.4f} {QUOTE_ASSET} nominal; con apalancamiento según config margin)"
    )

    try:
        order = client.create_margin_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=qty,
            sideEffectType="MARGIN_BUY"   # clave para usar margin de verdad
        )

        fills = order.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            total_quote = sum(float(f["price"]) * float(f["qty"]) for f in fills)
            avg_price = total_quote / total_qty if total_qty > 0 else price
        else:
            avg_price = price

        logger.info(f"✅ BUY MARGIN {symbol} ejecutada. Precio medio ≈ {avg_price:.8f}")
        return order, avg_price

    except BinanceAPIException as e:
        logger.error(f"❌ Error BUY MARGIN {symbol}: {e}")
        return None, None
    except Exception as e:
        logger.error(f"❌ Error inesperado BUY MARGIN {symbol}: {e}")
        return None, None


def vender_symbol_margin(client: Client, symbol: str):
    """
    Vende en margin:
    - side='SELL'
    - type='MARKET'
    - sideEffectType='AUTO_REPAY' para repagar automáticamente el préstamo.
    """
    base_asset = symbol.replace(QUOTE_ASSET, "")
    saldo_base_margin = obtener_saldo_margin(client, base_asset)

    if saldo_base_margin <= 0:
        logger.warning(f"No hay saldo MARGIN para vender en {symbol}.")
        return None

    price = obtener_precio(client, symbol)
    qty = ajustar_qty_desde_balance_margin(client, symbol, saldo_base_margin, price)
    if qty is None:
        logger.warning(
            f"⛔ No se puede vender {symbol}: saldo margin insuficiente o no cumple LOT_SIZE/minNotional."
        )
        return None

    logger.info(
        f"VENDIENDO MARGIN {symbol} qty={qty:.8f} "
        f"(≈ {qty * price:.4f} {QUOTE_ASSET}) con AUTO_REPAY"
    )

    try:
        order = client.create_margin_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=qty,
            sideEffectType="AUTO_REPAY"   # repaga préstamo automáticamente
        )
        logger.info(f"✅ SELL MARGIN {symbol} ejecutada.")
        return order

    except BinanceAPIException as e:
        logger.error(f"❌ Error SELL MARGIN {symbol}: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Error inesperado SELL MARGIN {symbol}: {e}")
        return None


# ============================
# LOOP PRINCIPAL
# ============================

def main():
    logger.info("Iniciando CryptoBot MARGIN x5 (REST anti-ban + auto-ajuste LOT_SIZE/minQty)")

    client = Client(API_KEY, API_SECRET)

    # Detectar si estamos básicamente en USDC o en alguna alt en margin
    symbol_actual, entry_price = detectar_posicion_actual_margin(client)

    while True:
        try:
            saldo_usdc_margin_free = obtener_saldo_margin(client, QUOTE_ASSET)

            # 1) Estamos en USDC -> elegir mejor alt y comprar en margin
            if symbol_actual == "USDC":
                mejor_symbol, mejor_score = elegir_mejor_symbol(client)

                if mejor_symbol is None:
                    logger.warning("No se encontró ningún símbolo válido. Se queda en USDC (MARGIN).")
                    time.sleep(REST_SECONDS)
                    continue

                logger.info(
                    f"Actual: USDC (MARGIN) | Mejor: {mejor_symbol} ({mejor_score:.3f}% 24h)"
                )

                if saldo_usdc_margin_free <= 0.0:
                    logger.warning(
                        f"Saldo USDC MARGIN free muy bajo ({saldo_usdc_margin_free:.4f}). "
                        f"Aun así, con MARGIN_BUY Binance puede usar apalancamiento si tienes margen libre."
                    )

                order, avg_price = comprar_symbol_margin(client, mejor_symbol, saldo_usdc_margin_free)
                if order is not None and avg_price is not None:
                    symbol_actual = mejor_symbol
                    entry_price = avg_price
                else:
                    logger.warning("No se pudo ejecutar la compra MARGIN. Se mantiene en USDC.")

            # 2) Estamos en una alt -> revisar PnL sobre el precio de entrada y decidir venta
            else:
                price_now = obtener_precio(client, symbol_actual)
                pnl_pct = (price_now / entry_price - 1.0) * 100.0

                logger.info(
                    f"Actual (MARGIN): {symbol_actual} | Entry={entry_price:.8f} | "
                    f"Now={price_now:.8f} | PnL={pnl_pct:.3f}%"
                )

                if pnl_pct >= TAKE_PROFIT:
                    logger.info(f"🎯 TAKE PROFIT alcanzado ({pnl_pct:.3f}%). Vendiendo MARGIN...")
                    order = vender_symbol_margin(client, symbol_actual)
                    if order is not None:
                        symbol_actual = "USDC"
                        entry_price = 1.0
                    else:
                        logger.warning("Fallo al vender en TAKE_PROFIT (MARGIN).")

                elif pnl_pct <= -STOP_LOSS:
                    logger.info(f"⚠️ STOP LOSS alcanzado ({pnl_pct:.3f}%). Vendiendo MARGIN...")
                    order = vender_symbol_margin(client, symbol_actual)
                    if order is not None:
                        symbol_actual = "USDC"
                        entry_price = 1.0
                    else:
                        logger.warning("Fallo al vender en STOP_LOSS (MARGIN).")

                else:
                    logger.info(
                        f"Sin acción: PnL {pnl_pct:.3f}% "
                        f"(TP {TAKE_PROFIT:.2f}% / SL -{STOP_LOSS:.2f}%) [MARGIN]"
                    )

            time.sleep(REST_SECONDS)

        except KeyboardInterrupt:
            logger.info("Detenido por el usuario (Ctrl+C).")
            break
        except BinanceAPIException as e:
            logger.error(f"Error API Binance (MARGIN): {e}")
            time.sleep(REST_SECONDS * 2)
        except Exception as e:
            logger.error(f"Error inesperado en el loop principal (MARGIN): {e}")
            time.sleep(REST_SECONDS * 2)


if __name__ == "__main__":
    main()