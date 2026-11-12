"""Automatic MT5 trader using closed-candle M5 scalp signals.

Usage:
    python mt5_auto.py                 # log-only mode
    python mt5_auto.py --mode auto     # demo orders; asks for MT5 credentials
    python mt5_auto.py --mode auto --live  # live orders; requires typing LIVE

Delete MT5_PAUSE_SWITCH.txt to resume entries. Creating MT5_KILL_SWITCH.txt
stops the runner; any open positions retain their broker-side SL/TP.
Valid M5 scalp signals for all configured symbols arm two-sided, ATR-spaced
BUY STOP/SELL STOP brackets with attached stop-loss/take-profit and one-candle
expiry. The runner cancels the opposite pending order after a fill; this OCO
handling is best-effort, not atomic at the broker. Scalps use fixed USD risk and
a broker-side cash target.
Public InvestingLive RSS headlines are informational alerts only and never
control trade entries or exits.
"""
import argparse
import csv
import getpass
import html
import json
import os
import re
import time

from collections.abc import Iterable
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, cast
from urllib.request import Request, urlopen
from xml.etree import ElementTree

# The MetaTrader5 package is installed with the MT5 terminal.  Keep this script
# self-contained so it can authenticate the account selected at startup.
try:
    import MetaTrader5 as mt5_module
except ImportError:
    mt5_module = None

mt5 = cast(Any, mt5_module)

# Instruments are matched by exact broker symbol first, then by a unique suffix
# match (for example EURUSD.a). Ambiguous broker variants are never guessed.
WATCHLIST = {
    "EURUSD": ("EURUSD",),
    "XAUUSD": ("XAUUSD", "GOLD"),
    "GBPUSD": ("GBPUSD",),
    "USDJPY": ("USDJPY",),
}

TIMEFRAME = mt5.TIMEFRAME_M15 if mt5 is not None else None
TIMEFRAME_SECONDS = 15 * 60
SCALP_TIMEFRAME = mt5.TIMEFRAME_M5 if mt5 is not None else None
SCALP_TIMEFRAME_SECONDS = 5 * 60
POLL_SECONDS = 5
NEWS_POLL_SECONDS = 300
NEWS_RSS_URL = os.environ.get(
    "MT5_NEWS_RSS_URL",
    "https://investinglive.com/feed/news/",
)
NEWS_MAX_ALERT_ITEMS = 3
CANDLE_DELAY_SECONDS = 5
MAX_TICK_AGE_SECONDS = 60
MAX_CLOCK_OFFSET_SECONDS = 14 * 60 * 60
MAX_CLOCK_CALIBRATION_ERROR_SECONDS = 120
FAST_EMA = 9
SLOW_EMA = 21
TREND_EMA = 100
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
ADX_PERIOD = 14
MIN_ADX = 20.0
ATR_PERIOD = 14
STOP_ATR_MULTIPLIER = 2.0
RISK_REWARD_RATIO = 2.5
MAX_SPREAD_ATR_RATIO = 0.15
BREAKOUT_ENTRY_ATR_MULTIPLIER = 0.5

SCALP_PULLBACK_EMA = 20
SCALP_TREND_EMA = 50
SCALP_DIRECTION_EMA = 200
SCALP_MIN_ADX = 20.0
SCALP_BUY_RSI_RANGE = (50.0, 70.0)
SCALP_SELL_RSI_RANGE = (30.0, 50.0)
SCALP_TAKE_PROFIT_CURRENCY = "USD"
SCALP_TAKE_PROFIT_AMOUNT = 3.0
SCALP_RISK_CASH = None  # Use the same 0.5% balance risk as non-scalp orders.
MIN_LIVE_ACCOUNT_BALANCE = 100.0
MIN_TRADE_VOLUME = 0.01

RISK_PERCENT = 0.5
DAILY_LOSS_LIMIT_PERCENT = 2.0
MAX_TRADES_PER_DAY = 4
MAX_OPEN_POSITIONS = 8
MAX_OPEN_POSITIONS_SMALL_ACCOUNT = 2
MAGIC = 20261005
SCALP_MAGIC = 20261006
BOT_MAGICS = (MAGIC, SCALP_MAGIC)
DEVIATION_POINTS = 30
BREAKOUT_ORDER_COMMENT = "mt5-auto-brk"

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "mt5_auto_state.json")
TRADE_LOG = os.path.join(HERE, "mt5_auto_trades.csv")
EQUITY_LOG = os.path.join(HERE, "mt5_auto_equity.csv")
PAUSE_FILE = os.path.join(HERE, "MT5_PAUSE_SWITCH.txt")
KILL_FILE = os.path.join(HERE, "MT5_KILL_SWITCH.txt")
BROKER_UTC_OFFSET_SECONDS: int | None = None
EXPECTED_ACCOUNT_MODE: int | None = None


class SafetyStop(RuntimeError):
    """Raised when the runner cannot safely continue automated trading."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def calibrate_broker_clock(symbols: Iterable[str]) -> None:
    """Infer MT5 server-time offset from live ticks; reject stale/inconsistent clocks."""
    global BROKER_UTC_OFFSET_SECONDS
    wall_time = utc_now().timestamp()
    offsets: list[int] = []
    for symbol in symbols:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise SafetyStop(f"Cannot calibrate MT5 server clock from {symbol}: {mt5.last_error()}")
        raw_offset = float(tick.time) - wall_time
        offset = round(raw_offset / 900) * 900
        if abs(offset) > MAX_CLOCK_OFFSET_SECONDS:
            raise SafetyStop(f"MT5 clock offset for {symbol} is outside the supported range.")
        if abs(raw_offset - offset) > MAX_CLOCK_CALIBRATION_ERROR_SECONDS:
            raise SafetyStop(
                f"Cannot reliably determine MT5 server-time offset for {symbol}; "
                "check the computer clock and that live quotes are updating."
            )
        offsets.append(offset)
    if not offsets:
        raise SafetyStop("Cannot calibrate MT5 server clock without an available configured symbol.")
    if len(set(offsets)) != 1:
        raise SafetyStop("MT5 symbols report inconsistent server-clock offsets; refusing automatic trading.")
    BROKER_UTC_OFFSET_SECONDS = offsets[0]
    say(f"MT5 server clock offset from UTC: {BROKER_UTC_OFFSET_SECONDS // 3600:+d} hours.")


def broker_now_epoch() -> float:
    if BROKER_UTC_OFFSET_SECONDS is None:
        raise SafetyStop("MT5 server clock has not been calibrated.")
    return utc_now().timestamp() + BROKER_UTC_OFFSET_SECONDS


def tick_age_seconds(tick_time: float) -> float:
    return broker_now_epoch() - tick_time


def candle_status(
    bar_time: int,
    previous_bar: int | None,
    age: float,
    timeframe_seconds: int = TIMEFRAME_SECONDS,
) -> str:
    if previous_bar is not None and bar_time <= previous_bar:
        return "duplicate"
    if age < CANDLE_DELAY_SECONDS:
        return "wait"
    if age > 2 * timeframe_seconds:
        return "stale"
    return "ready"


def say(message: str | None) -> None:
    text = "" if message is None else str(message)
    print(f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S')} UTC] {text}", flush=True)


def alert(message: str | None) -> None:
    text = "" if message is None else str(message)
    say(text)
    # Notifications are optional; console output remains available when the
    # optional broker integration notifier is not installed.


def read_mt5_credentials(
    input_fn: Any = input,
    password_fn: Any = getpass.getpass,
) -> tuple[int, str | None, str | None]:
    """Read MT5 login details; a blank password uses the terminal's saved login."""
    raw_account = input_fn("MT5 account/login number: ").strip()
    if not raw_account.isdigit() or int(raw_account) <= 0:
        raise SafetyStop("MT5 account/login must be a positive numeric account number.")
    password = password_fn("MT5 password (press Enter if saved in the terminal): ") or None
    server = input_fn("MT5 server (press Enter to use the terminal default): ").strip() or None
    return int(raw_account), password, server


