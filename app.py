import os
import time
import threading
from flask import Flask, jsonify
import ccxt

# ==============================================================================
# CONFIGURATION
# ==============================================================================
raw_key = os.getenv("DELTA_API_KEY") or os.getenv("DELTA_DEMO_API_KEY") or ""
raw_secret = os.getenv("DELTA_API_SECRET") or os.getenv("DELTA_DEMO_API_SECRET") or ""

API_KEY = raw_key.strip().replace('"', '').replace("'", "")
API_SECRET = raw_secret.strip().replace('"', '').replace("'", "")

SYMBOL = os.getenv("TRADING_SYMBOL", "BTC/USD:BTC")
TIMEFRAME = "1m"
FAST_PERIOD = 9
SLOW_PERIOD = 15
RISK_USD = 1.0     # Strict $1 Risk per trade
RRR = 2.0          # 1:2 Target ($2 Profit)

app = Flask(__name__)

bot_state = {
    "status": "Starting engine...",
    "connected_endpoint": "Scanning...",
    "symbol": "Resolving...",
    "wallet_balance": "Fetching...",
    "last_price": 0.0,
    "fast_ema_9": 0.0,
    "slow_ema_15": 0.0,
    "active_trade": None,
    "total_trades": 0,
    "last_action": "None",
    "last_error": None
}

# ==============================================================================
# SMART DELTA DEMO CONNECTOR (AUTO-DETECTS DEMO VS TESTNET)
# ==============================================================================
def connect_to_delta():
    # Endpoints prioritized for demo.delta.exchange
    candidate_endpoints = [
        "https://api.demo.delta.exchange",
        "https://cdn.demo.delta.exchange",
        "https://api.testnet.delta.exchange",
        "https://cdn.testnet.delta.exchange",
        "https://testnet-api.delta.exchange"
    ]
    
    for base_url in candidate_endpoints:
        try:
            ex = ccxt.delta({
                "apiKey": API_KEY,
                "secret": API_SECRET,
                "enableRateLimit": True,
                "urls": {
                    "api": {
                        "public": base_url,
                        "private": base_url
                    }
                },
                "options": {
                    "defaultType": "swap"
                }
            })
            bal = ex.fetch_balance()
            # If fetch_balance succeeded without error, we found the right server!
            return ex, base_url, bal
        except Exception as e:
            continue

    # Fallback default
    fallback = ccxt.delta({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "urls": {
            "api": {
                "public": "https://api.demo.delta.exchange",
                "private": "https://api.demo.delta.exchange"
            }
        },
        "options": {"defaultType": "swap"}
    })
    return fallback, "https://api.demo.delta.exchange", None

# ==============================================================================
# PURE PYTHON EMA CALCULATION
# ==============================================================================
def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1]
    multiplier = 2.0 / (period + 1.0)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1.0 - multiplier))
    return ema

def parse_balance_string(bal):
    if not bal:
        return "$0.00"
    try:
        for asset in ["USDT", "USD", "BTC", "DETO"]:
            if asset in bal and float(bal[asset].get("total", 0) or 0) > 0:
                t = float(bal[asset]["total"])
                f = float(bal[asset]["free"])
                return f"{t:.4f} {asset} (Available: {f:.4f})"
        for k, v in bal.get("total", {}).items():
            if float(v or 0) > 0:
                return f"{float(v):.4f} {k}"
        return "$0.00 (Empty or Check Demo Funds)"
    except Exception as e:
        return f"Error: {e}"

