import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mt5_auto as bot


def fake_mt5():
    info = SimpleNamespace(
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        point=0.00001,
        trade_tick_size=0.00001,
        digits=5,
        trade_stops_level=0,
        trade_mode=1,
        filling_mode=1,
    )
    account = SimpleNamespace(
        balance=10_000.0,
        margin_free=10_000.0,
        equity=10_000.0,
        currency="USD",
        trade_mode=0,
    )
    tick = SimpleNamespace(
        ask=1.1000,
        bid=1.0999,
        time=int(datetime.now(timezone.utc).timestamp()) - 10,
    )
    return SimpleNamespace(
        ORDER_TYPE_BUY=0,
        ORDER_TYPE_SELL=1,
        ORDER_TYPE_BUY_STOP=4,
        ORDER_TYPE_SELL_STOP=5,
        ORDER_FILLING_FOK=0,
        ORDER_FILLING_IOC=1,
        ORDER_FILLING_RETURN=2,
        TRADE_ACTION_DEAL=1,
        TRADE_ACTION_PENDING=5,
        TRADE_ACTION_REMOVE=2,
        TRADE_ACTION_SLTP=3,
        ORDER_TIME_GTC=0,
        ORDER_TIME_SPECIFIED=2,
        POSITION_TYPE_BUY=0,
        POSITION_TYPE_SELL=1,
        TRADE_RETCODE_DONE=10009,
        TRADE_RETCODE_DONE_PARTIAL=10010,
        TRADE_RETCODE_PLACED=10008,
        SYMBOL_TRADE_MODE_DISABLED=0,
        ACCOUNT_TRADE_MODE_DEMO=0,
        symbol_info=lambda _symbol: info,
        symbol_info_tick=lambda _symbol: tick,
        account_info=lambda: account,
        order_calc_profit=lambda *_args: -100.0,
        order_calc_margin=lambda *_args: 100.0,
        order_check=lambda _request: SimpleNamespace(retcode=0, comment="ok"),
        order_send=lambda _request: SimpleNamespace(retcode=10009, order=42, comment="done"),
        last_error=lambda: (1, "Success"),
        info=info,
        account=account,
    )