def connect_to_mt5(account: int, password: str | None, server: str | None) -> None:
    """Initialize MT5 and authenticate the exact account supplied by the user."""
    if mt5 is None:
        raise SafetyStop("MetaTrader5 is unavailable. Install the MT5 Python package in the active environment.")
    initialize = getattr(mt5, "initialize", None)
    if not callable(initialize):
        raise SafetyStop("The installed MetaTrader5 package has no initialize() function.")
    kwargs: dict[str, Any] = {"login": account}
    if password:
        kwargs["password"] = password
    if server:
        kwargs["server"] = server
    if not initialize(**kwargs):
        raise SafetyStop(f"MT5 login failed for account {account}: {mt5.last_error()}")
    account_info = mt5.account_info()
    if account_info is None or int(getattr(account_info, "login", 0)) != account:
        shutdown = getattr(mt5, "shutdown", None)
        if callable(shutdown):
            shutdown()
        actual = getattr(account_info, "login", "unknown") if account_info else "unknown"
        raise SafetyStop(f"MT5 authenticated account {actual}, not the requested account {account}.")


def disconnect_from_mt5() -> None:
    if mt5 is not None:
        shutdown = getattr(mt5, "shutdown", None)
        if callable(shutdown):
            shutdown()


def fetch_market_news() -> list[dict[str, str]]:
    request = Request(
        NEWS_RSS_URL,
        headers={"User-Agent": "MT5-Demo-Market-Context/1.0"},
    )
    with urlopen(request, timeout=10) as response:
        payload = response.read(1_000_001)
    if len(payload) > 1_000_000:
        raise ValueError("Market-news RSS feed exceeded the 1 MB size limit.")

    root = ElementTree.fromstring(payload)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Market-news RSS feed has no channel.")

    items = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        published = (item.findtext("pubDate") or "").strip()
        if title and guid:
            items.append({
                "id": guid,
                "title": html.unescape(re.sub(r"<[^>]*>", "", title))[:240],
                "link": link,
                "published": published,
            })
    return items


def process_market_news(seen_ids: set[str] | None) -> set[str]:
    items = fetch_market_news()
    current_ids = {item["id"] for item in items}
    if seen_ids is None:
        say(f"Market-news RSS connected; initialized with {len(current_ids)} recent headlines.")
        return current_ids

    new_items = [item for item in items if item["id"] not in seen_ids]
    if new_items:
        lines = [
            f"- {item['title']}"
            + (f" ({item['published']})" if item["published"] else "")
            + (f"\n  {item['link']}" if item["link"] else "")
            for item in new_items[:NEWS_MAX_ALERT_ITEMS]
        ]
        more_count = len(new_items) - len(lines)
        message = "Market news context only - not a trading signal:\n" + "\n".join(lines)
        if more_count:
            message += f"\n...and {more_count} more new headline(s)."
        alert(message)
    return seen_ids | current_ids


def append_csv(path: str, header: list[str], row: list[Any]) -> None:
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        if is_new:
            writer.writerow(header)
        writer.writerow(row)


def log_trade(action: str, symbol: str, side: str = "", volume: Any = "",
              entry: Any = "", sl: Any = "", tp: Any = "", ticket: Any = "",
              note: str = "") -> None:
    append_csv(
        TRADE_LOG,
        ["time_utc", "action", "symbol", "side", "volume", "entry", "sl", "tp", "ticket", "note"],
        [utc_now().isoformat(), action, symbol, side, volume, entry, sl, tp, ticket, note],
    )


def load_state() -> dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as stream:
            state = json.load(stream)
    except (OSError, ValueError, TypeError) as exc:
        raise SafetyStop(f"Cannot read {os.path.basename(STATE_FILE)}; entries are blocked: {exc}") from exc
    if not isinstance(state, dict):
        raise SafetyStop(f"{os.path.basename(STATE_FILE)} is not a valid state object; entries are blocked.")
    return state


def save_state(state: dict[str, Any]) -> None:
    temp_path = STATE_FILE + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, STATE_FILE)
    except OSError as exc:
        raise SafetyStop(f"Cannot persist trading state; stopping automated entries: {exc}") from exc


def daily_state(account: Any) -> dict[str, Any]:
    state = load_state()
    today = utc_now().strftime("%Y-%m-%d")
    if state.get("day_utc") != today:
        state = {
            "day_utc": today,
            "start_equity": float(account.equity),
            "trades_today": 0,
            "halted": False,
        }
        save_state(state)
        say(f"New UTC trading day. Starting equity: {account.equity:.2f}")
    start_equity = state.get("start_equity")
    if not isinstance(start_equity, (int, float)) or isinstance(start_equity, bool) or start_equity <= 0:
        raise SafetyStop("Daily start equity is invalid; entries are blocked.")
    if not isinstance(state.get("trades_today"), int) or state["trades_today"] < 0:
        raise SafetyStop("Daily trade count is invalid; entries are blocked.")
    return state


def resolve_symbol(aliases: Iterable[str]) -> str | None:
    for name in aliases:
        info = mt5.symbol_info(name)
        if info is not None:
            if mt5.symbol_select(name, True):
                return name
            say(f"Could not add exact symbol {name} to Market Watch.")
            return None

    all_symbols = mt5.symbols_get()
    if all_symbols is None:
        raise SafetyStop(f"Could not query broker symbols: {mt5.last_error()}")
    alias_names = tuple(name.upper() for name in aliases)
    matches = sorted({
        item.name for item in all_symbols
        if any(item.name.upper().startswith(alias) for alias in alias_names)
    })
    if len(matches) != 1:
        if matches:
            say(f"Ambiguous broker symbols {matches}; configure one exact name in WATCHLIST.")
        else:
            say(f"No broker symbol found for aliases {', '.join(aliases)}.")
        return None
    symbol = matches[0]
    if not mt5.symbol_select(symbol, True):
        say(f"Could not add {symbol} to Market Watch.")
        return None
    return symbol


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0 or len(values) < period:
        raise ValueError("Insufficient values for EMA.")
    alpha = 2.0 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def atr(bars: list[Any], period: int) -> float:
    ranges = []
    for index in range(1, len(bars)):
        high = float(bars[index]["high"])
        low = float(bars[index]["low"])
        previous_close = float(bars[index - 1]["close"])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if len(ranges) < period:
        raise ValueError("Insufficient candles for ATR.")
    value = sum(ranges[:period]) / period
    for current in ranges[period:]:
        value = (value * (period - 1) + current) / period
    return value


def rsi(values: list[float], period: int) -> float:
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    if len(changes) < period:
        raise ValueError("Insufficient values for RSI.")
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    for index in range(period, len(changes)):
        average_gain = (average_gain * (period - 1) + gains[index]) / period
        average_loss = (average_loss * (period - 1) + losses[index]) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def _directional_movement(bars: list[Any]) -> tuple[list[float], list[float], list[float]]:
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    true_ranges: list[float] = []
    for index in range(1, len(bars)):
        high, low = float(bars[index]["high"]), float(bars[index]["low"])
        previous_high = float(bars[index - 1]["high"])
        previous_low = float(bars[index - 1]["low"])
        previous_close = float(bars[index - 1]["close"])
        upward = high - previous_high
        downward = previous_low - low
        plus_dm.append(upward if upward > downward and upward > 0 else 0.0)
        minus_dm.append(downward if downward > upward and downward > 0 else 0.0)
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return true_ranges, plus_dm, minus_dm


def _smooth_directional_movement(
    values: tuple[float, float, float],
    current: tuple[float, float, float],
    period: int,
) -> tuple[float, float, float]:
    return (
        current[0] + values[0] - current[0] / period,
        current[1] + values[1] - current[1] / period,
        current[2] + values[2] - current[2] / period,
    )


