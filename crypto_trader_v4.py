"""
crypto_trader_v4.py - Bybit DEMO multi-coin bot (perpetual futures, 1x leverage)

SAFETY: hard-locked to Bybit DEMO. It refuses to start if demo mode is not active.

Usage:
    python crypto_trader_v4.py            -> test menu
    python crypto_trader_v4.py log        -> signals only, places NO orders
    python crypto_trader_v4.py auto       -> auto trading on DEMO

Needs in bot_settings.py:  BYBIT_API_KEY, BYBIT_API_SECRET
Optional (Telegram):       TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
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
VERSION = "v4"
WATCHLIST = [                      # liquid coins only; edit with care
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
    "BNB/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT",
    "LINK/USDT:USDT", "LTC/USDT:USDT",
]
TIMEFRAME = "15m"
LEVERAGE = 1                       # keep at 1 for now
RISK_PCT = 1.0                     # % of balance risked per trade (position cap may make it lower)
DAILY_LOSS_PCT = 3.0               # whole-account loss limit per UTC day
MAX_TRADES_PER_DAY = 8             # across all coins
MAX_OPEN_POSITIONS = 3             # each position gets at most 1/3 of the balance
MAX_PER_DIRECTION = 2              # crypto moves together; avoid 3 longs at once
MIN_STOP_PCT = 0.25               # skip if the stop would be tighter than this % of price
FEE_RATE_PCT = 0.055               # Bybit market-order fee per side (CHECK your account)
FEE_MULT = 4                       # take-profit must be >= 4x the round-trip fee
EXIT_ON_OPPOSITE = False           # False = let SL/TP end trades (fewer fees)
FAST_EMA, SLOW_EMA = 9, 21
TREND_EMA = 100                    # major trend filter: only BUY above, only SELL below
ATR_PERIOD = 14
SL_ATR_MULT = 2.0                  # 1.5x was too tight for 15m crypto — constant stop-outs
RR = 2.5                           # with wider stop, need wider target to stay profitable
RSI_PERIOD = 14
RSI_OB = 70                        # overbought — don't buy
RSI_OS = 30                        # oversold — don't sell
MIN_ADX = 20                       # only trade when the market is actually trending
POLL_SECONDS = 15
CANDLE_DELAY = 5                   # seconds to wait after a candle closes
# -----------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "bot_state.json")
KILL_FILE = os.path.join(HERE, "KILL_SWITCH.txt")
TRADE_LOG = os.path.join(HERE, "trades_log_v4.csv")
EQUITY_LOG = os.path.join(HERE, "equity_log_v4.csv")
TRADE_HEADER = ["time_utc", "action", "symbol", "side", "qty", "entry_approx",
                "sl", "tp", "order_id", "note"]

TG_TOKEN = getattr(S, "TELEGRAM_BOT_TOKEN", None)
TG_CHAT = getattr(S, "TELEGRAM_CHAT_ID", None)

SYMBOLS = []          # filled at startup with the watchlist coins Bybit really has
TG_OFFSET = None
PAUSED = False
BOT_CLOSED = set()    # symbols the bot itself just closed


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
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data={"chat_id": TG_CHAT, "text": "[BYBIT DEMO] " + msg},
                timeout=10,
            )
        except Exception as e:
            print("Telegram failed:", type(e).__name__)


def short(sym):
    return sym.split("/")[0]


def fmt_num(x, d=2):
    try:
        return f"{float(x):,.{d}f}"
    except Exception:
        return "n/a"


def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def log_trade(action, symbol, side="", qty="", entry="", sl="", tp="", oid="", note=""):
    append_csv(TRADE_LOG, TRADE_HEADER,
               [now().isoformat(), action, symbol, side, qty, entry, sl, tp, oid, note])


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def resolve_coin(arg):
    """'eth' -> 'ETH/USDT:USDT' if it is on the watchlist, else None."""
    sym = f"{arg.upper()}/USDT:USDT"
    return sym if sym in SYMBOLS else None


# ----------------------------- exchange --------------------------------
def make_exchange():
    global SYMBOLS
    ex = ccxt.bybit({
        "apiKey": str(S.BYBIT_API_KEY).strip(),
        "apiSecret": str(S.BYBIT_API_SECRET).strip(),
        "secret": str(S.BYBIT_API_SECRET).strip(),
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "swap"},
    })
    try:
        ex.enable_demo_trading(True)
    except Exception:
        ex.urls["api"] = {k: "https://api-demo.bybit.com" for k in ex.urls["api"]}
    if "demo" not in json.dumps(ex.urls.get("api", {})):
        raise SystemExit("SAFETY STOP: demo mode is not active. Refusing to run.")
    ex.load_markets()

    SYMBOLS = [s for s in WATCHLIST if s in ex.markets]
    missing = [s for s in WATCHLIST if s not in ex.markets]
    if missing:
        say("Not found on Bybit, ignored: " + ", ".join(short(m) for m in missing))
    if not SYMBOLS:
        raise SystemExit("No watchlist coins found on Bybit.")

    for s in SYMBOLS:
        try:
            ex.set_leverage(LEVERAGE, s)
        except Exception as e:
            if "110043" not in str(e) and "not modified" not in str(e).lower():
                say(f"Leverage note {short(s)}: {str(e)[:100]}")
    return ex


def equity(ex):
    bal = ex.fetch_balance()
    eq = float(bal["total"].get("USDT", 0) or 0)
    if eq <= 0:
        raise RuntimeError("Equity is zero or negative — API may have returned bad data")
    return eq


def get_positions(ex):
    """Returns {symbol: {side, qty, entry, mark, upnl, info}} for watchlist coins."""
    try:
        plist = ex.fetch_positions(SYMBOLS)
    except Exception:
        plist = []
        for s in SYMBOLS:
            plist += ex.fetch_positions([s])
    out = {}
    for p in plist:
        sym = p.get("symbol")
        c = float(p.get("contracts") or 0)
        if sym in SYMBOLS and c > 0:
            out[sym] = {
                "side": p["side"], "qty": c,
                "entry": float(p.get("entryPrice") or 0),
                "mark": p.get("markPrice"), "upnl": p.get("unrealizedPnl"),
                "info": p.get("info", {}),
            }
    return out


def last_price(ex, symbol):
    return float(ex.fetch_ticker(symbol)["last"])


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


def rsi(values, n):
    """Wilder's RSI."""
    deltas = [values[i] - values[i-1] for i in range(1, len(values))]
    gains, losses = [], []
    for d in deltas:
        gains.append(d if d > 0 else 0)
        losses.append(-d if d < 0 else 0)
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_loss == 0:
        return 100
    return 100 - 100 / (1 + avg_gain / avg_loss)


