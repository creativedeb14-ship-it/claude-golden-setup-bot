"""
=====================================================================
BTC + ETH Round-Level Breakout Strategy - LIVE ALGO BOT
Exchange: Binance SPOT Demo Mode (https://demo.binance.com)
Locked Spec v4 (session windows + carry-forward trade quota)
=====================================================================

IMPORTANT - EI JINISTA AGE PORO:
Ei bot SPOT market e trade kore. Spot e SHORT kora jay na (short korte
gele margin/futures lagbe). Tai strategy er "SELL/short" side ei bot e
DISABLED kora ache - sudhu "BUY" (upore breakout) side live thakbe.
Code er moddhe SELL side er logic already lekha ache, future e Delta
ba Bybit (duitai futures/perpetual) e move korle ekta config flag
(ALLOW_SHORT) True kore dile shei side o active hoye jabe. Kon jaygay
ki change korte hobe, file er shesh e "EXCHANGE MIGRATION NOTES" e
lekha ache.

SETUP (Colab / local / Render - jekono jaygay):
1. Binance account e login koro, "Binance Demo Trading" e jao, API key
   banao (Trading permission soho): https://demo.binance.com/en/my/settings/api-management
2. Neече BINANCE_API_KEY / BINANCE_API_SECRET environment variable
   hishebe set koro (code er moddhe hardcode koro na, nirapod na):
       export BINANCE_API_KEY="..."
       export BINANCE_API_SECRET="..."
3. pip install requests
4. python binance_demo_algo_bot.py
"""

import os
import json
import time
import hmac
import hashlib
import logging
import math
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN

import requests

# =====================================================================
# 0. LOGGING
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("algo_bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("algo_bot")

# =====================================================================
# 1. CONFIG
# =====================================================================
ALLOW_SHORT = False  # Binance Spot Demo e False thakte hobe. Delta/Bybit e True koro.

BASE_URL = "https://demo-api.binance.com/api"  # Binance SPOT DEMO MODE
IST = timezone(timedelta(hours=5, minutes=30))

CONFIG = {
    "BTC": {
        "symbol": "BTCUSDT",
        "round_size": 500,
        "buffer": 50,
        "max_sl": 150,
        "breakeven_trigger": 500,
        "breakeven_sl_offset": 400,
        "trail_step_move": 200,
        "trail_step_sl": 100,
        "max_trades_per_day": 2,
    },
    "ETH": {
        "symbol": "ETHUSDT",
        "round_size": 50,
        "buffer": 5,
        "max_sl": 10,
        "breakeven_trigger": 30,
        "breakeven_sl_offset": 20,
        "trail_step_move": 10,
        "trail_step_sl": 5,
        "max_trades_per_day": 2,
    }
}

# Trading windows in IST (start_hour, start_min, end_hour, end_min)
SESSION_WINDOWS = [
    ("ASIA",    7, 0, 9, 0),
    ("LONDON", 13, 0, 14, 0),
    ("US",     18, 0, 20, 0),
]

CAPITAL = 100.0          # fixed reference capital (USDT)
POSITION_PCT = 0.05      # capital er exact 5% -> position notional
PARTIAL_EXIT_PCT = 0.80
RUNNER_PCT = 0.20

POLL_INTERVAL_SEC = 5    # koto second por por price check hobe
STATE_FILE = "algo_state.json"
RECV_WINDOW = 5000

API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")


# =====================================================================
# 2. BINANCE DEMO REST CLIENT
# =====================================================================
class BinanceDemoClient:
    def __init__(self, api_key, api_secret, base_url=BASE_URL):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "X-MBX-APIKEY": self.api_key,
            "User-Agent": "python-algo-bot"
        })
        self._server_time_offset_ms = 0
        self._filters_cache = {}

    def _sign(self, params):
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return query_string, signature

    def _timestamp(self):
        return int(time.time() * 1000) + self._server_time_offset_ms

    def sync_time(self):
        r = self.session.get(f"{self.base_url}/v3/time", timeout=10)
        r.raise_for_status()
        server_ms = r.json()["serverTime"]
        local_ms = int(time.time() * 1000)
        self._server_time_offset_ms = server_ms - local_ms
        log.info(f"Server time synced, offset={self._server_time_offset_ms}ms")

    def public_get(self, path, params=None):
        params = params or {}
        r = self.session.get(f"{self.base_url}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def signed_request(self, method, path, params=None):
        params = dict(params or {})
        params["timestamp"] = self._timestamp()
        params["recvWindow"] = RECV_WINDOW
        query_string, signature = self._sign(params)
        url = f"{self.base_url}{path}?{query_string}&signature={signature}"
        r = self.session.request(method, url, timeout=10)
        if not r.ok:
            log.error(f"API error {r.status_code}: {r.text}")
        r.raise_for_status()
        return r.json()

    # ---- Market data ----
    def get_price(self, symbol):
        data = self.public_get("/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_symbol_filters(self, symbol):
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        data = self.public_get("/v3/exchangeInfo", {"symbol": symbol})
        sym_info = data["symbols"][0]
        filters = {f["filterType"]: f for f in sym_info["filters"]}
        step_size = filters.get("LOT_SIZE", {}).get("stepSize", "1")
        min_qty = filters.get("LOT_SIZE", {}).get("minQty", "0")
        tick_size = filters.get("PRICE_FILTER", {}).get("tickSize", "0.01")
        min_notional = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {})).get("minNotional", "0")
        result = {
            "step_size": Decimal(step_size),
            "min_qty": Decimal(min_qty),
            "tick_size": Decimal(tick_size),
            "min_notional": Decimal(min_notional),
        }
        self._filters_cache[symbol] = result
        return result

    def round_qty(self, symbol, qty):
        f = self.get_symbol_filters(symbol)
        step = f["step_size"]
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return float(q)

    # ---- Account ----
    def get_account(self):
        return self.signed_request("GET", "/v3/account")

    def get_free_balance(self, asset):
        acc = self.get_account()
        for b in acc["balances"]:
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    # ---- Orders ----
    def place_market_order(self, symbol, side, quantity, client_order_id):
        params = {
            "symbol": symbol,
            "side": side,           # BUY or SELL
            "type": "MARKET",
            "quantity": quantity,
            "newClientOrderId": client_order_id,
        }
        return self.signed_request("POST", "/v3/order", params)

    def get_order_by_client_id(self, symbol, client_order_id):
        params = {"symbol": symbol, "origClientOrderId": client_order_id}
        return self.signed_request("GET", "/v3/order", params)