def _compute_dx(smooth_tr: float, smooth_plus: float, smooth_minus: float) -> float:
    if smooth_tr == 0:
        return 0.0
    plus_di = 100.0 * smooth_plus / smooth_tr
    minus_di = 100.0 * smooth_minus / smooth_tr
    denominator = plus_di + minus_di
    if denominator == 0:
        return 0.0
    return 100.0 * abs(plus_di - minus_di) / denominator


def _calculate_adx(
    true_ranges: list[float], plus_dm: list[float], minus_dm: list[float], period: int
) -> float:
    smooth_tr = sum(true_ranges[:period])
    smooth_plus = sum(plus_dm[:period])
    smooth_minus = sum(minus_dm[:period])
    dx_values: list[float] = []
    adx_value: float | None = None
    for index in range(period - 1, len(true_ranges)):
        if index >= period:
            smooth_tr, smooth_plus, smooth_minus = _smooth_directional_movement(
                (true_ranges[index], plus_dm[index], minus_dm[index]),
                (smooth_tr, smooth_plus, smooth_minus),
                period,
            )
        dx = _compute_dx(smooth_tr, smooth_plus, smooth_minus)
        if adx_value is None:
            dx_values.append(dx)
            if len(dx_values) == period:
                adx_value = sum(dx_values) / period
        else:
            adx_value = (adx_value * (period - 1) + dx) / period
    return adx_value if adx_value is not None else 0.0


def adx(bars: list[Any], period: int) -> float:
    true_ranges, plus_dm, minus_dm = _directional_movement(bars)
    if len(true_ranges) < period * 2:
        raise ValueError("Insufficient candles for ADX.")
    return _calculate_adx(true_ranges, plus_dm, minus_dm, period)


def get_signal(symbol: str) -> dict[str, Any]:
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 250)
    if rates is None or len(rates) < TREND_EMA + 3:
        raise RuntimeError(f"Insufficient candle data for {symbol}: {mt5.last_error()}")
    closed = list(rates[:-1])
    closes = [float(bar["close"]) for bar in closed]
    fast = ema(closes, FAST_EMA)
    slow = ema(closes, SLOW_EMA)
    trend = ema(closes, TREND_EMA)[-1]
    rsi_value = rsi(closes, RSI_PERIOD)
    adx_value = adx(closed, ADX_PERIOD)
    atr_value = atr(closed, ATR_PERIOD)

    signal = None
    if fast[-2] <= slow[-2] and fast[-1] > slow[-1] and closes[-1] > trend and rsi_value < RSI_OVERBOUGHT and adx_value >= MIN_ADX:
        signal = "BUY"
    elif fast[-2] >= slow[-2] and fast[-1] < slow[-1] and closes[-1] < trend and rsi_value > RSI_OVERSOLD and adx_value >= MIN_ADX:
        signal = "SELL"
    return {
        "signal": signal,
        "bar_time": int(closed[-1]["time"]),
        "close": closes[-1],
        "atr": atr_value,
        "rsi": rsi_value,
        "adx": adx_value,
    }


def get_scalp_signal(symbol: str) -> dict[str, Any]:
    rates = mt5.copy_rates_from_pos(symbol, SCALP_TIMEFRAME, 0, 250)
    if rates is None or len(rates) < SCALP_DIRECTION_EMA + 3:
        raise RuntimeError(f"Insufficient M5 candle data for {symbol}: {mt5.last_error()}")
    closed = list(rates[:-1])
    closes = [float(bar["close"]) for bar in closed]
    pullback_ema = ema(closes, SCALP_PULLBACK_EMA)
    trend_ema = ema(closes, SCALP_TREND_EMA)
    direction_ema = ema(closes, SCALP_DIRECTION_EMA)
    rsi_value = rsi(closes, RSI_PERIOD)
    adx_value = adx(closed, ADX_PERIOD)
    atr_value = atr(closed, ATR_PERIOD)

    last_bar = closed[-1]
    signal = None
    if (
        pullback_ema[-1] > trend_ema[-1] > direction_ema[-1]
        and float(last_bar["low"]) <= pullback_ema[-1] < closes[-1]
        and closes[-1] > float(last_bar["open"])
        and SCALP_BUY_RSI_RANGE[0] <= rsi_value < SCALP_BUY_RSI_RANGE[1]
        and adx_value >= SCALP_MIN_ADX
    ):
        signal = "BUY"
    elif (
        pullback_ema[-1] < trend_ema[-1] < direction_ema[-1]
        and float(last_bar["high"]) >= pullback_ema[-1] > closes[-1]
        and closes[-1] < float(last_bar["open"])
        and SCALP_SELL_RSI_RANGE[0] < rsi_value <= SCALP_SELL_RSI_RANGE[1]
        and adx_value >= SCALP_MIN_ADX
    ):
        signal = "SELL"
    return {
        "signal": signal,
        "bar_time": int(last_bar["time"]),
        "close": closes[-1],
        "atr": atr_value,
        "rsi": rsi_value,
        "adx": adx_value,
    }


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("Broker volume step must be positive.")
    step_decimal = Decimal(str(step))
    units = (Decimal(str(value)) / step_decimal).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * step_decimal)


