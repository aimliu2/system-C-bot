from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from runtime.config import load_runtime_config
from runtime.reconciliation import BrokerReconciler
from runtime.runner import SequentialPortfolioRunner
from runtime.state_store import build_clean_state


class FakeMt5:
    DEAL_REASON_SL = 1
    DEAL_REASON_TP = 2
    DEAL_REASON_CLIENT = 3
    DEAL_REASON_MOBILE = 4
    DEAL_REASON_WEB = 5
    DEAL_REASON_EXPERT = 6


class FakeAdapter:
    name = "fake"

    def __init__(self, history_by_position: dict[int, list[dict[str, Any]]] | None = None):
        self.mt5 = FakeMt5()
        self.history_by_position = history_by_position or {}

    def history_deals_get(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.history_by_position.get(int(kwargs["position"]), [])


class FakeLogger:
    def __init__(self):
        self.events: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []

    def trade(self, row: dict[str, Any]) -> None:
        self.trades.append(row)

    def event(self, event_type: str, **kwargs: Any) -> None:
        self.events.append({"event_type": event_type, **kwargs})


class ReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_runtime_config()

    def make_state(self, ticket: int = 101, *, direction: str = "long") -> dict[str, Any]:
        state = build_clean_state(self.cfg, "live")
        state["open_trades"].append({
            "ticket": ticket,
            "mode": "live",
            "symbol": "EURUSD",
            "candidate_id": "EURUSD-test",
            "hypothesis": "A1",
            "direction": direction,
            "session": "london",
            "entry_price": 1.1000,
            "sl_price": 1.0980,
            "tp_price": 1.1040,
            "risk_pct": self.cfg.base_risk_pct,
            "lot_size": 0.1,
            "open_time": "2026-04-22T00:00:00+00:00",
            "bar_time": "2026-04-22T00:00:00+00:00",
            "bars_held": 0,
        })
        return state

    def deals(self, *, exit_price: float, reason: int) -> list[dict[str, Any]]:
        return [
            {"entry": 0, "price": 1.1000, "time": 1770000000, "reason": 0},
            {"entry": 1, "price": exit_price, "time": 1770000300, "reason": reason},
        ]

    def reconcile(self, state: dict[str, Any], history: dict[int, list[dict[str, Any]]]):
        adapter = FakeAdapter(history)
        reconciler = BrokerReconciler(self.cfg, adapter)
        return reconciler.reconcile(state, broker_positions=[])

    def test_missing_broker_ticket_writes_close_row_and_clears_state(self) -> None:
        state = self.make_state(ticket=101)

        result = self.reconcile(state, {101: self.deals(exit_price=1.1040, reason=FakeMt5.DEAL_REASON_TP)})

        self.assertEqual(result.closed_tickets, {101})
        self.assertEqual(state["open_trades"], [])
        self.assertEqual(len(result.close_rows), 1)
        self.assertEqual(result.close_rows[0]["decision"], "broker_closed")
        self.assertTrue(result.close_rows[0]["close_time"])

    def test_sl_history_computes_minus_one_r(self) -> None:
        state = self.make_state(ticket=102)

        result = self.reconcile(state, {102: self.deals(exit_price=1.0980, reason=FakeMt5.DEAL_REASON_SL)})

        self.assertEqual(result.close_rows[0]["exit_reason"], "SL")
        self.assertEqual(result.close_rows[0]["r_result"], "-1.0000")

    def test_tp_history_computes_positive_r(self) -> None:
        state = self.make_state(ticket=103)

        result = self.reconcile(state, {103: self.deals(exit_price=1.1040, reason=FakeMt5.DEAL_REASON_TP)})

        self.assertEqual(result.close_rows[0]["exit_reason"], "TP")
        self.assertGreater(float(result.close_rows[0]["r_result"]), 0.0)

    def test_broker_magic_ticket_missing_from_state_blocks_entries(self) -> None:
        state = build_clean_state(self.cfg, "live")
        runner = SequentialPortfolioRunner(self.cfg, FakeAdapter())
        runner.logger = FakeLogger()
        saved_states: list[dict[str, Any]] = []
        runner.save_state = lambda payload: saved_states.append(deepcopy(payload))

        blocked = runner._reconcile_broker(
            state,
            broker_positions=[{"ticket": 999, "magic": self.cfg.magic_number, "symbol": "EURUSD", "volume": 0.1}],
            loop_id="test-loop",
        )

        self.assertTrue(blocked)
        self.assertEqual(state["diagnostics"]["last_invariant_status"], "BROKER_ORPHAN_POSITION")
        self.assertEqual(state["diagnostics"]["last_orphan_positions"][0]["ticket"], 999)
        self.assertTrue(any(event["event_type"] == "PORTFOLIO_ENTRY_BLOCKED" for event in runner.logger.events))
        self.assertTrue(saved_states)

    def test_no_history_clears_cap_only_with_degraded_review_marker(self) -> None:
        state = self.make_state(ticket=104)
        runner = SequentialPortfolioRunner(self.cfg, FakeAdapter(history_by_position={104: []}))
        runner.logger = FakeLogger()
        saved_states: list[dict[str, Any]] = []
        runner.save_state = lambda payload: saved_states.append(deepcopy(payload))

        blocked = runner._reconcile_broker(state, broker_positions=[], loop_id="test-loop")

        self.assertFalse(blocked)
        self.assertEqual(state["open_trades"], [])
        self.assertEqual(runner.logger.trades[0]["decision"], "broker_closed")
        self.assertEqual(runner.logger.trades[0]["exit_reason"], "UNKNOWN")
        self.assertEqual(runner.logger.trades[0]["r_result"], "")
        self.assertEqual(state["diagnostics"]["last_reconciliation_status"], "DEGRADED_HISTORY_MISSING")
        self.assertIn("104:no history deals", state["diagnostics"]["last_reconciliation_history_errors"])
        self.assertIn("Review broker history", state["diagnostics"]["last_review_action"])
        self.assertTrue(any(event["event_type"] == "BROKER_CLOSE_HISTORY_ERROR" for event in runner.logger.events))
        self.assertTrue(any(event["event_type"] == "BROKER_RECONCILIATION_DEGRADED" for event in runner.logger.events))
        self.assertTrue(saved_states)

    def test_gps_skips_inside_configured_interval(self) -> None:
        state = build_clean_state(self.cfg, "live")
        state["diagnostics"]["last_gps_run_time"] = datetime.now(timezone.utc).isoformat()
        runner = SequentialPortfolioRunner(self.cfg, FakeAdapter())
        runner.logger = FakeLogger()

        with patch("runtime.runner.write_reports") as write_reports:
            runner._maybe_write_gps(state, loop_id="test-loop")

        write_reports.assert_not_called()
        self.assertIn("not_due", state["diagnostics"]["last_gps_skip_reason"])

    def test_gps_runs_when_forced_by_trade_close(self) -> None:
        state = build_clean_state(self.cfg, "live")
        state["diagnostics"]["last_gps_run_time"] = datetime.now(timezone.utc).isoformat()
        runner = SequentialPortfolioRunner(self.cfg, FakeAdapter())
        runner.logger = FakeLogger()

        with patch("runtime.runner.write_reports", return_value={"report": "fake.md"}) as write_reports:
            runner._maybe_write_gps(state, loop_id="test-loop", force=True)

        write_reports.assert_called_once()
        self.assertEqual(state["diagnostics"]["last_gps_run_reason"], "trade_close")
        self.assertTrue(any(event["event_type"] == "GPS_REPORTS_WRITTEN" for event in runner.logger.events))

    def test_slow_stage_logs_deferred_feature_rebuild_watch(self) -> None:
        runner = SequentialPortfolioRunner(self.cfg, FakeAdapter())
        runner.logger = FakeLogger()

        runner._warn_if_slow("symbol_evaluation", 999999, loop_id="test-loop", symbol="EURUSD")

        self.assertTrue(any(
            event["event_type"] == "PERF_SLOW_STAGE"
            and "deferred_watch=incremental_feature_rebuild" in event["detail"]
            for event in runner.logger.events
        ))


if __name__ == "__main__":
    unittest.main()
