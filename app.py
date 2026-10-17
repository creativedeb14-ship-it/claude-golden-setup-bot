import os
import time
import threading
from flask import Flask, jsonify
import ccxt

# ==============================================================================
# CONFIGURATION
# ==============================================================================
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")
SYMBOL = os.getenv("TRADING_SYMBOL", "BTC/USD:USD")
TIMEFRAME = "1m"

FAST_PERIOD = 9
SLOW_PERIOD = 15
RISK_USD = 1.0     # Strict $1 Risk
RRR = 2.0          # 1:2 Target

app = Flask(__name__)

bot_state = {
    "status": "Starting",
    "last_price": 0.0,
    "fast_ema": 0.0,
    "slow_ema": 0.0,
    "active_trade": None,
    "trades_count": 0,
    "last_action": "None",
    "last_error": None
}

# ==============================================================================
# DELTA TESTNET DIRECT CONNECTION
# ==============================================================================
def create_exchange():
    exchange = ccxt.delta({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "urls": {
            "api": {
                "public": "https://testnet-api.delta.exchange",
                "private": "https://testnet-api.delta.exchange"
            }
        },
        "options": {
            "defaultType": "swap"
        }
    })
    return exchange

# ==============================================================================
# PURE PYTHON EMA CALCULATION (NO PANDAS / NUMPY)
# ==============================================================================
def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1]
    multiplier = 2.0 / (period + 1.0)
    ema = sum(prices[:period]) / period  # SMA as initial seed
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1.0 - multiplier))
    return ema

# ==============================================================================
# BOT BACKGROUND WORKER
# ==============================================================================
def bot_worker():
    global bot_state
    time.sleep(3)  # Wait for Flask to bind port first
    
    exchange = create_exchange()
    contract_size = 1.0
    min_qty = 1.0

    try:
        exchange.load_markets()
        if SYMBOL in exchange.markets:
            m = exchange.market(SYMBOL)
            contract_size = m.get("contractSize", 1.0)
            min_qty = m.get("limits", {}).get("amount", {}).get("min", 1.0)
        bot_state["status"] = "Running & Connected"
        print(f"Connected to Delta Testnet for {SYMBOL}. Min Qty: {min_qty}")
    except Exception as e:
        bot_state["last_error"] = str(e)
        print(f"Init Error: {e}")

    active_trade = None
    last_candle_time = 0

    while True:
        try:
            # Fetch last 30 candles: [timestamp, open, high, low, close, volume]
            ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=35)
            if not ohlcv or len(ohlcv) < 20:
                time.sleep(2)
                continue

            closes = [c[4] for c in ohlcv]
            curr_price = closes[-1]
            fast_ema = calculate_ema(closes, FAST_PERIOD)
            slow_ema = calculate_ema(closes, SLOW_PERIOD)

            bot_state["last_price"] = curr_price
            bot_state["fast_ema"] = round(fast_ema, 2)
            bot_state["slow_ema"] = round(slow_ema, 2)

            # -------------------------------------------------------------
            # A) ACTIVE TRADE MANAGEMENT (SL / TP CHECK)
            # -------------------------------------------------------------
            if active_trade is not None:
                side = active_trade["side"]
                sl = active_trade["sl"]
                tp = active_trade["tp"]
                qty = active_trade["qty"]

                if side == "BUY":
                    if curr_price <= sl:
                        exchange.create_order(SYMBOL, "market", "sell", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"BUY SL Hit (-$1.00) @ {curr_price}"
                        active_trade = None
                        bot_state["active_trade"] = None
                        print(f"🛑 BUY SL Hit @ {curr_price}")
                    elif curr_price >= tp:
                        exchange.create_order(SYMBOL, "market", "sell", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"BUY TP Hit (+$2.00) @ {curr_price}"
                        active_trade = None
                        bot_state["active_trade"] = None
                        print(f"🎯 BUY TP Hit @ {curr_price}")

                elif side == "SELL":
                    if curr_price >= sl:
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"SELL SL Hit (-$1.00) @ {curr_price}"
                        active_trade = None
                        bot_state["active_trade"] = None
                        print(f"🛑 SELL SL Hit @ {curr_price}")
                    elif curr_price <= tp:
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"SELL TP Hit (+$2.00) @ {curr_price}"
                        active_trade = None
                        bot_state["active_trade"] = None
                        print(f"🎯 SELL TP Hit @ {curr_price}")

            # -------------------------------------------------------------
            # B) SIGNAL CHECK ON CLOSED 1M CANDLE
            # -------------------------------------------------------------
            else:
                # ohlcv[-2] is the completed candle, ohlcv[-3] is previous
                c_candle = ohlcv[-2]
                p_candle = ohlcv[-3]
                c_time = c_candle[0]

                if c_time > last_candle_time:
                    c_open, c_high, c_low, c_close = c_candle[1], c_candle[2], c_candle[3], c_candle[4]
                    p_open, p_high, p_low, p_close = p_candle[1], p_candle[2], p_candle[3], p_candle[4]

                    # 1. Bullish Engulfing: Prev red, Curr green, body covers prev body
                    is_bull_engulf = (p_close < p_open) and (c_close > c_open) and (c_open <= p_close) and (c_close >= p_open)

                    # 2. Bearish Engulfing: Prev green, Curr red, body covers prev body
                    is_bear_engulf = (p_close > p_open) and (c_close < c_open) and (c_open >= p_close) and (c_close <= p_open)

                    # BUY Condition
                    if fast_ema > slow_ema and is_bull_engulf:
                        entry = curr_price
                        sl = c_low
                        dist = abs(entry - sl)
                        if dist > 0:
                            qty = max(min_qty, round((RISK_USD / dist) / contract_size, 3))
                            tp = entry + (dist * RRR)
                            exchange.create_order(SYMBOL, "market", "buy", qty)
                            active_trade = {"side": "BUY", "entry": entry, "sl": sl, "tp": tp, "qty": qty}
                            bot_state["active_trade"] = active_trade
                            bot_state["trades_count"] += 1
                            last_candle_time = c_time
                            print(f"🚀 BUY: Entry {entry} | SL {sl} | TP {tp} | Qty {qty}")

                    # SELL Condition
                    elif fast_ema < slow_ema and is_bear_engulf:
                        entry = curr_price
                        sl = c_high
                        dist = abs(sl - entry)
                        if dist > 0:
                            qty = max(min_qty, round((RISK_USD / dist) / contract_size, 3))
                            tp = entry - (dist * RRR)
                            exchange.create_order(SYMBOL, "market", "sell", qty)
                            active_trade = {"side": "SELL", "entry": entry, "sl": sl, "tp": tp, "qty": qty}
                            bot_state["active_trade"] = active_trade
                            bot_state["trades_count"] += 1
                            last_candle_time = c_time
                            print(f"🔻 SELL: Entry {entry} | SL {sl} | TP {tp} | Qty {qty}")

        except Exception as e:
            bot_state["last_error"] = str(e)

        time.sleep(3)

# Start background thread automatically
worker = threading.Thread(target=bot_worker, daemon=True)
worker.start()

# ==============================================================================
# WEB ENDPOINTS
# ==============================================================================
@app.route("/")
@app.route("/status")
def status():
    return jsonify({
        "status": "ONLINE 24/7",
        "bot": bot_state
    }), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    