def adx(candles, n=14):
    """Average Directional Index — measures trend strength, not direction."""
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i-1][4]
        ph, pl = candles[i-1][2], candles[i-1][3]
        up = h - ph
        down = pl - l
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder smoothing
    k = 2 / (n + 1)
    def smooth(vals):
        out = [sum(vals[:n]) / n]
        for v in vals[n:]:
            out.append(out[-1] * (1 - k) + v * k)
        return out
    s_tr = smooth(trs)
    s_pdm = smooth(plus_dm)
    s_mdm = smooth(minus_dm)
    dx_list = []
    for i in range(len(s_tr)):
        if s_tr[i] == 0:
            dx_list.append(0)
            continue
        pdi = s_pdm[i] / s_tr[i] * 100
        mdi = s_mdm[i] / s_tr[i] * 100
        if pdi + mdi == 0:
            dx_list.append(0)
        else:
            dx_list.append(abs(pdi - mdi) / (pdi + mdi) * 100)
    if not dx_list:
        return 0
    adx_val = sum(dx_list[-n:]) / min(n, len(dx_list))
    return adx_val


def get_signal(ex, symbol):
    """Returns ('BUY'|'SELL'|None, closed_candle_open_time, atr_value). CLOSED candles only.
    Filters: EMA crossover + trend EMA + RSI extremes + ADX trend strength."""
    raw = ex.fetch_ohlcv(symbol, TIMEFRAME, limit=200)
    closed = raw[:-1]
    closes = [c[4] for c in closed]
    f, s = ema(closes, FAST_EMA), ema(closes, SLOW_EMA)
    trend = ema(closes, TREND_EMA)[-1]
    rsi_val = rsi(closes, RSI_PERIOD)
    adx_val = adx(closed, ATR_PERIOD)
    a = atr(closed, ATR_PERIOD)

    sig = None
    if f[-2] <= s[-2] and f[-1] > s[-1]:  # bullish cross
        if closes[-1] > trend and rsi_val < RSI_OB and adx_val >= MIN_ADX:
            sig = "BUY"
    elif f[-2] >= s[-2] and f[-1] < s[-1]:  # bearish cross
        if closes[-1] < trend and rsi_val > RSI_OS and adx_val >= MIN_ADX:
            sig = "SELL"
    return sig, closed[-1][0], a


