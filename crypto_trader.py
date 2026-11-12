"""
crypto_trader.py - Bybit DEMO trading bot (perpetual futures, 1x leverage)   [v2 - fee-aware]

SAFETY: This file is hard-locked to Bybit DEMO. It refuses to run if demo mode
cannot be enabled. Do not change that until you have 4-8 weeks of results.

Usage:
    python crypto_trader.py            -> test menu
    python crypto_trader.py log        -> signals only, places NO orders
    python crypto_trader.py auto       -> auto trading on DEMO

Needs in bot_settings.py:
    BYBIT_API_KEY, BYBIT_API_SECRET
Optional in bot_settings.py (Telegram alerts). Either naming works:
    TELEGRAM_TOKEN + CHAT_ID      or      TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID

WHAT CHANGED IN v2 (why your first demo trades lost money):
  1. FEE FILTER: a trade is skipped unless its take-profit distance is at least
     MIN_TP_TO_FEE x the round-trip fee. Before, targets were ~0.10% away while
     fees cost ~0.11%, so even a WIN lost money.
  2. 15m candles, wider stop (2 x ATR) and a trend filter, so fewer, better-sized trades.
  3. A new signal in the opposite direction no longer closes a trade early
     (that doubled the fees). The stop-loss / take-profit decide.
  4. NET P&L AFTER FEES is logged for every trade in results_log.csv.
  5. Safety net: a position found without a stop-loss is closed immediately.
  6. Position size capped at MAX_NOTIONAL_PCT of equity; alerts show the REAL risk.
  7. 30s request timeout (you saw RequestTimeout), and Telegram name mismatch fixed.
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import ccxt
import requests

import bot_settings as S

# ------------------------- SETTINGS (defaults) -------------------------
SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "15m"
LEVERAGE = 1                 # keep at 1 for now
RISK_PCT = 1.0               # max % of balance risked per trade (size is capped, see below)
MAX_NOTIONAL_PCT = 50.0      # a position never exceeds this % of equity (x leverage)
DAILY_LOSS_PCT = 3.0         # stop trading for the day at this % loss
MAX_TRADES_PER_DAY = 4       # limits fee churn
FAST_EMA, SLOW_EMA = 9, 21   # placeholder strategy (NOT proven to make money)
TREND_EMA = 100              # only BUY above it, only SELL below it
ATR_PERIOD = 14
SL_ATR_MULT = 2.0            # stop-loss distance = 2 x ATR
RR = 2.0                     # take-profit = 2 x stop distance
TAKER_FEE = 0.00055          # Bybit standard taker fee 0.055% (check YOUR tier in Bybit)
MIN_TP_TO_FEE = 3.0          # take-profit distance must be >= 3 x round-trip fees
CLOSE_ON_OPPOSITE = False    # False = let SL/TP decide (saves fees)
CLOSE_UNPROTECTED = True     # close any position that has no stop-loss
POLL_SECONDS = 15
CANDLES_FETCHED = 300
RSI_PERIOD = 14
RSI_OB = 70                        # don't buy when overbought
RSI_OS = 30                        # don't sell when oversold
# -----------------------------------------------------------------------

ROUND_TRIP_FEE = 2 * TAKER_FEE

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "bot_state.json")
KILL_FILE = os.path.join(HERE, "KILL_SWITCH.txt")
TRADE_LOG = os.path.join(HERE, "trades_log.csv")
EQUITY_LOG = os.path.join(HERE, "equity_log.csv")
RESULTS_LOG = os.path.join(HERE, "results_log.csv")

TG_TOKEN = getattr(S, "TELEGRAM_BOT_TOKEN", None) or getattr(S, "TELEGRAM_TOKEN", None)
TG_CHAT = getattr(S, "TELEGRAM_CHAT_ID", None) or getattr(S, "CHAT_ID", None)


# ----------------------------- helpers ---------------------------------
def now():
    return datetime.now(timezone.utc)


def say(msg):
    print(f"[{now().strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}", flush=True)


def alert(msg):
    say(msg)
    if TG_TOKEN and TG_CHAT:
        try:
            requests.post(
                f"https://api.telegram.org/bot{str(TG_TOKEN).strip()}/sendMessage",
                data={"chat_id": str(TG_CHAT).strip(), "text": "[BYBIT DEMO] " + msg},
                timeout=10,
            )
        except requests.RequestException as e:
            print("Telegram failed:", type(e).__name__)


def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def nap(seconds):
    """Sleep; returns False if you pressed Ctrl+C (so the bot can stop cleanly)."""
    try:
        time.sleep(seconds)
        return True
    except KeyboardInterrupt:
        alert("Bot stopped by you (Ctrl+C). Open positions keep their SL/TP on the exchange.")
        return False


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Could not read bot state from {STATE_FILE}: {e}") from e
    return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


# ----------------------------- exchange --------------------------------
def make_exchange():
    ex = ccxt.bybit({
        "apiKey": str(S.BYBIT_API_KEY).strip(),
        "secret": str(S.BYBIT_API_SECRET).strip(),
        "enableRateLimit": True,
        "timeout": 30000,                       # 30 s (default ~10 s caused RequestTimeout)
        "options": {"defaultType": "swap"},
    })
    enable_demo_trading = getattr(ex, "enable_demo_trading", None)
    if enable_demo_trading is None:
        # Older ccxt releases may not expose the demo-mode helper.
        ex.urls["api"] = dict.fromkeys(ex.urls["api"], "https://api-demo.bybit.com")
    else:
        try:
            enable_demo_trading(True)
        except ccxt.NotSupported:
            # Older exchange implementations may not support the demo-mode helper.
            ex.urls["api"] = dict.fromkeys(ex.urls["api"], "https://api-demo.bybit.com")
    # Safety lock: confirm we are on the demo host
    api_urls = json.dumps(ex.urls.get("api", {}))
    if "demo" not in api_urls:
        raise SystemExit("SAFETY STOP: demo mode is not active. Refusing to run.")
    ex.load_markets()
    try:
        ex.set_leverage(LEVERAGE, SYMBOL)
    except ccxt.BaseError as e:
        # Bybit returns an error if leverage is already set to this value
        if "110043" not in str(e) and "not modified" not in str(e).lower():
            say(f"Leverage note: {str(e)[:120]}")
    return ex


def equity(ex):
    bal = ex.fetch_balance()
    eq = float(bal["total"].get("USDT", 0) or 0)
    if eq <= 0:
        raise RuntimeError("Equity is zero or negative — API may have returned bad data")
    return eq


def raw_position(ex):
    """The open position as ccxt returns it, or None."""
    for p in ex.fetch_positions([SYMBOL]):
        if float(p.get("contracts") or 0) > 0:
            return p
    return None


def get_position(ex):
    """Return (side, contracts, entry) or None."""
    p = raw_position(ex)
    if not p:
        return None
    return p["side"], float(p.get("contracts") or 0), float(p.get("entryPrice") or 0)


def position_has_stop(p):
    """True if Bybit shows a stop-loss on this position."""
    try:
        return float((p.get("info") or {}).get("stopLoss") or 0) > 0
    except (TypeError, ValueError):
        return False


def last_price(ex):
    return float(ex.fetch_ticker(SYMBOL)["last"])


# ----------------------------- signals ---------------------------------
def ema(values, n):
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(candles, n):
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


def rsi_calc(values, n):
    """Wilder's RSI."""
    deltas = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_loss == 0:
        return 100
    return 100 - 100 / (1 + avg_gain / avg_loss)


