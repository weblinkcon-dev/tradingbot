import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mt5_auto
import mt5_trader
import news_guard


class NewsGuardIntegrationTests(unittest.TestCase):
    def test_auto_runner_blocks_order_when_guard_rejects_symbol(self):
        request = {"action": 5}
        context = {
            "is_pending": True,
            "volume": 0.01,
            "entry": 1.1,
            "sl": 1.0,
            "tp": 1.2,
        }
        fake_mt5 = SimpleNamespace(order_check=Mock(), order_send=Mock())

        with (
            patch.object(mt5_auto, "mt5", fake_mt5),
            patch.object(
                mt5_auto,
                "_prepare_trade_request_context",
                return_value=(request, context, None),
            ),
            patch.object(mt5_auto.news_guard, "can_trade", return_value=(False, ["high-impact news"])),
            patch.object(mt5_auto, "log_trade"),
            patch.object(mt5_auto, "say"),
        ):
            result = mt5_auto.order_trade("EURUSD", "BUY", 0.001, {})

        self.assertFalse(result)
        fake_mt5.order_check.assert_not_called()
        fake_mt5.order_send.assert_not_called()

    def test_shared_order_helper_blocks_new_entry_when_guard_rejects_symbol(self):
        send = Mock()
        fake_mt5 = SimpleNamespace(
            ACCOUNT_TRADE_MODE_DEMO=0,
            TRADE_ACTION_DEAL=1,
            TRADE_ACTION_PENDING=5,
            account_info=lambda: SimpleNamespace(trade_mode=0),
            order_send=send,
        )

        with (
            patch.object(mt5_trader, "mt5", fake_mt5),
            patch.object(mt5_trader.news_guard, "can_trade", return_value=(False, ["risky headline"])),
            patch("builtins.print"),
        ):
            result = mt5_trader._send({"action": 1, "symbol": "EURUSD"})

        self.assertIsNone(result)
        send.assert_not_called()

    def test_shared_order_helper_sends_entry_when_guard_allows_symbol(self):
        result = SimpleNamespace(retcode=10009, comment="done")
        send = Mock(return_value=result)
        fake_mt5 = SimpleNamespace(
            ACCOUNT_TRADE_MODE_DEMO=0,
            TRADE_ACTION_DEAL=1,
            TRADE_ACTION_PENDING=5,
            TRADE_RETCODE_DONE=10009,
            TRADE_RETCODE_PLACED=10008,
            account_info=lambda: SimpleNamespace(trade_mode=0),
            order_send=send,
        )

        with (
            patch.object(mt5_trader, "mt5", fake_mt5),
            patch.object(mt5_trader.news_guard, "can_trade", return_value=(True, [])),
        ):
            actual = mt5_trader._send({"action": 1, "symbol": "EURUSD"})

        self.assertIs(actual, result)
        send.assert_called_once()

    def test_auto_runner_cancels_pending_brackets_during_guard_pause(self):
        with (
            patch.object(mt5_auto.news_guard, "can_trade", return_value=(False, ["news"])),
            patch.object(mt5_auto, "cancel_breakout_orders") as cancel,
            patch.object(mt5_auto, "say"),
            patch.object(mt5_auto, "ENTRY_GUARD_BLOCKED", set()),
        ):
            mt5_auto._enforce_entry_guards(["EURUSD"])

        self.assertEqual(cancel.call_count, len(mt5_auto.BOT_MAGICS))
        self.assertEqual(
            {call.args[1] for call in cancel.call_args_list},
            set(mt5_auto.BOT_MAGICS),
        )


class NewsGuardLogicTests(unittest.TestCase):
    def test_session_guard_blocks_weekends(self):
        blocked, reason = news_guard.session_check(
            datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
        )

        self.assertTrue(blocked)
        self.assertEqual(reason, "weekend")

    def test_calendar_guard_blocks_high_impact_currency_event(self):
        now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        event = {
            "date": now.isoformat(),
            "country": "USD",
            "title": "Inflation report",
            "impact": "High",
        }
        with patch.object(news_guard, "load_calendar", return_value=[event]):
            blocked, reason = news_guard.calendar_check("EURUSD", now)

        self.assertTrue(blocked)
        self.assertIn("Inflation report", reason)

    def test_market_guard_blocks_a_spread_spike(self):
        rates = [{"spread": 10, "high": 1.01, "low": 1.00} for _ in range(60)]
        fake_mt5 = SimpleNamespace(
            TIMEFRAME_M5=5,
            symbol_info=lambda _symbol: SimpleNamespace(spread=30),
            copy_rates_from_pos=lambda *_args: rates,
        )
        with patch.object(news_guard, "mt5", fake_mt5):
            blocked, reason = news_guard.market_check("EURUSD")

        self.assertTrue(blocked)
        self.assertIn("spread jumped", reason)


if __name__ == "__main__":
    unittest.main()
