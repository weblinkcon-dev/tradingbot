"""Cost-aware, chronological out-of-sample replay for mt5_auto strategies.

Run against a connected MT5 DEMO terminal:
    python mt5_walkforward.py --start 2026-01-01T00:00:00Z \
        --end 2026-10-01T00:00:00Z --commission-per-lot 7 --slippage-points 2

The CSV must contain time_utc,currency,impact,title and cover the entire tested
date range; optional end_utc supports all-day/multi-hour events. MT5 historical
bars provide OHLC and recorded spread; calendar filtering is disabled.
Slippage and round-turn commission are explicit user inputs.
"""
import argparse
import csv
import json
import math
import os
from datetime import datetime, timezone
from typing import Any

import mt5_auto as bot

REPORT_PATH = os.path.join(bot.HERE, "mt5_auto_backtest.json")
# Hold out the final 30% of the date range for chronological validation.
OUT_OF_SAMPLE_FRACTION = 0.30
MINIMUM_HISTORY_DAYS = bot.BACKTEST_MINIMUM_HISTORY_DAYS
M1_LOOKBACK = 249
M15_LOOKBACK = 249
OUTCOME_RESOLUTION_BUFFER_SECONDS = bot.BACKTEST_OUTCOME_RESOLUTION_BUFFER_SECONDS
M1_OUTCOME_HORIZON_SECONDS = bot.SCALP_BACKTEST_OUTCOME_HORIZON_SECONDS
M15_OUTCOME_HORIZON_SECONDS = bot.M15_BACKTEST_OUTCOME_HORIZON_SECONDS
PATTERN_MIN_TRAINING_TRADES = 30


def _bar_time(bar: Any) -> int:
    return int(bar["time"])


def _bar_value(bar: Any, key: str) -> float:
    return float(bar[key])


def _spread_price(bar: Any, point: float) -> float:
    spread_points = float(bar["spread"])
    if spread_points < 0 or not math.isfinite(spread_points):
        raise ValueError("Historical bar has an invalid spread.")
    return spread_points * point


