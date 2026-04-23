from __future__ import annotations

import unittest

from runtime.config import load_runtime_config
from runtime.portfolio import PortfolioReducer
from runtime.state_store import build_clean_state


def candidate(symbol: str, candidate_id: str, *, bar_time: str = "2026-04-23T08:00:00+00:00") -> dict:
    return {
        "bar_time": bar_time,
        "symbol": symbol,
        "hypothesis": "A1",
        "direction": "short",
        "candidate_id": candidate_id,
    }


class PortfolioReducerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_runtime_config()

    def test_duplicate_candidate_id_is_rejected_after_first_decision(self) -> None:
        state = build_clean_state(self.cfg, "live")
        reducer = PortfolioReducer(self.cfg)

        first = reducer.reduce([candidate("AUDUSD", "AUDUSD-A1-test")], state)
        second = reducer.reduce([candidate("AUDUSD", "AUDUSD-A1-test")], state)

        self.assertEqual(len(first.accepted), 1)
        self.assertEqual(len(second.accepted), 0)
        self.assertEqual(second.rejected[0]["reject_reason"], "duplicate_candidate")
        self.assertIn("AUDUSD-A1-test", state["portfolio"]["seen_candidate_ids"])

    def test_same_batch_symbol_cap_counts_newly_accepted_candidate(self) -> None:
        state = build_clean_state(self.cfg, "live")
        reducer = PortfolioReducer(self.cfg)

        result = reducer.reduce([
            candidate("AUDUSD", "AUDUSD-A1-first"),
            candidate("AUDUSD", "AUDUSD-A1-second"),
        ], state)

        self.assertEqual([row["candidate_id"] for row in result.accepted], ["AUDUSD-A1-first"])
        self.assertEqual(result.rejected[0]["candidate_id"], "AUDUSD-A1-second")
        self.assertEqual(result.rejected[0]["reject_reason"], "symbol_cap_full")

    def test_open_trade_candidate_id_is_rejected_as_duplicate(self) -> None:
        state = build_clean_state(self.cfg, "live")
        state["open_trades"].append({"symbol": "AUDUSD", "candidate_id": "AUDUSD-A1-open"})
        reducer = PortfolioReducer(self.cfg)

        result = reducer.reduce([candidate("AUDUSD", "AUDUSD-A1-open")], state)

        self.assertEqual(len(result.accepted), 0)
        self.assertEqual(result.rejected[0]["reject_reason"], "duplicate_candidate")


if __name__ == "__main__":
    unittest.main()