def get_signal(ex):
    """Returns (signal, closed_candle_time, atr_value, cross).
    signal = 'BUY' / 'SELL' / None (after filters).
    cross  = the raw EMA crossover before filters.
    Uses CLOSED candles only (drops the still-forming last candle)."""
    raw = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=CANDLES_FETCHED)
    closed = raw[:-1]
    closes = [c[4] for c in closed]
    f, s = ema(closes, FAST_EMA), ema(closes, SLOW_EMA)
    trend = ema(closes, TREND_EMA)[-1]
    rsi_val = rsi_calc(closes, RSI_PERIOD)
    cross = None
    if f[-2] <= s[-2] and f[-1] > s[-1]:
        cross = "BUY"
    elif f[-2] >= s[-2] and f[-1] < s[-1]:
        cross = "SELL"
    sig = cross
    if cross == "BUY" and (closes[-1] < trend or rsi_val >= RSI_OB):
        sig = None
    if cross == "SELL" and (closes[-1] > trend or rsi_val <= RSI_OS):
        sig = None
    return sig, closed[-1][0], atr(closed, ATR_PERIOD), cross


# ----------------------------- orders ----------------------------------
def plan_levels(side, entry, atr_val):
    """Stop-loss, take-profit, stop distance, and take-profit distance as a fraction of price."""
    stop_dist = atr_val * SL_ATR_MULT
    if side == "buy":
        sl, tp = entry - stop_dist, entry + stop_dist * RR
    else:
        sl, tp = entry + stop_dist, entry - stop_dist * RR
    tp_frac = abs(tp - entry) / entry
    return sl, tp, stop_dist, tp_frac


