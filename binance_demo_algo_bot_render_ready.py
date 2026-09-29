import hashlib
import hmac
import json
import logging
import math
import os
import time
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify
import threading

# =============================================================================
# BTC + ETH ROUND-LEVEL BREAKOUT ALGO BOT — Binance USDT-M Futures DEMO
# Render-ready / execution-safe revision
# =============================================================================
# STRATEGY RULES ARE KEPT FROM THE ORIGINAL BOT.
# Only execution, validation, recovery, logging and state-safety are hardened.
# =============================================================================

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
        "qty_precision": 3,
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
        "qty_precision": 3,
    },
}

CAPITAL = 100.0
POSITION_PCT = 0.05
PARTIAL_EXIT_PCT = 0.80
RUNNER_PCT = 0.20
LEVERAGE = 50
AUTO_BUMP_TO_MIN_NOTIONAL = True

IST = ZoneInfo("Asia/Kolkata")
SESSION_WINDOWS = [
    ("ASIA", 7, 0, 9, 0),
    ("LONDON", 13, 0, 14, 0),
    ("US", 18, 0, 20, 0),
]

PRICE_POLL_SECONDS = 5
REFERENCE_CLOSE_REFRESH_SECONDS = 5 * 60
HEARTBEAT_SECONDS = 15 * 60
HTTP_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5
STATE_FILE = os.environ.get("STATE_FILE", "bot_state.json")
LOG_FILE = os.environ.get("LOG_FILE", "algo_bot.log")

# Render Web Service requires an HTTP listener. This tiny health server does
# not participate in trading logic; it only satisfies Render port binding.
app = Flask(__name__)


@app.get("/")
def home():
    return jsonify({"status": "ok", "service": "binance-demo-algo-bot"})


@app.get("/health")
def health():
    return jsonify({"status": "healthy"})


BASE_URL = os.environ.get("BINANCE_BASE_URL", "https://demo-fapi.binance.com").rstrip("/")
API_KEY = os.environ.get("BINANCE_DEMO_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_DEMO_API_SECRET", "")

session = requests.Session()
session.headers.update({
    "X-MBX-APIKEY": API_KEY,
    "User-Agent": "python-algo-bot-render/2.0",
})


def now_ist():
    return datetime.now(IST)


def today_ist():
    return now_ist().date().isoformat()


def _sign(params: dict) -> dict:
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query_string = urlencode(params)
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    params["signature"] = signature
    return params


def _request(method: str, path: str, params=None, signed=False, retries=MAX_RETRIES):
    params = dict(params or {})
    url = BASE_URL + path

    for attempt in range(1, retries + 1):
        try:
            request_params = _sign(params) if signed else params
            resp = session.request(
                method,
                url,
                params=request_params,
                timeout=HTTP_TIMEOUT,
            )

            try:
                data = resp.json()
            except ValueError:
                data = None

            if 200 <= resp.status_code < 300 and data is not None:
                if isinstance(data, dict) and data.get("code") is not None and data.get("code") < 0:
                    logging.error("[API ERROR] %s %s -> %s", method, path, data)
                    return None
                return data

            # Do not blindly retry permanent Binance errors such as invalid order.
            if isinstance(data, dict):
                code = data.get("code")
                msg = data.get("msg", "")
                if code is not None and int(code) < 0 and int(code) not in (-1001, -1003, -1007, -1021):
                    logging.error("[API ERROR] %s %s HTTP=%s -> %s", method, path, resp.status_code, data)
                    return None

            logging.warning(
                "[HTTP RETRY] %s %s attempt=%s/%s HTTP=%s body=%s",
                method, path, attempt, retries, resp.status_code, data,
            )
        except requests.RequestException as exc:
            logging.warning(
                "[NETWORK RETRY] %s %s attempt=%s/%s -> %s",
                method, path, attempt, retries, exc,
            )
        except Exception as exc:
            logging.exception("[REQUEST ERROR] %s %s -> %s", method, path, exc)
            return None

        if attempt < retries:
            time.sleep(RETRY_BACKOFF * attempt)

    logging.error("[REQUEST FAILED] %s %s after %s attempts", method, path, retries)
    return None


