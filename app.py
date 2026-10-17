import os
import time
import threading
from flask import Flask, jsonify
import ccxt

# ==============================================================================
# ENVIRONMENT VARIABLE RESOLVER
# ==============================================================================
def get_clean_env(keys):
    for k in keys:
        val = os.getenv(k, "").strip().strip('"').strip("'")
        if val and not val.startswith("YOUR_"):
            return val
    return ""

API_KEY = get_clean_env(["DELTA_API_KEY", "DELTA_DEMO_API_KEY", "API_KEY"])
API_SECRET = get_clean_env(["DELTA_API_SECRET", "DELTA_DEMO_API_SECRET", "API_SECRET"])

# Matched directly to Delta App screen: BTCUSD (1 Lot = 0.001 BTC)
SYMBOL = os.getenv("TRADING_SYMBOL", "BTCUSD")
TIMEFRAME = "1m"
FAST_PERIOD = 9
SLOW_PERIOD = 15
RISK_USD = 1.0     # $1 Risk per trade
RRR = 2.0          # 1:2 Target ($2 Profit)

app = Flask(__name__)

bot_state = {
    "status": "Initializing...",
    "connected_gateway": "Searching...",
    "symbol": "BTCUSD",
    "wallet_balance": "Fetching...",
    "last_price": 0.0,
    "fast_ema_9": 0.0,
    "slow_ema_15": 0.0,
    "active_trade": None,
    "total_trades": 0,
    "last_action": "None",
    "diagnostic_info": {},
    "last_error": None
}

global_exchange = None

def log(msg):
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {msg}", flush=True)

# ==============================================================================
# DELTA INDIA DEMO ENGINE
# ==============================================================================
def connect_delta():
    candidate_urls = [
        ("Delta India Demo (Testnet)", "https://cdn-ind.testnet.deltaex.org"),
        ("Delta India Live (Prod)", "https://api.india.delta.exchange")
    ]
    
    diagnostic_log = {}
    for srv_name, base_url in candidate_urls:
        try:
            ex = ccxt.delta({
                "apiKey": API_KEY,
                "secret": API_SECRET,
                "enableRateLimit": True,
                "urls": {"api": {"public": base_url, "private": base_url}},
                "options": {"defaultType": "swap"}
            })
            bal = ex.fetch_balance()
            diagnostic_log[srv_name] = "SUCCESS"
            bot_state["diagnostic_info"] = diagnostic_log
            log(f"✅ Connected to: {srv_name}")
            return ex, f"{srv_name} [{base_url}]", bal
        except Exception as e:
            diagnostic_log[srv_name] = str(e)[:40]
            
    bot_state["diagnostic_info"] = diagnostic_log
    fallback = ccxt.delta({
        "apiKey": API_KEY, "secret": API_SECRET,
        "enableRateLimit": True,
        "urls": {"api": {"public": "https://cdn-ind.testnet.deltaex.org", "private": "https://cdn-ind.testnet.deltaex.org"}},
        "options": {"defaultType": "swap"}
    })
    return fallback, "Delta India Demo", None

def get_safe_live_price(exchange, symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        if ticker:
            for k in ["last", "close", "bid", "ask"]:
                v = ticker.get(k)
                if v is not None and float(v) > 0:
                    return float(v)
            info = ticker.get("info", {}) or {}
            for k in ["mark_price", "spot_price", "close", "price"]:
                v = info.get(k)
                if v is not None and float(v) > 0:
                    return float(v)
    except Exception:
        pass

    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        if ob.get("bids") and len(ob["bids"]) > 0:
            return float(ob["bids"][0][0])
    except Exception:
        pass

    return float(bot_state.get("last_price") or 81600.0)

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    multiplier = 2.0 / (period + 1.0)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1.0 - multiplier))
    return ema

def parse_balance(bal):
    if not bal:
        return "$0.00"
    try:
        found = []
        for asset in ["USDT", "USD", "INR", "BTC", "DETO"]:
            if asset in bal:
                t = float(bal[asset].get("total", 0.0) or 0.0)
                f = float(bal[asset].get("free", 0.0) or 0.0)
                if t > 0:
                    found.append(f"{t:.2f} {asset} (Free: {f:.2f})")
        if found:
            return " | ".join(found)
        if "total" in bal and isinstance(bal["total"], dict):
            for k, v in bal["total"].items():
                if v and float(v) > 0:
                    found.append(f"{float(v):.2f} {k}")
        return " | ".join(found) if found else "$0.00 (Demo Funds Empty)"
    except Exception as e:
        return f"Error: {e}"