def load_calendar_csv(
    path: str,
    range_start_ts: float,
    range_end_ts: float,
) -> list[bot.CalendarEvent]:
    events: list[bot.CalendarEvent] = []
    with open(path, newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = {"time_utc", "currency", "impact", "title"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(
                "Calendar CSV requires time_utc,currency,impact,title columns."
            )
        for line_number, row in enumerate(reader, start=2):
            if (row.get("impact") or "").strip().lower() != "high":
                continue
            timestamp_text = (row.get("time_utc") or "").strip()
            try:
                timestamp = datetime.fromisoformat(
                    timestamp_text.replace("Z", "+00:00")
                )
                if timestamp.tzinfo is None:
                    raise ValueError("Calendar times must include a UTC offset.")
                timestamp_utc = timestamp.astimezone(timezone.utc).timestamp()
            except ValueError as exc:
                raise ValueError(
                    f"Invalid time_utc on calendar CSV line {line_number}: {exc}"
                ) from exc
            currency = (row.get("currency") or "").strip().upper()
            title = (row.get("title") or "").strip()
            if not currency or not title:
                raise ValueError(f"Missing currency/title on calendar CSV line {line_number}.")
            end_text = (row.get("end_utc") or "").strip()
            if end_text:
                try:
                    event_end = datetime.fromisoformat(
                        end_text.replace("Z", "+00:00")
                    )
                    if event_end.tzinfo is None:
                        raise ValueError("Calendar event end must include a UTC offset.")
                    event_end_ts = event_end.astimezone(timezone.utc).timestamp()
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid end_utc on calendar CSV line {line_number}: {exc}"
                    ) from exc
                if event_end_ts < timestamp_utc:
                    raise ValueError(f"Calendar event ends before it starts on line {line_number}.")
            else:
                event_end_ts = timestamp_utc
            if timestamp_utc <= range_end_ts and event_end_ts >= range_start_ts:
                events.append(
                    bot.CalendarEvent(timestamp_utc, event_end_ts, currency, title)
                )
    if not events:
        raise ValueError("Calendar CSV contains no high-impact events within the test range.")
    return events


def _higher_timeframe_confirms(closed_bars: list[Any], side: str) -> bool:
    if len(closed_bars) < bot.HTF_EMA_PERIOD:
        return False
    closes = [_bar_value(bar, "close") for bar in closed_bars]
    average = bot.ema(closes, bot.HTF_EMA_PERIOD)[-1]
    last_close = closes[-1]
    return last_close > average if side == "BUY" else last_close < average


def _higher_bars_at(
    bars: list[Any],
    signal_close_ts: int,
    timeframe_seconds: int,
) -> list[Any]:
    end = 0
    for index, bar in enumerate(bars):
        if _bar_time(bar) + timeframe_seconds <= signal_close_ts:
            end = index + 1
        else:
            break
    return bars[max(0, end - (bot.HTF_EMA_PERIOD + 2)):end]


def _net_profit(
    symbol: str,
    side: str,
    volume: float,
    entry: float,
    exit_price: float,
    commission_per_lot: float,
) -> float:
    order_type = bot.mt5.ORDER_TYPE_BUY if side == "BUY" else bot.mt5.ORDER_TYPE_SELL
    gross = bot.mt5.order_calc_profit(order_type, symbol, volume, entry, exit_price)
    if gross is None or not math.isfinite(float(gross)):
        raise RuntimeError(f"MT5 cannot value a historical {symbol} trade.")
    return float(gross) - commission_per_lot * volume


def _risk_volume(
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    risk_cash: float,
) -> float:
    info = bot.mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Cannot read broker contract settings for {symbol}.")
    order_type = bot.mt5.ORDER_TYPE_BUY if side == "BUY" else bot.mt5.ORDER_TYPE_SELL
    one_lot_loss = bot.mt5.order_calc_profit(order_type, symbol, 1.0, entry, stop)
    if one_lot_loss is None or one_lot_loss >= 0 or not math.isfinite(float(one_lot_loss)):
        return 0.0
    volume = bot.floor_to_step(
        min(risk_cash / abs(float(one_lot_loss)), float(info.volume_max)),
        float(info.volume_step),
    )
    return volume if volume >= float(info.volume_min) else 0.0


def _exit_trade(
    bars: list[Any],
    entry_index: int,
    side: str,
    stop: float,
    target: float,
    spread_point: float,
    slippage: float,
    max_holding_seconds: int,
) -> tuple[int, float, str] | None:
    deadline = _bar_time(bars[entry_index]) + max_holding_seconds
    for index in range(entry_index, len(bars)):
        bar = bars[index]
        if _bar_time(bar) >= deadline:
            return index, (
                stop - slippage if side == "BUY" else stop + slippage
            ), "TIME_STOP"
        spread = _spread_price(bar, spread_point)
        opening = _bar_value(bar, "open")
        high = _bar_value(bar, "high")
        low = _bar_value(bar, "low")
        if side == "BUY":
            stop_hit = low <= stop
            target_hit = high >= target
            if stop_hit and target_hit:
                return index, min(opening, stop) - slippage, "SL"
            if stop_hit:
                return index, min(opening, stop) - slippage, "SL"
            if target_hit:
                return index, max(opening, target) - slippage, "TP"
        else:
            ask_open, ask_high, ask_low = opening + spread, high + spread, low + spread
            stop_hit = ask_high >= stop
            target_hit = ask_low <= target
            if stop_hit and target_hit:
                return index, max(ask_open, stop) + slippage, "SL"
            if stop_hit:
                return index, max(ask_open, stop) + slippage, "SL"
            if target_hit:
                return index, min(ask_open, target) + slippage, "TP"
    return None


def _finish_trade(
    symbol: str,
    side: str,
    volume: float,
    entry: float,
    bars: list[Any],
    entry_index: int,
    stop: float,
    target: float,
    point: float,
    slippage_points: float,
    commission_per_lot: float,
    max_holding_seconds: int,
) -> dict[str, Any] | None:
    exit_result = _exit_trade(
        bars,
        entry_index,
        side,
        stop,
        target,
        point,
        slippage_points * point,
        max_holding_seconds,
    )
    if exit_result is None:
        return None
    exit_index, exit_price, exit_reason = exit_result
    net = _net_profit(
        symbol, side, volume, entry, exit_price, commission_per_lot
    )
    return {
        "entry_ts": _bar_time(bars[entry_index]),
        "exit_ts": _bar_time(bars[exit_index]),
        "net": net,
        "side": side,
        "exit_reason": exit_reason,
    }


def backtest_scalp(
    symbol: str,
    bars: list[Any],
    _h1_bars: list[Any],
    commission_per_lot: float,
    slippage_points: float,
    test_start_ts: float,
    test_end_ts: float,
    unresolved: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    info = bot.mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Cannot read broker contract settings for {symbol}.")
    point = float(info.point)
    completed: list[dict[str, Any]] = []
    next_available_ts = 0
    for signal_index in range(M1_LOOKBACK - 1, len(bars) - 1):
        signal_bar = bars[signal_index]
        signal_close_ts = _bar_time(signal_bar) + bot.SCALP_TIMEFRAME_SECONDS
        if signal_close_ts > test_end_ts:
            break
        if signal_close_ts < test_start_ts or signal_close_ts <= next_available_ts:
            continue
        window = bars[max(0, signal_index - M1_LOOKBACK + 1):signal_index + 1]
        signal = bot.signal_from_scalp_bars(window)
        side = signal["signal"]
        if side not in ("BUY", "SELL"):
            continue
        execution_index = signal_index + 1
        execution_bar = bars[execution_index]
        signal_spread = _spread_price(signal_bar, point)
        max_spread = (
            float(signal["atr"])
            * bot.STOP_ATR_MULTIPLIER
            * bot.SCALP_MAX_SPREAD_STOP_RATIO
        )
        if signal_spread > max_spread:
            continue
        fill_spread = _spread_price(execution_bar, point)
        if fill_spread > max_spread:
            next_available_ts = _bar_time(execution_bar) + bot.SCALP_TIMEFRAME_SECONDS
            continue
        attempts.append({
            "setup_ts": signal_close_ts,
            "symbol": symbol,
            "strategy": "m1",
        })
        opening = _bar_value(execution_bar, "open")
        trade_side = str(side)
        entry = opening + (fill_spread if trade_side == "BUY" else 0.0)
        entry += slippage_points * point * (1 if trade_side == "BUY" else -1)
        stop_distance = float(signal["atr"]) * bot.STOP_ATR_MULTIPLIER
        stop = (
            bot.normalize_price(entry - stop_distance, info, "down")
            if trade_side == "BUY"
            else bot.normalize_price(entry + stop_distance, info, "up")
        )
        volume = _risk_volume(
            symbol, trade_side, entry, stop, bot.SCALP_RISK_CASH
        )
        if volume <= 0:
            next_available_ts = _bar_time(execution_bar) + bot.SCALP_TIMEFRAME_SECONDS
            continue
        target = bot.cash_target_price(
            symbol, trade_side, entry, volume,
            bot.SCALP_TAKE_PROFIT_AMOUNT, info,
        )
        if target is None:
            next_available_ts = _bar_time(execution_bar) + bot.SCALP_TIMEFRAME_SECONDS
            continue
        result = _finish_trade(
            symbol, trade_side, volume, entry, bars, execution_index,
            stop, target, point, slippage_points, commission_per_lot,
            M1_OUTCOME_HORIZON_SECONDS,
        )
        if result is not None:
            result["setup_ts"] = signal_close_ts
            result["strategy"] = "m1"
            completed.append(result)
            next_available_ts = result["exit_ts"]
        else:
            next_available_ts = _bar_time(bars[-1]) + bot.SCALP_TIMEFRAME_SECONDS

    return completed


def backtest_m15(
    symbol: str,
    bars: list[Any],
    h4_bars: list[Any],
    commission_per_lot: float,
    slippage_points: float,
    test_start_ts: float,
    test_end_ts: float,
    unresolved: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    info = bot.mt5.symbol_info(symbol)
    account = bot.mt5.account_info()
    if info is None or account is None:
        raise RuntimeError(f"Cannot read broker account/contract settings for {symbol}.")
    if str(account.currency) != "USD":
        raise ValueError("The M15/backtest risk configuration currently requires a USD account.")
    hedging_mode = getattr(bot.mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", None)
    if (
        hedging_mode is None
        or getattr(account, "margin_mode", None) != hedging_mode
    ):
        return []
    point = float(info.point)
    completed: list[dict[str, Any]] = []
    next_available_ts = 0
    balance = float(account.balance)
    for signal_index in range(M15_LOOKBACK - 1, len(bars) - 1):
        signal_bar = bars[signal_index]
        signal_close_ts = _bar_time(signal_bar) + bot.TIMEFRAME_SECONDS
        if signal_close_ts > test_end_ts:
            break
        if signal_close_ts < test_start_ts or signal_close_ts <= next_available_ts:
            continue
        window = bars[max(0, signal_index - M15_LOOKBACK + 1):signal_index + 1]
        signal = bot.signal_from_m15_bars(window)
        side = signal["signal"]
        if side not in ("BUY", "SELL"):
            continue
        signal_spread = _spread_price(signal_bar, point)
        if signal_spread > float(signal["atr"]) * bot.MAX_SPREAD_ATR_RATIO:
            continue
        h4_window = _higher_bars_at(
            h4_bars, signal_close_ts, 4 * 60 * 60
        )
        if not _higher_timeframe_confirms(h4_window, side):
            continue
        execution_index = signal_index + 1
        execution_bar = bars[execution_index]
        spread = _spread_price(execution_bar, point)
        if spread > float(signal["atr"]) * bot.MAX_SPREAD_ATR_RATIO:
            continue
        opening = _bar_value(execution_bar, "open")
        raw_entry = opening + spread if side == "BUY" else opening
        entry = raw_entry + slippage_points * point * (1 if side == "BUY" else -1)
        atr_value = float(signal["atr"])
        stop_distance = atr_value * bot.STOP_ATR_MULTIPLIER
        stop = (
            bot.normalize_price(entry - stop_distance, info, "down")
            if side == "BUY"
            else bot.normalize_price(entry + stop_distance, info, "up")
        )
        target = (
            bot.normalize_price(
                entry + stop_distance * bot.RISK_REWARD_RATIO, info, "up"
            )
            if side == "BUY"
            else bot.normalize_price(
                entry - stop_distance * bot.RISK_REWARD_RATIO, info, "down"
            )
        )
        volume = _risk_volume(
            symbol, side, entry, stop,
            balance * bot.RISK_PERCENT / 100.0,
        )
        if volume <= 0:
            continue
        attempts.append({
            "setup_ts": signal_close_ts,
            "symbol": symbol,
            "strategy": "m15",
        })
        result = _finish_trade(
            symbol, side, volume, entry, bars, execution_index,
            stop, target, point, slippage_points, commission_per_lot,
            M15_OUTCOME_HORIZON_SECONDS,
        )
        if result is not None:
            result["setup_ts"] = signal_close_ts
            result["strategy"] = "m15"
            completed.append(result)
            balance += result["net"]
            next_available_ts = result["exit_ts"]
        else:
            unresolved.append({
                "setup_ts": signal_close_ts,
                "entry_ts": _bar_time(execution_bar),
                "symbol": symbol,
                "strategy": "m15",
                "side": side,
            })
            next_available_ts = _bar_time(bars[-1]) + bot.TIMEFRAME_SECONDS
    return completed


def _metrics(trades: list[dict[str, Any]], test_start_ts: float) -> dict[str, Any]:
    out_of_sample = [trade for trade in trades if trade["entry_ts"] >= test_start_ts]
    net_results = [float(trade["net"]) for trade in out_of_sample]
    total = sum(net_results)
    return {
        "out_of_sample_trades": len(out_of_sample),
        "net_profit": round(total, 2),
        "net_expectancy": round(total / len(net_results), 4) if net_results else 0.0,
        "wins": sum(result > 0 for result in net_results),
        "losses": sum(result <= 0 for result in net_results),
        "tp_first": sum(trade["exit_reason"] == "TP" for trade in out_of_sample),
        "sl_first": sum(trade["exit_reason"] == "SL" for trade in out_of_sample),
        "time_stop": sum(trade["exit_reason"] == "TIME_STOP" for trade in out_of_sample),
        "tp_before_stop_rate": (
            sum(trade["exit_reason"] == "TP" for trade in out_of_sample)
            / len(out_of_sample)
            if out_of_sample else 0.0
        ),
        "profitable": len(net_results) >= bot.VALIDATION_MIN_TRADES and total > 0,
    }


def _filter_portfolio_capacity(
    trades: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    grouped: dict[tuple[str, str, float], list[dict[str, Any]]] = {}
    for trade in trades:
        key = (trade["strategy"], trade["symbol"], float(trade["setup_ts"]))
        grouped.setdefault(key, []).append(trade)
    unresolved_groups: dict[tuple[str, str, float], list[dict[str, Any]]] = {}
    for position in unresolved:
        key = (position["strategy"], position["symbol"], float(position["setup_ts"]))
        unresolved_groups.setdefault(key, []).append(position)
    attempt_groups: dict[tuple[str, str, float], dict[str, Any]] = {}
    for attempt in attempts:
        key = (attempt["strategy"], attempt["symbol"], float(attempt["setup_ts"]))
        attempt_groups[key] = attempt

    accepted: list[dict[str, Any]] = []
    accepted_unresolved: list[dict[str, Any]] = []
    daily_counts: dict[tuple[str, str], int] = {}
    rejected_setups = 0
    for strategy, symbol, setup_ts in sorted(
        attempt_groups, key=lambda item: item[2]
    ):
        key = (strategy, symbol, setup_ts)
        candidates = grouped.get(key, [])
        unresolved_candidates = unresolved_groups.get(key, [])
        day = datetime.fromtimestamp(setup_ts, timezone.utc).date().isoformat()
        daily_key = (strategy, day)
        daily_limit = (
            bot.MAX_TRADES_PER_DAY if strategy == "m1"
            else bot.MAX_M15_TRADES_PER_DAY
        )
        if daily_counts.get(daily_key, 0) >= daily_limit:
            rejected_setups += 1
            continue

        active_positions = sum(
            1 for trade in accepted
            if trade["entry_ts"] <= setup_ts < trade["exit_ts"]
        )
        active_positions += sum(
            1 for position in accepted_unresolved
            if position["entry_ts"] <= setup_ts
        )
        requested_slots = 1
        if active_positions + requested_slots > bot.MAX_OPEN_POSITIONS:
            rejected_setups += 1
            continue

        accepted.extend(candidates)
        accepted_unresolved.extend(unresolved_candidates)
        daily_counts[daily_key] = daily_counts.get(daily_key, 0) + 1
    return accepted, accepted_unresolved, rejected_setups


def strategy_fingerprint() -> str:
    return bot.strategy_fingerprint()


def run_backtest(
    symbols: dict[str, str],
    start: datetime,
    end: datetime,
    commission_per_lot: float,
    slippage_points: float,
) -> dict[str, Any]:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("Backtest start/end must be ordered timezone-aware datetimes.")
    if (
        not math.isfinite(commission_per_lot)
        or not math.isfinite(slippage_points)
        or commission_per_lot < 0
        or slippage_points < 0
    ):
        raise ValueError("Commission and slippage inputs must be finite and non-negative.")
    if (end - start).days < MINIMUM_HISTORY_DAYS:
        raise ValueError(f"Backtest history must cover at least {MINIMUM_HISTORY_DAYS} days.")
    mt5 = bot.mt5
    account = mt5.account_info()
    if account is None or account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise RuntimeError("Historical backtests must run against a connected DEMO terminal.")
    if str(account.currency) != "USD":
        raise ValueError("The scalp target/risk configuration requires a USD account.")
    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    all_data: dict[str, dict[str, list[Any]]] = {}
    for symbol in symbols.values():
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"Cannot read symbol info for {symbol}: {mt5.last_error()}")
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Cannot select {symbol} in Market Watch: {mt5.last_error()}")
        rates: dict[str, list[Any]] = {}
        for key, timeframe, seconds in (
            ("m1", mt5.TIMEFRAME_M1, bot.SCALP_TIMEFRAME_SECONDS),
            ("m15", mt5.TIMEFRAME_M15, bot.TIMEFRAME_SECONDS),
            ("h4", mt5.TIMEFRAME_H4, 4 * 60 * 60),
        ):
            raw = mt5.copy_rates_range(symbol, timeframe, start_utc, end_utc)
            if raw is None or len(raw) == 0:
                raise RuntimeError(
                    f"No historical {key.upper()} rates for {symbol}: {mt5.last_error()}"
                )
            rates[key] = [
                bar for bar in raw
                if _bar_time(bar) + seconds <= end_utc.timestamp()
                and _bar_time(bar) + seconds <= datetime.now(timezone.utc).timestamp()
            ]
            if not rates[key]:
                raise RuntimeError(f"No completed historical {key.upper()} rates for {symbol}.")
        all_data[symbol] = rates

    earliest = max(
        _bar_time(all_data[symbol]["m1"][0]) for symbol in symbols.values()
    )
    latest = min(
        _bar_time(all_data[symbol]["m1"][-1]) for symbol in symbols.values()
    )
    if latest - earliest < MINIMUM_HISTORY_DAYS * 24 * 60 * 60:
        raise ValueError("Broker history does not cover the required 180-day test window.")
    split_ts = earliest + (latest - earliest) * (1.0 - OUT_OF_SAMPLE_FRACTION)
    test_end_ts = latest + bot.SCALP_TIMEFRAME_SECONDS - OUTCOME_RESOLUTION_BUFFER_SECONDS
    if test_end_ts <= split_ts:
        raise ValueError("Insufficient chronological holdout data after reserving the outcome-resolution buffer.")
    unresolved: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []

    summary: dict[str, Any] = {
        "strategy_version": bot.STRATEGY_VERSION,
        "strategy_fingerprint": strategy_fingerprint(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "history_start_utc": datetime.fromtimestamp(earliest, timezone.utc).isoformat(),
        "history_end_utc": datetime.fromtimestamp(latest, timezone.utc).isoformat(),
        "out_of_sample_start_utc": datetime.fromtimestamp(split_ts, timezone.utc).isoformat(),
        "out_of_sample_end_utc": datetime.fromtimestamp(test_end_ts, timezone.utc).isoformat(),
        "calendar_filter": "disabled; calendar is informational only",
        "unresolved_open_positions": 0,
        "cost_model": {
            "spread": "MT5 historical bar spread field",
            "commission_per_lot_round_turn": commission_per_lot,
            "slippage_points_per_side": slippage_points,
        },
        "execution_model": {
            "ohlc_replay": True,
            "same_bar_stop_and_target": "stop first",
            "m1_market_entry": "next M1 bar open; spread and adverse slippage applied",
            "m1_time_stop_bars": bot.SCALP_MAX_HOLD_BARS,
            "time_horizon": {
                "m1_seconds": M1_OUTCOME_HORIZON_SECONDS,
                "m15_seconds": M15_OUTCOME_HORIZON_SECONDS,
                "timeout_result": "full stop loss",
            },
            "unfilled_market_entries": "not included in resolved-trade expectancy",
            "portfolio_cap": bot.MAX_OPEN_POSITIONS,
        },
        "strategies": {"m1": {}, "m15": {}},
    }
    candidates: list[dict[str, Any]] = []
    for label, symbol in symbols.items():
        data = all_data[symbol]
        for key, result_key, func, htf_key in (
            ("m1", "m1", backtest_scalp, None),
            ("m15", "m15", backtest_m15, "h4"),
        ):
            trades = func(
                symbol,
                data[key],
                data[htf_key] if htf_key is not None else [],
                commission_per_lot,
                slippage_points,
                split_ts,
                test_end_ts,
                unresolved,
                attempts,
            )
            for trade in trades:
                trade["symbol"] = symbol
                trade["label"] = label
                candidates.append(trade)

    accepted, accepted_unresolved, capacity_rejected = _filter_portfolio_capacity(
        candidates, attempts, unresolved
    )
    accepted_unresolved_keys = {
        (item["strategy"], item["symbol"], item["setup_ts"])
        for item in accepted_unresolved
    }
    summary["unresolved_open_positions"] = len(accepted_unresolved)
    filled_keys = {
        (item["strategy"], item["symbol"], item["setup_ts"])
        for item in accepted
    } | accepted_unresolved_keys
    summary["unfilled_market_entries"] = sum(
        1 for item in attempts
        if (item["strategy"], item["symbol"], item["setup_ts"]) not in filled_keys
    )
    summary["capacity_rejected_setups"] = capacity_rejected
    for result_key in ("m1", "m15"):
        for label in symbols:
            selected = [
                trade for trade in accepted
                if trade["strategy"] == result_key and trade["label"] == label
            ]
            summary["strategies"][result_key][label] = _metrics(selected, split_ts)
    summary["strategies"]["m1"]["aggregate"] = _aggregate(
        summary["strategies"]["m1"]
    )
    summary["strategies"]["m15"]["aggregate"] = _aggregate(
        summary["strategies"]["m15"]
    )
    summary["pattern_review"] = _pattern_review(
        candidates, attempts, unresolved, accepted, split_ts
    )
    summary["unresolved_open_positions"] = len(unresolved)
    summary["auto_gate_passed"] = (
        not accepted_unresolved
        and all(
            summary["strategies"][name]["aggregate"]["profitable"]
            for name in ("m1", "m15")
        )
    )
    return summary


def _aggregate(results: dict[str, Any]) -> dict[str, Any]:
    count = sum(item["out_of_sample_trades"] for item in results.values())
    profit = sum(item["net_profit"] for item in results.values())
    tp_first = sum(item["tp_first"] for item in results.values())
    return {
        "out_of_sample_trades": count,
        "net_profit": round(profit, 2),
        "net_expectancy": round(profit / count, 4) if count else 0.0,
        "tp_first": tp_first,
        "tp_before_stop_rate": tp_first / count if count else 0.0,
        "profitable": count >= bot.VALIDATION_MIN_TRADES and profit > 0,
    }


def _pattern_review(
    candidates: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    split_ts: float,
) -> dict[str, Any]:
    """Test training-selected symbol exclusions on holdout data; never changes live rules."""
    review: dict[str, Any] = {
        "live_settings_changed": False,
        "selection": "negative in-sample net expectancy by strategy and symbol",
        "minimum_training_trades": PATTERN_MIN_TRAINING_TRADES,
        "minimum_holdout_trades": bot.VALIDATION_MIN_TRADES,
        "candidates": [],
    }
    groups = sorted({
        (trade["strategy"], trade["symbol"])
        for trade in accepted
        if trade["entry_ts"] < split_ts
    })
    for strategy, symbol in groups:
        training = [
            trade for trade in accepted
            if trade["strategy"] == strategy
            and trade["symbol"] == symbol
            and trade["entry_ts"] < split_ts
        ]
        training_expectancy = sum(float(trade["net"]) for trade in training) / len(training)
        if len(training) < PATTERN_MIN_TRAINING_TRADES or training_expectancy >= 0:
            continue

        candidate_trades = [
            trade for trade in candidates
            if (trade["strategy"], trade["symbol"]) != (strategy, symbol)
        ]
        candidate_attempts = [
            attempt for attempt in attempts
            if (attempt["strategy"], attempt["symbol"]) != (strategy, symbol)
        ]
        candidate_unresolved = [
            position for position in unresolved
            if (position["strategy"], position["symbol"]) != (strategy, symbol)
        ]
        candidate_accepted, candidate_open, _ = _filter_portfolio_capacity(
            candidate_trades, candidate_attempts, candidate_unresolved
        )
        baseline_metrics = _metrics(
            [trade for trade in accepted if trade["strategy"] == strategy], split_ts
        )
        candidate_metrics = _metrics(
            [trade for trade in candidate_accepted if trade["strategy"] == strategy],
            split_ts,
        )
        supported = (
            not candidate_open
            and candidate_metrics["out_of_sample_trades"] >= bot.VALIDATION_MIN_TRADES
            and candidate_metrics["profitable"]
            and candidate_metrics["net_expectancy"] > baseline_metrics["net_expectancy"]
        )
        review["candidates"].append({
            "strategy": strategy,
            "symbol": symbol,
            "change_tested": f"exclude {symbol} from {strategy.upper()} entries",
            "training_trades": len(training),
            "training_net_expectancy": round(training_expectancy, 4),
            "baseline_holdout": {
                "trades": baseline_metrics["out_of_sample_trades"],
                "net_expectancy": baseline_metrics["net_expectancy"],
            },
            "candidate_holdout": {
                "trades": candidate_metrics["out_of_sample_trades"],
                "net_expectancy": candidate_metrics["net_expectancy"],
                "profitable": candidate_metrics["profitable"],
            },
            "supported_by_holdout": supported,
            "recommendation": (
                "Promising candidate; require a fresh independent backtest and demo run before any live change."
                if supported else
                "Not supported by the holdout; do not adopt this change."
            ),
        })
    return review


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Date inputs must include a timezone offset.")
    return parsed.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5 cost-aware out-of-sample walk-forward test.")
    parser.add_argument("--start", type=_parse_utc, required=True, help="UTC start, e.g. 2026-01-01T00:00:00Z")
    parser.add_argument("--end", type=_parse_utc, required=True, help="UTC end, e.g. 2026-10-01T00:00:00Z")
    parser.add_argument("--commission-per-lot", type=float, required=True, help="Round-turn commission in account currency per lot.")
    parser.add_argument("--slippage-points", type=float, required=True, help="Adverse slippage per side in broker points.")
    args = parser.parse_args()
    connected = False
    try:
        symbols = bot.initialize(require_trading=False, calibrate_clock=False)
        connected = True
        report = run_backtest(
            symbols, args.start, args.end,
            args.commission_per_lot, args.slippage_points,
        )
        temporary_report = REPORT_PATH + ".tmp"
        with open(temporary_report, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_report, REPORT_PATH)
        print(json.dumps(report, indent=2))
        print(f"Report saved to {REPORT_PATH}")
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Backtest failed; no gate report written: {exc}\n")
    finally:
        if connected:
            bot.trader.disconnect()


if __name__ == "__main__":
    main()
