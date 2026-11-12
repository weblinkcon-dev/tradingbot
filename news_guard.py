"""
news_guard.py - decides whether your bot is ALLOWED to trade right now.

Four independent checks (switch each on/off below):
  1. SESSION   - only trade inside your chosen hours (default 08:00-18:00 Nigeria time, Mon-Fri)
  2. CALENDAR  - pause before and after high-impact economic events (free Forex Factory weekly feed)
  3. RSS       - pause when a fresh headline contains a risky keyword (your MT5_NEWS_RSS_URL feed)
  4. MARKET    - pause when the spread or candle size suddenly explodes (works even if the feeds fail)

Use inside your bot:
    import news_guard
    ok, reasons = news_guard.can_trade("XAUUSD")
    if not ok:
        print("Paused:", "; ".join(reasons))
        # do not arm / place new orders. Optionally: news_guard.cancel_pending("XAUUSD", MAGIC)

Test it on its own (places NO orders):   python news_guard.py
"""
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

# ============================ SETTINGS ============================
USE_SESSION = True
# News is context-only. It must never veto a valid strategy signal.
USE_CALENDAR = False
USE_RSS = False
USE_MARKET = True

# --- 1. session window, in UTC (Nigeria is UTC+1, so 07-17 UTC = 08:00-18:00 Nigeria time)
SESSIONS_UTC = [(7, 17)]

# --- 2. economic calendar
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_REFRESH_MIN = 60          # download at most once an hour (the feed blocks heavy users)
IMPACTS = {"High"}                 # add "Medium" to be stricter, "Holiday" for bank holidays
MINUTES_BEFORE = 30                # pause this long BEFORE an event
MINUTES_AFTER = 30                 # ...and this long AFTER it
STRICT_CALENDAR = False            # True = stop trading if the calendar cannot be loaded at all
SYMBOL_CURRENCIES = {              # which currencies' news matters for which symbol
    "XAUUSD": ["USD"],
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"],
}

# --- 3. headline keywords (matched as whole words, any capitalisation)
RSS_URLS = tuple(dict.fromkeys(
    url.strip()
    for url in (
        os.environ.get("MT5_NEWS_RSS_URL", "https://investinglive.com/feed/news/"),
        "https://www.fxstreet.com/rss/news",
    )
    if url.strip()
))
HEADLINE_WINDOW_MIN = 20           # only headlines newer than this count
KEYWORDS = [
    "FOMC", "Powell", "Fed", "rate decision", "rate cut", "rate hike", "CPI", "NFP", "payrolls",
    "jobless claims", "GDP", "intervention", "emergency", "tariff", "tariffs", "sanctions",
    "ceasefire", "attack", "default", "ECB", "Lagarde", "BOJ", "Ueda", "BOE", "Bailey",
]

# --- 4. market guards (need MT5 to be connected)
MAX_SPREAD_MULT = 2.5              # pause if spread > 2.5 x its recent median
VOL_SPIKE_MULT = 2.5               # pause if the last candle's range > 2.5 x the recent average range
# ===================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "calendar_cache.json")
WAT = timezone(timedelta(hours=1))          # Nigeria time
UA = {"User-Agent": "Mozilla/5.0"}
_last_fail = 0.0
_rss_cache = {"t": 0.0, "result": (False, "")}


def _now():
    return datetime.now(timezone.utc)


# ------------------------- 2. calendar -------------------------
def _read_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_calendar():
    """Raw event list from the weekly feed (cached). None if nothing usable is available."""
    global _last_fail
    cache = _read_cache()
    if cache and time.time() - cache.get("saved", 0) < CALENDAR_REFRESH_MIN * 60:
        return cache["events"]
    if time.time() - _last_fail > 900:             # do not hammer the feed after a failure
        try:
            r = requests.get(CALENDAR_URL, timeout=15, headers=UA)
            r.raise_for_status()
            events = r.json()
            if isinstance(events, list) and events:
                with open(CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"saved": time.time(), "events": events}, f)
                return events
        except Exception as e:
            _last_fail = time.time()
            print("news_guard: calendar download failed:", type(e).__name__)
    if cache and time.time() - cache.get("saved", 0) < 8 * 86400:
        return cache["events"]                     # an older copy is better than nothing
    return None


def parse_events(raw):
    out = []
    for e in raw or []:
        try:
            t = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            out.append({
                "time": t.astimezone(timezone.utc),
                "currency": str(e.get("country", "")).upper(),
                "title": e.get("title", ""),
                "impact": e.get("impact", ""),
            })
        except Exception:
            continue
    return sorted(out, key=lambda x: x["time"])


def currencies_for(symbol):
    s = symbol.upper()
    for key, cur in SYMBOL_CURRENCIES.items():
        if s.startswith(key):
            return cur
    return [s[:3], s[3:6]] if len(s) >= 6 else ["USD"]


def calendar_check(symbol, now=None):
    now = now or _now()
    raw = load_calendar()
    if raw is None:
        return (True, "economic calendar unavailable") if STRICT_CALENDAR else (False, "")
    cur = currencies_for(symbol)
    for ev in parse_events(raw):
        if ev["impact"] not in IMPACTS or ev["currency"] not in cur:
            continue
        start = ev["time"] - timedelta(minutes=MINUTES_BEFORE)
        end = ev["time"] + timedelta(minutes=MINUTES_AFTER)
        if start <= now <= end:
            when = ev["time"].astimezone(WAT).strftime("%a %H:%M")
            return True, f"{ev['currency']} {ev['title']} ({ev['impact']}) at {when} Nigeria time"
    return False, ""