# =====================================================================
# 3. STATE PERSISTENCE (crash-safe, atomic write)
# =====================================================================
def default_symbol_state():
    return {
        "trades_today": 0,
        "position": None,   # dict when open, None when flat
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "date": datetime.now(IST).strftime("%Y-%m-%d"),
            "symbols": {name: default_symbol_state() for name in CONFIG}
        }
    with open(STATE_FILE, "r") as f:
        state = json.load(f)

    today = datetime.now(IST).strftime("%Y-%m-%d")
    if state.get("date") != today:
        log.info(f"Notun din shuru hoyeche ({today}), trade counters reset hocche.")
        state["date"] = today
        for name in CONFIG:
            state["symbols"].setdefault(name, default_symbol_state())
            state["symbols"][name]["trades_today"] = 0
            # NOTE: open position thakle seta carry forward hobe, exit
            # kono time-restricted na, tai reset kora hocche na.
    for name in CONFIG:
        state["symbols"].setdefault(name, default_symbol_state())
    return state


def save_state(state):
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, STATE_FILE)  # atomic on POSIX + Windows


# =====================================================================
# 4. SESSION WINDOW CHECK
# =====================================================================
def get_active_window(now_ist):
    for name, sh, sm, eh, em in SESSION_WINDOWS:
        start = now_ist.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now_ist.replace(hour=eh, minute=em, second=0, microsecond=0)
        if start <= now_ist < end:
            return name
    return None


def is_last_window_of_day(window_name):
    return window_name == SESSION_WINDOWS[-1][0]


# =====================================================================
# 5. ROUND-LEVEL TRIGGER
# =====================================================================
def get_trigger_levels(prev_close, round_size, buffer):
    upper_level = math.ceil(prev_close / round_size) * round_size
    lower_level = math.floor(prev_close / round_size) * round_size
    if upper_level == prev_close:
        upper_level += round_size
    if lower_level == prev_close:
        lower_level -= round_size
    buy_trigger = upper_level + buffer
    sell_trigger = lower_level - buffer
    return buy_trigger, sell_trigger


