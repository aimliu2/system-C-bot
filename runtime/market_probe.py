"""One-shot MT5 market-data probe for System C bot V2."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from runtime.adapters import Mt5Adapter
from runtime.broker_time import detect_broker_utc_offset
from runtime.config import RuntimeConfig
from runtime.data_cache import (
    _rates_dataframe,
    _rates_empty,
    normalize_timeframe,
    rates_to_frame,
    timeframe_duration,
)


PROBE_FIELDS = (
    "timestamp",
    "adapter",
    "symbol",
    "timeframe",
    "request_bars",
    "raw_count",
    "raw_columns",
    "raw_first_open_time",
    "raw_last_open_time",
    "raw_last_close_time",
    "closed_count",
    "latest_closed_bar",
    "now_utc",
    "status",
    "mt5_last_error",
    "error",
)


@dataclass(frozen=True)
class ProbeResult:
    row: dict[str, Any]

    @property
    def status(self) -> str:
        return str(self.row.get("status", ""))


class MarketDataProbe:
    def __init__(self, cfg: RuntimeConfig, adapter: Mt5Adapter, *, broker_utc_offset_hours: int = 0):
        self.cfg = cfg
        self.adapter = adapter
        self.broker_utc_offset_hours = int(broker_utc_offset_hours)

    def run(self, *, bars: int | None = None, yyyymm: str | None = None) -> list[ProbeResult]:
        request_bars = bars or int(self.cfg.raw.get("data", {}).get("cache", {}).get("latest_bar_probe_bars", 2))
        results: list[ProbeResult] = []
        for symbol in self.cfg.deployment_symbols:
            for timeframe in sorted(self._required_timeframes(symbol)):
                result = self.probe(symbol, timeframe, request_bars=request_bars)
                results.append(result)
                append_probe_row(self.probe_log_path(yyyymm), result.row)
        return results

    def probe(self, symbol: str, timeframe: str, *, request_bars: int) -> ProbeResult:
        normalized = normalize_timeframe(timeframe)
        now = datetime.now(timezone.utc)
        row: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "adapter": self.adapter.name,
            "symbol": symbol,
            "timeframe": normalized,
            "request_bars": request_bars,
            "raw_count": 0,
            "raw_columns": "",
            "raw_first_open_time": "",
            "raw_last_open_time": "",
            "raw_last_close_time": "",
            "closed_count": 0,
            "latest_closed_bar": "",
            "now_utc": now.isoformat(),
            "status": "",
            "mt5_last_error": "",
            "error": f"start_pos=1 broker_utc_offset_hours={self.broker_utc_offset_hours}",
        }
        try:
            rates = self.adapter.copy_rates_from_pos(symbol, self.adapter.timeframe_value(normalized), 1, request_bars)
            raw_count = _safe_len(rates)
            row["raw_count"] = raw_count
            row["mt5_last_error"] = _safe_last_error(self.adapter)
            if _rates_empty(rates):
                row["status"] = "EMPTY_RATES"
                return ProbeResult(row)

            raw_frame = _rates_dataframe(rates)
            row["raw_columns"] = ",".join(str(column) for column in raw_frame.columns)
            if "time" not in raw_frame.columns:
                row["status"] = "MISSING_TIME"
                row["error"] = f"columns={list(raw_frame.columns)}"
                return ProbeResult(row)

            open_times = pd.to_datetime(raw_frame["time"], unit="s", utc=True, errors="coerce").dropna()
            if open_times.empty:
                row["status"] = "BAD_TIME"
                row["error"] = "time column could not be parsed as epoch seconds"
                return ProbeResult(row)

            row["raw_first_open_time"] = open_times.iloc[0].isoformat()
            row["raw_last_open_time"] = open_times.iloc[-1].isoformat()
            row["raw_last_close_time"] = (open_times.iloc[-1] + timeframe_duration(normalized)).isoformat()

            frame = rates_to_frame(
                rates,
                normalized,
                broker_utc_offset_hours=self.broker_utc_offset_hours,
            )
            row["closed_count"] = len(frame)
            if frame.empty:
                row["status"] = "EMPTY_CLOSED_FRAME"
                return ProbeResult(row)

            row["latest_closed_bar"] = frame.index[-1].isoformat()
            latest = frame.index[-1]
            if latest > pd.Timestamp(now) + pd.Timedelta(seconds=30):
                row["status"] = "FUTURE_AFTER_OFFSET"
            else:
                row["status"] = "OK"
            return ProbeResult(row)
        except Exception as exc:
            row["status"] = "ERROR"
            row["mt5_last_error"] = _safe_last_error(self.adapter)
            row["error"] = str(exc)
            return ProbeResult(row)

    def probe_log_path(self, yyyymm: str | None = None) -> Path:
        month = yyyymm or datetime.now(timezone.utc).strftime("%Y%m")
        return self.cfg.get_log_dir() / f"market_probe_{month}.csv"

    def _required_timeframes(self, symbol: str) -> set[str]:
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


def append_probe_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROBE_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in PROBE_FIELDS})


def run_market_data_probe(cfg: RuntimeConfig, adapter: Mt5Adapter, *, bars: int | None = None) -> list[ProbeResult]:
    adapter.connect()
    try:
        offset = detect_broker_utc_offset(adapter)
        print(
            (
                f"Broker UTC offset: UTC{offset.offset_hours:+d} "
                f"status={offset.status} broker_time={offset.broker_time or 'N/A'} "
                f"utc_now={offset.utc_now} detail={offset.detail}"
            ),
            flush=True,
        )
        probe = MarketDataProbe(cfg, adapter, broker_utc_offset_hours=offset.offset_hours)
        results = probe.run(bars=bars)
        print_probe_summary(probe.probe_log_path(), results)
        return results
    finally:
        adapter.close()


def print_probe_summary(path: Path, results: list[ProbeResult]) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    print(f"Market-data probe wrote: {path}", flush=True)
    print("Probe status counts: " + ", ".join(f"{key}={counts[key]}" for key in sorted(counts)), flush=True)
    for result in results:
        row = result.row
        print(
            (
                f"{row['symbol']} {row['timeframe']}: status={row['status']} "
                f"raw_count={row['raw_count']} closed_count={row['closed_count']} "
                f"latest_closed={row['latest_closed_bar'] or 'N/A'} "
                f"last_error={row['mt5_last_error'] or 'N/A'}"
            ),
            flush=True,
        )


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except TypeError:
        return 0


def _safe_last_error(adapter: Mt5Adapter) -> str:
    try:
        return str(adapter.last_error())
    except Exception as exc:
        return f"last_error failed: {exc}"
