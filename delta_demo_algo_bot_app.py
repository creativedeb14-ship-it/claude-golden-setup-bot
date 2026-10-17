import os
import time
import math
import threading
import pandas as pd
import numpy as np
import ccxt
from flask import Flask, jsonify

# ==============================================================================
# CONFIGURATION & PARAMETERS
# ==============================================================================
API_KEY = os.getenv("DELTA_API_KEY", "YOUR_DEMO_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET", "YOUR_DEMO_API_SECRET")
SYMBOL = os.getenv("TRADING_SYMBOL", "BTC/USD:USD")  # Delta perpetual contract
TIMEFRAME = "1m"

FAST_EMA = 9
SLOW_EMA = 15
RISK_USD = 1.0        # Strict $1 Risk per trade
RRR = 2.0             # 1:2 Risk to Reward Ratio
CHECK_INTERVAL = 3    # Seconds between checks

app = Flask(__name__)

# State management for dashboard & Render health ping
bot_state = {
    "status": "Initializing",
    "last_price": 0.0,
    "fast_ema": 0.0,
    "slow_ema": 0.0,
    "trend_bias": "NEUTRAL",
    "active_position": None,
    "last_trade_result": "None",
    "total_trades": 0,
    "last_error": None
}

# ==============================================================================
# EXCHANGE INITIALIZATION (DELTA DEMO / TESTNET)
# ==============================================================================
def get_exchange():
    exchange = ccxt.delta({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap"
        }
    })
    exchange.set_sandbox_mode(True)  # Forces testnet.delta.exchange endpoints
    return exchange

# ==============================================================================
# STRATEGY LOGIC: EMA & ENGULFING DETECTION
# ==============================================================================
def calculate_indicators(df):
    df["fast_ema"] = df["close"].ewm(span=FAST_EMA, adjust=False).mean()
    df["slow_ema"] = df["close"].ewm(span=SLOW_EMA, adjust=False).mean()
    return df

def is_bullish_engulfing(prev_c, curr_c):
    # Prev candle was Red, Current candle is Green
    prev_is_red = prev_c["close"] < prev_c["open"]
    curr_is_green = curr_c["close"] > curr_c["open"]
    
    # Current body completely engulfs previous body
    body_engulfed = (curr_c["open"] <= prev_c["close"]) and (curr_c["close"] >= prev_c["open"])
    
    return prev_is_red and curr_is_green and body_engulfed

def is_bearish_engulfing(prev_c, curr_c):
    # Prev candle was Green, Current candle is Red
    prev_is_green = prev_c["close"] > prev_c["open"]
    curr_is_red = curr_c["close"] < curr_c["open"]
    
    # Current body completely engulfs previous body
    body_engulfed = (curr_c["open"] >= prev_c["close"]) and (curr_c["close"] <= prev_c["open"])
    
    return prev_is_green and curr_is_red and body_engulfed