# =====================================================================
# 6. ENTRY
# =====================================================================
def try_entry(client, name, cfg, state, now_ist, prev_close, current_price):
    sym_state = state["symbols"][name]

    if sym_state["position"] is not None:
        return  # already open, duplicate protection

    if sym_state["trades_today"] >= cfg["max_trades_per_day"]:
        return  # quota exhausted for the day

    window = get_active_window(now_ist)
    if window is None:
        return  # baire time e kono entry na

    buy_trigger, sell_trigger = get_trigger_levels(prev_close, cfg["round_size"], cfg["buffer"])

    side = None
    entry_price = None
    if current_price >= buy_trigger:
        side, entry_price = "LONG", buy_trigger
    elif ALLOW_SHORT and current_price <= sell_trigger:
        side, entry_price = "SHORT", sell_trigger
    else:
        return  # kono trigger hoyni

    if side == "SHORT" and not ALLOW_SHORT:
        return  # spot e short disabled

    notional = CAPITAL * POSITION_PCT
    raw_qty = notional / entry_price
    qty = client.round_qty(cfg["symbol"], raw_qty)

    filters = client.get_symbol_filters(cfg["symbol"])
    if Decimal(str(qty)) < filters["min_qty"] or qty * entry_price < float(filters["min_notional"]):
        log.warning(f"{name}: position size ({qty}) exchange er minimum theke choto, entry skip hocche.")
        return

    client_order_id = f"algo{name}{uuid.uuid4().hex[:10]}"
    binance_side = "BUY" if side == "LONG" else "SELL"

    try:
        log.info(f"{name}: {side} entry trigger! price={current_price}, trigger={entry_price}, qty={qty}, window={window}")
        order = client.place_market_order(cfg["symbol"], binance_side, qty, client_order_id)
        avg_fill = float(order.get("cummulativeQuoteQty", 0)) / max(float(order.get("executedQty", 1e-9)), 1e-9) \
            if order.get("executedQty") else entry_price

        sym_state["position"] = {
            "side": side,
            "entry": avg_fill,
            "qty": qty,
            "sl": avg_fill - cfg["max_sl"] if side == "LONG" else avg_fill + cfg["max_sl"],
            "peak": avg_fill,
            "breakeven_done": False,
            "partial_done": False,
            "client_order_id": client_order_id,
        }
        sym_state["trades_today"] += 1
        save_state(state)
        log.info(f"{name}: order filled @ {avg_fill}, SL set to {sym_state['position']['sl']}")
    except Exception as e:
        log.error(f"{name}: order placement FAILED, kono position track hocche na: {e}")


