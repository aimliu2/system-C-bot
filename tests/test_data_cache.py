from __future__ import annotations

import unittest

import numpy as np

from runtime.data_cache import rates_to_frame


class RatesToFrameTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
