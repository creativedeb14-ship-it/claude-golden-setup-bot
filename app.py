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
SYMBOL = os.getenv("TRADING_SYMBOL", "BTC/USD:BTC")
TIMEFRAME = "1m"

FAST_PERIOD = 9
SLOW_PERIOD = 15
RISK_USD = 1.0     # Strict $1 Risk per trade
RRR = 2.0          # 1:2 Target ($2 Profit)

app = Flask(__name__)

bot_state = {
    "status": "Starting engine...",
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
# DELTA TESTNET CLIENT
# ==============================================================================
def create_exchange():
    return ccxt.delta({
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

# ==============================================================================
# PURE PYTHON EMA (NO PANDAS / NUMPY)
# ==============================================================================
def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1]
    multiplier = 2.0 / (period + 1.0)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1.0 - multiplier))
    return ema

# ==============================================================================
# LIVE WALLET FETCHER
# ==============================================================================
def update_wallet_balance(exchange):
    try:
        bal = exchange.fetch_balance()
        for asset in ["USDT", "USD", "BTC"]:
            if asset in bal and float(bal[asset].get("total", 0) or 0) > 0:
                total = float(bal[asset]["total"])
                free = float(bal[asset]["free"])
                return f"{total:.4f} {asset} (Available: {free:.4f})"
        
        # Fallback to any positive balance
        for k, v in bal.get("total", {}).items():
            if float(v or 0) > 0:
                return f"{float(v):.4f} {k}"
        return "$0.00 (No Funds Found / Check API Key)"
    except Exception as e:
        return f"Fetch Error: {str(e)}"

# ==============================================================================
# 24/7 BACKGROUND TRADING WORKER (QUIET LOGS)
# ==============================================================================
def bot_worker():
    global bot_state, SYMBOL
    time.sleep(3)

    exchange = create_exchange()
    contract_size = 1.0
    min_qty = 1.0

    # 1. Resolve Exact Symbol on Delta Testnet
    try:
        exchange.load_markets()
        matched_symbol = None

        if SYMBOL in exchange.markets:
            matched_symbol = SYMBOL
        else:
            for s, m in exchange.markets.items():
                if m.get("id") == "BTCUSD" or s == "BTC/USD:BTC" or m.get("id") == "BTCUSDT":
                    matched_symbol = s
                    break

        if matched_symbol:
            SYMBOL = matched_symbol
            m = exchange.market(SYMBOL)
            contract_size = m.get("contractSize", 1.0)
            min_qty = m.get("limits", {}).get("amount", {}).get("min", 1.0)
            
            bot_state["symbol"] = SYMBOL
            bot_state["wallet_balance"] = update_wallet_balance(exchange)
            bot_state["status"] = "Active & Monitoring 1m Candles"
            
            # Single clean startup log
            print(f"✅ Bot Online | Symbol: {SYMBOL} | Balance: {bot_state['wallet_balance']}")
        else:
            bot_state["last_error"] = "BTC market not found on Delta"
            print("❌ Error: Could not locate BTC market on Delta")
            return

    except Exception as e:
        bot_state["last_error"] = str(e)
        print(f"❌ Connection Error: {e}")
        return

    active_trade = None
    last_candle_time = 0
    balance_check_timer = 0

    while True:
        try:
            # 2. Fetch live 1-minute OHLCV candles
            ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=35)
            if not ohlcv or len(ohlcv) < 20:
                time.sleep(2)
                continue

            closes = [c[4] for c in ohlcv]
            curr_price = closes[-1]
            fast_ema = calculate_ema(closes, FAST_PERIOD)
            slow_ema = calculate_ema(closes, SLOW_PERIOD)

            bot_state["last_price"] = curr_price
            bot_state["fast_ema_9"] = round(fast_ema, 2)
            bot_state["slow_ema_15"] = round(slow_ema, 2)

            # Update wallet balance quietly every 2 minutes
            if time.time() - balance_check_timer > 120:
                bot_state["wallet_balance"] = update_wallet_balance(exchange)
                balance_check_timer = time.time()

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
                        print(f"🛑 [BUY SL HIT] Closed @ {curr_price} | PnL: -$1.00")
                        active_trade = None
                        bot_state["active_trade"] = None
                        bot_state["wallet_balance"] = update_wallet_balance(exchange)
                    elif curr_price >= tp:
                        exchange.create_order(SYMBOL, "market", "sell", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"BUY TP Hit (+$2.00) @ {curr_price}"
                        print(f"🎯 [BUY TP HIT] Closed @ {curr_price} | PnL: +$2.00")
                        active_trade = None
                        bot_state["active_trade"] = None
                        bot_state["wallet_balance"] = update_wallet_balance(exchange)

                elif side == "SELL":
                    if curr_price >= sl:
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"SELL SL Hit (-$1.00) @ {curr_price}"
                        print(f"🛑 [SELL SL HIT] Closed @ {curr_price} | PnL: -$1.00")
                        active_trade = None
                        bot_state["active_trade"] = None
                        bot_state["wallet_balance"] = update_wallet_balance(exchange)
                    elif curr_price <= tp:
                        exchange.create_order(SYMBOL, "market", "buy", qty, params={"reduceOnly": True})
                        bot_state["last_action"] = f"SELL TP Hit (+$2.00) @ {curr_price}"
                        print(f"🎯 [SELL TP HIT] Closed @ {curr_price} | PnL: +$2.00")
                        active_trade = None
                        bot_state["active_trade"] = None
                        bot_state["wallet_balance"] = update_wallet_balance(exchange)

            # -------------------------------------------------------------
            # B) SIGNAL DETECTION ON LATEST CLOSED CANDLE
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

                    # BUY Signal
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
                            bot_state["total_trades"] += 1
                            bot_state["last_action"] = f"BUY Opened @ {entry}"
                            last_candle_time = c_time
                            print(f"🚀 [BUY ORDER EXECUTED] Entry: {entry} | SL: {sl} | TP: {tp} | Qty: {qty}")

                    # SELL Signal
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
                            bot_state["total_trades"] += 1
                            bot_state["last_action"] = f"SELL Opened @ {entry}"
                            last_candle_time = c_time
                            print(f"🔻 [SELL ORDER EXECUTED] Entry: {entry} | SL: {sl} | TP: {tp} | Qty: {qty}")

        except Exception as e:
            bot_state["last_error"] = str(e)

        time.sleep(3)

# Start background thread
worker = threading.Thread(target=bot_worker, daemon=True)
worker.start()

# ==============================================================================
# WEB STATUS ENDPOINT (CLEAN JSON MONITOR)
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
                
