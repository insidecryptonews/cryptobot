#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CryptoBot (REST anti-ban + auto-ajuste LOT_SIZE/minQty)

- Una sola posición: USDC o una alt (DOTUSDC, AVAXUSDC, NEARUSDC, etc.).
- Si está en USDC → busca la mejor alt de la lista y compra.
- Si está en una alt → vende a USDC con TP/SL (en %).
- Ajusta cantidad a LOT_SIZE / minQty / minNotional para evitar -1013.

IMPORTANTE:
    - Rellena tu API_KEY y API_SECRET.
    - Este script sustituye COMPLETAMENTE al que tienes ahora.
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

API_KEY = "PON_AQUI_TU_API_KEY"
API_SECRET = "PON_AQUI_TU_API_SECRET"

QUOTE_ASSET = "USDC"

# Símbolos que va a operar (puedes cambiar esta lista si quieres)
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

# Usa este % del saldo USDC en cada operación
CAPITAL_PORCENTAJE = 0.98  # 98% del saldo

# Take Profit y Stop Loss en %
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
logger = logging.getLogger("CryptoBot")


# ============================
# FUNCIONES AUXILIARES
# ============================

def get_symbol_filters(client: Client, symbol: str):
    """Obtiene LOT_SIZE y MIN_NOTIONAL del símbolo."""
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


def ajustar_qty_desde_capital(client: Client, symbol: str, capital_usdc: float, price: float):
    """
    Devuelve una qty que respete LOT_SIZE / minQty / minNotional,
    calculada a partir de un capital en USDC.
    """
    min_qty, step_size, min_notional = get_symbol_filters(client, symbol)

    if capital_usdc <= 0:
        return None

    raw_qty = capital_usdc / price
    if raw_qty <= 0:
        return None

    # Ajustar al stepSize (siempre hacia abajo)
    qty = math.floor(raw_qty / step_size) * step_size
    qty = float(f"{qty:.8f}")

    if qty < min_qty:
        return None

    if qty * price < min_notional:
        return None

    return qty


def ajustar_qty_desde_balance(client: Client, symbol: str, balance_qty: float, price: float):
    """
    Devuelve una qty vendible que respete LOT_SIZE / minQty / minNotional
    a partir del balance de la alt.
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


def obtener_saldo(client: Client, asset: str) -> float:
    b = client.get_asset_balance(asset=asset)
    if not b:
        return 0.0
    return float(b["free"])


def obtener_precio(client: Client, symbol: str) -> float:
    t = client.get_symbol_ticker(symbol=symbol)
    return float(t["price"])


def obtener_score_24h(client: Client, symbol: str) -> float:
    """Devuelve el % de variación 24h. Lo usamos para elegir 'mejor' símbolo."""
    t = client.get_ticker(symbol=symbol)
    return float(t["priceChangePercent"])


def detectar_posicion_actual(client: Client):
    """
    Mira balances de todas las alts de SIMBOLOS.
    Si encuentra alguna con valor significativo en USDC,
    asume que esa es la posición actual.
    Si no, asume que estamos en USDC.
    """
    saldo_usdc = obtener_saldo(client, QUOTE_ASSET)
    mejor_symbol = "USDC"
    mejor_valor_usdc = 0.0

    for symbol in SIMBOLOS:
        base = symbol.replace(QUOTE_ASSET, "")
        saldo_base = obtener_saldo(client, base)
        if saldo_base <= 0:
            continue
        price = obtener_precio(client, symbol)
        valor_usdc = saldo_base * price
        if valor_usdc > mejor_valor_usdc:
            mejor_valor_usdc = valor_usdc
            mejor_symbol = symbol

    if mejor_symbol == "USDC":
        logger.info(f"Iniciando en USDC con saldo {saldo_usdc:.4f} {QUOTE_ASSET}")
        return "USDC", 1.0
    else:
        entry_price = obtener_precio(client, mejor_symbol)
        logger.info(
            f"Iniciando detectando posición en {mejor_symbol} "
            f"(≈ {mejor_valor_usdc:.2f} {QUOTE_ASSET} al precio {entry_price:.8f})"
        )
        return mejor_symbol, entry_price


def elegir_mejor_symbol(client: Client):
    """
    Elige el símbolo con peor rendimiento 24h (score más bajo),
    con idea de comprar 'lo más castigado'.
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


def comprar_symbol(client: Client, symbol: str, saldo_usdc: float):
    price = obtener_precio(client, symbol)
    capital_usar = saldo_usdc * CAPITAL_PORCENTAJE

    qty = ajustar_qty_desde_capital(client, symbol, capital_usar, price)
    if qty is None:
        logger.warning(
            f"⛔ No se puede comprar {symbol} con {capital_usar:.4f} {QUOTE_ASSET} "
            f"(LOT_SIZE / minQty / minNotional)."
        )
        return None, None

    logger.info(f"COMPRANDO {symbol} {qty:.8f} (≈ {qty * price:.4f} {QUOTE_ASSET})")

    try:
        order = client.create_order(
            symbol=symbol,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=qty
        )
        # Precio medio real
        fills = order.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            total_quote = sum(float(f["price"]) * float(f["qty"]) for f in fills)
            avg_price = total_quote / total_qty if total_qty > 0 else price
        else:
            avg_price = price

        logger.info(f"✅ BUY {symbol} ejecutada. Precio medio ≈ {avg_price:.8f}")
        return order, avg_price

    except BinanceAPIException as e:
        logger.error(f"❌ Error BUY {symbol}: {e}")
        return None, None
    except Exception as e:
        logger.error(f"❌ Error inesperado BUY {symbol}: {e}")
        return None, None