def ceil_to_step(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("Broker volume step must be positive.")
    step_decimal = Decimal(str(step))
    units = (Decimal(str(value)) / step_decimal).to_integral_value(rounding=ROUND_CEILING)
    return float(units * step_decimal)


def normalize_price(price: float, info: Any, rounding: str) -> float:
    tick_size = float(getattr(info, "trade_tick_size", 0.0) or info.point)
    if tick_size <= 0:
        raise ValueError("Broker price tick size must be positive.")
    mode = ROUND_FLOOR if rounding == "down" else ROUND_CEILING
    tick = Decimal(str(tick_size))
    units = (Decimal(str(price)) / tick).to_integral_value(rounding=mode)
    return round(float(units * tick), int(info.digits))


def cash_target_price(
    symbol: str,
    side: str,
    entry: float,
    volume: float,
    target_cash: float,
    info: Any,
) -> float | None:
    tick_size = float(getattr(info, "trade_tick_size", 0.0) or info.point)
    if tick_size <= 0 or volume <= 0 or target_cash <= 0:
        return None
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    direction = 1.0 if side == "BUY" else -1.0
    probe_price = entry + direction * tick_size
    one_tick_profit = mt5.order_calc_profit(order_type, symbol, volume, entry, probe_price)
    if one_tick_profit is None or one_tick_profit <= 0:
        return None
    tick_count = (
        Decimal(str(target_cash)) / Decimal(str(one_tick_profit))
    ).to_integral_value(rounding=ROUND_CEILING)
    raw_target = entry + direction * tick_size * float(tick_count)
    target = normalize_price(raw_target, info, "up" if side == "BUY" else "down")
    estimated_profit = mt5.order_calc_profit(order_type, symbol, volume, entry, target)
    if estimated_profit is None or estimated_profit < target_cash:
        return None
    return target


def calculate_volume(
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    risk_cash: float | None = None,
) -> float | None:
    info = mt5.symbol_info(symbol)
    account = mt5.account_info()
    if info is None or account is None:
        raise SafetyStop(f"Cannot read symbol/account data to size {symbol}.")
    if account.balance <= 0 or info.volume_min <= 0 or info.volume_max <= 0:
        raise SafetyStop("Invalid account balance or broker volume limits.")
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    one_lot_pnl = mt5.order_calc_profit(order_type, symbol, 1.0, entry, stop)
    if one_lot_pnl is None or one_lot_pnl >= 0:
        say(f"Could not confirm a loss at stop for {symbol}: {mt5.last_error()}")
        return None
    allowed_risk = account.balance * RISK_PERCENT / 100.0 if risk_cash is None else risk_cash
    if allowed_risk <= 0:
        raise SafetyStop("Risk budget must be positive.")
    raw_volume = allowed_risk / abs(one_lot_pnl)
    volume_step = float(info.volume_step)
    minimum_volume = max(MIN_TRADE_VOLUME, float(info.volume_min))
    if minimum_volume > float(info.volume_max):
        raise SafetyStop(f"Configured minimum volume {minimum_volume} exceeds broker maximum for {symbol}.")
    volume = floor_to_step(min(raw_volume, float(info.volume_max)), volume_step)
    if volume < minimum_volume:
        volume = ceil_to_step(minimum_volume, volume_step)
    if volume > float(info.volume_max):
        say(f"Skipping {symbol}: minimum volume {volume} exceeds broker maximum {info.volume_max}.")
        return None
    margin = mt5.order_calc_margin(order_type, symbol, volume, entry)
    if margin is None:
        say(f"Cannot verify required margin for {symbol}: {mt5.last_error()}")
        return None
    if account.margin_free <= 0 or margin > account.margin_free * 0.9:
        say(f"Skipping {symbol}: required margin {margin:.2f} exceeds the 90% free-margin cap.")
        return None
    return volume


def filling_mode(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise SafetyStop(f"Cannot read symbol settings for {symbol}.")
    modes = int(info.filling_mode)
    if modes & 1:
        return mt5.ORDER_FILLING_FOK
    if modes & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _result_rows(result: Any, operation: str) -> list[Any]:
    if result is None:
        raise SafetyStop(f"Cannot read {operation}: {mt5.last_error()}")
    return list(result)


def get_bot_positions() -> list[Any]:
    positions = _result_rows(mt5.positions_get(), "open positions")
    return [position for position in positions if position.magic == MAGIC]


def get_scalp_positions() -> list[Any]:
    positions = _result_rows(mt5.positions_get(), "open scalp positions")
    return [position for position in positions if position.magic == SCALP_MAGIC]


def get_bot_orders() -> list[Any]:
    orders = _result_rows(mt5.orders_get(), "pending orders")
    return [order for order in orders if order.magic == MAGIC]


def get_managed_positions() -> list[Any]:
    positions = _result_rows(mt5.positions_get(), "open positions")
    return [position for position in positions if position.magic in BOT_MAGICS]


def get_managed_orders() -> list[Any]:
    orders = _result_rows(mt5.orders_get(), "pending orders")
    return [order for order in orders if order.magic in BOT_MAGICS]


def has_any_symbol_exposure(symbol: str) -> bool:
    positions = _result_rows(mt5.positions_get(symbol=symbol), f"positions for {symbol}")
    orders = _result_rows(mt5.orders_get(symbol=symbol), f"orders for {symbol}")
    return bool(positions or orders)


def _validate_trade_configuration(
    symbol: str,
    magic: int,
    cash_take_profit: float | None,
    pending_type: str | None,
    pending_entry: float | None,
    expiration: int | None,
    account: Any,
    info: Any,
    tick: Any,
) -> None:
    if info is None or tick is None or account is None:
        raise SafetyStop(f"Cannot read live trading data for {symbol}: {mt5.last_error()}")
    if EXPECTED_ACCOUNT_MODE is not None and account.trade_mode != EXPECTED_ACCOUNT_MODE:
        raise SafetyStop("MT5 account mode changed after startup; refusing to send an order.")
    if magic == SCALP_MAGIC and cash_take_profit is None:
        raise SafetyStop("M5 scalp orders require a cash take-profit target.")
    if cash_take_profit is not None and cash_take_profit <= 0:
        raise ValueError("Cash take-profit must be positive.")
    if pending_type not in (None, "BUY_STOP", "SELL_STOP"):
        raise ValueError(f"Unsupported automatic pending order type: {pending_type}.")
    if (pending_type is None) != (pending_entry is None):
        raise ValueError("Pending orders require both a type and entry price.")
    if pending_type is not None and (expiration is None or expiration <= broker_now_epoch()):
        raise ValueError("Pending orders require a future expiration time.")


def _skip_invalid_symbol(symbol: str, info: Any, tick: Any) -> str | None:
    if info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        return f"Skipping {symbol}: broker has disabled trading."
    tick_age = tick_age_seconds(float(tick.time))
    if tick_age < -MAX_CLOCK_CALIBRATION_ERROR_SECONDS or tick_age > MAX_TICK_AGE_SECONDS:
        return f"Skipping {symbol}: latest quote is stale ({tick_age:.0f}s old)."
    return None


def _resolve_order_entry(
    symbol: str,
    side: str,
    atr_value: float,
    tick: Any,
    pending_type: str | None,
    pending_entry: float | None,
) -> tuple[float, float, str | None]:
    if pending_entry is not None:
        entry = float(pending_entry)
    elif side == "BUY":
        entry = float(tick.ask)
    else:
        entry = float(tick.bid)
    spread = float(tick.ask) - float(tick.bid)
    if entry <= 0 or atr_value <= 0:
        return entry, spread, f"Skipping {symbol}: invalid price or ATR."
    if spread < 0 or spread > atr_value * MAX_SPREAD_ATR_RATIO:
        return entry, spread, (
            f"Skipping {symbol}: spread {spread:.6g} is too wide for ATR {atr_value:.6g}."
        )
    if pending_type == "BUY_STOP" and (side != "BUY" or entry <= float(tick.ask)):
        return entry, spread, f"Skipping {symbol}: BUY STOP entry must be above the current ask."
    if pending_type == "SELL_STOP" and (side != "SELL" or entry >= float(tick.bid)):
        return entry, spread, f"Skipping {symbol}: SELL STOP entry must be below the current bid."
    return entry, spread, None


def _calculate_stop_targets(
    symbol: str,
    side: str,
    entry: float,
    atr_value: float,
    info: Any,
) -> tuple[float, float, float, str | None]:
    stop_distance = atr_value * STOP_ATR_MULTIPLIER
    if side == "BUY":
        sl = normalize_price(entry - stop_distance, info, "down")
        tp = normalize_price(entry + stop_distance * RISK_REWARD_RATIO, info, "up")
    else:
        sl = normalize_price(entry + stop_distance, info, "up")
        tp = normalize_price(entry - stop_distance * RISK_REWARD_RATIO, info, "down")
    min_stop = float(info.trade_stops_level) * float(info.point)
    if entry <= 0 or abs(entry - sl) < min_stop:
        return sl, tp, min_stop, (
            f"Skipping {symbol}: stop distance is inside the broker's minimum stop level."
        )
    return sl, tp, min_stop, None


def _resolve_cash_take_profit_target(
    symbol: str,
    side: str,
    entry: float,
    volume: float,
    account: Any,
    info: Any,
    cash_take_profit: float | None,
    min_stop: float,
) -> tuple[float, str | None]:
    if cash_take_profit is None:
        return entry, None
    account_currency = getattr(account, "currency", "unknown")
    if account_currency != SCALP_TAKE_PROFIT_CURRENCY:
        return entry, (
            f"Skipping {symbol}: cash scalp target is in {SCALP_TAKE_PROFIT_CURRENCY}, "
            f"but account currency is {account_currency}."
        )
    target = cash_target_price(symbol, side, entry, volume, cash_take_profit, info)
    if target is None:
        return entry, f"Could not verify the {cash_take_profit:.2f} cash target price for {symbol}."
    if abs(target - entry) < min_stop:
        return entry, f"Skipping {symbol}: cash scalp target is inside the broker's minimum stop level."
    return target, None


def _build_order_request(
    symbol: str,
    side: str,
    entry: float,
    sl: float,
    tp: float,
    volume: float,
    magic: int,
    pending_type: str | None,
    expiration: int | None,
) -> dict[str, Any]:
    is_pending = pending_type is not None
    order_types = {
        "BUY_STOP": mt5.ORDER_TYPE_BUY_STOP,
        "SELL_STOP": mt5.ORDER_TYPE_SELL_STOP,
    }
    if is_pending:
        comment = BREAKOUT_ORDER_COMMENT
    elif magic == SCALP_MAGIC:
        comment = "mt5-auto-scalp"
    else:
        comment = "mt5-auto-order"

    if pending_type:
        order_type = order_types[pending_type]
    else:
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    request = {
        "action": mt5.TRADE_ACTION_PENDING if is_pending else mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": entry,
        "sl": sl,
        "tp": tp,
        "deviation": DEVIATION_POINTS,
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_SPECIFIED if is_pending else mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN if is_pending else filling_mode(symbol),
    }
    if is_pending:
        request["expiration"] = expiration
    return request


def _prepare_order_request(
    symbol: str,
    side: str,
    entry: float,
    sl: float,
    volume: float,
    magic: int,
    pending_type: str | None,
    expiration: int | None,
    account: Any,
    info: Any,
    cash_take_profit: float | None,
    min_stop: float,
) -> tuple[dict[str, Any] | None, str | None]:
    resolved_tp, reason = _resolve_cash_take_profit_target(
        symbol,
        side,
        entry,
        volume,
        account,
        info,
        cash_take_profit,
        min_stop,
    )
    if reason is not None:
        return None, reason
    request = _build_order_request(
        symbol,
        side,
        entry,
        sl,
        resolved_tp,
        volume,
        magic,
        pending_type,
        expiration,
    )
    return request, None


def _prepare_trade_request_context(
    symbol: str,
    side: str,
    atr_value: float,
    magic: int,
    cash_take_profit: float | None,
    risk_cash: float | None,
    pending_type: str | None,
    pending_entry: float | None,
    expiration: int | None,
) -> tuple[dict[str, Any] | None, dict[str, Any], str | None]:
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    account = mt5.account_info()
    _validate_trade_configuration(
        symbol,
        magic,
        cash_take_profit,
        pending_type,
        pending_entry,
        expiration,
        account,
        info,
        tick,
    )

    skip_reason = _skip_invalid_symbol(symbol, info, tick)
    if skip_reason is not None:
        say(skip_reason)
        return None, {}, skip_reason

    entry, spread, reason = _resolve_order_entry(symbol, side, atr_value, tick, pending_type, pending_entry)
    if reason is not None:
        if spread != 0 and "spread" in reason:
            log_trade("SKIP", symbol, side, entry=entry, note="spread exceeds ATR filter")
        say(reason)
        return None, {"symbol": symbol, "side": side, "entry": entry}, reason

    sl, tp, min_stop, reason = _calculate_stop_targets(symbol, side, entry, atr_value, info)
    if reason is not None:
        say(reason)
        log_trade("SKIP", symbol, side, entry=entry, sl=sl, tp=tp, note="broker minimum stop distance")
        return None, {"symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp}, reason

    volume = calculate_volume(symbol, side, entry, sl, risk_cash=risk_cash)
    if volume is None:
        log_trade("SKIP", symbol, side, entry=entry, sl=sl, tp=tp, note="risk or margin sizing rejected")
        return None, {"symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp}, "risk or margin sizing rejected"

    request, reason = _prepare_order_request(
        symbol,
        side,
        entry,
        sl,
        volume,
        magic,
        pending_type,
        expiration,
        account,
        info,
        cash_take_profit,
        min_stop,
    )
    if request is None:
        say(reason)
        return None, {"symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp, "volume": volume}, reason

    context = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "volume": volume,
        "pending_type": pending_type,
        "is_pending": pending_type is not None,
        "magic": magic,
        "cash_take_profit": cash_take_profit,
        "risk_cash": risk_cash,
        "expiration": expiration,
    }
    return request, context, None


def _trade_rejection_reason(response: Any) -> str:
    return "no broker response" if response is None else f"retcode {response.retcode}: {response.comment}"


def _accepted_trade_result(response: Any) -> bool:
    if response is None:
        return False
    accepted = {
        mt5.TRADE_RETCODE_DONE,
        getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", mt5.TRADE_RETCODE_DONE),
        getattr(mt5, "TRADE_RETCODE_PLACED", mt5.TRADE_RETCODE_DONE),
    }
    return response.retcode in accepted


def order_trade(
    symbol: str,
    side: str,
    atr_value: float,
    state: dict[str, Any],
    magic: int = MAGIC,
    cash_take_profit: float | None = None,
    risk_cash: float | None = None,
    pending_type: str | None = None,
    pending_entry: float | None = None,
    expiration: int | None = None,
    count_trade: bool = True,
) -> bool:
    request, context, reason = _prepare_trade_request_context(
        symbol,
        side,
        atr_value,
        magic,
        cash_take_profit,
        risk_cash,
        pending_type,
        pending_entry,
        expiration,
    )
    if reason is not None:
        return False

    is_pending = context["is_pending"]
    volume = context["volume"]
    entry = context["entry"]
    sl = context["sl"]
    tp = context["tp"]

    log_trade("CHECK", symbol, side, volume, entry, sl, tp, note="pre-send")
    check = mt5.order_check(request)
    if check is None or check.retcode != 0:
        reason = _trade_rejection_reason(check)
        log_trade("REJECT", symbol, side, volume, entry, sl, tp, note=reason)
        say(f"Order check rejected {symbol}: {reason}")
        return False

    if count_trade:
        state["trades_today"] += 1
        save_state(state)

    result = mt5.order_send(request)
    if not _accepted_trade_result(result):
        reason = _trade_rejection_reason(result)
        log_trade("REJECT", symbol, side, volume, entry, sl, tp, note=reason)
        say(f"Broker rejected {symbol}: {reason}")
        if result is not None and count_trade:
            state["trades_today"] -= 1
            save_state(state)
        return False

    if is_pending:
        note = f"{pending_type}; expires {expiration}"
    elif magic == SCALP_MAGIC:
        note = "M5 scalp; cash target"
    else:
        note = "auto"
    log_trade(
        "PENDING" if is_pending else "OPEN",
        symbol,
        pending_type or side,
        volume,
        entry,
        sl,
        tp,
        result.order,
        note=note,
    )
    if is_pending:
        alert(
            f"Placed {pending_type} {symbol} {volume} lots at {entry}; "
            f"SL {sl}, TP {tp}; expires in one strategy candle."
        )
        return True

    if magic == SCALP_MAGIC:
        alert(
            f"Opened M5 scalp {side} {symbol} {volume} lots near {entry}; "
            f"planned stop risk about ${risk_cash:.2f}; SL {sl}; "
            f"broker TP {tp} targets about ${cash_take_profit:.2f}."
        )
        return True

    alert(f"Opened {side} {symbol} {volume} lots near {entry}; SL {sl}, TP {tp}.")
    return True


def cancel_breakout_orders(symbol: str, magic: int, reason: str) -> bool:
    orders = [
        order for order in get_managed_orders()
        if order.symbol == symbol
        and order.magic == magic
        and str(getattr(order, "comment", "")).startswith(BREAKOUT_ORDER_COMMENT)
    ]
    all_cancelled = True
    for order in orders:
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": order.ticket,
            "magic": magic,
            "comment": "mt5-auto OCO cancel",
        })
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            detail = mt5.last_error() if result is None else f"{result.retcode}: {result.comment}"
            say(f"Could not cancel breakout order {order.ticket} for {reason}: {detail}")
            all_cancelled = False
        else:
            log_trade(
                "CANCEL",
                symbol,
                ticket=order.ticket,
                note=f"breakout bracket: {reason}",
            )
    return all_cancelled


def cancel_all_breakout_orders(reason: str) -> bool:
    groups = {
        (order.symbol, order.magic)
        for order in get_managed_orders()
        if str(getattr(order, "comment", "")).startswith(BREAKOUT_ORDER_COMMENT)
    }
    all_cancelled = True
    for symbol, magic in groups:
        if not cancel_breakout_orders(symbol, magic, reason):
            all_cancelled = False
    return all_cancelled


def manage_breakout_brackets() -> None:
    positions = get_managed_positions()
    orders = get_managed_orders()
    groups = {
        (order.symbol, order.magic)
        for order in orders
        if str(getattr(order, "comment", "")).startswith(BREAKOUT_ORDER_COMMENT)
    }
    for symbol, magic in groups:
        if any(
            position.symbol == symbol and position.magic == magic
            for position in positions
        ) and cancel_breakout_orders(symbol, magic, "opposite order filled"):
            alert(
                f"Breakout OCO: cancelled remaining {symbol} pending order(s) "
                "after a bracket position opened."
            )


def place_breakout_bracket(
    symbol: str,
    atr_value: float,
    state: dict[str, Any],
    timeframe_seconds: int,
    magic: int,
    setup_side: str,
    cash_take_profit: float | None = None,
    risk_cash: float | None = None,
) -> bool:
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        raise SafetyStop(f"Cannot read data to place a breakout bracket for {symbol}: {mt5.last_error()}")
    if atr_value <= 0:
        say(f"Skipping {symbol}: invalid ATR for breakout distance.")
        return False

    entry_offset = atr_value * BREAKOUT_ENTRY_ATR_MULTIPLIER
    buy_entry = normalize_price(float(tick.ask) + entry_offset, info, "up")
    sell_entry = normalize_price(float(tick.bid) - entry_offset, info, "down")
    expiration = int(broker_now_epoch() + timeframe_seconds)

    if not order_trade(
        symbol,
        "BUY",
        atr_value,
        state,
        magic=magic,
        cash_take_profit=cash_take_profit,
        risk_cash=risk_cash,
        pending_type="BUY_STOP",
        pending_entry=buy_entry,
        expiration=expiration,
    ):
        return False

    if not order_trade(
        symbol,
        "SELL",
        atr_value,
        state,
        magic=magic,
        cash_take_profit=cash_take_profit,
        risk_cash=risk_cash,
        pending_type="SELL_STOP",
        pending_entry=sell_entry,
        expiration=expiration,
        count_trade=False,
    ):
        cancelled = cancel_breakout_orders(symbol, magic, "incomplete bracket")
        position_opened = any(
            position.symbol == symbol and position.magic == magic
            for position in get_managed_positions()
        )
        if cancelled and not position_opened:
            state["trades_today"] -= 1
            save_state(state)
        elif not cancelled:
            alert(
                f"WARNING: {symbol} breakout bracket could not be completed or fully cancelled. "
                "Check MT5 pending orders before restarting the bot."
            )
        elif position_opened:
            alert(
                f"WARNING: a {symbol} bracket order filled while the other side was rejected. "
                "The filled position keeps its attached stop-loss/take-profit; verify MT5."
            )
        return False

    alert(
        f"Armed {symbol} two-sided breakout bracket after the {setup_side} setup: "
        f"BUY STOP {buy_entry} / SELL STOP {sell_entry}, expires in "
        f"{timeframe_seconds // 60} minutes. Each stop has its own risk-sized SL; "
        "the opposite order will be cancelled after a fill."
    )
    return True


def close_managed_position(position: Any, reason: str) -> bool:
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        say(f"Could not read quote to close bot position {position.ticket}: {mt5.last_error()}")
        return False
    is_buy = position.type == mt5.POSITION_TYPE_BUY
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": position.volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": position.ticket,
        "price": tick.bid if is_buy else tick.ask,
        "deviation": DEVIATION_POINTS,
        "magic": position.magic,
        "comment": "mt5-auto scalp exit" if position.magic == SCALP_MAGIC else "mt5-auto risk stop",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode(position.symbol),
    }
    try:
        result = mt5.order_send(request)
        if result is None or result.retcode not in {
            mt5.TRADE_RETCODE_DONE,
            getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", mt5.TRADE_RETCODE_DONE),
        }:
            detail = mt5.last_error() if result is None else f"{result.retcode}: {result.comment}"
            say(f"Could not close bot position {position.ticket} ({position.symbol}) for {reason}: {detail}")
            return False
        side = "SELL" if is_buy else "BUY"
        log_trade(
            "CLOSE", position.symbol, side,
            position.volume, request["price"], ticket=position.ticket, note=reason,
        )
        if position.magic == SCALP_MAGIC and reason == "scalp cash target reached":
            alert(
                f"Closed M5 scalp {position.symbol} position {position.ticket} "
                f"at about ${float(position.profit):.2f} floating profit."
            )
        return True
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        say(f"Could not close bot position {position.ticket} ({position.symbol}) for {reason}: {exc}")
        return False


def _update_scalp_target(position) -> bool:
    if float(position.profit) >= SCALP_TAKE_PROFIT_AMOUNT:
        close_managed_position(position, "scalp cash target reached")
        return True

    info = mt5.symbol_info(position.symbol)
    if info is None:
        say(f"Could not read symbol info to set scalp target for {position.ticket}: {mt5.last_error()}")
        return False

    side = "BUY" if position.type == mt5.POSITION_TYPE_BUY else "SELL"
    target = cash_target_price(
        position.symbol,
        side,
        float(position.price_open),
        float(position.volume),
        SCALP_TAKE_PROFIT_AMOUNT,
        info,
    )
    if target is None:
        say(f"Could not calculate the ${SCALP_TAKE_PROFIT_AMOUNT:.2f} target for scalp {position.ticket}.")
        return False

    tick_size = float(getattr(info, "trade_tick_size", 0.0) or info.point)
    if abs(float(position.tp) - target) < tick_size / 2:
        return False

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": position.symbol,
        "sl": position.sl,
        "tp": target,
        "magic": SCALP_MAGIC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        detail = mt5.last_error() if result is None else f"{result.retcode}: {result.comment}"
        say(f"Could not update scalp target for position {position.ticket}: {detail}")
        return False

    alert(
        f"Updated M5 scalp {position.symbol} position {position.ticket} "
        f"to a full-close target of about ${SCALP_TAKE_PROFIT_AMOUNT:.2f}; "
        "the existing stop-loss was retained."
    )
    return True


def close_scalp_positions_at_target() -> None:
    for position in get_scalp_positions():
        _update_scalp_target(position)


def close_bot_exposure(reason: str) -> None:
    """Close/cancel only orders and positions carrying this runner's magic numbers."""
    for order in get_managed_orders():
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": order.ticket,
            "magic": order.magic,
            "comment": "mt5-auto risk stop",
        }
        try:
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                detail = mt5.last_error() if result is None else f"{result.retcode}: {result.comment}"
                say(f"Could not cancel bot pending order {order.ticket} ({order.symbol}) for {reason}: {detail}")
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            say(f"Could not cancel bot pending order {order.ticket} ({order.symbol}) for {reason}: {exc}")
    for position in get_managed_positions():
        close_managed_position(position, reason)


