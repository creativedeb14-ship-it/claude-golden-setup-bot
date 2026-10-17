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

def log(msg):
    """Render Console Real-time Flush Logger"""
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {msg}", flush=True)

# ==============================================================================
# DELTA INDIA DEMO & LIVE CONNECTIVITY ENGINE
# ==============================================================================
def smart_delta_connect():
    global bot_state
    
    # Delta India Demo uses cdn-ind.testnet.deltaex.org
    candidate_urls = [
        ("Delta India Demo (Testnet)", "https://cdn-ind.testnet.deltaex.org"),
        ("Delta India Live (Prod)", "https://api.india.delta.exchange"),
        ("Delta Global Testnet", "https://testnet-api.delta.exchange"),
        ("Delta Global Live", "https://api.delta.exchange")
    ]
    
    diagnostic_log = {}

    for srv_name, base_url in candidate_urls:
        if not API_KEY or not API_SECRET:
            log("⚠️ API_KEY or API_SECRET is missing from environment variables!")
            diagnostic_log["Config"] = "API Keys Missing"
            break

        try:
            log(f"🔍 Testing connection to {srv_name} ({base_url})...")
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
            
            # Fetch balance to verify private API key authentication
            bal = ex.fetch_balance()
            diagnostic_log[srv_name] = "SUCCESS"
            bot_state["diagnostic_info"] = diagnostic_log
            log(f" Connected successfully to: {srv_name}")
            return ex, f"{srv_name} [{base_url}]", bal

        except Exception as e:
            err_msg = str(e)
            if "invalid_api_key" in err_msg.lower():
                diagnostic_log[srv_name] = "Invalid Key"
                log(f"❌ {srv_name}: Invalid API Key")
            elif "ip" in err_msg.lower() and "whitelist" in err_msg.lower():
                diagnostic_log[srv_name] = "IP Not Whitelisted"
                log(f"❌ {srv_name}: IP Not Whitelisted on Delta account")
            else:
                diagnostic_log[srv_name] = err_msg[:50]
                log(f"❌ {srv_name} Error: {err_msg[:60]}")

    bot_state["diagnostic_info"] = diagnostic_log
    
    # Default fallback to India Demo
    log("⚠️ Falling back to default India Demo endpoint...")
    fallback = ccxt.delta({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "urls": {
            "api": {
                "public": "https://cdn-ind.testnet.deltaex.org",
                "private": "https://cdn-ind.testnet.deltaex.org"
            }
        },
        "options": {"defaultType": "swap"}
    })
    return fallback, "Delta India Demo (Fallback)", None

# ==============================================================================
# PURE PYTHON EMA & SAFE BALANCE PARSER
# ==============================================================================
def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1]
    multiplier = 2.0 / (period + 1.0)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1.0 - multiplier))
    return ema

def parse_balance(bal):
    if not bal:
        return "Balance: $0.00 / Fetching Failed"
    try:
        found_balances = []
        # Check standard collaterals on Delta India (USDT, USD, INR, BTC)
        for asset in ["USDT", "USD", "INR", "BTC", "DETO"]:
            if asset in bal:
                total_val = float(bal[asset].get("total", 0.0) or 0.0)
                free_val = float(bal[asset].get("free", 0.0) or 0.0)
                if total_val > 0:
                    found_balances.append(f"{total_val:.2f} {asset} (Free: {free_val:.2f})")
        
        if found_balances:
            return " | ".join(found_balances)

        # Fallback check inside bal['total']
        if "total" in bal and isinstance(bal["total"], dict):
            for k, v in bal["total"].items():
                if v and float(v) > 0:
                    found_balances.append(f"{float(v):.2f} {k}")
        
        return " | ".join(found_balances) if found_balances else "$0.00 (Demo Funds Empty)"
    except Exception as e:
        return f"Parse Error: {e}"