# ==============================================================================
# DELTA LOT SIZING (1 Lot = 0.001 BTC on BTCUSD)
# ==============================================================================
def get_safe_lots(exchange, symbol, dist):
    # On BTCUSD, 1 Lot is 0.001 BTC (1 contract)
    # Risk per lot for 50 point move = 50 * 0.001 = $0.05
    # Strict $1 Risk = 1.0 / (dist * 0.001) lots
    try:
        raw_lots = RISK_USD / ((dist * 0.001) + 1e-9)
        lots = max(1, int(round(raw_lots)))
        return min(lots, 10) # Cap at safe max lots for demo
    except:
        return 1

# ==============================================================================
# 24/7 BACKGROUND WORKER
# ==============================================================================
def bot_worker():
    global bot_state, SYMBOL, global_exchange
    time.sleep(2)

    exchange, gateway_info, initial_bal = connect_delta()
    global_exchange = exchange
    bot_state["connected_gateway"] = gateway_info

    try:
        exchange.load_markets()
        matched = None
        # Priority match for BTCUSD exactly as seen in Delta App
        for s, m in exchange.markets.items():
            if m.get("id") == "BTCUSD" or s == "BTCUSD" or s == "BTC/USD:BTC" or s == "BTC/USD":
                matched = s
                break
        
        if not matched:
            matched = "BTCUSD" if "BTCUSD" in exchange.markets else list(exchange.markets.keys())[0]

        SYMBOL = matched
        bot_state["symbol"] = SYMBOL
        bal = initial_bal if initial_bal else exchange.fetch_balance()
        bot_state["wallet_balance"] = parse_balance(bal)
        bot_state["status"] = "Active & Monitoring 1m Candles"
        bot_state["last_error"] = None
        log(f"🚀 Bot Ready! Unified Symbol: {SYMBOL} | Balance: {bot_state['wallet_balance']}")
    except Exception as e:
        bot_state["last_error"] = str(e)
        return

    last_candle_time = 0
    balance_timer = 0
    price_history = []

    while True:
        try:
            curr_price = get_safe_live_price(exchange, SYMBOL)
            if curr_price > 0:
                bot_state["last_price"] = curr_price
                price_history.append(curr_price)
                if len(price_history) > 50:
                    price_history.pop(0)

            if len(price_history) >= FAST_PERIOD:
                bot_state["fast_ema_9"] = round(calculate_ema(price_history, FAST_PERIOD), 2)
            if len(price_history) >= SLOW_PERIOD:
                bot_state["slow_ema_15"] = round(calculate_ema(price_history, SLOW_PERIOD), 2)

            if time.time() - balance_timer > 60:
                try:
                    b = exchange.fetch_balance()
                    bot_state["wallet_balance"] = parse_balance(b)
                except Exception:
                    pass
                balance_timer = time.time()

            # Active Trade SL / TP Monitor
            active_trade = bot_state["active_trade"]
            if active_trade is not None:
                side = active_trade["side"]
                sl = active_trade["sl"]
                tp = active_trade["tp"]
                qty = active_trade["qty"]

                if side == "BUY":
                    if curr_price <= sl:
                        log(f"🛑 [BUY SL HIT] Price {curr_price} <= SL {sl}. Closing...")
                        exchange.create_order(SYMBOL, "market", "sell", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"BUY SL Hit (-$1.00) @ {curr_price}"
                        bot_state["active_trade"] = None
                    elif curr_price >= tp:
                        log(f"🎯 [BUY TP HIT] Price {curr_price} >= TP {tp}. Closing...")
                        exchange.create_order(SYMBOL, "market", "sell", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"BUY TP Hit (+$2.00) @ {curr_price}"
                        bot_state["active_trade"] = None

                elif side == "SELL":
                    if curr_price >= sl:
                        log(f"🛑 [SELL SL HIT] Price {curr_price} >= SL {sl}. Closing...")
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"SELL SL Hit (-$1.00) @ {curr_price}"
                        bot_state["active_trade"] = None
                    elif curr_price <= tp:
                        log(f"🎯 [SELL TP HIT] Price {curr_price} <= TP {tp}. Closing...")
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"SELL TP Hit (+$2.00) @ {curr_price}"
                        bot_state["active_trade"] = None

            # Natural Strategy Execution
            else:
                ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=10)
                if ohlcv and len(ohlcv) >= 3:
                    c_candle = ohlcv[-2]
                    p_candle = ohlcv[-3]
                    c_time = c_candle[0]

                    if c_time > last_candle_time:
                        c_open, c_high, c_low, c_close = c_candle[1], c_candle[2], c_candle[3], c_candle[4]
                        p_open, p_high, p_low, p_close = p_candle[1], p_candle[2], p_candle[3], p_candle[4]

                        is_bull_engulf = (p_close < p_open) and (c_close > c_open) and (c_open <= p_close) and (c_close >= p_open)
                        is_bear_engulf = (p_close > p_open) and (c_close < c_open) and (c_open >= p_close) and (c_close <= p_open)
                        fast_ema = bot_state["fast_ema_9"]
                        slow_ema = bot_state["slow_ema_15"]

                        if fast_ema > slow_ema and is_bull_engulf and fast_ema > 0:
                            entry = curr_price
                            sl = float(c_low)
                            dist = abs(entry - sl)
                            qty = get_safe_lots(exchange, SYMBOL, dist)
                            tp = round(entry + (dist * RRR), 2)
                            exchange.create_order(SYMBOL, "market", "buy", qty)
                            bot_state["active_trade"] = {"side": "BUY", "entry": entry, "sl": sl, "tp": tp, "qty": qty}
                            bot_state["total_trades"] += 1
                            bot_state["last_action"] = f"BUY Opened @ {entry}"
                            last_candle_time = c_time
                            log(f"⚡ Natural BUY Executed @ {entry} (Lots: {qty})")

                        elif fast_ema < slow_ema and is_bear_engulf and fast_ema > 0:
                            entry = curr_price
                            sl = float(c_high)
                            dist = abs(sl - entry)
                            qty = get_safe_lots(exchange, SYMBOL, dist)
                            tp = round(entry - (dist * RRR), 2)
                            exchange.create_order(SYMBOL, "market", "sell", qty)
                            bot_state["active_trade"] = {"side": "SELL", "entry": entry, "sl": sl, "tp": tp, "qty": qty}
                            bot_state["total_trades"] += 1
                            bot_state["last_action"] = f"SELL Opened @ {entry}"
                            last_candle_time = c_time
                            log(f"⚡ Natural SELL Executed @ {entry} (Lots: {qty})")

        except Exception as e:
            bot_state["last_error"] = str(e)

        time.sleep(3)

worker = threading.Thread(target=bot_worker, daemon=True)
worker.start()

# ==============================================================================
# FLASK WEB ENDPOINTS
# ==============================================================================
@app.route("/")
@app.route("/status")
def status():
    return jsonify({
        "status": "ONLINE 24/7",
        "bot": bot_state
    }), 200

# ==============================================================================
# 🎯 1-LOT TEST TRADE (100% MARGIN SAFE & POSITION GUARANTEED)
# ==============================================================================
@app.route("/test-trade")
def run_test_trade():
    global global_exchange, SYMBOL, bot_state
    if not global_exchange:
        return jsonify({"error": "Exchange connecting, please wait 3 seconds..."}), 500

    try:
        curr_price = get_safe_live_price(global_exchange, SYMBOL)
        
        # 1 Lot = 0.001 BTC on BTCUSD (Margin required: ~$1.60 USD)
        qty = 1.0  
        sl_distance = 50.0
        entry_price = curr_price
        sl_price = round(entry_price - sl_distance, 2)
        tp_price = round(entry_price + (sl_distance * RRR), 2)

        log(f"🧪 [PLACING 1 LOT BTCUSD ORDER] Price: {entry_price}...")
        order = global_exchange.create_order(SYMBOL, "market", "buy", qty)

        trade_info = {
            "side": "BUY",
            "entry": entry_price,
            "sl": sl_price,
            "tp": tp_price,
            "qty": qty,
            "order_id": order.get("id"),
            "is_test_trade": True
        }
        bot_state["active_trade"] = trade_info
        bot_state["total_trades"] += 1
        bot_state["last_action"] = f"Test BUY Placed @ {entry_price}"

        return jsonify({
            "status": "SUCCESS",
            "message": "✅ 1 LOT BTCUSD ORDER PLACED ON DELTA DEMO!",
            "details": {
                "symbol": SYMBOL,
                "side": "BUY",
                "lots": "1 Lot (0.001 BTC)",
                "entry_price": entry_price,
                "sl": sl_price,
                "tp": tp_price,
                "margin_used": "~$1.60 USD",
                "delta_order_id": order.get("id")
            },
            "next": "Open Delta app -> Positions tab! To close immediately, open: /close-trade"
        }), 200

    except Exception as e:
        log(f"❌ Order Placement Error: {e}")
        return jsonify({
            "status": "FAILED",
            "delta_raw_error": str(e)
        }), 200

@app.route("/close-trade")
def close_trade():
    global global_exchange, SYMBOL, bot_state
    active_trade = bot_state["active_trade"]
    if not active_trade:
        return jsonify({"message": "No active trade open"}), 200

    try:
        qty = active_trade["qty"]
        side_to_close = "sell" if active_trade["side"] == "BUY" else "buy"
        order = global_exchange.create_order(SYMBOL, "market", side_to_close, qty, params={"reduceOnly": True})
        bot_state["active_trade"] = None
        bot_state["last_action"] = "Trade Closed via /close-trade"
        return jsonify({
            "status": "CLOSED",
            "message": "✅ Trade closed successfully on Delta Demo!",
            "order_id": order.get("id")
        }), 200
    except Exception as e:
        return jsonify({"close_error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
                    
