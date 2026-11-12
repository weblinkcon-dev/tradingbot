import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mt5_auto as bot
import mt5_walkforward as walkforward


class CalendarFilterTests(unittest.TestCase):
    def test_high_impact_calendar_event_is_converted_from_eastern_time(self):
        payload = b"""<weeklyevents>
        <event><title>Inflation report</title><country>USD</country>
        <date>10-14-2026</date><time>8:30am</time><impact>High</impact></event>
        <event><title>Minor release</title><country>USD</country>
        <date>10-14-2026</date><time>9:00am</time><impact>Low</impact></event>
        </weeklyevents>"""

        events = bot._parse_forexfactory_calendar(payload)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].currency, "USD")
        self.assertEqual(
            datetime.fromtimestamp(events[0].start_ts, timezone.utc),
            datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc),
        )

    def test_all_day_events_respect_dst_transition(self):
        payload = b"""<weeklyevents><event>
        <title>Scheduled release</title><country>USD</country>
        <date>11-01-2026</date><time>All Day Event</time><impact>High</impact>
        </event></weeklyevents>"""

        event = bot._parse_forexfactory_calendar(payload)[0]

        self.assertEqual(event.end_ts - event.start_ts, 25 * 60 * 60)

    def test_calendar_feed_failure_is_advisory_not_an_entry_block(self):
        event = bot.CalendarEvent(1000.0, 1000.0, "USD", "Rate decision")
        state = {
            "events": [event],
            "last_ok": 100.0,
            "next": 0.0,
            "ok": True,
        }
        with (
            patch.object(bot, "CALENDAR_STATE", state),
            patch.object(bot.time, "monotonic", return_value=200.0),
            patch.object(bot, "fetch_calendar_events", side_effect=OSError("offline")),
            patch.object(bot, "say") as say,
        ):
            bot.poll_calendar()

        self.assertFalse(state["ok"])
        self.assertIn("entry trading continues", say.call_args.args[0])