def fee_check(tp_frac):
    """True if the take-profit distance clearly covers the round-trip fees."""
    return tp_frac >= MIN_TP_TO_FEE * ROUND_TRIP_FEE


def calc_qty(ex, bal, entry, stop_dist):
    risk_amount = bal * RISK_PCT / 100
    qty = risk_amount / stop_dist
    max_qty = (bal * (MAX_NOTIONAL_PCT / 100) * LEVERAGE) / entry   # size cap
    qty = min(qty, max_qty)
    qty = float(ex.amount_to_precision(SYMBOL, qty))
    min_qty = ex.market(SYMBOL)["limits"]["amount"]["min"] or 0
    return qty, min_qty


def verify_protected(ex):
    """After an order: confirm a position exists AND has a stop-loss. If it has none, close it."""
    raw = None
    for _ in range(4):
        time.sleep(2)
        raw = raw_position(ex)
        if raw and position_has_stop(raw):
            return True
    if raw:
        alert("DANGER: the position has NO stop-loss. Closing it now.")
        close_position(ex, "no stop-loss")
    else:
        say("No open position found after the order (it may not have filled).")
    return False


def open_trade(ex, side, atr_val, qty_override=None, note="auto", check_fees=True):
    """side: 'buy' or 'sell'. Market order with SL + TP attached."""
    entry = last_price(ex)
    sl, tp, stop_dist, tp_frac = plan_levels(side, entry, atr_val)
    sl = float(ex.price_to_precision(SYMBOL, sl))
    tp = float(ex.price_to_precision(SYMBOL, tp))

    if check_fees and not fee_check(tp_frac):
        say(f"Trade skipped: target is only {tp_frac*100:.2f}% away but fees cost "
            f"{ROUND_TRIP_FEE*100:.2f}% (need at least {MIN_TP_TO_FEE*ROUND_TRIP_FEE*100:.2f}%).")
        return None

    bal = equity(ex)
    qty, min_qty = calc_qty(ex, bal, entry, stop_dist)
    if qty_override:
        qty = qty_override
    if qty < min_qty:
        alert(f"Trade skipped: size {qty} is below exchange minimum {min_qty}.")
        return None

    risk_pct = qty * stop_dist / bal * 100
    est_fee = qty * entry * ROUND_TRIP_FEE

    order_id = None
    try:
        order = ex.create_order(
            SYMBOL, "market", side, qty, None,
            {"stopLoss": {"triggerPrice": sl}, "takeProfit": {"triggerPrice": tp}},
        )
        order_id = order.get("id")
    except ccxt.BaseError as e:
        alert(f"Order error: {type(e).__name__}. Checking whether a position opened...")

    if not verify_protected(ex):
        return None

    append_csv(TRADE_LOG,
               ["time_utc", "action", "side", "qty", "entry_approx", "sl", "tp", "order_id", "note"],
               [now().isoformat(), "OPEN", side, qty, entry, sl, tp, order_id,
                f"{note} | real risk {risk_pct:.2f}% | est fees {est_fee:.2f}"])
    st = load_state()
    st["trades_today"] = st.get("trades_today", 0) + 1
    st["in_trade"] = True
    st["open_equity"] = bal
    st["open_ts"] = time.time()
    save_state(st)
    alert(f"OPENED {side.upper()} {qty} {SYMBOL} @ ~{entry} | SL {sl} | TP {tp} | "
          f"real risk {risk_pct:.2f}% of equity | est. round-trip fees {est_fee:.2f} USDT")
    return order_id


def close_position(ex, reason="manual"):
    pos = get_position(ex)
    if not pos:
        say("No open position to close.")
        return
    side, qty, _ = pos
    opp = "sell" if side == "long" else "buy"
    ex.create_order(SYMBOL, "market", opp, qty, None, {"reduceOnly": True})
    append_csv(TRADE_LOG,
               ["time_utc", "action", "side", "qty", "entry_approx", "sl", "tp", "order_id", "note"],
               [now().isoformat(), "CLOSE", opp, qty, last_price(ex), "", "", "", reason])
    alert(f"CLOSED {side} {qty} {SYMBOL} ({reason})")