def daily_loss_allows_entries() -> tuple[bool, dict[str, Any]]:
    account = mt5.account_info()
    if account is None or account.equity <= 0:
        raise SafetyStop(f"Cannot safely verify current account equity: {mt5.last_error()}")
    state = daily_state(account)
    start = float(state["start_equity"])
    loss_percent = (start - float(account.equity)) / start * 100.0
    if state.get("halted"):
        close_bot_exposure("daily loss limit retry")
        return False, state
    if loss_percent >= DAILY_LOSS_LIMIT_PERCENT:
        state["halted"] = True
        save_state(state)
        close_bot_exposure("daily loss limit")
        alert(
            f"Daily loss limit reached ({loss_percent:.2f}%). "
            "Bot positions/orders are being closed/cancelled; entries stay halted until the next UTC day."
        )
        return False, state
    if state["trades_today"] >= MAX_TRADES_PER_DAY:
        return False, state
    return True, state


def can_open(symbol: str, side: str) -> bool:
    del side
    if os.path.exists(PAUSE_FILE):
        say("Pause switch is present; no new entries will be opened.")
        return False
    if has_any_symbol_exposure(symbol):
        say(f"Skipping {symbol}: a position or pending order already exists on this symbol.")
        return False
    positions = get_managed_positions()
    orders = get_managed_orders()
    account = mt5.account_info()
    if account is None:
        raise SafetyStop(f"Cannot verify account balance before opening {symbol}: {mt5.last_error()}")
    exposure_cap = (
        MAX_OPEN_POSITIONS_SMALL_ACCOUNT
        if float(account.balance) < 500.0
        else MAX_OPEN_POSITIONS
    )
    if len(positions) + len(orders) + 2 > exposure_cap:
        say(
            f"Skipping {symbol}: a two-order breakout bracket would exceed the "
            f"{exposure_cap}-position/order exposure cap."
        )
        return False
    return True