class HigherTimeframeFilterTests(unittest.TestCase):
    def test_only_allows_entry_on_correct_side_of_closed_ema(self):
        rising = [{"close": float(index)} for index in range(bot.HTF_EMA_PERIOD + 3)]
        falling = [{"close": float(100 - index)} for index in range(bot.HTF_EMA_PERIOD + 3)]
        fake = SimpleNamespace(
            copy_rates_from_pos=lambda _symbol, timeframe, _start, _count: (
                rising if timeframe == 1 else falling
            ),
            last_error=lambda: (1, "Success"),
        )
        with patch.object(bot, "mt5", fake):
            self.assertIsNone(bot.higher_timeframe_entry_block("EURUSD", "BUY", 1))
            self.assertIsNotNone(bot.higher_timeframe_entry_block("EURUSD", "SELL", 1))
            self.assertIsNone(bot.higher_timeframe_entry_block("EURUSD", "SELL", 2))

    def test_unavailable_timeframe_fails_closed(self):
        self.assertIsNotNone(bot.higher_timeframe_entry_block("EURUSD", "BUY", None))

    def test_live_mode_requires_verified_real_account_and_exact_confirmation(self):
        fake = SimpleNamespace(ACCOUNT_TRADE_MODE_REAL=1)
        real_account = SimpleNamespace(
            trade_mode=1,
            login=123,
            server="RealBroker",
            balance=1000.0,
            currency="USD",
        )
        with patch.object(bot, "mt5", fake):
            self.assertTrue(
                bot.confirm_live_trading(real_account, lambda _prompt: "YES LIVE")
            )
            self.assertFalse(
                bot.confirm_live_trading(real_account, lambda _prompt: "YES")
            )
            with self.assertRaises(bot.SafetyStop):
                bot.confirm_live_trading(
                    SimpleNamespace(trade_mode=0), lambda _prompt: "YES LIVE"
                )

    def test_auto_account_selector_maps_demo_and_real_choices(self):
        self.assertEqual(bot.choose_auto_account(lambda _prompt: "1"), "auto")
        self.assertEqual(bot.choose_auto_account(lambda _prompt: "2"), "live")
        self.assertIsNone(bot.choose_auto_account(lambda _prompt: "3"))

    def test_real_login_authenticates_and_verifies_the_configured_account(self):
        demo_account = SimpleNamespace(login=113565765, trade_mode=0)
        real_account = SimpleNamespace(
            login=bot.REAL_ACCOUNT_LOGIN,
            trade_mode=1,
        )
        login_arguments = {}
        fake = SimpleNamespace(
            ACCOUNT_TRADE_MODE_REAL=1,
            login=lambda login, **kwargs: login_arguments.update(
                login=login, **kwargs
            ) or True,
            account_info=lambda: real_account,
            last_error=lambda: (1, "Success"),
        )

        with patch.object(bot, "mt5", fake):
            result = bot.login_to_real_account(
                demo_account,
                lambda _prompt: "Broker-Live",
                lambda _prompt: "test-password",
            )

        self.assertIs(result, real_account)
        self.assertEqual(login_arguments["login"], bot.REAL_ACCOUNT_LOGIN)
        self.assertEqual(login_arguments["server"], "Broker-Live")
        self.assertEqual(login_arguments["password"], "test-password")

    def test_real_login_refuses_a_different_account_after_authentication(self):
        demo_account = SimpleNamespace(login=113565765, trade_mode=0)
        wrong_account = SimpleNamespace(login=123456, trade_mode=1)
        fake = SimpleNamespace(
            ACCOUNT_TRADE_MODE_REAL=1,
            login=lambda _login, **_kwargs: True,
            account_info=lambda: wrong_account,
            last_error=lambda: (1, "Success"),
        )

        with (
            patch.object(bot, "mt5", fake),
            self.assertRaisesRegex(bot.SafetyStop, "did not verify real account"),
        ):
            bot.login_to_real_account(
                demo_account,
                lambda _prompt: "Broker-Live",
                lambda _prompt: "test-password",
            )

    def test_automatic_startup_routes_through_account_picker_and_confirms_demo(self):
        with patch.object(bot, "choose_auto_account", return_value="auto"):
            self.assertEqual(
                bot._confirm_startup_mode("auto", lambda _prompt: "YES"),
                "auto",
            )
        with patch.object(bot, "choose_auto_account", return_value="live"):
            confirmer = unittest.mock.Mock()
            self.assertEqual(bot._confirm_startup_mode("live", confirmer), "live")
        confirmer.assert_not_called()


