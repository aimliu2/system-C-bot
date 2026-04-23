from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from runtime.data_cache import rates_to_frame
from runtime.config import load_runtime_config
from runtime.market_probe import MarketDataProbe


class FakeProbeAdapter:
    name = "fake"

    def __init__(self, rates):
        self.rates = rates
        self.start_positions = []

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        self.start_positions.append(start_pos)
        return self.rates

    def timeframe_value(self, timeframe):
        return timeframe

    def last_error(self):
        return (0, "OK")


class RatesToFrameTests(unittest.TestCase):
    def test_accepts_empty_mt5_structured_array(self) -> None:
        rates = np.array(
            [],
            dtype=[
                ("time", "i8"),
                ("open", "f8"),
                ("high", "f8"),
                ("low", "f8"),
                ("close", "f8"),
                ("tick_volume", "i8"),
                ("spread", "i4"),
                ("real_volume", "i8"),
            ],
        )

        frame = rates_to_frame(rates, "5m")

        self.assertTrue(frame.empty)
        self.assertEqual(list(frame.columns), ["open", "high", "low", "close", "volume"])

    def test_accepts_mt5_structured_array(self) -> None:
        rates = np.array(
            [(1770000000, 1.10, 1.11, 1.09, 1.105, 100, 2, 0)],
            dtype=[
                ("time", "i8"),
                ("open", "f8"),
                ("high", "f8"),
                ("low", "f8"),
                ("close", "f8"),
                ("tick_volume", "i8"),
                ("spread", "i4"),
                ("real_volume", "i8"),
            ],
        )

        frame = rates_to_frame(rates, "5m")

        self.assertEqual(list(frame.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(float(frame.iloc[0]["close"]), 1.105)
        self.assertEqual(int(frame.iloc[0]["volume"]), 100)

    def test_recovers_standard_mt5_tuple_rows(self) -> None:
        rates = [(1770000000, 1.10, 1.11, 1.09, 1.105, 100, 2, 0)]

        frame = rates_to_frame(rates, "5m")

        self.assertEqual(float(frame.iloc[0]["open"]), 1.10)
        self.assertEqual(int(frame.iloc[0]["volume"]), 100)

    def test_applies_broker_utc_offset(self) -> None:
        rates = [(1770000000, 1.10, 1.11, 1.09, 1.105, 100, 2, 0)]

        frame = rates_to_frame(rates, "5m", broker_utc_offset_hours=3)

        self.assertEqual(frame.index[0].isoformat(), "2026-02-01T23:45:00+00:00")


class MarketDataProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_runtime_config()

    def probe(self, rates, timeframe: str = "5m", *, broker_utc_offset_hours: int = 0):
        adapter = FakeProbeAdapter(rates)
        probe = MarketDataProbe(self.cfg, adapter, broker_utc_offset_hours=broker_utc_offset_hours)
        return probe.probe("EURUSD", timeframe, request_bars=20).row, adapter

    def test_probe_classifies_empty_rates(self) -> None:
        row, adapter = self.probe([])

        self.assertEqual(row["status"], "EMPTY_RATES")
        self.assertEqual(row["raw_count"], 0)
        self.assertEqual(adapter.start_positions, [1])

    def test_probe_classifies_future_after_offset(self) -> None:
        future_open = datetime.now(timezone.utc) + timedelta(hours=1)
        rates = [(int(future_open.timestamp()), 1.10, 1.11, 1.09, 1.105, 100, 2, 0)]

        row, _ = self.probe(rates)

        self.assertEqual(row["status"], "FUTURE_AFTER_OFFSET")
        self.assertEqual(row["raw_count"], 1)
        self.assertEqual(row["closed_count"], 1)

    def test_probe_classifies_latest_closed_bar(self) -> None:
        closed_open = datetime.now(timezone.utc) - timedelta(minutes=10)
        rates = [(int(closed_open.timestamp()), 1.10, 1.11, 1.09, 1.105, 100, 2, 0)]

        row, _ = self.probe(rates)

        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["raw_count"], 1)
        self.assertEqual(row["closed_count"], 1)
        self.assertTrue(row["latest_closed_bar"])

    def test_probe_accepts_broker_server_time_after_offset(self) -> None:
        broker_open = datetime.now(timezone.utc) + timedelta(hours=3, minutes=-10)
        rates = [(int(broker_open.timestamp()), 1.10, 1.11, 1.09, 1.105, 100, 2, 0)]

        row, _ = self.probe(rates, broker_utc_offset_hours=3)

        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["closed_count"], 1)


if __name__ == "__main__":
    unittest.main()
