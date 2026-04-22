"""Closed-bar MT5 data cache for System C bot V2."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from typing import Any

import pandas as pd

from runtime.adapters import Mt5Adapter
from runtime.config import RuntimeConfig


MT5_RATE_COLUMNS = ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")
TIMEFRAME_DURATION = {
    "1min": "1min",
    "1m": "1min",
    "m1": "1min",
    "5m": "5min",
    "m5": "5min",
    "15m": "15min",
    "m15": "15min",
    "1h": "1h",
    "h1": "1h",
    "4h": "4h",
    "h4": "4h",
}


def normalize_timeframe(timeframe: str) -> str:
    value = str(timeframe).lower().strip()
    aliases = {
        "m1": "1min",
        "1m": "1min",
        "m5": "5m",
        "m15": "15m",
        "h1": "1h",
        "h4": "4h",
    }
    return aliases.get(value, value)


def timeframe_duration(timeframe: str) -> pd.Timedelta:
    normalized = normalize_timeframe(timeframe)
    if normalized not in TIMEFRAME_DURATION:
        raise ValueError(f"unsupported timeframe duration: {timeframe}")
    return pd.Timedelta(TIMEFRAME_DURATION[normalized])


def rates_to_frame(rates: Any, timeframe: str) -> pd.DataFrame:
    if _rates_empty(rates):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = _rates_dataframe(rates)
    if "time" not in df.columns:
        raise ValueError(f"MT5 rates are missing time column; columns={list(df.columns)}")
    if "tick_volume" in df.columns and "volume" not in df.columns:
        df = df.rename(columns={"tick_volume": "volume"})
    if "volume" not in df.columns:
        df["volume"] = 0
    open_time = pd.to_datetime(df["time"], unit="s", utc=True)
    close_time = open_time + timeframe_duration(timeframe)
    df.index = close_time
    df.index.name = "bar_close_time"
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def _rates_empty(rates: Any) -> bool:
    if rates is None:
        return True
    try:
        return len(rates) == 0
    except TypeError:
        return False


def _rates_dataframe(rates: Any) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    if "time" in df.columns:
        return df
    dtype_names = getattr(getattr(rates, "dtype", None), "names", None)
    if dtype_names:
        return pd.DataFrame.from_records(rates, columns=list(dtype_names))
    if len(df.columns) == len(MT5_RATE_COLUMNS):
        df.columns = MT5_RATE_COLUMNS
        return df
    return df


@dataclass
class CacheUpdate:
    symbol: str
    timeframe: str
    updated: bool
    latest_closed_bar: Any = None
    rows: int = 0
    error: str = ""


@dataclass
class BarCache:
    cfg: RuntimeConfig
    frames: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict)
    latest_closed: dict[tuple[str, str], pd.Timestamp] = field(default_factory=dict)

    def required_timeframes(self, symbol: str) -> set[str]:
        sym_cfg = self.cfg.symbols[symbol]
        phases = sym_cfg.raw.get("architecture_phases", {})
        required = {"1min"}
        if sym_cfg.cross_phase_enabled:
            for phase_name in sym_cfg.branch_phases.values():
                phase = phases[phase_name]
                required.add(normalize_timeframe(phase["entry_timeframe"]))
                required.add(normalize_timeframe(phase["context_timeframe"]))
        else:
            phase = phases[sym_cfg.selected_phase]
            required.add(normalize_timeframe(phase["entry_timeframe"]))
            required.add(normalize_timeframe(phase["context_timeframe"]))
        return required

    def _bar_count(self, timeframe: str, startup: bool) -> int:
        normalized = normalize_timeframe(timeframe)
        data_cfg = self.cfg.raw["data"]
        if startup:
            if normalized == "1min":
                return int(data_cfg["startup_bars"]["execution_1min"])
            if normalized in {"1h", "4h"}:
                return int(data_cfg["startup_bars"]["context"])
            return int(data_cfg["startup_bars"]["entry"])
        minimums = data_cfg["min_bars_by_timeframe"]
        return int(minimums.get(normalized, data_cfg["running_bars"]["entry"]))

    def warm_start(self, adapter: Mt5Adapter) -> list[CacheUpdate]:
        updates = []
        for symbol in self.cfg.deployment_symbols:
            for timeframe in sorted(self.required_timeframes(symbol)):
                updates.append(self.fetch_full(adapter, symbol, timeframe, startup=True))
        return updates

    def fetch_full(self, adapter: Mt5Adapter, symbol: str, timeframe: str, *, startup: bool) -> CacheUpdate:
        normalized = normalize_timeframe(timeframe)
        count = self._bar_count(normalized, startup=startup)
        try:
            rates = adapter.copy_rates_from_pos(symbol, adapter.timeframe_value(normalized), 0, count)
            frame = rates_to_frame(rates, normalized)
            frame = self._closed_only(frame)
            self.frames[(symbol, normalized)] = frame.tail(self._retain_count(normalized))
            if not frame.empty:
                self.latest_closed[(symbol, normalized)] = frame.index[-1]
            return CacheUpdate(symbol, normalized, True, self.latest_closed.get((symbol, normalized)), len(frame))
        except Exception as exc:
            return CacheUpdate(symbol, normalized, False, error=str(exc))

    def probe_latest_closed(self, adapter: Mt5Adapter, symbol: str, timeframe: str) -> pd.Timestamp | None:
        normalized = normalize_timeframe(timeframe)
        count = int(self.cfg.raw["data"]["cache"].get("latest_bar_probe_bars", 2))
        rates = adapter.copy_rates_from_pos(symbol, adapter.timeframe_value(normalized), 0, count)
        frame = rates_to_frame(rates, normalized)
        frame = self._closed_only(frame)
        if frame.empty:
            return None
        return frame.index[-1]

    def update_delta(self, adapter: Mt5Adapter, symbol: str, timeframe: str) -> CacheUpdate:
        normalized = normalize_timeframe(timeframe)
        latest = self.probe_latest_closed(adapter, symbol, normalized)
        if latest is None:
            return CacheUpdate(symbol, normalized, False, error="no closed bars in probe")
        key = (symbol, normalized)
        if self.latest_closed.get(key) == latest:
            return CacheUpdate(symbol, normalized, False, latest, rows=len(self.frames.get(key, [])))
        overlap = int(self.cfg.raw["data"]["cache"].get("fetch_overlap_bars", 2))
        count = max(overlap + 2, 4)
        rates = adapter.copy_rates_from_pos(symbol, adapter.timeframe_value(normalized), 0, count)
        delta = self._closed_only(rates_to_frame(rates, normalized))
        prior = self.frames.get(key, pd.DataFrame())
        combined = pd.concat([prior, delta]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.tail(self._retain_count(normalized))
        self.frames[key] = combined
        self.latest_closed[key] = combined.index[-1]
        return CacheUpdate(symbol, normalized, True, self.latest_closed[key], len(combined))

    def has_frame(self, symbol: str, timeframe: str) -> bool:
        frame = self.frames.get((symbol, normalize_timeframe(timeframe)))
        return frame is not None and not frame.empty

    def frame(self, symbol: str, timeframe: str) -> pd.DataFrame:
        return self.frames[(symbol, normalize_timeframe(timeframe))]

    def _retain_count(self, timeframe: str) -> int:
        normalized = normalize_timeframe(timeframe)
        return int(self.cfg.raw["data"]["cache"]["max_retained_bars"].get(normalized, 600))

    def _closed_only(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return frame
        now = pd.Timestamp.now(tz=timezone.utc)
        return frame[frame.index <= now].copy()
