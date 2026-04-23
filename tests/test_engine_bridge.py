from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from runtime.config import load_runtime_config
from runtime.engine_bridge import EngineBridge
from runtime.state_store import build_clean_state


class FakeCache:
    def has_frame(self, symbol: str, timeframe: str) -> bool:
        return True


class FakeEngine:
    def on_bar(self, feature_row, context_row):
        return []


class EngineBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_runtime_config()

    def test_cross_phase_symbol_only_evaluates_due_entry_timeframes(self) -> None:
        bridge = EngineBridge(self.cfg, FakeCache())  # type: ignore[arg-type]
        state = build_clean_state(self.cfg, "live")
        calls: list[tuple[str, str, str]] = []

        def aligned(symbol: str, entry_tf: str, context_tf: str, engine_cfg: dict):
            calls.append((symbol, entry_tf, context_tf))
            return pd.DataFrame(
                [{"st_dir": 1, "ctx_st_dir": 1, "close": 1.0}],
                index=[pd.Timestamp("2026-04-23T08:00:00Z")],
            )

        with (
            patch.object(bridge, "_aligned_features", side_effect=aligned),
            patch("runtime.engine_bridge.InstrumentEngine", return_value=FakeEngine()),
            patch("runtime.engine_bridge.hydrate_engine", side_effect=lambda engine, payload: engine),
            patch("runtime.engine_bridge.serialize_engine", return_value={}),
            patch("runtime.engine_bridge.policy_3", return_value=[]),
        ):
            result = bridge.evaluate_symbol(
                "AUDUSD",
                state["symbols"]["AUDUSD"],
                due_entry_timeframes={"15m"},
            )

        self.assertTrue(result.evaluated)
        self.assertEqual(calls, [("AUDUSD", "15m", "1h")])

    def test_cross_phase_symbol_evaluates_all_due_entry_timeframes(self) -> None:
        bridge = EngineBridge(self.cfg, FakeCache())  # type: ignore[arg-type]
        state = build_clean_state(self.cfg, "live")
        calls: list[tuple[str, str, str]] = []

        def aligned(symbol: str, entry_tf: str, context_tf: str, engine_cfg: dict):
            calls.append((symbol, entry_tf, context_tf))
            return pd.DataFrame(
                [{"st_dir": 1, "ctx_st_dir": 1, "close": 1.0}],
                index=[pd.Timestamp("2026-04-23T08:00:00Z")],
            )

        with (
            patch.object(bridge, "_aligned_features", side_effect=aligned),
            patch("runtime.engine_bridge.InstrumentEngine", return_value=FakeEngine()),
            patch("runtime.engine_bridge.hydrate_engine", side_effect=lambda engine, payload: engine),
            patch("runtime.engine_bridge.serialize_engine", return_value={}),
            patch("runtime.engine_bridge.policy_3", return_value=[]),
        ):
            result = bridge.evaluate_symbol(
                "AUDUSD",
                state["symbols"]["AUDUSD"],
                due_entry_timeframes={"5m", "15m"},
            )

        self.assertTrue(result.evaluated)
        self.assertEqual(calls, [
            ("AUDUSD", "15m", "1h"),
            ("AUDUSD", "5m", "1h"),
            ("AUDUSD", "5m", "1h"),
        ])


if __name__ == "__main__":
    unittest.main()
