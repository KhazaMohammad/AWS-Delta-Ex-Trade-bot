"""
Unit and integration test suite for Algo-Trade-Delta.
Tests all requirements: Heiken Ashi calculation, entry/scaling/reversal rules,
risk limits, deduplication, reconciliation, and end-to-end scenarios A & B.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from config import (
    SYMBOL_CONFIG,
    base_lot,
    max_entries,
    max_position_size,
)
from database import Database
from delta_client import DeltaClient
from heiken_ashi import Candle, HeikenAshiCandle, compute_heiken_ashi, candle_color
from position_manager import PositionManager
from strategy import Strategy


class TestTradingSystem(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_trading.db")
        self.db = Database(self.db_path)
        self.mock_client = MagicMock(spec=DeltaClient)
        self.mock_client.dry_run = True
        self.pm = PositionManager(client=self.mock_client, db=self.db, dry_run=True)
        self.pm.initialize(["ETHUSD", "OILUSD"])

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.test_dir)

    def test_symbol_config_and_limits(self) -> None:
        """Requirement 2 & 3: Symbol config, base_lot, max_entries, max_position_size."""
        self.assertEqual(base_lot("ETHUSD"), 5)
        self.assertEqual(max_entries("ETHUSD"), 5)
        self.assertEqual(max_position_size("ETHUSD"), 25)

        self.assertEqual(base_lot("OILUSD"), 2)
        self.assertEqual(max_entries("OILUSD"), 5)
        self.assertEqual(max_position_size("OILUSD"), 10)

    def test_heiken_ashi_formula_and_color(self) -> None:
        """Requirement 4: Heiken Ashi calculation from exchange OHLC."""
        candles = [
            Candle(timestamp="2025-01-01T00:00:00Z", open=100.0, high=120.0, low=95.0, close=115.0),
            Candle(timestamp="2025-01-01T02:00:00Z", open=115.0, high=118.0, low=80.0, close=85.0),
        ]
        ha = compute_heiken_ashi(candles)
        self.assertEqual(len(ha), 2)

        # Candle 0: HA_Open = (100 + 115) / 2 = 107.5
        # HA_Close = (100 + 120 + 95 + 115) / 4 = 107.5
        self.assertAlmostEqual(ha[0].ha_open, 107.5)
        self.assertAlmostEqual(ha[0].ha_close, 107.5)
        self.assertEqual(ha[0].color, "GREEN")
        self.assertTrue(ha[0].is_bullish)

        # Candle 1: HA_Open = (107.5 + 107.5) / 2 = 107.5
        # HA_Close = (115 + 118 + 80 + 85) / 4 = 99.5
        # HA_Close < HA_Open -> RED
        self.assertAlmostEqual(ha[1].ha_open, 107.5)
        self.assertAlmostEqual(ha[1].ha_close, 99.5)
        self.assertEqual(ha[1].color, "RED")
        self.assertFalse(ha[1].is_bullish)

    def test_scaling_and_max_entries_enforcement(self) -> None:
        """Requirement 6 & 14: Long position scaling and limit capping."""
        symbol = "ETHUSD"
        self.mock_client.place_market_order.return_value = {
            "id": "ord_scale_1",
            "filled_size": 5,
            "avg_fill_price": 3000.0,
        }
        self.mock_client.get_positions.return_value = [
            {"product_symbol": symbol, "size": 5, "side": "buy"}
        ]

        # 1. Initial entry
        self.assertTrue(self.pm.execute_entry(symbol, "LONG", price=3000.0))
        self.assertEqual(self.pm.state(symbol).position_size, 5)
        self.assertEqual(self.pm.state(symbol).entry_count, 1)

        # 2. Scale entries 2, 3, 4, 5
        for i in range(2, 6):
            self.mock_client.get_positions.return_value = [
                {"product_symbol": symbol, "size": i * 5, "side": "buy"}
            ]
            self.assertTrue(self.pm.execute_scale(symbol, "LONG", price=3000.0))
            self.assertEqual(self.pm.state(symbol).entry_count, i)
            self.assertEqual(self.pm.state(symbol).position_size, i * 5)

        # 3. 6th scaling attempt must be skipped and audited
        self.assertFalse(self.pm.execute_scale(symbol, "LONG", price=3000.0))
        self.assertEqual(self.pm.state(symbol).entry_count, 5)
        self.assertEqual(self.pm.state(symbol).position_size, 25)

        # Verify audit log recorded skipped scaling
        audit = self.db.get_audit_log(symbol=symbol)
        skipped_events = [a for a in audit if a["event_type"] == "SCALING_SKIPPED"]
        self.assertTrue(len(skipped_events) > 0)

    def test_reconciliation_startup_recovery(self) -> None:
        """Requirement 9: Position reconciliation treating exchange as source of truth."""
        symbol = "OILUSD"
        self.assertEqual(self.pm.state(symbol).position_direction, "FLAT")

        # Discrepancy: exchange has 4 lots SHORT
        self.mock_client.get_positions.return_value = [
            {"product_symbol": symbol, "size": 4, "side": "sell"}
        ]

        self.pm.reconcile_with_exchange([symbol])
        state = self.pm.state(symbol)
        self.assertEqual(state.position_direction, "SHORT")
        self.assertEqual(state.position_size, 4.0)
        self.assertEqual(state.entry_count, 0)

        # Audit log verification
        audit = self.db.get_audit_log(symbol=symbol)
        reconciled = [a for a in audit if a["event_type"] == "RECONCILIATION"]
        self.assertTrue(len(reconciled) > 0)

    def test_scenario_a_green_green_green_red(self) -> None:
        """Requirement 23 Scenario A: GREEN -> GREEN -> GREEN -> RED (ETHUSD)."""
        symbol = "ETHUSD"
        strat = Strategy(client=self.mock_client, db=self.db, position_manager=self.pm)

        self.mock_client.place_market_order.return_value = {
            "id": "ord_a",
            "filled_size": 5,
            "avg_fill_price": 3100.0,
        }

        # Candle 1: GREEN -> Entry LONG
        self.mock_client.get_positions.return_value = [
            {"product_symbol": symbol, "size": 5, "side": "buy"}
        ]
        strat._apply_signal(symbol, "GREEN", price=3100.0)
        self.assertEqual(self.pm.state(symbol).position_direction, "LONG")
        self.assertEqual(self.pm.state(symbol).position_size, 5)
        self.assertEqual(self.pm.state(symbol).entry_count, 1)

        # Candle 2: GREEN -> Scale LONG
        self.mock_client.get_positions.return_value = [
            {"product_symbol": symbol, "size": 10, "side": "buy"}
        ]
        strat._apply_signal(symbol, "GREEN", price=3120.0)
        self.assertEqual(self.pm.state(symbol).position_size, 10)
        self.assertEqual(self.pm.state(symbol).entry_count, 2)

        # Candle 3: GREEN -> Scale LONG
        self.mock_client.get_positions.return_value = [
            {"product_symbol": symbol, "size": 15, "side": "buy"}
        ]
        strat._apply_signal(symbol, "GREEN", price=3140.0)
        self.assertEqual(self.pm.state(symbol).position_size, 15)
        self.assertEqual(self.pm.state(symbol).entry_count, 3)

        # Candle 4: RED -> Atomic Reversal
        # Update mock order response for the reversal market order fill price
        self.mock_client.place_market_order.return_value = {
            "id": "ord_a_rev",
            "filled_size": 15,
            "avg_fill_price": 3080.0,
        }
        call_count = 0
        def mock_get_pos():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"product_symbol": symbol, "size": 0, "side": "buy"}]
            return [{"product_symbol": symbol, "size": 5, "side": "sell"}]

        self.mock_client.get_positions.side_effect = mock_get_pos
        strat._apply_signal(symbol, "RED", price=3080.0)

        # State should be SHORT, 5 lots, entry count 1
        self.assertEqual(self.pm.state(symbol).position_direction, "SHORT")
        self.assertEqual(self.pm.state(symbol).position_size, 5)
        self.assertEqual(self.pm.state(symbol).entry_count, 1)

        # Trade history should contain the 3 closed long trades
        trades = self.db.get_trade_history(symbol)
        self.assertEqual(len(trades), 3)
        for t in trades:
            self.assertEqual(t["side"], "LONG")
            self.assertEqual(t["exit_price"], 3080.0)

    def test_scenario_b_red_red_green(self) -> None:
        """Requirement 23 Scenario B: RED -> RED -> GREEN (OILUSD)."""
        symbol = "OILUSD"
        strat = Strategy(client=self.mock_client, db=self.db, position_manager=self.pm)

        self.mock_client.place_market_order.return_value = {
            "id": "ord_b",
            "filled_size": 2,
            "avg_fill_price": 75.0,
        }

        # Candle 1: RED -> Entry SHORT
        self.mock_client.get_positions.return_value = [
            {"product_symbol": symbol, "size": 2, "side": "sell"}
        ]
        strat._apply_signal(symbol, "RED", price=75.0)
        self.assertEqual(self.pm.state(symbol).position_direction, "SHORT")
        self.assertEqual(self.pm.state(symbol).position_size, 2)
        self.assertEqual(self.pm.state(symbol).entry_count, 1)

        # Candle 2: RED -> Scale SHORT
        self.mock_client.get_positions.return_value = [
            {"product_symbol": symbol, "size": 4, "side": "sell"}
        ]
        strat._apply_signal(symbol, "RED", price=74.0)
        self.assertEqual(self.pm.state(symbol).position_size, 4)
        self.assertEqual(self.pm.state(symbol).entry_count, 2)

        # Candle 3: GREEN -> Reversal
        self.mock_client.place_market_order.return_value = {
            "id": "ord_b_rev",
            "filled_size": 4,
            "avg_fill_price": 76.0,
        }
        call_count = 0
        def mock_get_pos():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"product_symbol": symbol, "size": 0, "side": "sell"}]
            return [{"product_symbol": symbol, "size": 2, "side": "buy"}]

        self.mock_client.get_positions.side_effect = mock_get_pos
        strat._apply_signal(symbol, "GREEN", price=76.0)

        self.assertEqual(self.pm.state(symbol).position_direction, "LONG")
        self.assertEqual(self.pm.state(symbol).position_size, 2)
        self.assertEqual(self.pm.state(symbol).entry_count, 1)

        trades = self.db.get_trade_history(symbol)
        self.assertEqual(len(trades), 2)
        for t in trades:
            self.assertEqual(t["side"], "SHORT")
            self.assertEqual(t["exit_price"], 76.0)

    def test_deduplication_exact_once(self) -> None:
        """Requirement 11: Deduplication ensures each candle processed once."""
        symbol = "ETHUSD"
        strat = Strategy(client=self.mock_client, db=self.db, position_manager=self.pm)

        c1 = Candle(timestamp="2025-01-01T00:00:00Z", open=100.0, high=120.0, low=95.0, close=115.0)
        strat.fetch_closed_candles = MagicMock(return_value=[c1])
        strat.latest_closed_candle = MagicMock(return_value=c1)

        self.mock_client.place_market_order.return_value = {
            "id": "ord_dedup",
            "filled_size": 5,
            "avg_fill_price": 107.5,
        }
        self.mock_client.get_positions.return_value = [
            {"product_symbol": symbol, "size": 5, "side": "buy"}
        ]

        # First evaluation: processes candle
        strat.evaluate_symbol(symbol)
        self.assertEqual(self.pm.state(symbol).position_size, 5)

        # Second evaluation with same candle: must skip without additional orders
        strat.evaluate_symbol(symbol)
        self.assertEqual(self.pm.state(symbol).position_size, 5)
        self.assertEqual(self.mock_client.place_market_order.call_count, 1)


if __name__ == "__main__":
    unittest.main()
