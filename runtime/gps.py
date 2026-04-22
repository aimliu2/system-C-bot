"""Portfolio GPS reports for System C bot V2."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import pstdev
from typing import Any

from runtime.config import RuntimeConfig, load_runtime_config


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _drawdown(values: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def read_closed_trades(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    closed = [row for row in rows if row.get("close_time") and row.get("r_result") not in ("", None)]
    return sorted(closed, key=lambda row: _parse_time(row.get("close_time", "")) or datetime.min.replace(tzinfo=timezone.utc))


def filter_window(rows: list[dict[str, Any]], months: int | None, now: datetime) -> list[dict[str, Any]]:
    if months is None:
        return rows
    cutoff = now - timedelta(days=int(months * 30.4375))
    return [
        row for row in rows
        if (closed_at := _parse_time(row.get("close_time", ""))) is not None and closed_at >= cutoff
    ]


def compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    r_values = [_float(row.get("r_result")) for row in rows]
    months: dict[str, float] = {}
    symbols: dict[str, float] = {}
    for row, r_value in zip(rows, r_values):
        close_time = _parse_time(row.get("close_time", ""))
        if close_time is not None:
            month = close_time.strftime("%Y-%m")
            months[month] = months.get(month, 0.0) + r_value
        symbol = row.get("symbol")
        if symbol:
            symbols[symbol] = symbols.get(symbol, 0.0) + r_value
    return {
        "trade_count": len(rows),
        "total_r": sum(r_values),
        "max_dd_r": _drawdown(r_values),
        "worst_month_r": min(months.values()) if months else 0.0,
        "monthly_std_r": pstdev(months.values()) if len(months) > 1 else 0.0,
        "symbol_r": symbols,
    }


def classify(cfg: RuntimeConfig, metrics: dict[str, Any]) -> tuple[str, str, str]:
    seed = cfg.raw["gps"]["seed_baseline"]
    min_trades = int(cfg.raw["gps"].get("min_trade_count", 30))
    if metrics["trade_count"] < min_trades:
        return "GRAY", f"trade_count {metrics['trade_count']} < {min_trades}", "collect more data"
    if metrics["max_dd_r"] >= float(seed["max_dd_r"]) * 1.25:
        return "RED", "max drawdown exceeds seed guardrail by 25%", "pause and review execution plus symbol contribution"
    if metrics["worst_month_r"] <= float(seed["worst_month_r"]) * 1.25:
        return "RED", "worst month exceeds seed guardrail by 25%", "pause and review execution plus symbol contribution"
    if (
        metrics["worst_month_r"] <= float(seed["worst_month_r"])
        or metrics["max_dd_r"] >= float(seed["max_dd_r"])
        or metrics["total_r"] < 0
    ):
        return "YELLOW", "live rolling shape degraded versus seed guardrails", "review symbol contribution"
    return "GREEN", "live rolling shape remains inside interim guardrails", "continue"


def write_reports(cfg: RuntimeConfig) -> dict[str, Path]:
    log_paths = cfg.get_log_paths()
    trades = read_closed_trades(log_paths["trade"])
    now = datetime.now(timezone.utc)
    windows: list[tuple[str, int | None]] = [
        (f"{months}m", int(months))
        for months in cfg.raw["gps"].get("rolling_windows_months", [])
    ]
    if cfg.raw["gps"].get("include_full_history", True):
        windows.append(("full", None))
    window_rows = []
    for label, months in windows:
        metrics = compute_metrics(filter_window(trades, months, now))
        status, reason, action = classify(cfg, metrics)
        window_rows.append((label, metrics, status, reason, action))
    full_label, full_metrics, full_status, full_reason, full_action = window_rows[-1]

    gps_dir = cfg.get_log_dir() / cfg.raw["logs"]["gps_dir"]
    gps_dir.mkdir(parents=True, exist_ok=True)
    reports = cfg.raw["gps"]["reports"]
    metrics_path = gps_dir / reports["rolling_window_metrics"]
    report_path = gps_dir / reports["rolling_gps_report"]
    drift_path = gps_dir / reports["frontier_drift_report"]

    with metrics_path.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "window",
            "status",
            "reason",
            "review_action",
            "trade_count",
            "total_r",
            "max_dd_r",
            "worst_month_r",
            "monthly_std_r",
            "symbol_contribution",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for label, metrics, status, reason, action in window_rows:
            writer.writerow({
                "window": label,
                "status": status,
                "reason": reason,
                "review_action": action,
                "trade_count": metrics["trade_count"],
                "total_r": f"{metrics['total_r']:.2f}",
                "max_dd_r": f"{metrics['max_dd_r']:.2f}",
                "worst_month_r": f"{metrics['worst_month_r']:.2f}",
                "monthly_std_r": f"{metrics['monthly_std_r']:.2f}",
                "symbol_contribution": ";".join(
                    f"{symbol}:{value:.2f}"
                    for symbol, value in sorted(metrics["symbol_r"].items())
                ),
            })

    report_path.write_text(
        "\n".join([
            "# System C Portfolio GPS",
            "",
            f"status: {full_status}",
            f"reason: {full_reason}",
            f"review_action: {full_action}",
            f"window: {full_label}",
            f"trade_count: {full_metrics['trade_count']}",
            f"total_r: {full_metrics['total_r']:.2f}",
            f"max_dd_r: {full_metrics['max_dd_r']:.2f}",
            f"worst_month_r: {full_metrics['worst_month_r']:.2f}",
            f"monthly_std_r: {full_metrics['monthly_std_r']:.2f}",
            "",
            "windows:",
            *[
                (
                    f"- {label}: {status}, trades={metrics['trade_count']}, "
                    f"total_r={metrics['total_r']:.2f}, max_dd_r={metrics['max_dd_r']:.2f}"
                )
                for label, metrics, status, _, _ in window_rows
            ],
            "",
        ]),
        encoding="utf-8",
    )
    seed = cfg.raw["gps"]["seed_baseline"]
    drift_path.write_text(
        "\n".join([
            "# System C Frontier Drift",
            "",
            f"status: {full_status}",
            f"trade_count: {full_metrics['trade_count']}",
            f"seed_total_r: {float(seed['total_r']):.2f}",
            f"live_total_r: {full_metrics['total_r']:.2f}",
            f"delta_total_r: {full_metrics['total_r'] - float(seed['total_r']):.2f}",
            f"seed_max_dd_r: {float(seed['max_dd_r']):.2f}",
            f"live_max_dd_r: {full_metrics['max_dd_r']:.2f}",
            f"delta_max_dd_r: {full_metrics['max_dd_r'] - float(seed['max_dd_r']):.2f}",
            f"seed_worst_month_r: {float(seed['worst_month_r']):.2f}",
            f"live_worst_month_r: {full_metrics['worst_month_r']:.2f}",
            f"delta_worst_month_r: {full_metrics['worst_month_r'] - float(seed['worst_month_r']):.2f}",
            "",
        ]),
        encoding="utf-8",
    )
    return {"metrics": metrics_path, "report": report_path, "drift": drift_path}


def main() -> None:
    cfg = load_runtime_config()
    outputs = write_reports(cfg)
    for name, path in outputs.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