def can_open_scalp(symbol: str, side: str) -> bool:
    return can_open(symbol, side)


def initialize(
    require_trading: bool,
    live: bool = False,
    account: int | None = None,
    password: str | None = None,
    server: str | None = None,
) -> dict[str, str]:
    global EXPECTED_ACCOUNT_MODE
    if mt5 is None:
        raise SafetyStop("MetaTrader5 is unavailable. Install/run the bot in the MT5 Windows environment.")
    if account is not None or password is not None:
        if account is None:
            raise SafetyStop("An MT5 account number is required.")
        connect_to_mt5(account, password, server)
    elif not callable(getattr(mt5, "initialize", None)) or not mt5.initialize():
        raise SafetyStop(f"Could not connect to MT5: {mt5.last_error()}")
    try:
        account_info = mt5.account_info()
        terminal = mt5.terminal_info()
        if account_info is None:
            raise SafetyStop(f"Cannot read the authenticated MT5 account: {mt5.last_error()}")
        expected_mode = mt5.ACCOUNT_TRADE_MODE_REAL if live else mt5.ACCOUNT_TRADE_MODE_DEMO
        if account_info.trade_mode != expected_mode:
            account_kind = "live" if live else "demo"
            raise SafetyStop(f"Connected account is not a {account_kind} account; refusing to trade.")
        EXPECTED_ACCOUNT_MODE = expected_mode
        if live and (str(getattr(account_info, "currency", "")).upper() != "USD"
                     or float(account_info.balance) < MIN_LIVE_ACCOUNT_BALANCE):
            raise SafetyStop(
                f"Live trading requires a USD account with at least ${MIN_LIVE_ACCOUNT_BALANCE:.2f} balance."
            )
        if terminal is None:
            raise SafetyStop(f"Cannot read MT5 terminal status: {mt5.last_error()}")
        if require_trading and not terminal.trade_allowed:
            raise SafetyStop("MT5 Algo Trading is disabled; enable it before starting the runner.")

        resolved: dict[str, str] = {}
        missing: list[str] = []
        for label, aliases in WATCHLIST.items():
            symbol = resolve_symbol(aliases)
            if symbol:
                resolved[label] = symbol
            else:
                missing.append(label)
        if missing:
            raise SafetyStop(
                "All configured instruments are required for M5 scalping; "
                f"unavailable on this broker: {', '.join(missing)}."
            )
        calibrate_broker_clock(resolved.values())
        return resolved
    except Exception:
        disconnect_from_mt5()
        raise