# ==============================================================================
# 24/7 BACKGROUND BOT WORKER
# ==============================================================================
def bot_worker():
    global bot_state, SYMBOL
    time.sleep(3)

    exchange, endpoint, initial_bal = connect_to_delta()
    bot_state["connected_endpoint"] = endpoint

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
                if m.get("id") == "BTCUSD" or s == "BTC/USD:BTC" or m.get("id") == "BTCUSDT" or s == "BTC/USDT:USDT":
                    matched = s
                    break

        if matched:
            SYMBOL = matched
            m = exchange.market(SYMBOL)
            contract_size = float(m.get("contractSize", 1.0))
            min_qty = float(m.get("limits", {}).get("amount", {}).get("min", 1.0))
            is_inverse = bool(m.get("inverse", False))
            
            bot_state["symbol"] = SYMBOL
            bal = initial_bal if initial_bal else exchange.fetch_balance()
            bot_state["wallet_balance"] = parse_balance_string(bal)
            bot_state["status"] = "Active & Monitoring 1m Candles"
            bot_state["last_error"] = None
            print(f"✅ Bot Online | Endpoint: {endpoint} | Symbol: {SYMBOL} | Balance: {bot_state['wallet_balance']}")
        else:
            bot_state["last_error"] = "Market symbol not found"
            print("❌ Market symbol not found on Delta")
            return

    except Exception as e:
        bot_state["last_error"] = str(e)
        print(f"❌ Setup Error: {e}")
        return

    active_trade = None
    last_candle_time = 0
    balance_check_timer = 0

    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=35)
            if not ohlcv or len(ohlcv) < 20:
                time.sleep(2)
                continue

            closes = [c[4] for c in ohlcv]
            curr_price = float(closes[-1])
            fast_ema = calculate_ema(closes, FAST_PERIOD)
            slow_ema = calculate_ema(closes, SLOW_PERIOD)

            bot_state["last_price"] = curr_price
            bot_state["fast_ema_9"] = round(fast_ema, 2)
            bot_state["slow_ema_15"] = round(slow_ema, 2)

            # Quiet balance check every 2 mins
            if time.time() - balance_check_timer > 120:
                try:
                    b = exchange.fetch_balance()
                    bot_state["wallet_balance"] = parse_balance_string(b)
                except:
                    pass
                balance_check_timer = time.time()

            # -------------------------------------------------------------
            # A) MANAGE ACTIVE TRADE (SL / TP CHECK)
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
                        print(f"🛑 [BUY SL HIT] Closed @ {curr_price} | PnL: -$1.00")
                        active_trade = None
                        bot_state["active_trade"] = None
                    elif curr_price >= tp:
                        exchange.create_order(SYMBOL, "market", "sell", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"BUY TP Hit (+$2.00) @ {curr_price}"
                        print(f"🎯 [BUY TP HIT] Closed @ {curr_price} | PnL: +$2.00")
                        active_trade = None
                        bot_state["active_trade"] = None

                elif side == "SELL":
                    if curr_price >= sl:
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"SELL SL Hit (-$1.00) @ {curr_price}"
                        print(f"🛑 [SELL SL HIT] Closed @ {curr_price} | PnL: -$1.00")
                        active_trade = None
                        bot_state["active_trade"] = None
                    elif curr_price <= tp:
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"SELL TP Hit (+$2.00) @ {curr_price}"
                        print(f"🎯 [SELL TP HIT] Closed @ {curr_price} | PnL: +$2.00")
                        active_trade = None
                        bot_state["active_trade"] = None

            # -------------------------------------------------------------
            # B) SIGNAL CHECK ON CLOSED 1M CANDLE
            # -------------------------------------------------------------
            else:
                c_candle = ohlcv[-2]  # Completed candle
                p_candle = ohlcv[-3]  # Previous completed candle
                c_time = c_candle[0]

                if c_time > last_candle_time:
                    c_open, c_high, c_low, c_close = c_candle[1], c_candle[2], c_candle[3], c_candle[4]
                    p_open, p_high, p_low, p_close = p_candle[1], p_candle[2], p_candle[3], p_candle[4]

                    # 1. Bullish Engulfing
                    is_bull_engulf = (p_close < p_open) and (c_close > c_open) and (c_open <= p_close) and (c_close >= p_open)
                    # 2. Bearish Engulfing
                    is_bear_engulf = (p_close > p_open) and (c_close < c_open) and (c_open >= p_close) and (c_close <= p_open)

                    # Dynamic $1 Risk Qty
                    def calculate_qty(dist):
                        if is_inverse:
                            pct_risk = dist / curr_price
                            notional_usd = RISK_USD / (pct_risk + 1e-9)
                            raw_q = notional_usd / contract_size
                        else:
                            qty_base = RISK_USD / (dist + 1e-9)
                            raw_q = qty_base / contract_size
                        return max(min_qty, round(raw_q, 3))

                    # BUY Signal
                    if fast_ema > slow_ema and is_bull_engulf:
                        entry = curr_price
                        sl = float(c_low)
                        dist = abs(entry - sl)
                        if dist > 0:
                            qty = calculate_qty(dist)
                            tp = entry + (dist * RRR)
                            exchange.create_order(SYMBOL, "market", "buy", qty)
                            active_trade = {"side": "BUY", "entry": entry, "sl": sl, "tp": tp, "qty": qty}
                            bot_state["active_trade"] = active_trade
                            bot_state["total_trades"] += 1
                            bot_state["last_action"] = f"BUY Opened @ {entry}"
                            last_candle_time = c_time
                            print(f"🚀 [BUY ORDER] Entry: {entry} | SL: {sl} | TP: {tp} | Qty: {qty}")

                    # SELL Signal
                    elif fast_ema < slow_ema and is_bear_engulf:
                        entry = curr_price
                        sl = float(c_high)
                        dist = abs(sl - entry)
                        if dist > 0:
                            qty = calculate_qty(dist)
                            tp = entry - (dist * RRR)
                            exchange.create_order(SYMBOL, "market", "sell", qty)
                            active_trade = {"side": "SELL", "entry": entry, "sl": sl, "tp": tp, "qty": qty}
                            bot_state["active_trade"] = active_trade
                            bot_state["total_trades"] += 1
                            bot_state["last_action"] = f"SELL Opened @ {entry}"
                            last_candle_time = c_time
                            print(f"🔻 [SELL ORDER] Entry: {entry} | SL: {sl} | TP: {tp} | Qty: {qty}")

        except Exception as e:
            bot_state["last_error"] = str(e)

        time.sleep(3)

# Background Thread
worker = threading.Thread(target=bot_worker, daemon=True)
worker.start()

# Web Health Check
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
