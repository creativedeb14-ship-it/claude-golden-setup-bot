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

SYMBOL = os.getenv("TRADING_SYMBOL", "BTC/USDT:USDT")
TIMEFRAME = "1m"
FAST_PERIOD = 9
SLOW_PERIOD = 15
RISK_USD = 1.0     # $1 Risk per trade
RRR = 2.0          # 1:2 Target ($2 Profit)

app = Flask(__name__)

bot_state = {
    "status": "Initializing...",
    "connected_gateway": "Searching...",
    "symbol": "Resolving...",
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
        ("Delta India Live (Prod)", "https://api.india.delta.exchange"),
        ("Delta Global Testnet", "https://testnet-api.delta.exchange")
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
# SIZING CALCULATOR ($1 RISK SIZING)
# ==============================================================================
def get_safe_order_qty(exchange, symbol, entry_px, sl_px, is_inverse, contract_size, min_qty):
    dist = abs(entry_px - sl_px)
    if dist <= 0:
        dist = 50.0  # Safe default 50 points
        
    if is_inverse:
        pct_risk = dist / entry_px
        notional = RISK_USD / (pct_risk + 1e-9)
        raw_q = notional / contract_size
    else:
        qty_base = RISK_USD / (dist + 1e-9)
        raw_q = qty_base / contract_size

    target_q = max(min_qty, raw_q)
    try:
        return float(exchange.amount_to_precision(symbol, target_q))
    except:
        return max(min_qty, round(target_q, 3))

# ==============================================================================
# 24/7 BACKGROUND WORKER
# ==============================================================================
def bot_worker():
    global bot_state, SYMBOL, global_exchange
    time.sleep(2)

    exchange, gateway_info, initial_bal = connect_delta()
    global_exchange = exchange
    bot_state["connected_gateway"] = gateway_info

    contract_size = 1.0
    min_qty = 1.0
    is_inverse = False

    try:
        exchange.load_markets()
        matched = None
        if SYMBOL in exchange.markets:
            matched = SYMBOL
        else:
            for s, m in exchange.markets.items():
                m_id = m.get("id", "")
                if m_id in ["BTCUSDT", "BTCUSD"] or s in ["BTC/USDT:USDT", "BTC/USD:BTC", "BTC/USDT", "BTC/USD"]:
                    matched = s
                    break

        if matched:
            SYMBOL = matched
            m = exchange.market(SYMBOL)
            contract_size = float(m.get("contractSize", 1.0) or 1.0)
            min_qty = float(m.get("limits", {}).get("amount", {}).get("min", 1.0) or 1.0)
            is_inverse = bool(m.get("inverse", False))
            
            bot_state["symbol"] = SYMBOL
            bal = initial_bal if initial_bal else exchange.fetch_balance()
            bot_state["wallet_balance"] = parse_balance(bal)
            bot_state["status"] = "Active & Monitoring 1m Candles"
            bot_state["last_error"] = None
            log(f"🚀 Bot Ready! Symbol: {SYMBOL} | Balance: {bot_state['wallet_balance']}")
        else:
            bot_state["last_error"] = "BTC market symbol not found"
            return
    except Exception as e:
        bot_state["last_error"] = str(e)
        return

    last_candle_time = 0
    balance_timer = 0
    price_history = []

    while True:
        try:
            # 1. ALWAYS FETCH LIVE TICKER (NEVER 0.0 AGAIN)
            ticker = exchange.fetch_ticker(SYMBOL)
            curr_price = float(ticker.get("last") or ticker.get("close") or ticker.get("info", {}).get("mark_price", 0.0))
            if curr_price > 0:
                bot_state["last_price"] = curr_price
                price_history.append(curr_price)
                if len(price_history) > 50:
                    price_history.pop(0)

            # Update EMAs
            if len(price_history) >= FAST_PERIOD:
                bot_state["fast_ema_9"] = round(calculate_ema(price_history, FAST_PERIOD), 2)
            if len(price_history) >= SLOW_PERIOD:
                bot_state["slow_ema_15"] = round(calculate_ema(price_history, SLOW_PERIOD), 2)

            # Update Wallet Balance every 60s
            if time.time() - balance_timer > 60:
                try:
                    b = exchange.fetch_balance()
                    bot_state["wallet_balance"] = parse_balance(b)
                except:
                    pass
                balance_timer = time.time()

            # -------------------------------------------------------------
            # A) ACTIVE TRADE MANAGEMENT (SL / TP CHECK)
            # -------------------------------------------------------------
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

            # -------------------------------------------------------------
            # B) NATURAL SIGNAL SCAN ON CLOSED CANDLE
            # -------------------------------------------------------------
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
                            qty = get_safe_order_qty(exchange, SYMBOL, entry, sl, is_inverse, contract_size, min_qty)
                            tp = round(entry + (abs(entry - sl) * RRR), 2)
                            order = exchange.create_order(SYMBOL, "market", "buy", qty)
                            bot_state["active_trade"] = {"side": "BUY", "entry": entry, "sl": sl, "tp": tp, "qty": qty}
                            bot_state["total_trades"] += 1
                            bot_state["last_action"] = f"Natural BUY Opened @ {entry}"
                            last_candle_time = c_time
                            log(f"⚡ Natural BUY Executed @ {entry}")

                        elif fast_ema < slow_ema and is_bear_engulf and fast_ema > 0:
                            entry = curr_price
                            sl = float(c_high)
                            qty = get_safe_order_qty(exchange, SYMBOL, entry, sl, is_inverse, contract_size, min_qty)
                            tp = round(entry - (abs(sl - entry) * RRR), 2)
                            order = exchange.create_order(SYMBOL, "market", "sell", qty)
                            bot_state["active_trade"] = {"side": "SELL", "entry": entry, "sl": sl, "tp": tp, "qty": qty}
                            bot_state["total_trades"] += 1
                            bot_state["last_action"] = f"Natural SELL Opened @ {entry}"
                            last_candle_time = c_time
                            log(f"⚡ Natural SELL Executed @ {entry}")

        except Exception as e:
            bot_state["last_error"] = str(e)

        time.sleep(3)

# Thread Start
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
        "bot": bot_state,
        "test_instructions": "To simulate an instant live trade on Delta Demo with $1 risk, open: /test-trade"
    }), 200