# ----------------------------- orders ----------------------------------
def calc_qty(ex, symbol, bal, entry, stop_dist):
    m = ex.market(symbol)
    cs = m.get("contractSize") or 1
    risk_amount = bal * RISK_PCT / 100
    qty = risk_amount / (stop_dist * cs)
    cap_notional = bal * 0.95 * LEVERAGE / MAX_OPEN_POSITIONS   # per-position cap
    qty = min(qty, cap_notional / (entry * cs))
    qty = float(ex.amount_to_precision(symbol, qty))
    min_qty = m["limits"]["amount"]["min"] or 0
    return qty, min_qty, cs


def open_trade(ex, symbol, side, atr_val, qty_override=None, note="auto"):
    """side: 'buy' or 'sell'. Market order with SL + TP attached.
    Adds a slippage buffer to the stop-loss so the real stop is never tighter than intended."""
    entry = last_price(ex, symbol)
    slippage_buffer = entry * 0.001  # 0.1% slippage allowance
    stop_dist = atr_val * SL_ATR_MULT + slippage_buffer

    if note != "menu test":        # manual menu tests bypass the filters
        stop_pct = stop_dist / entry * 100
        tp_pct = stop_pct * RR
        round_trip_fee = 2 * FEE_RATE_PCT
        skip = None
        if stop_pct < MIN_STOP_PCT:
            skip = f"too quiet (stop {stop_pct:.2f}% < min {MIN_STOP_PCT}%)"
        elif tp_pct < FEE_MULT * round_trip_fee:
            skip = f"target {tp_pct:.2f}% too small vs fees {round_trip_fee:.2f}%"
        if skip:
            log_trade("SKIP", symbol, side, entry=entry, note=skip)
            say(f"SKIPPED {side.upper()} {short(symbol)}: {skip}")
            return None

    if side == "buy":
        sl, tp = entry - stop_dist, entry + stop_dist * RR
    else:
        sl, tp = entry + stop_dist, entry - stop_dist * RR
    sl = float(ex.price_to_precision(symbol, sl))
    tp = float(ex.price_to_precision(symbol, tp))

    bal = equity(ex)
    qty, min_qty, cs = calc_qty(ex, symbol, bal, entry, stop_dist)
    if qty_override:
        qty = qty_override
    if qty < min_qty:
        say(f"Trade skipped {short(symbol)}: size {qty} below exchange minimum {min_qty}.")
        log_trade("SKIP", symbol, side, qty, entry, note=f"size below minimum {min_qty}")
        return None

    order = ex.create_order(
        symbol, "market", side, qty, None,
        {"stopLoss": {"triggerPrice": sl}, "takeProfit": {"triggerPrice": tp}},
    )
    log_trade("OPEN", symbol, side, qty, entry, sl, tp, order.get("id"), note)
    st = load_state()
    st["trades_today"] = st.get("trades_today", 0) + 1
    save_state(st)
    risk_usdt = qty * cs * stop_dist
    alert(f"OPENED {side.upper()} {qty} {short(symbol)} @ ~{entry} | SL {sl} | TP {tp} "
          f"| risk if stopped ~{risk_usdt:.0f} USDT (+ fees)")
    return order