# ------------------------- 3. headlines -------------------------
def rss_check(now=None):
    now = now or _now()
    if time.time() - _rss_cache["t"] < 120:          # look at the feed at most every 2 minutes
        return _rss_cache["result"]
    result = (False, "")
    last_error = None
    try:
        root = None
        for rss_url in RSS_URLS:
            try:
                r = requests.get(rss_url, timeout=15, headers=UA)
                r.raise_for_status()
                root = ET.fromstring(r.content)
                break
            except (requests.RequestException, ET.ParseError) as exc:
                last_error = exc
        if root is None:
            raise last_error or RuntimeError("no RSS feed configured")
        pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in KEYWORDS) + r")\b", re.IGNORECASE)
        cutoff = now - timedelta(minutes=HEADLINE_WINDOW_MIN)
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            pub = item.findtext("pubDate")
            try:
                t = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
            except Exception:
                t = None
            if t is None or t < cutoff:
                continue
            m = pattern.search(title)
            if m:
                result = (True, f'fresh headline mentions "{m.group(1)}": {title[:70]}')
                break
    except Exception as e:
        print("news_guard: headline feeds unavailable:", type(e).__name__)
    _rss_cache.update({"t": time.time(), "result": result})
    return result


# ------------------------- 4. market guards -------------------------
def market_check(symbol):
    if mt5 is None:
        return False, ""
    info = mt5.symbol_info(symbol)
    if info is None:
        return False, ""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 220)
    if rates is None or len(rates) < 60:
        return False, ""
    spreads = sorted(int(r["spread"]) for r in rates[:-1])
    median_spread = spreads[len(spreads) // 2]
    if median_spread > 0 and info.spread > MAX_SPREAD_MULT * median_spread:
        return True, f"spread jumped to {info.spread} points (normal about {median_spread})"
    ranges = [float(r["high"] - r["low"]) for r in rates]
    last_range = ranges[-2]                              # last fully closed candle
    avg_range = sum(ranges[-16:-2]) / 14
    if avg_range > 0 and last_range > VOL_SPIKE_MULT * avg_range:
        return True, f"last candle is {last_range / avg_range:.1f}x the normal size (sudden volatility)"
    return False, ""


# ------------------------- 1. session -------------------------
def session_check(now=None):
    now = now or _now()
    if now.weekday() >= 5:
        return True, "weekend"
    hour = now.hour + now.minute / 60
    for a, b in SESSIONS_UTC:
        if a <= hour < b:
            return False, ""
    hours = ", ".join(f"{a + 1:02d}:00-{b + 1:02d}:00" for a, b in SESSIONS_UTC)
    return True, f"outside trading hours (allowed {hours} Nigeria time)"


# ------------------------- combined -------------------------
def can_trade(symbol, now=None, use_market=True):
    """Returns (allowed, [reasons it is blocked]). One broken feed never crashes your bot."""
    checks = []
    if USE_SESSION:
        checks.append(lambda: session_check(now))
    if USE_CALENDAR:
        checks.append(lambda: calendar_check(symbol, now))
    if USE_RSS:
        checks.append(lambda: rss_check(now))
    if USE_MARKET and use_market:
        checks.append(lambda: market_check(symbol))
    reasons = []
    for chk in checks:
        try:
            blocked, why = chk()
            if blocked:
                reasons.append(why)
        except Exception as e:
            print("news_guard: a check failed:", type(e).__name__)
    return (not reasons), reasons


def cancel_pending(symbol, magic):
    """Delete this bot's waiting (pending) orders on a symbol - use before a big news event.
    magic is REQUIRED so that orders placed by hand or by another bot are never touched."""
    if mt5 is None or magic is None:
        return
    for o in (mt5.orders_get(symbol=symbol) or []):
        if o.magic == magic:
            res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
            print(f"news_guard: cancelled pending order {o.ticket} ->", getattr(res, "comment", res))


# ------------------------- stand-alone test -------------------------
if __name__ == "__main__":
    print("=== news_guard test (places NO orders) ===")
    raw = load_calendar()
    if raw is None:
        print("Could not load the economic calendar (check your internet).")
    else:
        events = parse_events(raw)
        print(f"Calendar loaded: {len(events)} events this week.")
        print("\nUpcoming high-impact events (Nigeria time):")
        shown = 0
        for ev in events:
            if ev["time"] < _now() or ev["impact"] not in IMPACTS:
                continue
            if ev["currency"] not in ("USD", "EUR", "GBP", "JPY"):
                continue
            print(f"  {ev['time'].astimezone(WAT):%a %d %b %H:%M} | {ev['currency']} | {ev['title']} | {ev['impact']}")
            shown += 1
            if shown >= 12:
                break
        if shown == 0:
            print("  none left this week (the feed covers the current week only).")

    connected = bool(mt5 and mt5.initialize())
    print("\nMT5 connected (market checks active):", connected)
    print("\nCan the bot trade right now?")
    for sym in ("XAUUSD", "EURUSD", "GBPUSD", "USDJPY"):
        ok, reasons = can_trade(sym, use_market=connected)
        print(f"  {sym}: {'YES' if ok else 'NO - ' + '; '.join(reasons)}")
    if connected:
        mt5.shutdown()