def kill_switch(ex, reason="kill switch"):
    """Cancel orders, close position, create KILL file so auto mode stops."""
    with open(KILL_FILE, "w") as f:
        f.write(f"Kill switch activated {now().isoformat()} - {reason}\n")
    try:
        ex.cancel_all_orders(SYMBOL)
    except ccxt.BaseError as e:
        say(f"Cancel orders note: {str(e)[:100]}")
    close_position(ex, reason)
    alert(f"KILL SWITCH: {reason}. Delete KILL_SWITCH.txt to allow trading again.")


def track_closed(ex, pos_present):
    """When a trade has ended (stop-loss, take-profit or manual), log its NET result after fees.
    Net = equity now - equity just before the trade opened, so fees and funding are included."""
    st = load_state()
    if not st.get("in_trade") or pos_present:
        return
    if time.time() - st.get("open_ts", 0) < 20:      # give the exchange time to show the new position
        return
    eq = equity(ex)
    net = eq - st.get("open_equity", eq)
    append_csv(RESULTS_LOG, ["time_utc", "net_pnl_usdt_after_fees", "equity_after"],
               [now().isoformat(), round(net, 4), round(eq, 4)])
    st["in_trade"] = False
    save_state(st)
    alert(f"TRADE ENDED | net result after fees: {net:+.2f} USDT | equity {eq:.2f} USDT")


# ----------------------------- risk ------------------------------------
def check_daily_limit(ex):
    """Returns True if trading is allowed."""
    st = load_state()
    today = now().strftime("%Y-%m-%d")
    eq = equity(ex)
    if st.get("day") != today:
        keep = {k: st[k] for k in ("in_trade", "open_equity", "open_ts") if k in st}
        st = {"day": today, "start_equity": eq, "trades_today": 0, "halted": False, **keep}
        save_state(st)
        say(f"New day. Starting equity {eq:.2f} USDT")
    loss_pct = (st["start_equity"] - eq) / st["start_equity"] * 100
    if st.get("halted"):
        return False
    if loss_pct >= DAILY_LOSS_PCT:
        st["halted"] = True
        save_state(st)
        close_position(ex, "daily loss limit")
        alert(f"DAILY LOSS LIMIT hit ({loss_pct:.2f}%). No more trades until tomorrow (UTC).")
        return False
    return st.get("trades_today", 0) < MAX_TRADES_PER_DAY


# ----------------------------- run loops -------------------------------
def _enforce_protection(ex, raw):
    """Close any unprotected position. Returns the (possibly updated) raw position."""
    if (CLOSE_UNPROTECTED and raw and not position_has_stop(raw)
            and time.time() - load_state().get("open_ts", 0) > 60):
        alert("Found a position WITHOUT a stop-loss. Closing it.")
        close_position(ex, "no stop-loss")
        return None
    return raw


def _log_candle(ts, sig, cross):
    """Print what happened on the last closed candle."""
    if sig:
        say(f"Candle {ts} closed | signal: {sig}")
    elif cross:
        say(f"Candle {ts} closed | {cross} cross ignored (against the {TREND_EMA}-EMA trend)")
    else:
        say(f"Candle {ts} closed | signal: none")


def _act_on_signal(ex, sig, atr_val, place_orders, allowed):
    """Decide whether to open a trade based on the signal, mode, and risk limits."""
    entry = last_price(ex)
    _, _, _, tp_frac = plan_levels(sig.lower(), entry, atr_val)
    covers = fee_check(tp_frac)
    if not place_orders:
        say(f"WOULD {sig} here (log-only). Target {tp_frac*100:.2f}% away | "
            f"fee filter: {'PASS' if covers else 'SKIP - target too small for the fees'}")
        return
    if not allowed:
        return
    pos = get_position(ex)
    want = "long" if sig == "BUY" else "short"
    if pos and pos[0] != want:
        if CLOSE_ON_OPPOSITE:
            close_position(ex, "opposite signal")
            pos = None
        else:
            say("Opposite signal ignored: a trade with SL/TP is still open.")
    if not pos:
        open_trade(ex, sig.lower(), atr_val)