# ==============================================================================
# BOT BACKGROUND ENGINE (24/7 LOOP)
# ==============================================================================
def bot_worker():
    global bot_state
    exchange = get_exchange()
    
    try:
        exchange.load_markets()
        market = exchange.market(SYMBOL)
        contract_size = market.get("contractSize", 1.0)
        min_qty = market.get("limits", {}).get("amount", {}).get("min", 1.0)
        bot_state["status"] = "Connected to Delta Demo"
        print(f"Connected to Delta Testnet. Market: {SYMBOL} (Min Qty: {min_qty})")
    except Exception as e:
        bot_state["last_error"] = str(e)
        print(f"Connection Error: {e}")

    active_trade = None
    last_processed_timestamp = 0

    while True:
        try:
            # 1. Fetch live 1-minute OHLCV candles
            ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=30)
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df = calculate_indicators(df)

            curr_price = float(df["close"].iloc[-1])
            fast_ema = float(df["fast_ema"].iloc[-1])
            slow_ema = float(df["slow_ema"].iloc[-1])

            bot_state["last_price"] = curr_price
            bot_state["fast_ema"] = round(fast_ema, 2)
            bot_state["slow_ema"] = round(slow_ema, 2)

            # ------------------------------------------------------------------
            # A) ACTIVE TRADE MANAGEMENT (SL / TP MONITORING)
            # ------------------------------------------------------------------
            if active_trade is not None:
                side = active_trade["side"]
                entry_px = active_trade["entry"]
                sl_px = active_trade["sl"]
                tp_px = active_trade["tp"]
                qty = active_trade["qty"]

                # Check BUY Trade Exit
                if side == "BUY":
                    if curr_price <= sl_px:
                        # Market Close for SL ($1 loss)
                        exchange.create_order(SYMBOL, "market", "sell", qty, params={"reduceOnly": True})
                        active_trade = None
                        bot_state["active_position"] = None
                        bot_state["last_trade_result"] = f"SL Hit (-$1.00) @ {curr_price}"
                        print(f"🛑 BUY SL HIT @ {curr_price}")
                    elif curr_price >= tp_px:
                        # Market Close for TP ($2 profit)
                        exchange.create_order(SYMBOL, "market", "sell", qty, params={"reduceOnly": True})
                        active_trade = None
                        bot_state["active_position"] = None
                        bot_state["last_trade_result"] = f"TP Hit (+$2.00) @ {curr_price}"
                        print(f"🎯 BUY TP HIT @ {curr_price}")

                # Check SELL Trade Exit
                elif side == "SELL":
                    if curr_price >= sl_px:
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        active_trade = None
                        bot_state["active_position"] = None
                        bot_state["last_trade_result"] = f"SL Hit (-$1.00) @ {curr_price}"
                        print(f"🛑 SELL SL HIT @ {curr_price}")
                    elif curr_price <= tp_px:
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        active_trade = None
                        bot_state["active_position"] = None
                        bot_state["last_trade_result"] = f"TP Hit (+$2.00) @ {curr_price}"
                        print(f"🎯 SELL TP HIT @ {curr_price}")

            # ------------------------------------------------------------------
            # B) SIGNAL DETECTION ON LATEST CLOSED CANDLE
            # ------------------------------------------------------------------
            else:
                # df.iloc[-2] is the last fully closed 1m candle
                closed_c = df.iloc[-2]
                prev_closed_c = df.iloc[-3]
                closed_ts = int(closed_c["timestamp"])

                if closed_ts > last_processed_timestamp:
                    c_fast = float(closed_c["fast_ema"])
                    c_slow = float(closed_c["slow_ema"])
                    p_fast = float(prev_closed_c["fast_ema"])
                    p_slow = float(prev_closed_c["slow_ema"])

                    # 1. Check Bullish Crossover & Bullish Engulfing
                    crossover_up = (p_fast <= p_slow) or (c_fast > c_slow)
                    if c_fast > c_slow and is_bullish_engulfing(prev_closed_c, closed_c):
                        entry_price = curr_price
                        sl_price = float(closed_c["low"])
                        sl_distance = abs(entry_price - sl_price)

                        if sl_distance > 0:
                            # Dynamic Qty strictly capping risk to $1 USD
                            contracts = (RISK_USD / sl_distance) / contract_size
                            order_qty = max(min_qty, round(contracts, 3))
                            tp_price = entry_price + (sl_distance * RRR)

                            # Execute Market Buy
                            order = exchange.create_order(SYMBOL, "market", "buy", order_qty)
                            active_trade = {
                                "side": "BUY",
                                "entry": entry_price,
                                "sl": sl_price,
                                "tp": tp_price,
                                "qty": order_qty
                            }
                            bot_state["active_position"] = active_trade
                            bot_state["total_trades"] += 1
                            last_processed_timestamp = closed_ts
                            print(f"🚀 BUY ENTRY @ {entry_price} | SL: {sl_price} | TP: {tp_price} | Qty: {order_qty}")

                    # 2. Check Bearish Crossover & Bearish Engulfing
                    elif c_fast < c_slow and is_bearish_engulfing(prev_closed_c, closed_c):
                        entry_price = curr_price
                        sl_price = float(closed_c["high"])
                        sl_distance = abs(sl_price - entry_price)

                        if sl_distance > 0:
                            contracts = (RISK_USD / sl_distance) / contract_size
                            order_qty = max(min_qty, round(contracts, 3))
                            tp_price = entry_price - (sl_distance * RRR)

                            # Execute Market Sell
                            order = exchange.create_order(SYMBOL, "market", "sell", order_qty)
                            active_trade = {
                                "side": "SELL",
                                "entry": entry_price,
                                "sl": sl_price,
                                "tp": tp_price,
                                "qty": order_qty
                            }
                            bot_state["active_position"] = active_trade
                            bot_state["total_trades"] += 1
                            last_processed_timestamp = closed_ts
                            print(f"🔻 SELL ENTRY @ {entry_price} | SL: {sl_price} | TP: {tp_price} | Qty: {order_qty}")

        except Exception as e:
            bot_state["last_error"] = str(e)
            print(f"Loop Warning: {e}")

        time.sleep(CHECK_INTERVAL)

# ==============================================================================
# FLASK WEB ENDPOINT FOR RENDER & UPTIMEROBOT
# ==============================================================================
@app.route("/")
@app.route("/status")
def health_status():
    return jsonify({
        "bot_health": "ONLINE 24/7",
        "state": bot_state
    }), 200

if __name__ == "__main__":
    # Start bot loop in background thread
    worker_thread = threading.Thread(target=bot_worker, daemon=True)
    worker_thread.start()

    # Bind to Render dynamic port
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    