def _run_auto_risk_checks() -> tuple[bool, dict[str, object]]:
    try:
        manage_breakout_brackets()
    except SafetyStop:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        say(f"Error managing breakout OCO orders: {type(exc).__name__}: {exc}")
    try:
        close_scalp_positions_at_target()
    except SafetyStop:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        say(f"Error managing M5 scalp exits: {type(exc).__name__}: {exc}")
    try:
        allowed, risk_state = daily_loss_allows_entries()
    except SafetyStop:
        raise
    except Exception as exc:
        raise SafetyStop(f"Could not verify daily risk limit: {type(exc).__name__}: {exc}") from exc
    return allowed, risk_state


def _poll_market_news(
    seen_news_ids: set[str] | None,
    next_news_poll: float,
) -> tuple[set[str] | None, float]:
    if time.monotonic() < next_news_poll:
        return seen_news_ids, next_news_poll
    try:
        seen_news_ids = process_market_news(seen_news_ids)
    except (OSError, ValueError, ElementTree.ParseError) as exc:
        say(f"Market-news RSS update failed: {type(exc).__name__}: {exc}")
    finally:
        next_news_poll = time.monotonic() + NEWS_POLL_SECONDS
    return seen_news_ids, next_news_poll


def _record_ready_equity(scalp_symbol: str, scalp_bar_time: int) -> None:
    equity = mt5.account_info()
    if equity is None:
        raise SafetyStop(f"Cannot read account equity: {mt5.last_error()}")
    bar_time_text = datetime.fromtimestamp(scalp_bar_time, timezone.utc).strftime("%Y-%m-%d %H:%M")
    append_csv(
        EQUITY_LOG,
        ["time_utc", "equity", "symbol", "bar_time_utc"],
        [utc_now().isoformat(), equity.equity, scalp_symbol, bar_time_text],
    )


def _handle_ready_scalp_signal(
    scalp_label: str,
    scalp_symbol: str,
    scalp_data: dict[str, float | int | str],
    state: dict[str, object],
    is_auto: bool,
) -> None:
    tick = mt5.symbol_info_tick(scalp_symbol)
    if tick is None:
        say(f"Skipping M5 scalp alert for {scalp_symbol}: cannot read quote.")
        return
    tick_age = tick_age_seconds(float(tick.time))
    spread = float(tick.ask) - float(tick.bid)
    bar_time = int(scalp_data["bar_time"])
    close_price = float(scalp_data["close"])
    atr = float(scalp_data["atr"])
    rsi = float(scalp_data["rsi"])
    adx = float(scalp_data["adx"])
    signal = str(scalp_data["signal"])
    if tick_age < -MAX_CLOCK_CALIBRATION_ERROR_SECONDS or tick_age > MAX_TICK_AGE_SECONDS:
        say(f"Skipping M5 scalp alert for {scalp_symbol}: quote is stale ({tick_age:.0f}s old).")
        return
    if spread < 0 or atr <= 0 or spread > atr * MAX_SPREAD_ATR_RATIO:
        say(f"Skipping M5 scalp setup for {scalp_symbol}: spread is too wide.")
        return
    bar_time_text = datetime.fromtimestamp(bar_time, timezone.utc).strftime("%Y-%m-%d %H:%M")
    say(
        f"{scalp_label} ({scalp_symbol}) M5 close {bar_time_text}: "
        f"{signal} scalp setup "
        f"| RSI {rsi:.1f} | ADX {adx:.1f} "
        f"| ATR {atr:.6g}"
    )
    if is_auto:
        log_trade(
            "SCALP_SIGNAL",
            scalp_symbol,
            signal,
            entry=close_price,
            note="M5 pullback setup",
        )
        trades_today = state.get("trades_today")
        if not isinstance(trades_today, int) or trades_today < 0:
            raise SafetyStop("Daily trade count is invalid; entries are blocked.")
        if trades_today >= MAX_TRADES_PER_DAY:
            say(f"Skipping {scalp_symbol}: daily trade limit of {MAX_TRADES_PER_DAY} reached.")
            return
        if can_open_scalp(scalp_symbol, signal):
            place_breakout_bracket(
                scalp_symbol,
                atr,
                state,
                SCALP_TIMEFRAME_SECONDS,
                magic=SCALP_MAGIC,
                setup_side=signal,
                cash_take_profit=SCALP_TAKE_PROFIT_AMOUNT,
                risk_cash=SCALP_RISK_CASH,
            )
        return
    log_trade(
        "SCALP_ALERT",
        scalp_symbol,
        signal,
        entry=close_price,
        note="M5 pullback setup; alert only",
    )
    alert(
        f"Potential M5 {scalp_label} scalp setup: {signal} {scalp_symbol} "
        f"near {close_price}; closed candle {bar_time_text} UTC; "
        f"RSI {rsi:.1f}, ADX {adx:.1f}. "
        "Alert only—no order was placed."
    )