class ScalpSignalTests(unittest.TestCase):
    def test_scalp_cash_target_matches_reference_reward_risk_ratio(self):
        self.assertEqual(bot.SCALP_REWARD_RISK_RATIO, 0.8)
        self.assertEqual(bot.SCALP_TIMEFRAME_SECONDS, 60)
        self.assertEqual(bot.SCALP_RISK_CASH, 1.0)
        self.assertEqual(bot.SCALP_MAX_HOLD_BARS, 20)
        self.assertEqual(bot.SCALP_MAX_SPREAD_STOP_RATIO, 0.2)
        self.assertEqual(bot.MAX_TRADES_PER_DAY, 40)
        self.assertEqual(bot.DAILY_LOSS_LIMIT_CASH, 5.0)
        self.assertEqual(
            bot.SCALP_TAKE_PROFIT_AMOUNT,
            bot.SCALP_RISK_CASH * bot.SCALP_REWARD_RISK_RATIO,
        )

    def test_scalp_spread_limit_is_fraction_of_stop_distance(self):
        tick = SimpleNamespace(bid=1.0, ask=1.0039)
        entry, spread, reason = bot._resolve_order_entry(
            "EURUSD",
            "BUY",
            0.01,
            tick,
            None,
            None,
            bot.SCALP_MAX_SPREAD_STOP_RATIO * bot.STOP_ATR_MULTIPLIER,
        )
        self.assertIsNone(reason)
        self.assertEqual(entry, tick.ask)
        self.assertAlmostEqual(spread, 0.0039)

        tick.ask = 1.0041
        _entry, _spread, reason = bot._resolve_order_entry(
            "EURUSD",
            "BUY",
            0.01,
            tick,
            None,
            None,
            bot.SCALP_MAX_SPREAD_STOP_RATIO * bot.STOP_ATR_MULTIPLIER,
        )
        self.assertIn("spread", reason)

    @staticmethod
    def _bars(open_price, high, low, close):
        bars = [
            {
                "time": index * bot.SCALP_TIMEFRAME_SECONDS,
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "close": 99.5,
            }
            for index in range(bot.SCALP_DIRECTION_EMA + 2)
        ]
        bars[-1].update(
            open=open_price,
            high=high,
            low=low,
            close=close,
        )
        return bars

    def test_buy_setup_accepts_relaxed_pullback_rsi_and_adx_boundaries(self):
        bars = self._bars(100.0, 100.2, 100.1, 100.1)

        def trend_ema(values, period):
            value = {20: 100.0, 50: 99.0, 200: 98.0}[period]
            return [value] * len(values)

        with (
            patch.object(bot, "ema", side_effect=trend_ema),
            patch.object(bot, "rsi", return_value=45.0),
            patch.object(bot, "adx", return_value=bot.SCALP_MIN_ADX),
            patch.object(bot, "atr", return_value=1.0),
        ):
            result = bot.signal_from_scalp_bars(bars)

        self.assertEqual(result["signal"], "BUY")

    def test_sell_setup_accepts_relaxed_pullback_rsi_and_adx_boundaries(self):
        bars = self._bars(100.0, 100.0, 99.8, 99.9)

        def trend_ema(values, period):
            value = {20: 100.0, 50: 101.0, 200: 102.0}[period]
            return [value] * len(values)

        with (
            patch.object(bot, "ema", side_effect=trend_ema),
            patch.object(bot, "rsi", return_value=55.0),
            patch.object(bot, "adx", return_value=bot.SCALP_MIN_ADX),
            patch.object(bot, "atr", return_value=1.0),
        ):
            result = bot.signal_from_scalp_bars(bars)

        self.assertEqual(result["signal"], "SELL")

    def test_ready_scalp_signal_places_one_directional_market_order(self):
        tick = SimpleNamespace(time=1, bid=1.0, ask=1.0001)
        signal = {
            "signal": "SELL",
            "bar_time": 0,
            "close": 1.0,
            "atr": 0.01,
            "rsi": 50.0,
            "adx": 18.0,
        }
        state = {"trades_today": 0}
        with (
            patch.object(bot, "mt5", SimpleNamespace(symbol_info_tick=lambda _symbol: tick)),
            patch.object(bot, "tick_age_seconds", return_value=0),
            patch.object(bot, "backtest_entry_block", return_value=None) as backtest_check,
            patch.object(bot, "higher_timeframe_entry_block") as higher_timeframe_check,
            patch.object(bot, "can_open_scalp", return_value=True),
            patch.object(bot, "order_trade") as order_trade,
            patch.object(bot, "log_trade"),
            patch.object(bot, "say"),
        ):
            bot._handle_ready_scalp_signal("EURUSD", "EURUSD", signal, state, True)

        order_trade.assert_called_once_with(
            "EURUSD",
            "SELL",
            0.01,
            state,
            magic=bot.SCALP_MAGIC,
            cash_take_profit=bot.SCALP_TAKE_PROFIT_AMOUNT,
            risk_cash=bot.SCALP_RISK_CASH,
        )
        backtest_check.assert_called_once_with("m1")
        higher_timeframe_check.assert_not_called()

    def test_scalp_signal_still_requires_a_matching_backtest(self):
        tick = SimpleNamespace(time=1, bid=1.0, ask=1.0001)
        signal = {
            "signal": "BUY",
            "bar_time": 0,
            "close": 1.0,
            "atr": 0.01,
            "rsi": 50.0,
            "adx": 18.0,
        }
        with (
            patch.object(bot, "mt5", SimpleNamespace(symbol_info_tick=lambda _symbol: tick)),
            patch.object(bot, "tick_age_seconds", return_value=0),
            patch.object(bot, "backtest_entry_block", return_value="backtest missing"),
            patch.object(bot, "order_trade") as order_trade,
            patch.object(bot, "say"),
        ):
            bot._handle_ready_scalp_signal(
                "EURUSD", "EURUSD", signal, {"trades_today": 0}, True
            )

        order_trade.assert_not_called()

    def test_scalp_position_is_closed_at_maximum_holding_time(self):
        position = SimpleNamespace(time=100, ticket=1, symbol="EURUSD")
        with (
            patch.object(bot, "broker_now_epoch", return_value=100 + bot.SCALP_BACKTEST_OUTCOME_HORIZON_SECONDS),
            patch.object(bot, "close_managed_position") as close_position,
        ):
            self.assertTrue(bot._update_scalp_target(position))

        close_position.assert_called_once_with(
            position, "scalp maximum holding time reached"
        )