# ==============================================================================
# 24/7 BACKGROUND ALGO WORKER
# ==============================================================================
def bot_worker():
    global bot_state, SYMBOL
    time.sleep(2)

    exchange, gateway_info, initial_bal = smart_delta_connect()
    bot_state["connected_gateway"] = gateway_info

    contract_size = 1.0
    min_qty = 1.0
    is_inverse = False

    try:
        log("🔄 Loading exchange markets...")
        exchange.load_markets()
        
        matched = None
        if SYMBOL in exchange.markets:
            matched = SYMBOL
        else:
            # Common symbols on Delta India
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
            log(f"🚀 Bot Ready! Symbol: {SYMBOL} | Lot Min: {min_qty} | Balance: {bot_state['wallet_balance']}")
        else:
            bot_state["last_error"] = "BTC market symbol not found on exchange"
            log(f"❌ BTC market symbol not found in markets: {list(exchange.markets.keys())[:5]}")
            return

    except Exception as e:
        bot_state["last_error"] = str(e)
        log(f"❌ Market Setup Error: {e}")
        return

    active_trade = None
    last_candle_time = 0
    balance_timer = 0

    while True:
        try:
            # Fetch 1m candles
            ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=30)
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

            # Update & Log Wallet Balance every 60 seconds
            if time.time() - balance_timer > 60:
                try:
                    b = exchange.fetch_balance()
                    bot_state["wallet_balance"] = parse_balance(b)
                    log(f"💰 [WALLET UPDATE] {bot_state['wallet_balance']} | BTC Price: {curr_price}")
                except Exception as b_err:
                    log(f"⚠️ Balance fetch warning: {b_err}")
                balance_timer = time.time()

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
                        log(f"🛑 [BUY SL TRIGGERED] Current: {curr_price} <= SL: {sl}. Closing...")
                        exchange.create_order(SYMBOL, "market", "sell", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"BUY SL Hit (-$1.00) @ {curr_price}"
                        active_trade = None
                        bot_state["active_trade"] = None
                    elif curr_price >= tp:
                        log(f"🎯 [BUY TP TRIGGERED] Current: {curr_price} >= TP: {tp}. Closing...")
                        exchange.create_order(SYMBOL, "market", "sell", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"BUY TP Hit (+$2.00) @ {curr_price}"
                        active_trade = None
                        bot_state["active_trade"] = None

                elif side == "SELL":
                    if curr_price >= sl:
                        log(f"🛑 [SELL SL TRIGGERED] Current: {curr_price} >= SL: {sl}. Closing...")
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"SELL SL Hit (-$1.00) @ {curr_price}"
                        active_trade = None
                        bot_state["active_trade"] = None
                    elif curr_price <= tp:
                        log(f"🎯 [SELL TP TRIGGERED] Current: {curr_price} <= TP: {tp}. Closing...")
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"SELL TP Hit (+$2.00) @ {curr_price}"
                        active_trade = None
                        bot_state["active_trade"] = None

            # -------------------------------------------------------------
            # B) STRATEGY TRIGGER: EMA 9/15 CROSS + ENGULFING CANDLE
            # -------------------------------------------------------------
            else:
                c_candle = ohlcv[-2]  # Just closed candle
                p_candle = ohlcv[-3]  # Previous candle
                c_time = c_candle[0]

                if c_time > last_candle_time:
                    c_open, c_high, c_low, c_close = c_candle[1], c_candle[2], c_candle[3], c_candle[4]
                    p_open, p_high, p_low, p_close = p_candle[1], p_candle[2], p_candle[3], p_candle[4]

                    # Candlestick conditions
                    is_bull_engulf = (p_close < p_open) and (c_close > c_open) and (c_open <= p_close) and (c_close >= p_open)
                    is_bear_engulf = (p_close > p_open) and (c_close < c_open) and (c_open >= p_close) and (c_close <= p_open)

                    def get_safe_order_qty(distance):
                        if is_inverse:
                            pct_risk = distance / curr_price
                            notional = RISK_USD / (pct_risk + 1e-9)
                            raw_q = notional / contract_size
                        else:
                            qty_base = RISK_USD / (distance + 1e-9)
                            raw_q = qty_base / contract_size
                        
                        target_q = max(min_qty, raw_q)
                        try:
                            return float(exchange.amount_to_precision(SYMBOL, target_q))
                        except:
                            return max(min_qty, round(target_q, 2))

                    # BUY Entry: Fast EMA > Slow EMA + Bullish Engulfing
                    if fast_ema > slow_ema and is_bull_engulf:
                        entry = curr_price
                        sl = float(c_low)
                        dist = abs(entry - sl)
                        if dist > 0:
                            qty = get_safe_order_qty(dist)
                            tp = round(entry + (dist * RRR), 2)
                            log(f"⚡ BUY Signal Detected! Sending Order: Qty={qty}, SL={sl}, TP={tp}")
                            exchange.create_order(SYMBOL, "market", "buy", qty)
                            active_trade = {"side": "BUY", "entry": entry, "sl": sl, "tp": tp, "qty": qty}
                            bot_state["active_trade"] = active_trade
                            bot_state["total_trades"] += 1
                            bot_state["last_action"] = f"BUY Opened @ {entry}"
                            last_candle_time = c_time

                    # SELL Entry: Fast EMA < Slow EMA + Bearish Engulfing
                    elif fast_ema < slow_ema and is_bear_engulf:
                        entry = curr_price
                        sl = float(c_high)
                        dist = abs(sl - entry)
                        if dist > 0:
                            qty = get_safe_order_qty(dist)
                            tp = round(entry - (dist * RRR), 2)
                            log(f"⚡ SELL Signal Detected! Sending Order: Qty={qty}, SL={sl}, TP={tp}")
                            exchange.create_order(SYMBOL, "market", "sell", qty)
                            active_trade = {"side": "SELL", "entry": entry, "sl": sl, "tp": tp, "qty": qty}
                            bot_state["active_trade"] = active_trade
                            bot_state["total_trades"] += 1
                            bot_state["last_action"] = f"SELL Opened @ {entry}"
                            last_candle_time = c_time

        except Exception as e:
            bot_state["last_error"] = str(e)
            log(f"⚠️ Loop Warning: {e}")

        time.sleep(3)

# Background Thread Start
worker = threading.Thread(target=bot_worker, daemon=True)
worker.start()

# Status Webhook / Dashboard Endpoint
@app.route("/")
@app.route("/status")
def status():
    return jsonify({
        "status": "ONLINE 24/7",
        "bot": bot_state,
        "key_preview": {
            "key_loaded": f"{API_KEY[:4]}...{API_KEY[-4:]}" if len(API_KEY) >= 8 else "MISSING/SHORT",
            "key_length": len(API_KEY),
            "secret_loaded": f"{API_SECRET[:4]}...{API_SECRET[-4:]}" if len(API_SECRET) >= 8 else "MISSING/SHORT",
            "secret_length": len(API_SECRET)
        }
    }), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
                    