def _update_scalp_bar(
    scalp_symbol: str,
    scalp_data: dict[str, float | int | str],
    last_scalp_bars: dict[str, int],
) -> str:
    scalp_bar_time = int(scalp_data["bar_time"])
    previous_scalp_bar = last_scalp_bars.get(scalp_symbol)
    if previous_scalp_bar is None:
        last_scalp_bars[scalp_symbol] = scalp_bar_time
        return "skip"
    candle_close_time = scalp_bar_time + SCALP_TIMEFRAME_SECONDS
    age = broker_now_epoch() - candle_close_time
    status = candle_status(scalp_bar_time, previous_scalp_bar, age, SCALP_TIMEFRAME_SECONDS)
    if status not in ("duplicate", "wait"):
        last_scalp_bars[scalp_symbol] = scalp_bar_time
        if status == "stale":
            say(f"Skipping stale or not-yet-closed M5 candle for {scalp_symbol} (age {age:.0f}s).")
        elif status == "ready":
            _record_ready_equity(scalp_symbol, scalp_bar_time)
    return status


def _process_scalp_symbol(
    scalp_label: str,
    scalp_symbol: str,
    last_scalp_bars: dict[str, int],
    state: dict[str, object],
    is_auto: bool,
) -> None:
    try:
        scalp_data = get_scalp_signal(scalp_symbol)
        status = _update_scalp_bar(scalp_symbol, scalp_data, last_scalp_bars)
        if status in ("duplicate", "wait", "skip"):
            return
        if status == "ready" and scalp_data["signal"]:
            _handle_ready_scalp_signal(scalp_label, scalp_symbol, scalp_data, state, is_auto)
    except SafetyStop:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        say(f"Error checking M5 scalp setup for {scalp_label} ({scalp_symbol}): {type(exc).__name__}: {exc}")


def _start_scalp_run(mode: str, symbols: dict[str, str]) -> tuple[dict[str, object], dict[str, int], bool]:
    is_auto = mode == "auto"
    if is_auto:
        account = mt5.account_info()
        if account is None:
            raise SafetyStop(f"Cannot read account before starting: {mt5.last_error()}")
        state = daily_state(account)
        alert(
            f"Automatic M5 scalp mode started. Symbols: {', '.join(symbols.values())}; "
            f"planned risk {RISK_PERCENT:.2f}% of balance per trade with a "
            f"${SCALP_TAKE_PROFIT_AMOUNT:.2f} full-close profit target; "
            "setups use two-sided ATR breakout stop orders; "
            f"daily stop {DAILY_LOSS_LIMIT_PERCENT:.2f}%."
        )
    else:
        state = {}
        alert(f"Log-only mode started. No orders will be placed. Symbols: {', '.join(symbols.values())}.")

    last_scalp_bars: dict[str, int] = {}
    for scalp_symbol in symbols.values():
        try:
            initial = get_scalp_signal(scalp_symbol)
            last_scalp_bars[scalp_symbol] = int(initial["bar_time"])
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            say(f"Startup M5 candle read failed for {scalp_symbol}: {type(exc).__name__}: {exc}")
    say("Watching closed M5 candles for all configured symbols. Initial candles are skipped.")
    if symbols:
        say(
            f"Watching {', '.join(symbols.values())} for scalp setups; "
            f"a valid setup arms BUY STOP and SELL STOP orders {BREAKOUT_ENTRY_ATR_MULTIPLIER:.1f} ATR "
            f"away, with planned risk {RISK_PERCENT:.2f}% of balance per order."
        )
    return state, last_scalp_bars, is_auto


def _run_scalp_poll(
    is_auto: bool,
    state: dict[str, object],
    scalp_symbols: dict[str, str],
    last_scalp_bars: dict[str, int],
    seen_news_ids: set[str] | None,
    next_news_poll: float,
) -> tuple[dict[str, object], set[str] | None, float, bool]:
    if os.path.exists(KILL_FILE):
        alert(f"{os.path.basename(KILL_FILE)} found. Runner stopped; open trades retain broker-side SL/TP.")
        return state, seen_news_ids, next_news_poll, False
    if is_auto:
        allowed, state = _run_auto_risk_checks()
        if not allowed:
            time.sleep(POLL_SECONDS)
            return state, seen_news_ids, next_news_poll, True

    seen_news_ids, next_news_poll = _poll_market_news(seen_news_ids, next_news_poll)
    for scalp_label, scalp_symbol in scalp_symbols.items():
        _process_scalp_symbol(scalp_label, scalp_symbol, last_scalp_bars, state, is_auto)
    time.sleep(POLL_SECONDS)
    return state, seen_news_ids, next_news_poll, True


def run(mode: str, symbols: dict[str, str]) -> None:
    state, last_scalp_bars, is_auto = _start_scalp_run(mode, symbols)

    seen_news_ids: set[str] | None = None
    next_news_poll = 0.0
    while True:
        state, seen_news_ids, next_news_poll, keep_running = _run_scalp_poll(
            is_auto,
            state,
            symbols,
            last_scalp_bars,
            seen_news_ids,
            next_news_poll,
        )
        if not keep_running:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5 automatic trading runner; demo by default.")
    parser.add_argument("--mode", choices=("log", "auto"), default="log")
    parser.add_argument("--live", action="store_true", help="Allow live account trading (requires explicit confirmation).")
    args = parser.parse_args()
    if args.live and args.mode != "auto":
        parser.error("--live requires --mode auto")
    credentials: tuple[int, str | None, str | None] | None = None
    if args.mode == "auto":
        try:
            credentials = read_mt5_credentials()
        except (EOFError, KeyboardInterrupt):
            say("Automatic mode cancelled.")
            return
        except SafetyStop as exc:
            say(f"Automatic mode cancelled: {exc}")
            return
        confirmation = "LIVE" if args.live else "YES"
        prompt = "Start REAL-MONEY orders on the connected MT5 account? Type LIVE: " if args.live else "Start automatic orders on this DEMO account? Type YES: "
        if input(prompt).strip() != confirmation:
            say("Automatic mode cancelled.")
            return
    connected = False
    try:
        symbols = initialize(
            require_trading=args.mode == "auto",
            live=args.live,
            account=credentials[0] if credentials else None,
            password=credentials[1] if credentials else None,
            server=credentials[2] if credentials else None,
        )
        connected = True
        account_info = mt5.account_info()
        say(
            f"Connected MT5 account {account_info.login}"
            + (f" on {account_info.server}" if getattr(account_info, "server", None) else "")
            + "; symbols: " + ", ".join(symbols.values())
        )
        if args.mode == "auto":
            alert(
                ("LIVE trading enabled" if args.live else "Demo trading enabled")
                + f"; planned risk is {RISK_PERCENT:.2f}% of balance per trade."
            )
        run(args.mode, symbols)
    except KeyboardInterrupt:
        alert("Runner stopped by Ctrl+C. Existing positions retain their broker-side SL/TP.")
    except SafetyStop as exc:
        say(f"SAFETY STOP: {exc}")
    finally:
        if connected:
            if args.mode == "auto":
                try:
                    cancel_all_breakout_orders("runner stopped")
                except (AttributeError, OSError, RuntimeError, ValueError) as exc:
                    say(
                        f"WARNING: could not verify/cancel every pending breakout order on shutdown: "
                        f"{type(exc).__name__}: {exc}. Check MT5 pending orders manually."
                    )
            disconnect_from_mt5()


if __name__ == "__main__":
    main()