def close_position(ex, symbol, reason="manual"):
    pos = get_positions(ex).get(symbol)
    if not pos:
        say(f"No open {short(symbol)} position to close.")
        return
    BOT_CLOSED.add(symbol)
    opp = "sell" if pos["side"] == "long" else "buy"
    ex.create_order(symbol, "market", opp, pos["qty"], None, {"reduceOnly": True})
    log_trade("CLOSE", symbol, opp, pos["qty"], last_price(ex, symbol), note=reason)
    alert(f"CLOSED {pos['side']} {pos['qty']} {short(symbol)} ({reason})")


def close_all(ex, reason="manual"):
    for sym in list(get_positions(ex).keys()):
        try:
            close_position(ex, sym, reason)
        except Exception as e:
            alert(f"FAILED to close {short(sym)}: {type(e).__name__} {str(e)[:100]}")


def kill_switch(ex, reason="kill switch"):
    with open(KILL_FILE, "w") as f:
        f.write(f"Kill switch activated {now().isoformat()} - {reason}\n")
    for s in SYMBOLS:
        try:
            ex.cancel_all_orders(s)
        except Exception:
            pass
    close_all(ex, reason)
    alert(f"KILL SWITCH: {reason}. Delete KILL_SWITCH.txt to allow trading again.")


# ----------------------------- risk ------------------------------------
def check_daily_limit(ex):
    """Returns True if new trades are allowed (whole-account limits)."""
    st = load_state()
    today = now().strftime("%Y-%m-%d")
    eq = equity(ex)
    if st.get("day") != today:
        st = {"day": today, "start_equity": eq, "trades_today": 0, "halted": False}
        save_state(st)
        say(f"New day. Starting equity {eq:.2f} USDT")
    loss_pct = (st["start_equity"] - eq) / st["start_equity"] * 100
    if st.get("halted"):
        return False
    if loss_pct >= DAILY_LOSS_PCT:
        st["halted"] = True
        save_state(st)
        close_all(ex, "daily loss limit")
        alert(f"DAILY LOSS LIMIT hit ({loss_pct:.2f}%). All positions closed. No trades until tomorrow (UTC).")
        return False
    return st.get("trades_today", 0) < MAX_TRADES_PER_DAY


# ------------------------ telegram commands ----------------------------
HELP_TEXT = """Commands (coin = btc, eth, sol, xrp, bnb, doge, ada, avax, link, ltc):
/price [coin] - price, 24h change, high/low (default BTC)
/prices - all watchlist coins at a glance
/status - balance, today's result, positions, bot state
/positions - open positions with profit/loss, SL/TP
/trades - last 8 log entries (trades and skips)
/signal [coin] - current signal (no orders)
/pause - stop opening NEW trades
/resume - allow new trades again
/close coin|all - close one or all positions
/kill YES - close everything and stop the bot
/help - this list"""