# =====================================================================
# 7. POSITION MANAGEMENT (breakeven + trailing + 80/20 exit)
# =====================================================================
def manage_position(client, name, cfg, state, current_price):
    sym_state = state["symbols"][name]
    pos = sym_state["position"]
    if pos is None:
        return

    side = pos["side"]
    entry = pos["entry"]
    be_trigger = cfg["breakeven_trigger"]
    be_offset = cfg["breakeven_sl_offset"]
    trail_step_move = cfg["trail_step_move"]
    trail_step_sl = cfg["trail_step_sl"]

    if side == "LONG":
        pos["peak"] = max(pos["peak"], current_price)
        move = pos["peak"] - entry

        if not pos["breakeven_done"] and move >= be_trigger:
            pos["sl"] = entry + be_offset
            pos["breakeven_done"] = True

        if pos["breakeven_done"]:
            extra_move = pos["peak"] - entry - be_trigger
            steps = int(extra_move // trail_step_move)
            trailed_sl = entry + be_offset + steps * trail_step_sl
            pos["sl"] = max(pos["sl"], trailed_sl)

        hit = current_price <= pos["sl"]
    else:  # SHORT
        pos["peak"] = min(pos["peak"], current_price)
        move = entry - pos["peak"]

        if not pos["breakeven_done"] and move >= be_trigger:
            pos["sl"] = entry - be_offset
            pos["breakeven_done"] = True

        if pos["breakeven_done"]:
            extra_move = entry - pos["peak"] - be_trigger
            steps = int(extra_move // trail_step_move)
            trailed_sl = entry - be_offset - steps * trail_step_sl
            pos["sl"] = min(pos["sl"], trailed_sl)

        hit = current_price >= pos["sl"]

    save_state(state)  # SL update hoyeche, state e save

    if not hit:
        return

    exit_side = "SELL" if side == "LONG" else "BUY"

    if not pos["partial_done"]:
        qty_exit = client.round_qty(cfg["symbol"], pos["qty"] * PARTIAL_EXIT_PCT)
        if qty_exit <= 0:
            qty_exit = pos["qty"]  # dust hole shobtai exit
        client_order_id = f"algo{name}EXIT80{uuid.uuid4().hex[:8]}"
        try:
            log.info(f"{name}: SL hit @ {pos['sl']}, 80% exit hocche, qty={qty_exit}")
            client.place_market_order(cfg["symbol"], exit_side, qty_exit, client_order_id)
            pos["partial_done"] = True
            pos["qty"] = round(pos["qty"] - qty_exit, 10)
            save_state(state)
        except Exception as e:
            log.error(f"{name}: 80% EXIT ORDER FAILED - MANUAL CHECK DORKAR: {e}")
    else:
        client_order_id = f"algo{name}EXIT20{uuid.uuid4().hex[:8]}"
        try:
            log.info(f"{name}: runner SL hit @ {pos['sl']}, baki 20% exit hocche, qty={pos['qty']}")
            client.place_market_order(cfg["symbol"], exit_side, pos["qty"], client_order_id)
            sym_state["position"] = None
            save_state(state)
            log.info(f"{name}: position fully closed.")
        except Exception as e:
            log.error(f"{name}: RUNNER EXIT ORDER FAILED - MANUAL CHECK DORKAR: {e}")


# =====================================================================
# 8. STARTUP SAFETY CHECK (duplicate/orphan position protection)
# =====================================================================
def startup_reconciliation(client, state):
    """
    Bot restart howar por, jodi kono symbol e wallet e asset (BTC/ETH)
    ache kintu state file e kono position track kora nai, tahole
    notun order na diye WARNING dey - jate accidentally duplicate
    position na hoy.
    """
    for name, cfg in CONFIG.items():
        base_asset = cfg["symbol"].replace("USDT", "")
        try:
            free_qty = client.get_free_balance(base_asset)
        except Exception as e:
            log.error(f"Startup check: {name} balance fetch failed: {e}")
            continue

        tracked = state["symbols"][name]["position"]
        if free_qty > 1e-8 and tracked is None:
            log.warning(
                f"⚠️  {name}: wallet e {free_qty} {base_asset} ache kintu state file e "
                f"kono position track kora nai! Ei symbol e ei muhurte notun entry NEWA HOBE NA, "
                f"jotokkhon na manually check kore state file thik kora hoy."
            )
            state["symbols"][name]["trades_today"] = cfg["max_trades_per_day"]  # safety lock


# =====================================================================
# 9. MAIN LOOP
# =====================================================================
def main():
    if not API_KEY or not API_SECRET:
        raise SystemExit(
            "BINANCE_API_KEY / BINANCE_API_SECRET environment variable set kora nai. "
            "export BINANCE_API_KEY=... ar export BINANCE_API_SECRET=... koro, tarpor abar chalao."
        )

    client = BinanceDemoClient(API_KEY, API_SECRET)
    client.sync_time()

    state = load_state()
    startup_reconciliation(client, state)
    save_state(state)

    log.info("=== Algo bot shuru hocche (Binance Spot Demo) ===")
    log.info(f"Symbols: {[cfg['symbol'] for cfg in CONFIG.values()]}")
    log.info(f"Session windows (IST): {[w[0] for w in SESSION_WINDOWS]}")

    prev_close_cache = {name: None for name in CONFIG}
    last_minute_seen = {name: None for name in CONFIG}

    while True:
        try:
            state = load_state()  # din bodle geche kina check
            now_ist = datetime.now(IST)

            for name, cfg in CONFIG.items():
                symbol = cfg["symbol"]
                current_price = client.get_price(symbol)

                # prev_close ke prottek notun minute e update kora hocche
                # (1-minute candle close er approximation)
                this_minute = now_ist.replace(second=0, microsecond=0)
                if last_minute_seen[name] != this_minute:
                    prev_close_cache[name] = current_price
                    last_minute_seen[name] = this_minute

                prev_close = prev_close_cache[name] or current_price

                # Position open thakle age seta manage koro (SL/trailing -
                # ei check e kono time restriction nai)
                if state["symbols"][name]["position"] is not None:
                    manage_position(client, name, cfg, state, current_price)
                else:
                    try_entry(client, name, cfg, state, now_ist, prev_close, current_price)

            time.sleep(POLL_INTERVAL_SEC)

        except requests.exceptions.RequestException as e:
            log.error(f"Network/API error, {POLL_INTERVAL_SEC*2}s por retry hobe: {e}")
            time.sleep(POLL_INTERVAL_SEC * 2)
        except Exception as e:
            log.exception(f"Unexpected error, loop cholte thakbe: {e}")
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()


# =====================================================================
# EXCHANGE MIGRATION NOTES (Delta / Bybit demo e move korar jonno)
# =====================================================================
# 1. ALLOW_SHORT = True koro upore - tokhon SELL/short side active hobe.
# 2. BinanceDemoClient class ta shorie notun exchange er REST client
#    likhte hobe (base_url, signing method, order endpoint format
#    alada). Strategy logic (get_trigger_levels, manage_position,
#    try_entry er bhitorer math) EKDOM SAME thakbe - shudhu client er
#    method call gula (place_market_order, get_price, get_free_balance,
#    get_symbol_filters) notun exchange er API onujayi implement korte
#    hobe.
# 3. Delta/Bybit futures e position size calculation ektu alada hote
#    pare (contract-based vs coin-based quantity) - oi exchange er
#    product spec dekhe qty rounding thik korte hobe.
# 4. Delta te bracket order (entry+SL+TP ekshathe) native vabe support
#    kore - chaile manage_position() er polling-based SL check na kore
#    exchange er built-in stop-loss order use kora better (kom latency,
#    bot crash hoyeo SL thake).