class IndicatorTests(unittest.TestCase):
    def test_market_news_rss_parser_reads_headline_and_link(self):
        feed = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
        <title><![CDATA[FX &amp; rates update]]></title>
        <link>https://example.test/story</link>
        <guid>story-1</guid>
        <pubDate>Mon, 05 Oct 2026 11:00:00 GMT</pubDate>
        </item></channel></rss>"""
        with patch.object(bot, "urlopen", return_value=BytesIO(feed)):
            items = bot.fetch_market_news()

        self.assertEqual(items, [{
            "id": "story-1",
            "title": "FX & rates update",
            "link": "https://example.test/story",
            "published": "Mon, 05 Oct 2026 11:00:00 GMT",
        }])

    def test_market_news_alerts_only_new_headlines_as_context(self):
        old_item = {
            "id": "old",
            "title": "Existing headline",
            "link": "https://example.test/old",
            "published": "Mon, 05 Oct 2026 10:00:00 GMT",
        }
        new_item = {
            "id": "new",
            "title": "New headline",
            "link": "https://example.test/new",
            "published": "Mon, 05 Oct 2026 11:00:00 GMT",
        }
        with (
            patch.object(bot, "fetch_market_news", side_effect=[[old_item], [new_item, old_item], [new_item, old_item]]),
            patch.object(bot, "alert") as alert,
            patch.object(bot, "say"),
        ):
            seen = bot.process_market_news(None)
            self.assertEqual(seen, {"old"})
            seen = bot.process_market_news(seen)
            self.assertEqual(seen, {"old", "new"})
            bot.process_market_news(seen)

        alert.assert_called_once()
        self.assertIn("context only - not a trading signal", alert.call_args.args[0])
        self.assertIn("New headline", alert.call_args.args[0])

    def test_ema_rsi_atr_and_adx_on_rising_series(self):
        self.assertAlmostEqual(bot.ema([1.0, 2.0, 3.0], 2)[-1], 2.5555555556)
        self.assertEqual(bot.rsi([float(i) for i in range(20)], 14), 100.0)
        bars = [
            {"time": i, "high": i * 3.0 + 1.0, "low": i * 3.0 - 1.0, "close": i * 3.0}
            for i in range(60)
        ]
        self.assertAlmostEqual(bot.atr(bars, 14), 4.0)
        self.assertGreater(bot.adx(bars, 14), 99.0)

    def test_volume_rounding_never_exceeds_risk_budget(self):
        self.assertEqual(bot.floor_to_step(1.279, 0.01), 1.27)

    def test_volume_below_risk_sizing_floor_uses_minimum_volume(self):
        fake = fake_mt5()
        fake.order_calc_profit = lambda *_args: -20_000.0
        with patch.object(bot, "mt5", fake), patch.object(bot, "say"):
            self.assertEqual(bot.calculate_volume("EURUSD", "BUY", 1.1, 1.0, 20.0), 0.01)

    def test_broker_clock_offset_corrects_three_hour_server_time(self):
        fixed_now = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
        server_time = int(fixed_now.timestamp()) + 3 * 60 * 60
        fake = SimpleNamespace(
            symbol_info_tick=lambda _symbol: SimpleNamespace(time=server_time),
            last_error=lambda: (1, "Success"),
        )
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "utc_now", return_value=fixed_now),
            patch.object(bot, "say"),
        ):
            bot.calibrate_broker_clock(["EURUSD"])
            self.assertEqual(bot.BROKER_UTC_OFFSET_SECONDS, 3 * 60 * 60)
            self.assertAlmostEqual(bot.broker_now_epoch(), server_time)

    def test_inconsistent_broker_symbol_clocks_fail_closed(self):
        fixed_now = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
        server_time = int(fixed_now.timestamp())
        ticks = {
            "EURUSD": SimpleNamespace(time=server_time + 3 * 60 * 60),
            "XAUUSD": SimpleNamespace(time=server_time + 2 * 60 * 60),
        }
        fake = SimpleNamespace(
            symbol_info_tick=lambda symbol: ticks[symbol],
            last_error=lambda: (1, "Success"),
        )
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "utc_now", return_value=fixed_now),
            patch.object(bot, "say"),self.assertRaises(bot.SafetyStop)
        ):
            bot.calibrate_broker_clock(["EURUSD", "XAUUSD"])

    def test_tick_freshness_allows_small_clock_quantization_error(self):
        fixed_now = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
        server_now = fixed_now.timestamp() + 3 * 60 * 60
        with (
            patch.object(bot, "BROKER_UTC_OFFSET_SECONDS", 3 * 60 * 60),
            patch.object(bot, "utc_now", return_value=fixed_now),
        ):
            self.assertLessEqual(bot.tick_age_seconds(server_now + 0.5), 0)
            self.assertGreater(bot.tick_age_seconds(server_now - 120), bot.MAX_TICK_AGE_SECONDS)

    def test_candle_is_not_marked_processed_before_delay_finishes(self):
        bar_time = 1_791_198_000
        timeframe_seconds = bot.STRATEGIES[1].tf_seconds
        previous_bar = bar_time - timeframe_seconds
        self.assertEqual(
            bot.candle_status(bar_time, previous_bar, bot.CANDLE_DELAY_SECONDS - 2, timeframe_seconds),
            "wait",
        )
        self.assertEqual(
            bot.candle_status(bar_time, previous_bar, bot.CANDLE_DELAY_SECONDS + 1, timeframe_seconds),
            "ready",
        )
        self.assertEqual(
            bot.candle_status(bar_time, bar_time, bot.CANDLE_DELAY_SECONDS + 1, timeframe_seconds),
            "duplicate",
        )

    def test_old_unprocessed_candle_is_skipped_as_stale(self):
        bar_time = 1_791_198_000
        timeframe_seconds = bot.STRATEGIES[1].tf_seconds
        previous_bar = bar_time - timeframe_seconds
        self.assertEqual(
            bot.candle_status(bar_time, previous_bar, 2 * timeframe_seconds + 1, timeframe_seconds),
            "stale",
        )

    def test_scalp_candle_uses_its_own_one_minute_staleness_window(self):
        bar_time = 1_791_198_000
        self.assertEqual(
            bot.candle_status(
                bar_time,
                bar_time - bot.STRATEGIES[0].tf_seconds,
                2 * bot.STRATEGIES[0].tf_seconds + 1,
                bot.STRATEGIES[0].tf_seconds,
            ),
            "stale",
        )

    def test_gold_scalp_signal_uses_m1_pullback_rules(self):
        bars = [
            {"time": i * 60, "open": 2.0, "high": 4.0, "low": 1.0, "close": 2.0}
            for i in range(250)
        ]
        bars[-2].update(open=3.2, high=3.7, low=2.5, close=3.5)
        timeframes = []
        fake = SimpleNamespace(
            copy_rates_from_pos=lambda _symbol, timeframe, _start, _count: (
                timeframes.append(timeframe) or bars
            ),
            last_error=lambda: (1, "Success"),
        )

        def fake_ema(values, period):
            end_values = {20: 3.0, 50: 2.0, 200: 1.0}
            return [end_values[period]] * len(values)

        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "ema", side_effect=fake_ema),
            patch.object(bot, "rsi", return_value=60.0),
            patch.object(bot, "adx", return_value=25.0),
            patch.object(bot, "atr", return_value=0.1),
        ):
            result = bot.get_scalp_signal("XAUUSD")

        self.assertEqual(timeframes, [bot.SCALP_TIMEFRAME])
        self.assertEqual(result["signal"], "BUY")
        self.assertEqual(result["bar_time"], bars[-2]["time"])

    def test_gold_scalp_signal_rejects_overbought_pullback(self):
        bars = [
            {"time": i * 60, "open": 2.0, "high": 4.0, "low": 1.0, "close": 2.0}
            for i in range(250)
        ]
        bars[-2].update(open=3.2, high=3.7, low=2.5, close=3.5)
        fake = SimpleNamespace(
            copy_rates_from_pos=lambda *_args: bars,
            last_error=lambda: (1, "Success"),
        )
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "ema", side_effect=lambda values, period: [
                {20: 3.0, 50: 2.0, 200: 1.0}[period]
            ] * len(values)),
            patch.object(bot, "rsi", return_value=75.0),
            patch.object(bot, "adx", return_value=25.0),
            patch.object(bot, "atr", return_value=0.1),
        ):
            result = bot.get_scalp_signal("XAUUSD")

        self.assertIsNone(result["signal"])

    def test_gold_scalp_signal_detects_bearish_pullback(self):
        bars = [
            {"time": i * 60, "open": 2.0, "high": 4.0, "low": 1.0, "close": 2.0}
            for i in range(250)
        ]
        bars[-2].update(open=1.1, high=1.5, low=0.4, close=0.5)
        fake = SimpleNamespace(
            copy_rates_from_pos=lambda *_args: bars,
            last_error=lambda: (1, "Success"),
        )
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "ema", side_effect=lambda values, period: [
                {20: 1.0, 50: 2.0, 200: 3.0}[period]
            ] * len(values)),
            patch.object(bot, "rsi", return_value=40.0),
            patch.object(bot, "adx", return_value=25.0),
            patch.object(bot, "atr", return_value=0.1),
        ):
            result = bot.get_scalp_signal("XAUUSD")

        self.assertEqual(result["signal"], "SELL")


class EconomicCalendarTests(unittest.TestCase):
    def test_forexfactory_high_impact_time_converts_from_eastern_to_utc(self):
        feed = b"""<?xml version="1.0" encoding="utf-8"?>
        <weeklyevents>
          <event><title>Inflation report</title><country>USD</country>
            <date>10-14-2026</date><time>8:30am</time><impact>High</impact></event>
          <event><title>Minor release</title><country>USD</country>
            <date>10-14-2026</date><time>9:00am</time><impact>Low</impact></event>
        </weeklyevents>"""

        events = bot._parse_forexfactory_calendar(feed)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].currency, "USD")
        self.assertEqual(events[0].title, "Inflation report")
        self.assertEqual(
            datetime.fromtimestamp(events[0].start_ts, timezone.utc),
            datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(events[0].start_ts, events[0].end_ts)

    def test_forexfactory_all_day_events_cover_dst_transition(self):
        feed = b"""<weeklyevents>
          <event><title>Scheduled release</title><country>USD</country>
            <date>11-01-2026</date><time>All Day Event</time><impact>High</impact></event>
        </weeklyevents>"""

        event = bot._parse_forexfactory_calendar(feed)[0]

        self.assertEqual(event.end_ts - event.start_ts, 25 * 60 * 60)
        with patch.object(bot, "NEWS", {"events": [event]}):
            self.assertIs(bot.event_near("EURUSD", event.start_ts + 12 * 60 * 60, 0, 0), event)

    def test_calendar_url_defaults_to_public_forexfactory_feed(self):
        self.assertEqual(
            bot.DEFAULT_CALENDAR_URL,
            "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
        )

    def test_existing_json_and_csv_calendar_sources_are_supported(self):
        json_events = bot._parse_calendar_json(
            b'[{"title":"Rate decision","currency":"EUR",'
            b'"date":"2026-10-14T12:00:00Z","impact":"high"}]'
        )
        self.assertEqual(json_events[0].currency, "EUR")
        self.assertEqual(
            datetime.fromtimestamp(json_events[0].start_ts, timezone.utc),
            datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc),
        )

        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "events.csv"
            csv_path.write_text(
                "time_utc,currency,impact,title\n"
                "2026-10-14T13:00:00Z,GBP,high,UK rate decision\n",
                encoding="utf-8",
            )
            with (
                patch.object(bot, "CALENDAR_URL", ""),
                patch.object(bot, "EVENTS_CSV", str(csv_path)),
            ):
                csv_events = bot.fetch_calendar_events()

        self.assertEqual(len(csv_events), 1)
        self.assertEqual(csv_events[0].currency, "GBP")
        self.assertEqual(csv_events[0].title, "UK rate decision")

    def test_stale_calendar_failure_keeps_new_entries_blocked(self):
        stale_event = bot.CalendarEvent(0, 0, "USD", "Old event")
        news_state = {
            "events": [stale_event],
            "last_ok": 99.0,
            "next": 0.0,
        }
        with (
            patch.object(bot, "NEWS", news_state),
            patch.object(bot, "CALENDAR_URL", bot.DEFAULT_CALENDAR_URL),
            patch.object(bot, "NEWS_REQUIRED", True),
            patch.object(bot.time, "monotonic", return_value=1000.0),
            patch.object(bot, "urlopen", side_effect=OSError("offline")),
            patch.object(bot, "say"),
        ):
            bot.poll_calendar()
            self.assertFalse(bot.news_feed_ok())
            self.assertEqual(bot.news_entry_block("EURUSD"), "news calendar is stale/unavailable (fail-safe)")


class OrderSafetyTests(unittest.TestCase):
    def test_cash_target_rounds_up_one_tick_when_float_rounding_is_low(self):
        fake = fake_mt5()
        fake.order_calc_profit = lambda order_type, _symbol, volume, entry, exit_price: (
            (exit_price - entry) if order_type == fake.ORDER_TYPE_BUY else (entry - exit_price)
        ) * 10_000 * volume

        with patch.object(bot, "mt5", fake):
            target = bot.cash_target_price(
                "XAUUSD",
                "BUY",
                1.1,
                0.2,
                0.8,
                fake.info,
            )

        self.assertIsNotNone(target)
        self.assertGreaterEqual(
            fake.order_calc_profit(fake.ORDER_TYPE_BUY, "XAUUSD", 0.2, 1.1, target),
            0.8,
        )
    def test_trade_risk_is_one_percent_with_a_hard_one_dollar_limit(self):
        self.assertEqual(bot.trade_risk_budget(100.0, "USD"), 1.0)
        self.assertEqual(bot.trade_risk_budget(50.0, "USD"), 0.5)
        self.assertEqual(bot.trade_risk_budget(100_000.0, "USD"), 1.0)

    def test_dollar_risk_budget_rejects_non_usd_accounts(self):
        with self.assertRaises(bot.SafetyStop):
            bot.trade_risk_budget(100.0, "EUR")

    def test_prepare_order_returns_typed_stop_target_and_volume(self):
        fake = fake_mt5()
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "BROKER_UTC_OFFSET_SECONDS", 0),
        ):
            request, context, reason = bot.prepare_order(
                "EURUSD",
                "BUY",
                bot.STRATEGIES[1],
                0.001,
                20.0,
                None,
                None,
                None,
            )

        self.assertIsNone(reason)
        self.assertIsNotNone(request)
        self.assertGreater(context["sl"], 0)
        self.assertGreater(context["tp"], context["entry"])
        self.assertGreater(context["volume"], 0)

    def test_one_dollar_risk_does_not_round_up_to_oversized_minimum_lot(self):
        fake = fake_mt5()
        fake.order_calc_profit = lambda *_args: -20_000.0
        risk_cash = bot.trade_risk_budget(100.0, "USD")
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "BROKER_UTC_OFFSET_SECONDS", 0),
            patch.object(bot, "say"),
        ):
            request, context, reason = bot.prepare_order(
                "EURUSD",
                "BUY",
                bot.STRATEGIES[1],
                0.001,
                risk_cash,
                None,
                None,
                None,
            )

        self.assertIsNone(request)
        self.assertEqual(context, {})
        self.assertEqual(reason, "risk or margin sizing rejected")

    def test_setup_places_both_atr_spaced_stop_orders_with_candle_expiry(self):
        fake = fake_mt5()
        requests = []
        fake.order_send = lambda request: (
            requests.append(request)
            or SimpleNamespace(
                retcode=10008 if request["action"] == fake.TRADE_ACTION_PENDING else 10009,
                order=len(requests),
                comment="placed",
            )
        )
        state = {
            "day_utc": bot.utc_now().strftime("%Y-%m-%d"),
            "start_equity": 10_000.0,
            "trades_today": 0,
            "halted": False,
        }
        with (
            tempfile.TemporaryDirectory() as directory, patch.object(bot, "mt5", fake),
            patch.object(bot, "BROKER_UTC_OFFSET_SECONDS", 0),
            patch.object(bot, "STATE_FILE", str(Path(directory) / "state.json")),
            patch.object(bot, "TRADE_LOG", str(Path(directory) / "trades.csv")),
            patch.object(bot, "alert"),
        ):
            self.assertTrue(
                bot.place_breakout_bracket(
                    "EURUSD",
                    0.001,
                    state,
                    300,
                    bot.MAGIC,
                    setup_side="BUY",
                )
            )

        self.assertEqual(len(requests), 2)
        self.assertEqual(
            [request["type"] for request in requests],
            [fake.ORDER_TYPE_BUY_STOP, fake.ORDER_TYPE_SELL_STOP],
        )
        self.assertTrue(all(request["action"] == fake.TRADE_ACTION_PENDING for request in requests))
        self.assertEqual(requests[0]["type_time"], fake.ORDER_TIME_SPECIFIED)
        self.assertEqual(requests[0]["expiration"], requests[1]["expiration"])
        self.assertEqual(requests[0]["expiration"] - int(bot.utc_now().timestamp()), 300)
        self.assertAlmostEqual(requests[0]["price"], 1.1005)
        self.assertAlmostEqual(requests[1]["price"], 1.0994)
        self.assertEqual(state["trades_today"], 1)

    def test_incomplete_breakout_bracket_cancels_first_order_and_reverts_count(self):
        fake = fake_mt5()
        requests = []
        fake.orders_get = lambda **_kwargs: (
            SimpleNamespace(
                symbol="EURUSD",
                magic=bot.MAGIC,
                comment=bot.BREAKOUT_ORDER_COMMENT,
                ticket=44,
            ),
        )
        fake.positions_get = lambda **_kwargs: ()
        checks = iter([
            SimpleNamespace(retcode=0, comment="ok"),
            SimpleNamespace(retcode=10016, comment="invalid stops"),
        ])
        fake.order_check = lambda _request: next(checks)
        fake.order_send = lambda request: (
            requests.append(request)
            or SimpleNamespace(
                retcode=10008 if request["action"] == fake.TRADE_ACTION_PENDING else 10009,
                order=44,
                comment="done",
            )
        )
        state = {
            "day_utc": bot.utc_now().strftime("%Y-%m-%d"),
            "start_equity": 10_000.0,
            "trades_today": 0,
            "halted": False,
        }
        with (
            tempfile.TemporaryDirectory() as directory, patch.object(bot, "mt5", fake),
            patch.object(bot, "BROKER_UTC_OFFSET_SECONDS", 0),
            patch.object(bot, "STATE_FILE", str(Path(directory) / "state.json")),
            patch.object(bot, "TRADE_LOG", str(Path(directory) / "trades.csv")),
            patch.object(bot, "alert"),
            patch.object(bot, "say"),
        ):
            self.assertFalse(
                bot.place_breakout_bracket(
                    "EURUSD", 0.001, state, 300, bot.MAGIC, setup_side="BUY"
                )
            )

        self.assertEqual(state["trades_today"], 0)
        self.assertEqual(requests[-1]["action"], fake.TRADE_ACTION_REMOVE)

    def test_scalp_breakout_bracket_uses_reference_risk_and_reward_ratio(self):
        fake = fake_mt5()

        def calculate_profit(order_type, _symbol, volume, entry, exit_price):
            difference = exit_price - entry if order_type == fake.ORDER_TYPE_BUY else entry - exit_price
            return difference * 10_000 * volume

        fake.order_calc_profit = calculate_profit
        requests = []
        fake.order_send = lambda request: (
            requests.append(request)
            or SimpleNamespace(retcode=10008, order=len(requests), comment="placed")
        )
        state = {
            "day_utc": bot.utc_now().strftime("%Y-%m-%d"),
            "start_equity": 10_000.0,
            "trades_today": 0,
            "halted": False,
        }
        with (
            tempfile.TemporaryDirectory() as directory, patch.object(bot, "mt5", fake),
            patch.object(bot, "BROKER_UTC_OFFSET_SECONDS", 0),
            patch.object(bot, "STATE_FILE", str(Path(directory) / "state.json")),
            patch.object(bot, "TRADE_LOG", str(Path(directory) / "trades.csv")),
            patch.object(bot, "alert"),
        ):
            self.assertTrue(
                bot.place_breakout_bracket(
                    "EURUSD",
                    0.001,
                    state,
                    bot.SCALP_TIMEFRAME_SECONDS,
                    bot.SCALP_MAGIC,
                    setup_side="BUY",
                    cash_take_profit=bot.SCALP_TAKE_PROFIT_AMOUNT,
                    risk_cash=bot.SCALP_RISK_CASH,
                )
            )

        self.assertEqual(len(requests), 2)
        for request in requests:
            order_type = (
                fake.ORDER_TYPE_BUY
                if request["type"] == fake.ORDER_TYPE_BUY_STOP
                else fake.ORDER_TYPE_SELL
            )
            stop_loss = calculate_profit(
                order_type, "EURUSD", request["volume"], request["price"], request["sl"]
            )
            take_profit = calculate_profit(
                order_type, "EURUSD", request["volume"], request["price"], request["tp"]
            )
            self.assertGreaterEqual(stop_loss, -bot.SCALP_RISK_CASH)
            self.assertGreaterEqual(take_profit, bot.SCALP_TAKE_PROFIT_AMOUNT)

    def test_filled_breakout_cancels_other_bracket_order(self):
        fake = fake_mt5()
        filled = SimpleNamespace(symbol="EURUSD", magic=bot.MAGIC)
        pending = SimpleNamespace(
            symbol="EURUSD",
            magic=bot.MAGIC,
            comment=bot.BREAKOUT_ORDER_COMMENT,
            ticket=45,
        )
        fake.positions_get = lambda **_kwargs: (filled,)
        fake.orders_get = lambda **_kwargs: (pending,)
        requests = []
        fake.order_send = lambda request: (
            requests.append(request) or SimpleNamespace(retcode=10009, comment="done")
        )
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "alert") as alert,
            patch.object(bot, "log_trade"),
        ):
            bot.manage_breakout_brackets()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["action"], fake.TRADE_ACTION_REMOVE)
        alert.assert_called_once()

    def test_order_send_rechecks_demo_account_mode(self):
        fake = fake_mt5()
        fake.account.trade_mode = 1
        sends = []
        fake.order_send = lambda request: sends.append(request)
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "BROKER_UTC_OFFSET_SECONDS", 0),
            patch.object(bot, "say"),self.assertRaises(bot.SafetyStop)
        ):
            bot.order_trade("EURUSD", "BUY", 0.001, {"trades_today": 0})
        self.assertEqual(sends, [])

    def test_order_sends_attached_stop_and_target_and_persists_trade_count(self):
        fake = fake_mt5()
        requests = []
        fake.order_send = lambda request: (
            requests.append(request) or SimpleNamespace(retcode=10009, order=42, comment="done")
        )
        state = {
            "day_utc": bot.utc_now().strftime("%Y-%m-%d"),
            "start_equity": 10_000.0,
            "trades_today": 0,
            "halted": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            state_file = str(Path(directory) / "state.json")
            log_file = str(Path(directory) / "trades.csv")
            with (
                patch.object(bot, "mt5", fake),
                patch.object(bot, "STATE_FILE", state_file),
                patch.object(bot, "TRADE_LOG", log_file),
                patch.object(bot, "alert"),
            ):
                self.assertTrue(bot.order_trade("EURUSD", "BUY", 0.001, state))
            self.assertEqual(state["trades_today"], 1)
            self.assertTrue(Path(state_file).exists())
            self.assertLess(requests[0]["sl"], requests[0]["price"])
            self.assertGreater(requests[0]["tp"], requests[0]["price"])
            self.assertEqual(requests[0]["volume"], 0.5)

    def test_order_check_rejection_prevents_send(self):
        fake = fake_mt5()
        sends = []
        fake.order_check = lambda _request: SimpleNamespace(retcode=10016, comment="invalid stops")
        fake.order_send = lambda request: sends.append(request)
        state = {
            "day_utc": bot.utc_now().strftime("%Y-%m-%d"),
            "start_equity": 10_000.0,
            "trades_today": 0,
            "halted": False,
        }
        with patch.object(bot, "mt5", fake), patch.object(bot, "say"), patch.object(bot, "log_trade"):
            self.assertFalse(bot.order_trade("EURUSD", "BUY", 0.001, state))
        self.assertEqual(sends, [])
        self.assertEqual(state["trades_today"], 0)

    def test_scalp_order_uses_distinct_magic_and_reference_risk(self):
        fake = fake_mt5()
        fake.order_calc_profit = lambda order_type, _symbol, volume, entry, exit_price: (
            (exit_price - entry) if order_type == fake.ORDER_TYPE_BUY else (entry - exit_price)
        ) * 10_000 * volume
        requests = []
        fake.order_send = lambda request: (
            requests.append(request) or SimpleNamespace(retcode=10009, order=43, comment="done")
        )
        state = {
            "day_utc": bot.utc_now().strftime("%Y-%m-%d"),
            "start_equity": 10_000.0,
            "trades_today": 0,
            "halted": False,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(bot, "mt5", fake),
            patch.object(bot, "BROKER_UTC_OFFSET_SECONDS", 0),
            patch.object(bot, "STATE_FILE", str(Path(directory) / "state.json")),
            patch.object(bot, "TRADE_LOG", str(Path(directory) / "trades.csv")),
            patch.object(bot, "alert"),
        ):
            self.assertTrue(
                bot.order_trade(
                    "XAUUSD",
                    "BUY",
                    0.001,
                    state,
                    magic=bot.SCALP_MAGIC,
                    cash_take_profit=bot.SCALP_TAKE_PROFIT_AMOUNT,
                    risk_cash=bot.SCALP_RISK_CASH,
                )
            )

        request = requests[0]
        self.assertEqual(request["magic"], bot.SCALP_MAGIC)
        self.assertEqual(bot.SCALP_TAKE_PROFIT_AMOUNT, 0.8)
        self.assertEqual(bot.SCALP_RISK_CASH, 1.0)
        self.assertLessEqual(request["volume"], 0.4)
        self.assertGreater(request["tp"], request["price"])
        estimated_profit = fake.order_calc_profit(
            fake.ORDER_TYPE_BUY, "XAUUSD", request["volume"], request["price"], request["tp"]
        )
        self.assertGreaterEqual(estimated_profit, bot.SCALP_TAKE_PROFIT_AMOUNT)

    def test_scalp_take_profit_fully_closes_positions_at_target(self):
        fake = fake_mt5()
        target_position = SimpleNamespace(
            ticket=17,
            symbol="XAUUSD",
            time=0,
            volume=0.2,
            type=fake.POSITION_TYPE_BUY,
            magic=bot.SCALP_MAGIC,
            profit=bot.SCALP_TAKE_PROFIT_AMOUNT,
            price_open=1.1,
            sl=1.0,
        )
        below_target = SimpleNamespace(
            ticket=18,
            symbol="XAUUSD",
            time=0,
            volume=0.2,
            type=fake.POSITION_TYPE_SELL,
            magic=bot.SCALP_MAGIC,
            profit=bot.SCALP_TAKE_PROFIT_AMOUNT - 0.01,
            price_open=1.1,
            sl=1.2,
            tp=1.0,
        )
        other_strategy = SimpleNamespace(
            ticket=19,
            symbol="XAUUSD",
            volume=0.2,
            type=fake.POSITION_TYPE_BUY,
            magic=bot.MAGIC,
            profit=20.0,
        )
        positions = [target_position, below_target, other_strategy]
        requests = []
        fake.positions_get = lambda **_kwargs: tuple(positions)
        fake.symbol_info_tick = lambda _symbol: SimpleNamespace(bid=1.0999, ask=1.1000)
        fake.order_send = lambda request: (
            requests.append(request) or SimpleNamespace(retcode=10009, order=44, comment="done")
        )
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "broker_now_epoch", return_value=1),
            patch.object(bot, "log_trade"),
            patch.object(bot, "alert"),
            patch.object(bot, "say"),
        ):
            bot.close_scalp_positions_at_target()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["position"], 17)
        self.assertEqual(requests[0]["volume"], target_position.volume)
        self.assertEqual(requests[0]["magic"], bot.SCALP_MAGIC)

    def test_existing_scalp_position_tp_is_updated_to_target(self):
        fake = fake_mt5()
        position = SimpleNamespace(
            ticket=27,
            symbol="XAUUSD",
            time=0,
            volume=0.2,
            type=fake.POSITION_TYPE_BUY,
            magic=bot.SCALP_MAGIC,
            profit=bot.SCALP_TAKE_PROFIT_AMOUNT - 0.1,
            price_open=1.1,
            sl=1.0,
            tp=1.2,
        )
        fake.positions_get = lambda **_kwargs: (position,)
        fake.order_calc_profit = lambda order_type, _symbol, volume, entry, exit_price: (
            (exit_price - entry) if order_type == fake.ORDER_TYPE_BUY else (entry - exit_price)
        ) * 10_000 * volume
        requests = []
        fake.order_send = lambda request: (
            requests.append(request) or SimpleNamespace(retcode=10009, order=46, comment="done")
        )

        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "broker_now_epoch", return_value=1),
            patch.object(bot, "log_trade"),
            patch.object(bot, "alert"),
            patch.object(bot, "say"),
        ):
            bot.close_scalp_positions_at_target()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["action"], fake.TRADE_ACTION_SLTP)
        self.assertEqual(requests[0]["position"], position.ticket)
        self.assertEqual(requests[0]["sl"], position.sl)
        estimated_profit = fake.order_calc_profit(
            fake.ORDER_TYPE_BUY, "XAUUSD", position.volume, position.price_open, requests[0]["tp"]
        )
        self.assertGreaterEqual(estimated_profit, bot.SCALP_TAKE_PROFIT_AMOUNT)

    def test_breakout_bracket_respects_symbol_and_global_exposure_limits(self):
        fake = fake_mt5()
        positions = []
        orders = []
        fake.positions_get = lambda **kwargs: tuple(
            position for position in positions
            if "symbol" not in kwargs or position.symbol == kwargs["symbol"]
        )
        fake.orders_get = lambda **kwargs: tuple(
            order for order in orders
            if "symbol" not in kwargs or order.symbol == kwargs["symbol"]
        )
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "PAUSE_FILE", "missing-pause-file"),
        ):
            self.assertTrue(bot.can_open_scalp("EURUSD", "BUY"))
            pending_order = SimpleNamespace(symbol="EURUSD", magic=bot.MAGIC)
            orders.append(pending_order)
            self.assertFalse(bot.can_open_scalp("EURUSD", "BUY"))
            orders.clear()
            active_position = SimpleNamespace(symbol="XAUUSD", magic=bot.MAGIC)
            positions.append(active_position)
            self.assertFalse(bot.can_open("EURUSD", "SELL"))

    def test_daily_loss_halts_and_closes_bot_exposure(self):
        fake = fake_mt5()
        fake.account.equity = 9_700.0
        state = {
            "day_utc": bot.utc_now().strftime("%Y-%m-%d"),
            "start_equity": 10_000.0,
            "trades_today": 0,
            "halted": False,
        }
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "daily_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(bot, "close_bot_exposure") as close_exposure,
            patch.object(bot, "alert"),
        ):
            allowed, returned = bot.daily_loss_allows_entries()
        self.assertFalse(allowed)
        self.assertTrue(returned["halted"])
        close_exposure.assert_called_once_with("daily loss limit")

    def test_risk_stop_close_request_is_scoped_to_bot_position(self):
        fake = fake_mt5()
        position = SimpleNamespace(
            ticket=17,
            symbol="EURUSD",
            volume=0.2,
            type=fake.POSITION_TYPE_BUY,
            magic=bot.MAGIC,
        )
        requests = []
        fake.positions_get = lambda **_kwargs: (position,)
        fake.orders_get = lambda **_kwargs: ()
        fake.symbol_info_tick = lambda _symbol: SimpleNamespace(bid=1.0999, ask=1.1000)
        fake.order_send = lambda request: (
            requests.append(request) or SimpleNamespace(retcode=10009, order=42, comment="done")
        )
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot, "log_trade"),
            patch.object(bot, "say"),
        ):
            bot.close_bot_exposure("daily loss")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["position"], 17)
        self.assertEqual(requests[0]["magic"], bot.MAGIC)
        self.assertEqual(requests[0]["type"], fake.ORDER_TYPE_SELL)

    def test_log_only_allows_algo_trading_disabled_but_auto_refuses(self):
        fake = fake_mt5()
        fake.ACCOUNT_TRADE_MODE_DEMO = 0
        fake.account.trade_mode = 0
        fake.terminal_info = lambda: SimpleNamespace(trade_allowed=False)
        with (
            patch.object(bot, "mt5", fake),
            patch.object(bot.trader, "connect", return_value=True),
            patch.object(bot.trader, "disconnect") as disconnect,
            patch.object(bot, "resolve_symbol", side_effect=["EURUSD", "XAUUSD"]),
        ):
            self.assertEqual(
                bot.initialize(require_trading=False),
                {"EURUSD": "EURUSD", "XAUUSD": "XAUUSD"},
            )
            with self.assertRaises(bot.SafetyStop):
                bot.initialize(require_trading=True)
        disconnect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