# ==============================================================================
# 🎯 INSTANT TEST-TRADE ENDPOINT (REAL ORDER EXECUTION TEST)
# ==============================================================================
@app.route("/test-trade")
def run_test_trade():
    global global_exchange, SYMBOL, bot_state
    if not global_exchange:
        return jsonify({"error": "Exchange not connected yet"}), 500

    try:
        # 1. Fetch exact live price
        ticker = global_exchange.fetch_ticker(SYMBOL)
        curr_price = float(ticker.get("last") or ticker.get("close") or ticker.get("info", {}).get("mark_price", 0.0))
        
        m = global_exchange.market(SYMBOL)
        contract_size = float(m.get("contractSize", 1.0) or 1.0)
        min_qty = float(m.get("limits", {}).get("amount", {}).get("min", 1.0) or 1.0)
        is_inverse = bool(m.get("inverse", False))

        # 2. Strict $1 Risk Math (50 Point distance test)
        sl_distance = 50.0  # BTC moves 50 USDT
        entry_price = curr_price
        sl_price = round(entry_price - sl_distance, 2)
        tp_price = round(entry_price + (sl_distance * RRR), 2)

        # Quantity strictly capped to $1.00 Max Risk
        qty = get_safe_order_qty(global_exchange, SYMBOL, entry_price, sl_price, is_inverse, contract_size, min_qty)

        # 3. Fire Real Market Order to Delta Demo
        log(f"🧪 [SIMULATING TEST TRADE] Sending BUY Order for {qty} {SYMBOL}...")
        order = global_exchange.create_order(SYMBOL, "market", "buy", qty)

        # 4. Save to Active Trade so Bot monitors SL/TP
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

        # Calculate estimated margin blocked
        notional_value = qty * entry_price * contract_size
        est_margin_blocked = round(notional_value / 50.0, 3) # At 50x leverage

        return jsonify({
            "message": "✅ TEST TRADE SUCCESSFULLY PLACED ON DELTA DEMO!",
            "trade_breakdown": {
                "symbol": SYMBOL,
                "side": "BUY",
                "entry_price": entry_price,
                "stop_loss_price": sl_price,
                "take_profit_price": tp_price,
                "contracts_or_qty": qty,
                "max_risk_usd": "$1.00 USDT",
                "target_profit_usd": "$2.00 USDT",
                "risk_reward_ratio": "1:2",
                "approx_margin_blocked": f"~${est_margin_blocked} USDT (Leverage dependent)",
                "delta_order_id": order.get("id")
            },
            "next_step": "Check your Delta Demo browser tab -> Positions! To manually close this test trade immediately, visit: /close-trade"
        }), 200

    except Exception as e:
        return jsonify({"test_trade_failed": str(e)}), 500

# ==============================================================================
# 🛑 INSTANT CLOSE TEST-TRADE ENDPOINT
# ==============================================================================
@app.route("/close-trade")
def close_trade():
    global global_exchange, SYMBOL, bot_state
    active_trade = bot_state["active_trade"]
    if not active_trade:
        return jsonify({"message": "No active trade currently open"}), 200

    try:
        qty = active_trade["qty"]
        side_to_close = "sell" if active_trade["side"] == "BUY" else "buy"
        order = global_exchange.create_order(SYMBOL, "market", side_to_close, qty, params={"reduceOnly": True})
        bot_state["active_trade"] = None
        bot_state["last_action"] = "Trade Closed via /close-trade"
        return jsonify({
            "message": "✅ ACTIVE TRADE CLOSED SUCCESSFULLY ON DELTA DEMO!",
            "close_order_id": order.get("id")
        }), 200
    except Exception as e:
        return jsonify({"close_error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    