class EntryGateTests(unittest.TestCase):
    def test_daily_risk_state_is_scoped_to_account_login_and_server(self):
        first_account = SimpleNamespace(
            login=100,
            server="Demo-A",
            equity=10_000.0,
        )
        second_account = SimpleNamespace(
            login=200,
            server="Real-B",
            equity=2_000.0,
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(bot, "STATE_FILE", str(Path(directory) / "state.json")),
            patch.object(bot, "say"),
        ):
            first_state = bot.daily_state(first_account)
            first_state["trades_today"] = 5
            bot.save_state(first_state)
            second_state = bot.daily_state(second_account)

        self.assertEqual(second_state["account_login"], "200")
        self.assertEqual(second_state["account_server"], "Real-B")
        self.assertEqual(second_state["start_equity"], 2_000.0)
        self.assertEqual(second_state["trades_today"], 0)


class WalkForwardTests(unittest.TestCase):
    @staticmethod
    def _attempts(trades):
        seen = set()
        attempts = []
        for trade in trades:
            key = (trade["strategy"], trade["symbol"], trade["setup_ts"])
            if key not in seen:
                attempts.append({
                    "strategy": trade["strategy"],
                    "symbol": trade["symbol"],
                    "setup_ts": trade["setup_ts"],
                })
                seen.add(key)
        return attempts

    def test_scalp_backtest_models_next_bar_directional_market_entry_and_time_stop(self):
        bars = [
            {
                "time": index * bot.SCALP_TIMEFRAME_SECONDS,
                "open": 1.0,
                "high": 1.01,
                "low": 0.99,
                "close": 1.0,
                "spread": 1,
            }
            for index in range(260)
        ]
        signal_close = 250 * bot.SCALP_TIMEFRAME_SECONDS
        fake_mt5 = SimpleNamespace(
            symbol_info=lambda _symbol: SimpleNamespace(point=0.00001)
        )
        signal = {"signal": "BUY", "atr": 0.01}
        with (
            patch.object(bot, "mt5", fake_mt5),
            patch.object(bot, "signal_from_scalp_bars", return_value=signal),
            patch.object(walkforward, "_higher_timeframe_confirms", return_value=True),
            patch.object(walkforward, "_spread_price", return_value=0.0001),
            patch.object(bot, "normalize_price", side_effect=lambda price, *_args: price),
            patch.object(walkforward, "_risk_volume", return_value=0.1),
            patch.object(bot, "cash_target_price", return_value=1.01),
            patch.object(
                walkforward,
                "_finish_trade",
                return_value={
                    "entry_ts": bars[250]["time"],
                    "exit_ts": bars[251]["time"],
                    "net": 1.0,
                },
            ) as finish_trade,
        ):
            attempts = []
            trades = walkforward.backtest_scalp(
                "EURUSD",
                bars,
                [],
                7.0,
                2.0,
                signal_close,
                signal_close,
                [],
                attempts,
            )

        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(trades), 1)
        self.assertEqual(finish_trade.call_args.args[1], "BUY")
        self.assertAlmostEqual(finish_trade.call_args.args[3], 1.00012)
        self.assertEqual(
            finish_trade.call_args.args[-1],
            bot.SCALP_MAX_HOLD_BARS * bot.SCALP_TIMEFRAME_SECONDS,
        )

    def test_same_candle_stop_and_target_is_resolved_as_stop(self):
        bars = [{
            "time": 100,
            "open": 100.0,
            "high": 112.0,
            "low": 88.0,
            "spread": 2,
        }]

        result = walkforward._exit_trade(
            bars,
            0,
            "BUY",
            stop=90.0,
            target=110.0,
            spread_point=0.01,
            slippage=0.05,
            max_holding_seconds=3600,
        )

        self.assertEqual(result, (0, 89.95, "SL"))

    def test_trade_that_never_hits_tp_or_sl_is_counted_as_stop_at_horizon(self):
        bars = [
            {"time": 100, "open": 100.0, "high": 105.0, "low": 95.0, "spread": 1},
            {"time": 200, "open": 100.0, "high": 105.0, "low": 95.0, "spread": 1},
        ]

        result = walkforward._exit_trade(
            bars,
            0,
            "BUY",
            stop=90.0,
            target=110.0,
            spread_point=0.01,
            slippage=0.05,
            max_holding_seconds=100,
        )

        self.assertEqual(result, (1, 89.95, "TIME_STOP"))

    def test_calendar_csv_requires_timezone_and_reads_only_high_impact(self):
        with tempfile.TemporaryDirectory() as directory:
            calendar_file = Path(directory) / "events.csv"
            calendar_file.write_text(
                "time_utc,currency,impact,title\n"
                "2026-10-16T12:00:00Z,USD,high,Rate decision\n"
                "2026-10-16T13:00:00Z,EUR,low,Minor release\n",
                encoding="utf-8",
            )
            events = walkforward.load_calendar_csv(
                str(calendar_file),
                datetime(2026, 10, 16, tzinfo=timezone.utc).timestamp(),
                datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp(),
            )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].currency, "USD")

    def test_backtest_and_live_strategy_fingerprints_match(self):
        self.assertEqual(walkforward.strategy_fingerprint(), bot.strategy_fingerprint())

    def test_pattern_review_tests_training_exclusion_on_holdout_without_changing_live_rules(self):
        day_seconds = 24 * 60 * 60
        trades = []
        for day in range(30):
            trades.append({
                "strategy": "m15",
                "symbol": "EURUSD",
                "setup_ts": day * day_seconds,
                "entry_ts": day * day_seconds,
                "exit_ts": day * day_seconds + 60,
                "net": -1.0,
                "exit_reason": "SL",
            })
        for day in range(100):
            timestamp = (100 + day) * day_seconds
            trades.append({
                "strategy": "m15",
                "symbol": "EURUSD",
                "setup_ts": timestamp,
                "entry_ts": timestamp,
                "exit_ts": timestamp + 60,
                "net": -2.0,
                "exit_reason": "SL",
            })
            timestamp = (200 + day) * day_seconds
            trades.append({
                "strategy": "m15",
                "symbol": "GBPUSD",
                "setup_ts": timestamp,
                "entry_ts": timestamp,
                "exit_ts": timestamp + 60,
                "net": 2.5,
                "exit_reason": "TP",
            })
        attempts = self._attempts(trades)
        accepted, _unresolved, _rejected = walkforward._filter_portfolio_capacity(
            trades, attempts, []
        )

        review = walkforward._pattern_review(
            trades, attempts, [], accepted, 100 * day_seconds
        )

        self.assertFalse(review["live_settings_changed"])
        self.assertEqual(len(review["candidates"]), 1)
        candidate = review["candidates"][0]
        self.assertEqual(candidate["symbol"], "EURUSD")
        self.assertEqual(candidate["training_trades"], 30)
        self.assertEqual(candidate["baseline_holdout"]["trades"], 200)
        self.assertEqual(candidate["candidate_holdout"]["trades"], 100)
        self.assertTrue(candidate["supported_by_holdout"])

    def test_both_backtest_engines_run_shared_signal_calculators(self):
        bars = [
            {
                "time": index * 300,
                "open": 1.0,
                "high": 1.01,
                "low": 0.99,
                "close": 1.0,
                "spread": 1,
            }
            for index in range(260)
        ]
        fake = SimpleNamespace(
            symbol_info=lambda _symbol: SimpleNamespace(point=0.00001),
            account_info=lambda: SimpleNamespace(
                currency="USD",
                balance=10_000.0,
                margin_mode=2,
            ),
            ACCOUNT_MARGIN_MODE_RETAIL_HEDGING=2,
        )
        with patch.object(bot, "mt5", fake):
            self.assertEqual(
                walkforward.backtest_scalp(
                    "EURUSD", bars, bars, 7.0, 2.0, 0.0, 100_000.0, [], []
                ),
                [],
            )
            self.assertEqual(
                walkforward.backtest_m15(
                    "EURUSD", bars, bars, 7.0, 2.0, 0.0, 100_000.0, [], []
                ),
                [],
            )

    def test_portfolio_capacity_observes_daily_limits_and_shared_position_cap(self):
        same_time = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc).timestamp()
        scalp_trades = [
            {
                "strategy": "m1",
                "symbol": symbol,
                "setup_ts": same_time,
                "entry_ts": same_time,
                "exit_ts": same_time + 600,
                "net": 1.0,
            }
            for symbol in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD")
        ]
        m15_trade = {
            "strategy": "m15",
            "symbol": "EURUSD",
            "setup_ts": same_time,
            "entry_ts": same_time,
            "exit_ts": same_time + 600,
            "net": 1.0,
        }

        accepted, _unresolved, rejected = walkforward._filter_portfolio_capacity(
            scalp_trades + [m15_trade],
            self._attempts(scalp_trades + [m15_trade]),
            [],
        )

        self.assertEqual(len(accepted), 5)
        self.assertEqual(rejected, 0)

    def test_portfolio_daily_limit_is_shared_across_symbols(self):
        start = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc).timestamp()
        trades = [
            {
                "strategy": "m15",
                "symbol": f"FX{i}",
                "setup_ts": start + i * 2 * 60 * 60,
                "entry_ts": start + i * 2 * 60 * 60,
                "exit_ts": start + i * 2 * 60 * 60 + 60,
                "net": 1.0,
            }
            for i in range(bot.MAX_M15_TRADES_PER_DAY + 1)
        ]

        accepted, _unresolved, rejected = walkforward._filter_portfolio_capacity(
            trades, self._attempts(trades), []
        )

        self.assertEqual(len(accepted), bot.MAX_M15_TRADES_PER_DAY)
        self.assertEqual(rejected, 1)

    def test_daily_limit_counts_attempts_even_when_no_position_fills(self):
        start = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc).timestamp()
        attempts = [
            {
                "strategy": "m15",
                "symbol": f"FX{i}",
                "setup_ts": start + i * 2 * 60 * 60,
            }
            for i in range(bot.MAX_M15_TRADES_PER_DAY + 1)
        ]

        accepted, unresolved, rejected = walkforward._filter_portfolio_capacity(
            [], attempts, []
        )

        self.assertEqual(accepted, [])
        self.assertEqual(unresolved, [])
        self.assertEqual(rejected, 1)

    def test_auto_entry_requires_matching_recent_profitable_backtest(self):
        now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
        report = {
            "strategy_version": bot.STRATEGY_VERSION,
            "strategy_fingerprint": bot.strategy_fingerprint(),
            "generated_at_utc": now.isoformat(),
            "out_of_sample_start_utc": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "out_of_sample_end_utc": now.isoformat(),
            "calendar_coverage_start_utc": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
            "calendar_coverage_end_utc": now.isoformat(),
            "unresolved_open_positions": 0,
            "cost_model": {
                "commission_per_lot_round_turn": 7.0,
                "slippage_points_per_side": 2.0,
                "spread": "MT5 historical bar spread field",
            },
            "strategies": {
                "m1": {"aggregate": {
                    "out_of_sample_trades": 100,
                    "net_expectancy": 0.5,
                    "profitable": True,
                }},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "backtest.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with (
                patch.object(bot, "BACKTEST_REPORT", str(report_path)),
                patch.object(bot, "utc_now", return_value=now),
            ):
                self.assertIsNone(bot.backtest_entry_block("m1"))
                report["strategy_fingerprint"] = "stale-config"
                report_path.write_text(json.dumps(report), encoding="utf-8")
                self.assertIn("different strategy", bot.backtest_entry_block("m1"))

    def test_live_mode_backtest_check_does_not_require_demo_trade_history(self):
        fake = SimpleNamespace()
        with (
            patch.object(bot, "LIVE_TRADING_ENABLED", True),
            patch.object(bot, "mt5", fake),
            patch.object(bot, "backtest_entry_block", return_value=None),
        ):
            self.assertIsNone(bot.backtest_entry_block("m15"))


if __name__ == "__main__":
    unittest.main()
