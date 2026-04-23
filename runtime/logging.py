"""CSV logging helpers for System C bot V2."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.config import RuntimeConfig


EVENT_FIELDS = ("timestamp", "loop_id", "event_type", "symbol", "detail")
SNAPSHOT_FIELDS = (
    "timestamp",
    "loop_id",
    "mode",
    "open_trades",
    "portfolio_cap",
    "evaluated_symbols",
    "skipped_disabled",
    "skipped_no_new_bar",
    "skipped_data_error",
    "skipped_engine_not_ready",
)
SIGNAL_FIELDS = (
    "timestamp",
    "loop_id",
    "bar_time",
    "symbol",
    "entry_timeframe",
    "context_timeframe",
    "eval_status",
    "signal_status",
    "reason",
    "hypothesis",
    "direction",
    "candidate_id",
)
CANDIDATE_FIELDS = (
    "timestamp",
    "loop_id",
    "bar_time",
    "symbol",
    "hypothesis",
    "direction",
    "candidate_id",
    "status",
    "detail",
)
REDUCER_FIELDS = (
    "timestamp",
    "loop_id",
    "bar_time",
    "symbol",
    "hypothesis",
    "candidate_id",
    "decision",
    "reject_reason",
    "portfolio_open_count_before",
    "symbol_open_count_before",
)
TRADE_FIELDS = (
    "open_time",
    "close_time",
    "symbol",
    "candidate_id",
    "hypothesis",
    "direction",
    "session",
    "entry_price",
    "exit_price",
    "sl",
    "tp",
    "r_result",
    "risk_pct",
    "decision",
    "portfolio_open_count",
    "exit_reason",
    "ticket",
    "mode",
    "lot_size",
)
TIMING_FIELDS = ("timestamp", "loop_id", "stage", "duration_ms", "detail")
STATE_AUDIT_FIELDS = ("timestamp", "loop_id", "symbol", "action", "state_version", "detail")

LOG_FIELDSETS = {
    "event": EVENT_FIELDS,
    "snapshot": SNAPSHOT_FIELDS,
    "signal": SIGNAL_FIELDS,
    "candidate": CANDIDATE_FIELDS,
    "reducer": REDUCER_FIELDS,
    "trade": TRADE_FIELDS,
    "timing": TIMING_FIELDS,
    "state_audit": STATE_AUDIT_FIELDS,
}


class RuntimeLogger:
    def __init__(self, cfg: RuntimeConfig):
        self.cfg = cfg
        self.write_failures = 0
        self._last_write_failure = ""

    def _append(self, path: Path, fields: tuple[str, ...], row: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_header(path, fields)
            with path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writerow({field: row.get(field, "") for field in fields})
        except OSError as exc:
            self._record_write_failure(path, exc)

    def _record_write_failure(self, path: Path, exc: OSError) -> None:
        self.write_failures += 1
        message = f"LOG_WRITE_FAILED path={path} error={type(exc).__name__}: {exc}"
        if message != self._last_write_failure:
            print(message, flush=True)
            self._last_write_failure = message

    def _ensure_header(self, path: Path, fields: tuple[str, ...]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 0:
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.reader(handle)
                header = next(reader, [])
                has_rows = any(True for _ in reader)
            if header == list(fields):
                return
            if has_rows:
                raise RuntimeError(f"log schema mismatch with existing rows: {path}")
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()

    def ensure_headers(self) -> None:
        paths = self.cfg.get_log_paths()
        for label, fields in LOG_FIELDSETS.items():
            try:
                self._ensure_header(paths[label], fields)
            except OSError as exc:
                self._record_write_failure(paths[label], exc)

    def event(self, event_type: str, *, loop_id: str = "", symbol: str = "", detail: str = "") -> None:
        path = self.cfg.get_log_paths()["event"]
        self._append(path, EVENT_FIELDS, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "loop_id": loop_id,
            "event_type": event_type,
            "symbol": symbol,
            "detail": detail,
        })

    def snapshot(self, *, loop_id: str, mode: str, open_trades: int, portfolio_cap: int,
                 evaluated_symbols: int, skipped_disabled: int, skipped_no_new_bar: int,
                 skipped_data_error: int, skipped_engine_not_ready: int) -> None:
        path = self.cfg.get_log_paths()["snapshot"]
        self._append(path, SNAPSHOT_FIELDS, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "loop_id": loop_id,
            "mode": mode,
            "open_trades": open_trades,
            "portfolio_cap": portfolio_cap,
            "evaluated_symbols": evaluated_symbols,
            "skipped_disabled": skipped_disabled,
            "skipped_no_new_bar": skipped_no_new_bar,
            "skipped_data_error": skipped_data_error,
            "skipped_engine_not_ready": skipped_engine_not_ready,
        })

    def candidate(self, row: dict[str, Any]) -> None:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **row}
        self._append(self.cfg.get_log_paths()["candidate"], CANDIDATE_FIELDS, payload)

    def signal(self, row: dict[str, Any]) -> None:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **row}
        self._append(self.cfg.get_log_paths()["signal"], SIGNAL_FIELDS, payload)

    def reducer(self, row: dict[str, Any]) -> None:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **row}
        self._append(self.cfg.get_log_paths()["reducer"], REDUCER_FIELDS, payload)

    def trade(self, row: dict[str, Any]) -> None:
        self._append(self.cfg.get_log_paths()["trade"], TRADE_FIELDS, row)

    def timing(self, row: dict[str, Any]) -> None:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **row}
        self._append(self.cfg.get_log_paths()["timing"], TIMING_FIELDS, payload)

    def state_audit(self, row: dict[str, Any]) -> None:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **row}
        self._append(self.cfg.get_log_paths()["state_audit"], STATE_AUDIT_FIELDS, payload)