def get_mark_price(symbol: str):
    data = _request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
    try:
        return float(data["price"]) if data and "price" in data else None
    except (TypeError, ValueError):
        return None


def get_last_closed_kline_close(symbol: str, interval: str = "5m"):
    data = _request("GET", "/fapi/v1/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": 2,
    })
    if not data or len(data) < 2:
        return None
    try:
        return float(data[-2][4])
    except (TypeError, ValueError, IndexError):
        return None


def get_symbol_filters(symbol: str, fallback_precision: int):
    data = _request("GET", "/fapi/v1/exchangeInfo")
    precision = fallback_precision
    min_notional = 100.0
    if not data:
        logging.warning("[FILTERS] exchangeInfo unavailable for %s; using fallback precision/min_notional", symbol)
        return precision, min_notional

    for item in data.get("symbols", []):
        if item.get("symbol") != symbol:
            continue
        for f in item.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step = str(f.get("stepSize", "0.001"))
                if "." in step:
                    precision = len(step.split(".")[1].rstrip("0"))
            elif f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                mn = f.get("minNotional") or f.get("notional")
                if mn is not None:
                    min_notional = float(mn)
        break
    return precision, min_notional


def round_qty(qty: float, precision: int, round_up: bool = False) -> float:
    factor = 10 ** precision
    value = math.ceil(qty * factor) / factor if round_up else math.floor(qty * factor) / factor
    return float(f"{value:.{precision}f}")


def place_market_order(symbol: str, side: str, qty: float, client_order_id: str):
    if qty <= 0:
        return None
    result = _request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
        "newClientOrderId": client_order_id,
        "newOrderRespType": "RESULT",
    }, signed=True)
    if result and result.get("orderId"):
        logging.info("[ORDER PLACED] MARKET %s %s qty=%s id=%s orderId=%s", symbol, side, qty, client_order_id, result.get("orderId"))
        return result

    # If the POST timed out after Binance accepted the order, recover by
    # querying the same clientOrderId before ever attempting another order.
    recovered = get_order_status(symbol, client_order_id)
    if recovered and recovered.get("orderId"):
        logging.warning("[ORDER RECOVERED] MARKET %s id=%s status=%s", symbol, client_order_id, recovered.get("status"))
        return recovered

    logging.error("[ORDER FAILED] MARKET %s %s qty=%s id=%s", symbol, side, qty, client_order_id)
    return None


def place_stop_market_order(symbol: str, side: str, qty: float, stop_price: float, client_order_id: str):
    if qty <= 0:
        return None
    result = _request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "quantity": qty,
        "stopPrice": round(stop_price, 2),
        "reduceOnly": "true",
        "workingType": "MARK_PRICE",
        "newClientOrderId": client_order_id,
    }, signed=True)
    if result and result.get("orderId"):
        logging.info("[SL ORDER PLACED] %s %s qty=%s stop=%s id=%s", symbol, side, qty, stop_price, client_order_id)
        return result

    # Recover a stop order that may have been accepted even though the HTTP
    # response was lost. This prevents duplicate protective orders.
    recovered = get_order_status(symbol, client_order_id)
    if recovered and recovered.get("orderId"):
        logging.warning("[SL ORDER RECOVERED] %s id=%s status=%s", symbol, client_order_id, recovered.get("status"))
        return recovered

    logging.error("[SL ORDER FAILED] %s %s qty=%s stop=%s id=%s", symbol, side, qty, stop_price, client_order_id)
    return None


def cancel_order(symbol: str, client_order_id: str):
    if not client_order_id:
        return None
    result = _request("DELETE", "/fapi/v1/order", {
        "symbol": symbol,
        "origClientOrderId": client_order_id,
    }, signed=True)
    if result:
        logging.info("[ORDER CANCELLED] %s id=%s", symbol, client_order_id)
    return result