def command_reply(ex, text):
    global PAUSED
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        return HELP_TEXT

    if cmd == "/price":
        sym = resolve_coin(arg) if arg else "BTC/USDT:USDT"
        if not sym:
            return "Coin not on the watchlist. Send /help."
        t = ex.fetch_ticker(sym)
        return (f"{sym}\nLast: {fmt_num(t['last'], 4)}\n"
                f"Bid/Ask: {fmt_num(t['bid'], 4)} / {fmt_num(t['ask'], 4)}\n"
                f"24h change: {fmt_num(t.get('percentage'))}%\n"
                f"24h high/low: {fmt_num(t['high'], 4)} / {fmt_num(t['low'], 4)}")

    if cmd == "/prices":
        try:
            ticks = ex.fetch_tickers(SYMBOLS)
        except Exception:
            ticks = {}
            for s in SYMBOLS:
                try:
                    ticks[s] = ex.fetch_ticker(s)
                except Exception:
                    pass
        lines = []
        for s in SYMBOLS:
            t = ticks.get(s)
            lines.append(f"{short(s)}: {fmt_num(t['last'], 4)} ({fmt_num(t.get('percentage'))}% 24h)"
                         if t else f"{short(s)}: unavailable")
        return "\n".join(lines)

    if cmd in ("/positions", "/position"):
        pos = get_positions(ex)
        if not pos:
            return "No open positions."
        lines = []
        for s, p in pos.items():
            i = p["info"]
            lines.append(f"{short(s)} {p['side'].upper()} {p['qty']} | entry {fmt_num(p['entry'], 4)} "
                         f"| P&L {fmt_num(p['upnl'])} USDT | SL {i.get('stopLoss') or '-'} TP {i.get('takeProfit') or '-'}")
        return "\n".join(lines)

    if cmd == "/status":
        st = load_state()
        eq = equity(ex)
        start = st.get("start_equity") or eq
        pnl = eq - start
        pos = get_positions(ex)
        state = ("KILLED" if os.path.exists(KILL_FILE)
                 else "HALTED (daily limit)" if st.get("halted")
                 else "PAUSED" if PAUSED else "ACTIVE")
        plist = ", ".join(f"{short(s)} {p['side']}" for s, p in pos.items()) or "none"
        return (f"Bot {VERSION}: {state}\nEquity: {fmt_num(eq)} USDT\n"
                f"Today: {fmt_num(pnl)} USDT ({fmt_num(pnl / start * 100)}%)\n"
                f"Trades today: {st.get('trades_today', 0)}/{MAX_TRADES_PER_DAY}\n"
                f"Positions ({len(pos)}/{MAX_OPEN_POSITIONS}): {plist}")

    if cmd == "/trades":
        if not os.path.exists(TRADE_LOG):
            return "Nothing logged yet."
        with open(TRADE_LOG) as f:
            rows = list(csv.reader(f))[1:][-8:]
        out = []
        for r in rows:
            if r[1] == "SKIP":
                out.append(f"{r[0][5:16]} SKIP {short(r[2])} {r[3]}: {r[9]}")
            else:
                out.append(f"{r[0][5:16]} {r[1]} {short(r[2])} {r[3]} {r[4]} @ {fmt_num(r[5], 4)}")
        return "\n".join(out) or "Nothing logged yet."

    if cmd == "/signal":
        syms = [resolve_coin(arg)] if arg else SYMBOLS
        if None in syms:
            return "Coin not on the watchlist. Send /help."
        lines = []
        for s in syms:
            try:
                sig, _, a = get_signal(ex, s)
                lines.append(f"{short(s)}: {sig or 'none'}")
            except Exception:
                lines.append(f"{short(s)}: error")
        return "\n".join(lines)

    if cmd == "/pause":
        PAUSED = True
        return "Paused: no new trades. Open positions keep their SL/TP. /resume to continue."

    if cmd == "/resume":
        PAUSED = False
        return "Resumed: new trades allowed."

    if cmd == "/close":
        if not arg:
            return "Send /close coin (e.g. /close eth) or /close all"
        if arg.lower() == "all":
            close_all(ex, "Telegram /close all")
            return "Close-all sent."
        sym = resolve_coin(arg)
        if not sym:
            return "Coin not on the watchlist."
        close_position(ex, sym, "Telegram /close")
        return f"Close order sent for {short(sym)}."

    if cmd == "/kill":
        if arg.upper() == "YES":
            kill_switch(ex, "Telegram /kill")
            return "Kill switch done. Bot will stop. Delete KILL_SWITCH.txt on the PC to allow trading again."
        return "To confirm, send: /kill YES"

    return "Unknown command. Send /help."