def run(ex, place_orders):
    mode = "AUTO (demo orders)" if place_orders else "LOG-ONLY (no orders)"
    alert(f"Bot started: {mode} | {SYMBOL} {TIMEFRAME} | max risk {RISK_PCT}% | "
          f"daily limit {DAILY_LOSS_PCT}% | min target {MIN_TP_TO_FEE*ROUND_TRIP_FEE*100:.2f}% (fee filter)")
    last_seen = None
    errors = 0
    while True:
        try:
            if os.path.exists(KILL_FILE):
                alert("KILL_SWITCH.txt found. Bot stopped.")
                return
            allowed = check_daily_limit(ex) if place_orders else True
            raw = raw_position(ex)

            if place_orders:
                track_closed(ex, raw is not None)
                raw = _enforce_protection(ex, raw)

            sig, ctime, atr_val, cross = get_signal(ex)

            if ctime != last_seen:
                last_seen = ctime
                ts = datetime.fromtimestamp(ctime / 1000, timezone.utc).strftime("%H:%M")
                append_csv(EQUITY_LOG, ["time_utc", "equity_usdt"], [now().isoformat(), equity(ex)])
                _log_candle(ts, sig, cross)
                if sig:
                    _act_on_signal(ex, sig, atr_val, place_orders, allowed)
            errors = 0
        except KeyboardInterrupt:
            alert("Bot stopped by you (Ctrl+C). Open positions keep their SL/TP on the exchange.")
            return
        except (
            ccxt.BaseError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            ArithmeticError,
        ) as e:
            errors += 1
            say(f"Error ({errors}): {type(e).__name__}: {str(e)[:150]}")
            if errors >= 5:
                alert("5 errors in a row. Check the bot / internet.")
                errors = 0
            if not nap(30):
                return
        if not nap(POLL_SECONDS):
            return


# ----------------------------- menu ------------------------------------
def status(ex):
    st = load_state()
    track_closed(ex, get_position(ex) is not None)
    print(f"\nLast price : {last_price(ex)}")
    print(f"Equity     : {equity(ex):.2f} USDT")
    print(f"Position   : {get_position(ex) or 'none'}")
    print(f"Today      : start {st.get('start_equity')} | trades {st.get('trades_today', 0)} | halted {st.get('halted', False)}")
    print(f"Kill file  : {'PRESENT (trading blocked)' if os.path.exists(KILL_FILE) else 'not present'}")
    print(f"Fee filter : target must be >= {MIN_TP_TO_FEE*ROUND_TRIP_FEE*100:.2f}% away (round-trip fees {ROUND_TRIP_FEE*100:.2f}%)")


def menu(ex):
    while True:
        print("""
=== BYBIT DEMO BOT ===
1) Status
2) Test BUY  (minimum size, with SL/TP)
3) Test SELL (minimum size, with SL/TP)
4) Close position
5) Show current signal (no orders)
6) Run LOG-ONLY mode
7) Run AUTO mode (demo orders)
8) KILL SWITCH
9) Remove kill switch file
0) Exit""")
        c = input("Choose: ").strip()
        try:
            if c == "1":
                status(ex)
            elif c in ("2", "3"):
                min_q = ex.market(SYMBOL)["limits"]["amount"]["min"]
                _, _, a, _ = get_signal(ex)
                open_trade(ex, "buy" if c == "2" else "sell", a, qty_override=min_q,
                           note="menu test", check_fees=False)
            elif c == "4":
                close_position(ex, "menu")
            elif c == "5":
                sig, _, a, cross = get_signal(ex)
                entry = last_price(ex)
                side = (sig or cross or "buy").lower()
                _, _, _, tp_frac = plan_levels(side, entry, a)
                print("Signal:", sig or "none", "| raw cross:", cross or "none", "| ATR:", round(a, 2))
                print(f"Target would be {tp_frac*100:.2f}% away -> fee filter: "
                      f"{'PASS' if fee_check(tp_frac) else 'SKIP (too small for the fees)'}")
            elif c == "6":
                run(ex, False)
            elif c == "7":
                if input("Start AUTO on DEMO? type YES: ") == "YES":
                    run(ex, True)
            elif c == "8":
                kill_switch(ex, "manual from menu")
            elif c == "9":
                if os.path.exists(KILL_FILE):
                    os.remove(KILL_FILE)
                    say("Kill switch file removed.")
            elif c == "0":
                return
        except (
            ccxt.BaseError,
            requests.RequestException,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            ArithmeticError,
        ) as e:
            print("ERROR:", type(e).__name__, str(e)[:300])


if __name__ == "__main__":
    exchange = make_exchange()
    say("Connected to Bybit DEMO.")
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if arg == "log":
        run(exchange, False)
    elif arg == "auto":
        run(exchange, True)
    else:
        menu(exchange)