def vender_symbol(client: Client, symbol: str):
    base_asset = symbol.replace(QUOTE_ASSET, "")
    saldo_base = obtener_saldo(client, base_asset)

    if saldo_base <= 0:
        logger.warning(f"No hay saldo para vender en {symbol}.")
        return None

    price = obtener_precio(client, symbol)
    qty = ajustar_qty_desde_balance(client, symbol, saldo_base, price)
    if qty is None:
        logger.warning(
            f"⛔ No se puede vender {symbol}: saldo insuficiente o no cumple LOT_SIZE/minNotional."
        )
        return None

    logger.info(f"VENDIENDO {symbol} {qty:.8f} (≈ {qty * price:.4f} {QUOTE_ASSET})")

    try:
        order = client.create_order(
            symbol=symbol,
            side=SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=qty
        )
        logger.info(f"✅ SELL {symbol} ejecutada.")
        return order

    except BinanceAPIException as e:
        logger.error(f"❌ Error SELL {symbol}: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Error inesperado SELL {symbol}: {e}")
        return None


# ============================
# LOOP PRINCIPAL
# ============================

def main():
    logger.info("Iniciando CryptoBot (REST anti-ban + auto-ajuste LOT_SIZE/minQty)")

    client = Client(API_KEY, API_SECRET)

    symbol_actual, entry_price = detectar_posicion_actual(client)

    while True:
        try:
            saldo_usdc = obtener_saldo(client, QUOTE_ASSET)

            # 1) Estamos en USDC → buscar mejor alt y comprar
            if symbol_actual == "USDC":
                mejor_symbol, mejor_score = elegir_mejor_symbol(client)

                if mejor_symbol is None:
                    logger.warning("No se encontró ningún símbolo válido. Se queda en USDC.")
                    time.sleep(REST_SECONDS)
                    continue

                logger.info(
                    f"Actual: USDC (0.000%) | Mejor: {mejor_symbol} ({mejor_score:.3f}%)"
                )

                if saldo_usdc <= 1.0:
                    logger.warning(f"Saldo {QUOTE_ASSET} muy bajo ({saldo_usdc:.4f}). No se opera.")
                    time.sleep(REST_SECONDS)
                    continue

                order, avg_price = comprar_symbol(client, mejor_symbol, saldo_usdc)
                if order is not None and avg_price is not None:
                    symbol_actual = mejor_symbol
                    entry_price = avg_price
                else:
                    logger.warning("No se pudo ejecutar la compra. Se mantiene en USDC.")

            # 2) Estamos en una alt → mirar PnL y decidir vender o mantener
            else:
                price_now = obtener_precio(client, symbol_actual)
                pnl_pct = (price_now / entry_price - 1.0) * 100.0

                logger.info(
                    f"Actual: {symbol_actual} | Entry={entry_price:.8f} | "
                    f"Now={price_now:.8f} | PnL={pnl_pct:.3f}%"
                )

                if pnl_pct >= TAKE_PROFIT:
                    logger.info(f"🎯 TAKE PROFIT alcanzado ({pnl_pct:.3f}%). Vendiendo a {QUOTE_ASSET}...")
                    order = vender_symbol(client, symbol_actual)
                    if order is not None:
                        symbol_actual = "USDC"
                        entry_price = 1.0
                    else:
                        logger.warning("Fallo al vender en TAKE_PROFIT.")

                elif pnl_pct <= -STOP_LOSS:
                    logger.info(f"⚠️ STOP LOSS alcanzado ({pnl_pct:.3f}%). Vendiendo a {QUOTE_ASSET}...")
                    order = vender_symbol(client, symbol_actual)
                    if order is not None:
                        symbol_actual = "USDC"
                        entry_price = 1.0
                    else:
                        logger.warning("Fallo al vender en STOP_LOSS.")

                else:
                    logger.info(
                        f"Sin acción: PnL {pnl_pct:.3f}% "
                        f"(TP {TAKE_PROFIT:.2f}% / SL -{STOP_LOSS:.2f}%)"
                    )

            time.sleep(REST_SECONDS)

        except KeyboardInterrupt:
            logger.info("Detenido por el usuario.")
            break
        except BinanceAPIException as e:
            logger.error(f"Error API Binance: {e}")
            time.sleep(REST_SECONDS * 2)
        except Exception as e:
            logger.error(f"Error inesperado en el loop principal: {e}")
            time.sleep(REST_SECONDS * 2)


if __name__ == "__main__":
    main()