def get_order_status(symbol: str, client_order_id: str):
    if not client_order_id:
        return None
    return _request("GET", "/fapi/v1/order", {
        "symbol": symbol,
        "origClientOrderId": client_order_id,
    }, signed=True)


def load_state():
    default = {
        "date": today_ist(),
        "daily_count": {sym: 0 for sym in CONFIG},
        "positions": {sym: None for sym in CONFIG},
        "reference_close": {sym: None for sym in CONFIG},
        "last_ref_refresh": {sym: 0 for sym in CONFIG},
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Backward-compatible repair of missing keys.
        for key, value in default.items():
            if key not in state:
                state[key] = value
        for sym in CONFIG:
            state["daily_count"].setdefault(sym, 0)
            state["positions"].setdefault(sym, None)
            state["reference_close"].setdefault(sym, None)
            state["last_ref_refresh"].setdefault(sym, 0)
        return state
    except Exception as exc:
        logging.error("[STATE] Load failed; starting with safe fresh state: %s", exc)
        return default


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        logging.error("[STATE] Save failed: %s", exc)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def reset_if_new_day(state):
    today = today_ist()
    if state.get("date") != today:
        logging.info("[NEW DAY] %s - daily trade counters reset.", today)
        state["date"] = today
        state["daily_count"] = {sym: 0 for sym in CONFIG}
    return state


def active_session_now():
    now = now_ist()
    for name, sh, sm, eh, em in SESSION_WINDOWS:
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if start <= now < end:
            return name
    return None


def get_trigger_levels(prev_close: float, round_size: int, buffer: int):
    upper_level = (math.floor(prev_close / round_size) + 1) * round_size
    lower_level = math.floor(prev_close / round_size) * round_size
    if upper_level == prev_close:
        upper_level += round_size
    if lower_level == prev_close:
        lower_level -= round_size
    return upper_level + buffer, lower_level - buffer


def qty_for_symbol(price: float, precision: int, min_notional: float, sym_name: str):
    notional = CAPITAL * POSITION_PCT
    if notional < min_notional:
        if not AUTO_BUMP_TO_MIN_NOTIONAL:
            logging.warning(
                "[%s] 5%% notional $%.2f < exchange minimum $%.2f. Trade skipped.",
                sym_name, notional, min_notional,
            )
            return 0.0
        logging.warning(
            "[%s] 5%% notional $%.2f < exchange minimum $%.2f. AUTO_BUMP enabled; minimum will be used.",
            sym_name, notional, min_notional,
        )
        notional = min_notional * 1.02

    qty = round_qty(notional / price, precision)
    if qty * price < min_notional:
        qty = round_qty(min_notional * 1.02 / price, precision, round_up=True)
    return qty


def verify_order_filled(symbol: str, client_order_id: str, timeout=5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = get_order_status(symbol, client_order_id)
        if last and last.get("status") in ("FILLED", "PARTIALLY_FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            return last
        time.sleep(0.5)
    return last


def open_new_position(sym_name, cfg, side, entry_price, precision, min_notional):
    order_side = "BUY" if side == "LONG" else "SELL"
    close_side = "SELL" if side == "LONG" else "BUY"
    qty = qty_for_symbol(entry_price, precision, min_notional, sym_name)
    if qty <= 0:
        return None

    ts = int(time.time() * 1000)
    entry_client_id = f"{sym_name}_entry_{ts}"
    entry_result = place_market_order(cfg["symbol"], order_side, qty, entry_client_id)
    if not entry_result:
        logging.error("[%s] Entry rejected/failed; NO local position state created.", sym_name)
        return None

    entry_status = entry_result.get("status")
    if entry_status != "FILLED":
        logging.error("[%s] Entry status=%s; refusing to create position state until the market order is fully filled.", sym_name, entry_status)
        return None

    # Use actual average fill price when Binance provides it.
    try:
        actual_entry = float(entry_result.get("avgPrice") or entry_price)
        if actual_entry <= 0:
            actual_entry = entry_price
    except (TypeError, ValueError):
        actual_entry = entry_price

    initial_sl = actual_entry - cfg["max_sl"] if side == "LONG" else actual_entry + cfg["max_sl"]
    sl_client_id = f"{sym_name}_sl80_{ts}"

    # IMPORTANT SAFETY FIX:
    # Exchange SL protects only the initial 80%. The 20% runner is introduced
    # only after the first SL event. This prevents a full-size stop from filling
    # and then a second market order accidentally over-closing the position.
    partial_qty = round_qty(qty * PARTIAL_EXIT_PCT, precision)
    runner_qty = round_qty(qty - partial_qty, precision)

    if partial_qty <= 0:
        logging.error("[%s] Calculated 80%% SL quantity is zero; closing safety position.", sym_name)
        place_market_order(cfg["symbol"], close_side, qty, f"{sym_name}_safety_close_{ts}")
        return None

    sl_result = place_stop_market_order(cfg["symbol"], close_side, partial_qty, initial_sl, sl_client_id)
    if not sl_result:
        logging.error("[%s] Protective SL placement failed. Closing entry immediately for safety.", sym_name)
        place_market_order(cfg["symbol"], close_side, qty, f"{sym_name}_safety_close_{ts}")
        return None

    position = {
        "side": side,
        "entry": actual_entry,
        "qty": qty,
        "sl": initial_sl,
        "peak": actual_entry,
        "breakeven_done": False,
        "partial_done": False,
        "sl_client_id": sl_client_id,
        "sl_qty": partial_qty,
        "runner_qty": runner_qty,
    }
    logging.info(
        "[ENTRY] %s %s @ %.4f qty=%s initial_sl=%.4f protective_sl_qty=%s runner_qty=%s",
        sym_name, side, actual_entry, qty, initial_sl, partial_qty, runner_qty,
    )
    return position


def replace_protective_sl(sym_name, cfg, position, new_sl):
    side = position["side"]
    close_side = "SELL" if side == "LONG" else "BUY"
    old_id = position.get("sl_client_id")
    sl_qty = position.get("sl_qty", position.get("qty", 0))

    if old_id:
        old_status = get_order_status(cfg["symbol"], old_id)
        if old_status and old_status.get("status") == "FILLED":
            logging.warning("[%s] Existing SL already FILLED while replacing; refusing duplicate SL.", sym_name)
            return False
        cancel_order(cfg["symbol"], old_id)

    new_id = f"{sym_name}_sl80_{int(time.time() * 1000)}"
    result = place_stop_market_order(cfg["symbol"], close_side, sl_qty, new_sl, new_id)
    if not result:
        logging.error("[%s] New protective SL failed; keeping old local SL state unchanged.", sym_name)
        return False

    position["sl"] = new_sl
    position["sl_client_id"] = new_id
    return True


def update_trailing_sl(sym_name, cfg, position, live_price):
    side = position["side"]
    entry = position["entry"]
    old_sl = position["sl"]
    candidate = old_sl

    if side == "LONG":
        position["peak"] = max(position["peak"], live_price)
        move = position["peak"] - entry
        if not position["breakeven_done"] and move >= cfg["breakeven_trigger"]:
            candidate = entry + cfg["breakeven_sl_offset"]
            position["breakeven_done"] = True
        if position["breakeven_done"]:
            extra = position["peak"] - entry - cfg["breakeven_trigger"]
            steps = int(extra // cfg["trail_step_move"])
            candidate = max(candidate, entry + cfg["breakeven_sl_offset"] + steps * cfg["trail_step_sl"])
    else:
        position["peak"] = min(position["peak"], live_price)
        move = entry - position["peak"]
        if not position["breakeven_done"] and move >= cfg["breakeven_trigger"]:
            candidate = entry - cfg["breakeven_sl_offset"]
            position["breakeven_done"] = True
        if position["breakeven_done"]:
            extra = entry - position["peak"] - cfg["breakeven_trigger"]
            steps = int(extra // cfg["trail_step_move"])
            candidate = min(candidate, entry - cfg["breakeven_sl_offset"] - steps * cfg["trail_step_sl"])

    if candidate != old_sl:
        if replace_protective_sl(sym_name, cfg, position, candidate):
            logging.info("[SL TRAIL UPDATE] %s old_sl=%s -> new_sl=%s", sym_name, old_sl, candidate)
        else:
            position["breakeven_done"] = position["breakeven_done"]
    return position


def handle_protective_sl_event(sym_name, cfg, position, precision):
    """Detect exchange-side 80% stop fill and create the 20% runner stop."""
    sl_id = position.get("sl_client_id")
    status = get_order_status(cfg["symbol"], sl_id) if sl_id else None
    if not status:
        return position, False

    order_status = status.get("status")
    if order_status != "FILLED":
        return position, False

    if position.get("partial_done"):
        # Runner SL was filled -> position is fully closed.
        logging.info("[RUNNER EXIT] %s protective runner SL FILLED. Position FULL CLOSE.", sym_name)
        return None, True

    runner_qty = position.get("runner_qty", round_qty(position["qty"] * RUNNER_PCT, precision))
    if runner_qty <= 0:
        logging.info("[FULL EXIT] %s no runner quantity remains.", sym_name)
        return None, True

    close_side = "SELL" if position["side"] == "LONG" else "BUY"
    runner_id = f"{sym_name}_runner_sl_{int(time.time() * 1000)}"
    runner_result = place_stop_market_order(cfg["symbol"], close_side, runner_qty, position["sl"], runner_id)
    if not runner_result:
        # The 80% stop has already filled. Do NOT create a fake local position;
        # instead mark runner as pending and retry next loop.
        logging.error("[%s] 80%% SL filled but runner SL placement failed; retrying runner protection.", sym_name)
        position["partial_done"] = True
        position["qty"] = runner_qty
        position["sl_qty"] = runner_qty
        position["sl_client_id"] = None
        position["runner_pending"] = True
        return position, True

    position["qty"] = runner_qty
    position["sl_qty"] = runner_qty
    position["partial_done"] = True
    position["sl_client_id"] = runner_id
    position["runner_pending"] = False
    logging.info("[PARTIAL EXIT 80%%] %s protective SL FILLED. Runner qty=%s remains with same SL=%s.", sym_name, runner_qty, position["sl"])
    return position, True


def ensure_runner_protection(sym_name, cfg, position):
    if not position.get("runner_pending") or position.get("sl_client_id"):
        return position
    close_side = "SELL" if position["side"] == "LONG" else "BUY"
    runner_id = f"{sym_name}_runner_sl_{int(time.time() * 1000)}"
    result = place_stop_market_order(
        cfg["symbol"], close_side, position["qty"], position["sl"], runner_id
    )
    if result:
        position["sl_client_id"] = runner_id
        position["runner_pending"] = False
        logging.info("[%s] Runner SL successfully restored after retry.", sym_name)
    return position


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )


def main():
    setup_logging()

    if not API_KEY or not API_SECRET:
        logging.error("BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET environment variables are missing. Bot stopped.")
        return

    precisions = {}
    min_notionals = {}
    for sym_name, cfg in CONFIG.items():
        prec, min_not = get_symbol_filters(cfg["symbol"], cfg["qty_precision"])
        precisions[sym_name] = prec
        min_notionals[sym_name] = min_not
        logging.info(
            "[INIT] %s precision=%s min_order=$%.2f target_5pct=$%.2f",
            sym_name, prec, min_not, CAPITAL * POSITION_PCT,
        )

    state = reset_if_new_day(load_state())
    save_state(state)
    last_heartbeat = 0
    logging.info("=== ALGO BOT STARTED (Binance Futures DEMO) ===")
    logging.info("[TIME] Strategy session timezone: Asia/Kolkata")
    logging.info("[CONFIG] Base URL: %s", BASE_URL)

    while True:
        try:
            state = reset_if_new_day(state)
            now_ts = time.time()

            for sym_name, cfg in CONFIG.items():
                symbol = cfg["symbol"]
                live_price = get_mark_price(symbol)
                if live_price is None:
                    logging.warning("[%s] Live price unavailable; skipping this cycle.", sym_name)
                    continue

                # Refresh the last CLOSED 5m candle, not the current candle.
                if now_ts - float(state["last_ref_refresh"].get(sym_name, 0)) >= REFERENCE_CLOSE_REFRESH_SECONDS:
                    ref = get_last_closed_kline_close(symbol)
                    if ref is not None:
                        state["reference_close"][sym_name] = ref
                        state["last_ref_refresh"][sym_name] = now_ts
                        logging.info("[%s] Reference 5m close updated: %s", sym_name, ref)

                position = state["positions"].get(sym_name)

                if position is not None:
                    position = ensure_runner_protection(sym_name, cfg, position)

                    # Exchange-side stop is the primary protection. Check its
                    # status before changing/replacing it.
                    position, sl_event = handle_protective_sl_event(
                        sym_name, cfg, position, precisions[sym_name]
                    ) if position else (None, False)

                    if position is not None and not sl_event:
                        position = update_trailing_sl(sym_name, cfg, position, live_price)

                    state["positions"][sym_name] = position
                else:
                    session_now = active_session_now()
                    daily_count = int(state["daily_count"].get(sym_name, 0))
                    ref_close = state["reference_close"].get(sym_name)

                    if session_now and daily_count < cfg["max_trades_per_day"] and ref_close:
                        buy_trigger, sell_trigger = get_trigger_levels(
                            float(ref_close), cfg["round_size"], cfg["buffer"]
                        )
                        new_position = None

                        if live_price >= buy_trigger:
                            new_position = open_new_position(
                                sym_name, cfg, "LONG", live_price,
                                precisions[sym_name], min_notionals[sym_name],
                            )
                        elif live_price <= sell_trigger:
                            new_position = open_new_position(
                                sym_name, cfg, "SHORT", live_price,
                                precisions[sym_name], min_notionals[sym_name],
                            )

                        if new_position:
                            state["positions"][sym_name] = new_position
                            state["daily_count"][sym_name] = daily_count + 1
                            logging.info(
                                "[%s] Session=%s, trade %s/%s opened.",
                                sym_name, session_now,
                                daily_count + 1, cfg["max_trades_per_day"],
                            )

            save_state(state)

            if now_ts - last_heartbeat >= HEARTBEAT_SECONDS:
                status_parts = []
                for sym_name, cfg in CONFIG.items():
                    price = get_mark_price(cfg["symbol"])
                    pos = state["positions"].get(sym_name)
                    if pos:
                        pos_str = f"OPEN({pos['side']}, qty={pos['qty']}, sl={pos['sl']})"
                    else:
                        pos_str = "FLAT"
                    status_parts.append(f"{sym_name}={price} [{pos_str}]")
                logging.info("[HEARTBEAT] " + " | ".join(status_parts))
                last_heartbeat = now_ts

            time.sleep(PRICE_POLL_SECONDS)

        except KeyboardInterrupt:
            logging.info("Bot manually stopped.")
            break
        except Exception as exc:
            # Never let one unexpected symbol/API error kill the whole process.
            logging.exception("[MAIN LOOP ERROR] %s", exc)
            time.sleep(PRICE_POLL_SECONDS)


def _start_bot_background():
    thread = threading.Thread(target=main, name="trading-bot", daemon=True)
    thread.start()
    return thread


# Gunicorn imports this module for Render Web Service. Start exactly one bot
# thread per Gunicorn worker; Render Start Command must use --workers 1.
if os.environ.get("RUN_BOT", "1") == "1":
    _start_bot_background()


if __name__ == "__main__":
    # Local execution: run Flask and the bot together.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