def handle_commands(ex):
    """Poll Telegram for commands. Only your own chat ID is obeyed."""
    global TG_OFFSET
    if not (TG_TOKEN and TG_CHAT):
        return
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    if TG_OFFSET is None:           # first run: skip old backlog
        r = requests.get(f"{base}/getUpdates", params={"timeout": 0}, timeout=10).json()
        res = r.get("result", [])
        TG_OFFSET = res[-1]["update_id"] + 1 if res else 0
        return
    r = requests.get(f"{base}/getUpdates", params={"offset": TG_OFFSET, "timeout": 0}, timeout=10).json()
    for u in r.get("result", []):
        TG_OFFSET = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(TG_CHAT):
            continue                          # ignore strangers
        text = (m.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        try:
            reply = command_reply(ex, text)
        except Exception as e:
            reply = f"Error: {type(e).__name__}: {str(e)[:150]}"
        requests.post(f"{base}/sendMessage", data={"chat_id": TG_CHAT, "text": reply}, timeout=10)


# ----------------------------- run loop --------------------------------
def run(ex, place_orders):
    mode = "AUTO (demo orders)" if place_orders else "LOG-ONLY (no orders)"
    tf_ms = int(ex.parse_timeframe(TIMEFRAME) * 1000)
    boundary_now = int(time.time() * 1000) // tf_ms * tf_ms
    alert(f"Bot {VERSION} started: {mode} | {len(SYMBOLS)} coins | {TIMEFRAME} | "
          f"risk {RISK_PCT}% | daily limit {DAILY_LOSS_PCT}% | max {MAX_OPEN_POSITIONS} positions. "
          f"Send /help for commands.")
    nxt = datetime.fromtimestamp((boundary_now + tf_ms) / 1000, timezone.utc).strftime("%H:%M")
    say(f"Watching: {', '.join(short(s) for s in SYMBOLS)}. First check at the {nxt} UTC candle close.")

    done_boundary = boundary_now      # do not act on a stale candle at startup
    errors = 0
    prev = get_positions(ex)

    while True:
        try:
            try:
                handle_commands(ex)
            except Exception as e:
                say(f"Telegram command error: {type(e).__name__}")
            if os.path.exists(KILL_FILE):
                alert("KILL_SWITCH.txt found. Bot stopped.")
                return

            # detect positions that Bybit closed (stop-loss / take-profit)
            pos = get_positions(ex)
            for sym, p in prev.items():
                if sym not in pos and sym not in BOT_CLOSED:
                    eq_now = equity(ex)
                    st0 = load_state()
                    day_pnl = eq_now - (st0.get("start_equity") or eq_now)
                    alert(f"{short(sym)} position closed by Bybit (stop-loss or take-profit). "
                          f"Equity {eq_now:.2f} USDT | today {day_pnl:+.2f} USDT")
                    log_trade("CLOSE", sym, "", p["qty"], note="SL/TP on exchange")
            # Only clear bot-closed entries for symbols confirmed gone from the exchange
            BOT_CLOSED.difference_update(set(BOT_CLOSED) - set(pos.keys()))
            prev = pos

            allowed = (check_daily_limit(ex) if place_orders else True) and not PAUSED

            now_ms = int(time.time() * 1000)
            boundary = now_ms // tf_ms * tf_ms
            if boundary != done_boundary and now_ms - boundary >= CANDLE_DELAY * 1000:
                stale = now_ms - boundary > 60000
                sigs, ready = {}, True
                for sym in SYMBOLS:
                    try:
                        sig, ctime, a = get_signal(ex, sym)
                    except Exception as e:
                        say(f"{short(sym)} data error: {type(e).__name__}")
                        sigs[sym] = (None, None)
                        continue
                    if ctime != boundary - tf_ms:       # candle data not ready yet
                        if stale:
                            sigs[sym] = (None, None)
                            continue
                        ready = False
                        break
                    sigs[sym] = (sig, a)

                if ready:
                    done_boundary = boundary
                    ts = datetime.fromtimestamp((boundary - tf_ms) / 1000, timezone.utc).strftime("%H:%M")
                    append_csv(EQUITY_LOG, ["time_utc", "equity_usdt", "open_positions"],
                               [now().isoformat(), equity(ex), len(pos)])
                    say(f"Candle {ts} closed | " + " ".join(f"{short(s)}:{(v[0] or '-')}" for s, v in sigs.items()))

                    if not place_orders:
                        for s, (sig, a) in sigs.items():
                            if sig:
                                say(f"WOULD {sig} {short(s)} here (log-only).")
                    elif allowed:
                        pos = get_positions(ex)
                        for s, (sig, a) in sigs.items():
                            if not sig:
                                continue
                            want = "long" if sig == "BUY" else "short"
                            cur = pos.get(s)
                            if cur and cur["side"] != want and EXIT_ON_OPPOSITE:
                                close_position(ex, s, "opposite signal")
                                pos.pop(s, None)
                                cur = None
                            if cur:
                                continue
                            if load_state().get("trades_today", 0) >= MAX_TRADES_PER_DAY:
                                say("Max trades per day reached.")
                                break
                            if len(pos) >= MAX_OPEN_POSITIONS:
                                log_trade("SKIP", s, sig.lower(), note="max open positions")
                                say(f"SKIPPED {sig} {short(s)}: max open positions")
                                continue
                            if sum(1 for q in pos.values() if q["side"] == want) >= MAX_PER_DIRECTION:
                                log_trade("SKIP", s, sig.lower(), note="max positions in one direction")
                                say(f"SKIPPED {sig} {short(s)}: max {MAX_PER_DIRECTION} {want} positions")
                                continue
                            if open_trade(ex, s, sig.lower(), a):
                                pos[s] = {"side": want}
            errors = 0
        except KeyboardInterrupt:
            alert("Bot stopped by you (Ctrl+C). Open positions keep their SL/TP on the exchange.")
            return
        except Exception as e:
            errors += 1
            say(f"Error ({errors}): {type(e).__name__}: {str(e)[:150]}")
            if errors >= 5:
                alert("5 errors in a row. Check the bot / internet.")
                errors = 0
            time.sleep(30)
        time.sleep(POLL_SECONDS)


# ----------------------------- menu ------------------------------------
def status(ex):
    st = load_state()
    print(f"\nEquity     : {equity(ex):.2f} USDT")
    pos = get_positions(ex)
    if pos:
        for s, p in pos.items():
            print(f"Position   : {short(s)} {p['side']} {p['qty']} @ {p['entry']} | P&L {p['upnl']}")
    else:
        print("Position   : none")
    print(f"Today      : start {st.get('start_equity')} | trades {st.get('trades_today', 0)} | halted {st.get('halted', False)}")
    print(f"Kill file  : {'PRESENT (trading blocked)' if os.path.exists(KILL_FILE) else 'not present'}")
    print("Coins      : " + ", ".join(short(s) for s in SYMBOLS))


def ask_coin(default="BTC"):
    c = input(f"Coin [{default}]: ").strip() or default
    return resolve_coin(c)


def menu(ex):
    while True:
        print("""
=== BYBIT DEMO BOT v4 (multi-coin) ===
1) Status
2) Test BUY  (asks coin, minimum size, with SL/TP)
3) Test SELL (asks coin, minimum size, with SL/TP)
4) Close a position (or ALL)
5) Show signals for all coins (no orders)
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
                sym = ask_coin()
                if not sym:
                    print("Coin not on the watchlist.")
                    continue
                min_q = ex.market(sym)["limits"]["amount"]["min"]
                _, _, a = get_signal(ex, sym)
                open_trade(ex, sym, "buy" if c == "2" else "sell", a, qty_override=min_q, note="menu test")
            elif c == "4":
                t = input("Coin or ALL [ALL]: ").strip() or "ALL"
                if t.upper() == "ALL":
                    close_all(ex, "menu")
                else:
                    sym = resolve_coin(t)
                    if sym:
                        close_position(ex, sym, "menu")
                    else:
                        print("Coin not on the watchlist.")
            elif c == "5":
                for s in SYMBOLS:
                    sig, _, a = get_signal(ex, s)
                    print(f"{short(s):6} signal: {sig or 'none':5} | ATR {a:.4f}")
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
        except Exception as e:
            print("ERROR:", type(e).__name__, str(e)[:300])


if __name__ == "__main__":
    exchange = make_exchange()
    say(f"Connected to Bybit DEMO ({VERSION}).")
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if arg == "log":
        run(exchange, False)
    elif arg == "auto":
        run(exchange, True)
    else:
        menu(exchange)
