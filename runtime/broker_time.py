"""Broker-server clock diagnostics for System C bot V2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from runtime.adapters import Mt5Adapter


@dataclass(frozen=True)
class BrokerTimeOffset:
    offset_hours: int
    status: str
    source_symbol: str
    broker_time: str
    utc_now: str
    detail: str = ""


def detect_broker_utc_offset(adapter: Mt5Adapter, *, symbol: str = "EURUSD") -> BrokerTimeOffset:
    """Detect broker server offset from UTC using the latest MT5 tick time.

    MT5 bar timestamps can be encoded in broker-server time. The offset changes
    with broker DST, so V2 detects it at startup instead of hard-coding UTC+2/3.
    """

    now = datetime.now(timezone.utc)
    try:
        tick = adapter.symbol_info_tick(symbol)
        tick_time = _get(tick, "time")
        if tick_time in (None, ""):
            return BrokerTimeOffset(
                0,
                "UNKNOWN",
                symbol,
                "",
                now.isoformat(),
                "symbol_info_tick returned no time",
            )

        broker_time = datetime.fromtimestamp(float(tick_time), tz=timezone.utc)
        raw_offset = (broker_time - now).total_seconds() / 3600
        offset = round(raw_offset)
        if not -12 <= offset <= 14:
            return BrokerTimeOffset(
                0,
                "IGNORED",
                symbol,
                broker_time.isoformat(),
                now.isoformat(),
                f"computed offset {raw_offset:.2f}h is outside timezone bounds",
            )
        return BrokerTimeOffset(
            offset,
            "OK",
            symbol,
            broker_time.isoformat(),
            now.isoformat(),
            f"raw_offset_hours={raw_offset:.2f}",
        )
    except Exception as exc:
        return BrokerTimeOffset(0, "ERROR", symbol, "", now.isoformat(), str(exc))


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
