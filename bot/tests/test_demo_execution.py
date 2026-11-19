"""Exercise the demo boundary at order time, including a terminal account change."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution


class DemoExecutionTest(unittest.TestCase):
    def test_account_is_checked_for_each_request(self):
        api = Mock(ACCOUNT_TRADE_MODE_DEMO=0)
        api.account_info.side_effect = [SimpleNamespace(trade_mode=0), SimpleNamespace(trade_mode=2), None]
        request = {"symbol": "XAUUSD.s"}
        with patch.object(execution, "mt5", api):
            self.assertIs(execution._send_demo(request), api.order_send.return_value)
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "demo account"):
                    execution._send_demo(request)
        api.order_send.assert_called_once_with(request)

    def test_buy_and_sell_requests_with_protective_orders(self):
        from fake_mt5 import install
        from synthetic import synthetic_m1

        with patch.dict(sys.modules):
            api = install(synthetic_m1(days=2), start_bar=100)
            with patch.object(execution, "mt5", api):
                for side in (1, -1):
                    fill = execution.open_market("XAUUSD", side, 0.01, 1.0, 1.5, 10)
                    self.assertTrue(fill.ok)
                    position, = execution.bot_positions("XAUUSD")
                    self.assertEqual(position.type, api.ORDER_TYPE_BUY if side == 1 else api.ORDER_TYPE_SELL)
                    self.assertGreater(side * (fill.price - position.sl), 0)
                    self.assertGreater(side * (position.tp - fill.price), 0)
                    self.assertTrue(execution.close_position(position, 10).ok)
