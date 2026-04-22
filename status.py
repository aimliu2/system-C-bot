"""System C bot V2 status observer.

This is intentionally an observer-first tool for the portfolio V2 upgrade. It
loads ``bot/config/config.yaml`` through ``runtime.config`` and reads V2 state
through ``runtime.state_store``. Legacy CB/Highwind/rescale controls are kept
out of this path while those interventions are disabled or monitor-only.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.config import ConfigError, RuntimeConfig, load_runtime_config
from runtime.state_store import (
    atomic_write_json,
    build_clean_state,
    load_state,
    restore_state_from_template,
    validate_state_shape,
    verify_states,
)


GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
GRAY = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"

VALID_SYMBOL_MODES = {"live", "paper", "disabled"}
TRADE_LOG_REQUIRED_FIELDS = (
    "close_time",
    "symbol",
    "candidate_id",
    "hypothesis",
    "session",
    "exit_price",
    "r_result",
    "risk_pct",
    "decision",
    "portfolio_open_count",
    "exit_reason",
)


def color_bool(value: bool) -> str:
    return f"{GREEN}{value}{RESET}" if value else f"{GRAY}{value}{RESET}"


def color_mode(mode: str) -> str:
    if mode == "live":
        return f"{RED}live{RESET}"
    if mode == "paper":
        return f"{YELLOW}paper{RESET}"
    if mode == "disabled":
        return f"{GRAY}disabled{RESET}"
    return mode


def fmt_time(value: Any) -> str:
    if value in (None, ""):
        return f"{GRAY}N/A{RESET}"
    return str(value)


def file_summary(path: Path) -> str:
    if not path.exists():
        return f"{RED}missing{RESET}"
    stat = path.stat()
    if path.is_dir():
        count = len(list(path.iterdir()))
        return f"{GREEN}ok{RESET} dir, items={count}"
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"{GREEN}ok{RESET} {stat.st_size} bytes, mtime={mtime}"


def tail_csv(path: Path) -> dict[str, str] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return rows[-1] if rows else None


def csv_header(path: Path) -> list[str]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        return next(reader, [])


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def compact_file_state(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_dir():
        return f"ok dir items={len(list(path.iterdir()))}"
    stat = path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"ok bytes={stat.st_size} mtime={mtime}"


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def compute_drawdown(values: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def compute_trade_log_status(cfg: RuntimeConfig, trade_path: Path) -> dict[str, Any]:
    rows = read_csv_rows(trade_path)
    closed_rows = [
        row for row in rows
        if row.get("close_time") and row.get("r_result") not in (None, "")
    ]
    r_values = [parse_float(row.get("r_result")) for row in closed_rows]
    months: dict[str, float] = {}
    symbol_r: dict[str, float] = {}
    for row, r_value in zip(closed_rows, r_values):
        close_time = parse_datetime(row.get("close_time"))
        if close_time is not None:
            month = close_time.strftime("%Y-%m")
            months[month] = months.get(month, 0.0) + r_value
        symbol = row.get("symbol")
        if symbol:
            symbol_r[symbol] = symbol_r.get(symbol, 0.0) + r_value

    trade_count = len(closed_rows)
    total_r = sum(r_values)
    max_dd = compute_drawdown(r_values)
    worst_month = min(months.values()) if months else 0.0
    min_trade_count = int(cfg.raw.get("gps", {}).get("min_trade_count", 30))
    seed = cfg.raw["gps"]["seed_baseline"]

    if trade_count == 0:
        status = "GRAY"
        reason = "Seed baseline only; no closed trade rows in trade log."
        action = "Collect live trade rows before making portfolio-level judgment."
    elif trade_count < min_trade_count:
        status = "GRAY"
        reason = f"Only {trade_count} closed trades; minimum for GPS classification is {min_trade_count}."
        action = "Keep monitoring; do not change portfolio shape from sparse evidence."
    elif total_r < 0 or worst_month <= float(seed["worst_month_r"]):
        status = "YELLOW"
        reason = "Current trade log is degraded versus interim seed guardrails."
        action = "Review soon; inspect symbol contribution before changing deployment."
    else:
        status = "GREEN"
        reason = "Current trade log has enough rows and no interim hard degradation was detected."
        action = "Continue monitoring."

    return {
        "trade_count": trade_count,
        "total_r": total_r,
        "max_dd_r": max_dd,
        "worst_month_r": worst_month,
        "months": months,
        "symbols": sorted(symbol_r),
        "symbol_r": symbol_r,
        "status": status,
        "reason": reason,
        "action": action,
    }


def state_file_for_mode(cfg: RuntimeConfig, mode: str | None) -> tuple[str, Path]:
    selected = mode or ("paper" if cfg.paper_mode else "live")
    return selected, cfg.get_state_file(selected)


def load_validated_state(cfg: RuntimeConfig, mode: str) -> dict[str, Any]:
    path = cfg.get_state_file(mode)
    state = load_state(path)
    validate_state_shape(cfg, state, mode)
    return state


def print_header(cfg: RuntimeConfig, mode: str, state_path: Path) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode_label = f"{YELLOW}PAPER{RESET}" if mode == "paper" else f"{RED}LIVE{RESET}"
    print(f"\n{'=' * 78}")
    print(f"  {BOLD}System C V2 Status{RESET} [{mode_label}]")
    print(f"  {now}")
    print(f"  deployment={cfg.deployment_id}")
    print(f"  state={state_path}")
    print(f"{'=' * 78}")


def display_config(cfg: RuntimeConfig) -> None:
    raw = cfg.raw
    print(f"\n{BOLD}Runtime Config{RESET}")
    print(f"  Symbols          : {', '.join(cfg.deployment_symbols)}")
    print(f"  Portfolio cap    : {cfg.portfolio_cap}")
    print(f"  Base risk        : {cfg.base_risk_pct}%")
    print(f"  Global paper_mode: {color_bool(cfg.paper_mode)}")
    print(f"  Magic number     : {cfg.magic_number}")
    print(f"  Env file         : {cfg.env_file} ({file_summary(cfg.env_file)})")
    print(f"  Legacy config    : ignored by V2 ({cfg.bot_dir / 'config.yaml'})")
    notifications = raw.get("notifications", {})
    print(
        "  Notifications    : "
        f"enabled={color_bool(notifications.get('enabled', False))} "
        f"paper_trades={color_bool(notifications.get('paper_trades', False))} "
        f"live_trades={color_bool(notifications.get('live_trades', False))}"
    )

    intervention = raw["intervention"]
    print(f"\n{BOLD}Intervention Flags{RESET}")
    print(
        "  Highwind         : "
        f"enabled={color_bool(intervention['highwind']['enabled'])} "
        f"monitor_only={color_bool(intervention['highwind']['monitor_only'])}"
    )
    print(
        "  CB anchor        : "
        f"enabled={color_bool(intervention['cb_anchor']['enabled'])} "
        f"monitor_only={color_bool(intervention['cb_anchor']['monitor_only'])}"
    )
    print(f"  Rule 2           : enabled={color_bool(intervention['rule2']['enabled'])}")

    data = raw.get("data", {})
    cache = data.get("cache", {})
    indicators = raw.get("indicators", {})
    print(f"\n{BOLD}Data / Clock / Indicator Settings{RESET}")
    print(f"  Minimum resolution: {raw['clocks']['minimum_resolution']}")
    print(f"  Closed-bar probe  : {raw['clocks'].get('latest_closed_bar_probe_bars')} bars, "
          f"{raw['clocks'].get('latest_closed_bar_position')}")
    print(f"  Startup bars      : {data.get('startup_bars')}")
    print(f"  Running bars      : {data.get('running_bars')}")
    print(f"  Cache             : enabled={cache.get('enabled')} update={cache.get('update_mode')} "
          f"persist_to_disk={cache.get('persist_to_disk')}")
    print(f"  Indicators        : backend={indicators.get('backend')} "
          f"supertrend={indicators.get('supertrend_backend')} "
          f"compile_on_startup={indicators.get('compile_on_startup')}")


def display_state_files(cfg: RuntimeConfig) -> None:
    print(f"\n{BOLD}State Files{RESET}")
    for mode in ("paper", "live"):
        active = cfg.get_state_file(mode)
        template = cfg.get_state_template_file(mode)
        print(f"  {mode:<5} active  : {active} ({file_summary(active)})")
        print(f"  {mode:<5} template: {template} ({file_summary(template)})")


def display_portfolio(cfg: RuntimeConfig, state: dict[str, Any]) -> None:
    portfolio = state.get("portfolio", {})
    diagnostics = state.get("diagnostics", {})
    open_trades = state.get("open_trades", [])

    print(f"\n{BOLD}Portfolio State{RESET}")
    print(f"  State version     : {portfolio.get('state_version', 0)}")
    print(f"  Fresh deployment  : {color_bool(bool(state.get('fresh_deployment')))}")
    print(f"  Open trades       : {len(open_trades)} / {cfg.portfolio_cap}")
    print(f"  Base equity       : {portfolio.get('base_equity')}")
    print(f"  Peak equity       : {portfolio.get('peak_equity')}")
    print(f"  Next paper ticket : {portfolio.get('next_paper_ticket')}")

    rule2 = portfolio.get("rule2", {})
    cb = portfolio.get("cb_anchor", {})
    highwind = portfolio.get("highwind", {})
    print(f"  Rule 2            : enabled={rule2.get('enabled')} "
          f"triggered_today={rule2.get('triggered_today')} date={rule2.get('trigger_date')}")
    print(f"  CB anchor         : enabled={cb.get('enabled')} monitor_only={cb.get('monitor_only')} "
          f"would_trigger={cb.get('would_trigger')}")
    print(f"  Highwind          : enabled={highwind.get('enabled')} monitor_only={highwind.get('monitor_only')}")

    print(f"\n{BOLD}Diagnostics{RESET}")
    print(f"  Last loop id      : {fmt_time(diagnostics.get('last_loop_id'))}")
    print(f"  Last snapshot     : {fmt_time(diagnostics.get('last_snapshot_time'))}")
    print(f"  GPS status        : {fmt_time(diagnostics.get('last_gps_status'))}")
    print(f"  Invariant status  : {fmt_time(diagnostics.get('last_invariant_status'))}")


def display_symbols(cfg: RuntimeConfig, state: dict[str, Any]) -> None:
    print(f"\n{BOLD}Symbols{RESET}")
    print(f"  {'Symbol':<8} {'Mode':<18} {'Phase':<8} {'Combo':<10} {'XPhase':<8} Branches")
    print(f"  {'-' * 74}")
    symbols_state = state.get("symbols", {})
    for symbol in cfg.deployment_symbols:
        sym_cfg = cfg.symbols[symbol]
        sym_state = symbols_state.get(symbol, {})
        branches = ", ".join(f"{hyp}:{phase}" for hyp, phase in sym_cfg.branch_phases.items())
        print(
            f"  {symbol:<8} {color_mode(sym_state.get('mode', '?')):<18} "
            f"{sym_cfg.selected_phase:<8} {sym_cfg.selected_combo:<10} "
            f"{str(sym_cfg.cross_phase_enabled):<8} {branches}"
        )
        gates = ", ".join(
            f"{hyp}=gate:{sym_cfg.session_gates[hyp]} sessions:{'+'.join(sym_cfg.allowed_sessions[hyp])}"
            for hyp in sym_cfg.enabled_hypotheses
        )
        last_bars = sym_state.get("last_bar_times", {})
        engine_state = sym_state.get("engine_state", {})
        print(f"    gates      : {gates}")
        print(f"    last bars  : {last_bars or 'empty'}")
        print(f"    session    : {sym_state.get('session_state', {})}")
        print(f"    engine keys: {len(engine_state)}")


def display_open_trades(state: dict[str, Any]) -> None:
    open_trades = state.get("open_trades", [])
    print(f"\n{BOLD}Open Trades{RESET} ({len(open_trades)})")
    if not open_trades:
        print(f"  {GRAY}None{RESET}")
        return
    for trade in open_trades:
        symbol = trade.get("symbol", "?")
        direction = str(trade.get("direction", "?")).upper()
        hyp = trade.get("hypothesis", trade.get("strategy", "?"))
        mode = trade.get("mode", "?")
        ticket = trade.get("ticket", "?")
        print(f"  {symbol} {direction} [{hyp}] mode={mode} ticket={ticket}")
        print(f"    entry={trade.get('entry_price')} sl={trade.get('sl_price')} tp={trade.get('tp_price')}")
        print(f"    opened={trade.get('open_time')} bars={trade.get('bars_held', 0)}")


def display_logs(cfg: RuntimeConfig) -> None:
    print(f"\n{BOLD}Logs{RESET}")
    log_dir = cfg.get_log_dir()
    gps_dir = log_dir / cfg.raw["logs"]["gps_dir"]
    print(f"  Base dir: {log_dir} ({file_summary(log_dir)})")
    for label, path in cfg.get_log_paths().items():
        last = tail_csv(path)
        tail_text = ""
        if last:
            event = last.get("event_type") or last.get("decision") or last.get("symbol") or "last row"
            tail_text = f" last={event}"
        print(f"  {label:<11}: {path.name:<28} {file_summary(path)}{tail_text}")

    print(f"  gps dir    : {gps_dir} ({file_summary(gps_dir)})")
    reports = cfg.raw.get("gps", {}).get("reports", {})
    for name, filename in reports.items():
        path = gps_dir / filename
        print(f"  gps {name:<22}: {file_summary(path)}")


def display_gps_check(cfg: RuntimeConfig, state: dict[str, Any], mode: str, state_path: Path) -> None:
    diagnostics = state.get("diagnostics", {})
    log_paths = cfg.get_log_paths()
    gps_dir = cfg.get_log_dir() / cfg.raw["logs"]["gps_dir"]
    gps_reports = cfg.raw.get("gps", {}).get("reports", {})
    trade_path = log_paths["trade"]
    trade_header = csv_header(trade_path)
    trade_status = compute_trade_log_status(cfg, trade_path)
    seed = cfg.raw["gps"]["seed_baseline"]
    missing_trade_fields = [
        field for field in TRADE_LOG_REQUIRED_FIELDS
        if field not in trade_header
    ] if trade_header else list(TRADE_LOG_REQUIRED_FIELDS)

    print("System C V2 GPS readiness")
    print(f"mode={mode}")
    print(f"state_file={state_path}")
    print(f"deployment_id={state.get('deployment_id')}")
    print(f"last_gps_status={diagnostics.get('last_gps_status', 'N/A')}")
    print(f"last_gps_run_time={diagnostics.get('last_gps_run_time', 'N/A')}")
    print(f"last_gps_run_reason={diagnostics.get('last_gps_run_reason', 'N/A')}")
    print(f"last_gps_skip_reason={diagnostics.get('last_gps_skip_reason', 'N/A')}")
    print(f"last_loop_id={diagnostics.get('last_loop_id', 'N/A')}")
    print(f"last_snapshot_time={diagnostics.get('last_snapshot_time', 'N/A')}")
    print("")

    print("seed_baseline")
    print(f"label={seed['label']}")
    print(f"symbols={','.join(seed['symbols'])}")
    print(f"portfolio_cap={seed['portfolio_cap']}")
    print(f"base_risk_pct={seed['base_risk_pct']}")
    print(f"total_r={seed['total_r']}")
    print(f"ev_per_trade_r={seed['ev_per_trade_r']}")
    print(f"max_dd_r={seed['max_dd_r']}")
    print(f"r_over_dd={seed['r_over_dd']}")
    print(f"worst_month_r={seed['worst_month_r']}")
    print(f"monthly_std_r={seed['monthly_std_r']}")
    print("")

    print("logs")
    for label in ("event", "snapshot", "trade", "signal", "candidate", "reducer", "timing", "state_audit"):
        path = log_paths[label]
        last = tail_csv(path)
        if last:
            last_marker = (
                last.get("event_type")
                or last.get("decision")
                or last.get("symbol")
                or last.get("timestamp")
                or "last row"
            )
        else:
            last_marker = "none"
        print(f"{label}={compact_file_state(path)} last={last_marker}")
    print("")

    print("trade_log_contract")
    print(f"path={trade_path}")
    print(f"status={compact_file_state(trade_path)}")
    print(f"required_fields={','.join(TRADE_LOG_REQUIRED_FIELDS)}")
    print(f"missing_fields={','.join(missing_trade_fields) if missing_trade_fields else 'none'}")
    print("writer_status=not built" if missing_trade_fields else "writer_status=field contract present")
    print("")

    print("current_interim_status")
    if trade_status["trade_count"] == 0:
        print("source=seed_baseline")
        print(f"status={seed['status']}")
        print(f"reason={seed['reason']}")
        print("review_action=Collect live trade rows before making portfolio-level judgment.")
    else:
        print("source=trade_log")
        print(f"status={trade_status['status']}")
        print(f"reason={trade_status['reason']}")
        print(f"review_action={trade_status['action']}")
        print(f"closed_trades={trade_status['trade_count']}")
        print(f"total_r={trade_status['total_r']:.2f}")
        print(f"max_dd_r={trade_status['max_dd_r']:.2f}")
        print(f"worst_month_r={trade_status['worst_month_r']:.2f}")
        print("symbols=" + (",".join(trade_status["symbols"]) if trade_status["symbols"] else "none"))
    print("")

    print("gps_outputs")
    print(f"gps_dir={compact_file_state(gps_dir)}")
    for name, filename in gps_reports.items():
        path = gps_dir / filename
        print(f"{name}={compact_file_state(path)} path={path}")
    print("")

    gps_module = cfg.bot_dir / "runtime" / "gps.py"
    rolling_metrics = gps_dir / gps_reports.get("rolling_window_metrics", "rolling_window_metrics.csv")
    rolling_report = gps_dir / gps_reports.get("rolling_gps_report", "rolling_gps_report.md")
    frontier_report = gps_dir / gps_reports.get("frontier_drift_report", "frontier_drift_report.md")

    print("implementation_readiness")
    print(f"runtime_gps_py={'present' if gps_module.exists() else 'missing'}")
    print(f"trade_log_writer={'ready' if trade_path.exists() and not missing_trade_fields else 'not built'}")
    print(f"rolling_window_metrics={'generated' if rolling_metrics.exists() else 'not generated'}")
    print(f"rolling_gps_report={'generated' if rolling_report.exists() else 'not generated'}")
    print(f"frontier_drift_comparison={'generated' if frontier_report.exists() else 'not built'}")
    print("")

    print("target_design")
    print("runner_execution_writes=close_time,symbol,candidate_id,hypothesis,session,r_result,risk_pct,decision,portfolio_open_count,exit_reason")
    print("runtime_gps_reads=trade_logs")
    print("runtime_gps_computes=3m,6m,12m,24m,full,total_R,max_DD,worst_month,monthly_std,symbol_contribution")
    print("runtime_gps_classifies=GRAY,GREEN,YELLOW,RED")
    print("status_reads=gps_outputs")
    print("status_shows=current_conclusion,why,review_action_suggestion")


def display(cfg: RuntimeConfig, state: dict[str, Any], mode: str, state_path: Path) -> None:
    print_header(cfg, mode, state_path)
    display_config(cfg)
    display_state_files(cfg)
    display_portfolio(cfg, state)
    display_symbols(cfg, state)
    display_open_trades(state)
    display_logs(cfg)
    print(f"\n{'=' * 78}\n")


def confirm_live_reset() -> bool:
    prompt = f"{RED}Type CONFIRM to reset live V2 state: {RESET}"
    return input(prompt).strip() == "CONFIRM"


def set_symbol_mode(cfg: RuntimeConfig, mode: str, symbol: str, new_symbol_mode: str) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol not in cfg.deployment_symbols:
        raise ConfigError(f"unknown symbol {symbol}; expected one of {', '.join(cfg.deployment_symbols)}")
    if new_symbol_mode not in VALID_SYMBOL_MODES:
        raise ConfigError(f"invalid symbol mode {new_symbol_mode!r}")

    state_path = cfg.get_state_file(mode)
    state = load_validated_state(cfg, mode)
    old_mode = state["symbols"][symbol].get("mode")
    state["symbols"][symbol]["mode"] = new_symbol_mode
    state["portfolio"]["state_version"] = int(state["portfolio"].get("state_version", 0)) + 1
    state.setdefault("_reset_log", []).append({
        "action": "set-symbol-mode",
        "symbol": symbol,
        "state_mode": mode,
        "old_mode": old_mode,
        "new_mode": new_symbol_mode,
        "time": datetime.now(timezone.utc).isoformat(),
    })
    validate_state_shape(cfg, state, mode)
    atomic_write_json(state_path, state)
    print(f"Set {symbol} mode in {mode} state: {old_mode} -> {new_symbol_mode}")
    return state


def reset_one_state(cfg: RuntimeConfig, mode: str) -> Path:
    state = build_clean_state(cfg, mode)
    path = cfg.get_state_file(mode)
    atomic_write_json(path, state)
    validate_state_shape(cfg, state, mode)
    return path


def unavailable_control(name: str) -> None:
    print(f"{YELLOW}{name} is unavailable in V2 status.{RESET}")
    print("This tool is observation-first while CB and Highwind are disabled/monitor-only.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="System C V2 status observer")
    parser.add_argument("--paper", action="store_true", help="view paper state")
    parser.add_argument(
        "--live",
        nargs="?",
        const="__view__",
        metavar="SYMBOL",
        help="view live state, or set SYMBOL to live in live state",
    )
    parser.add_argument("--shadow", metavar="SYMBOL", help="set SYMBOL to paper in paper state")
    parser.add_argument("--disable", metavar="SYMBOL", help="set SYMBOL disabled in selected state")
    parser.add_argument("--state", choices=("paper", "live"), help="state file for --disable")
    parser.add_argument("--verify", action="store_true", help="verify config and both V2 states")
    parser.add_argument("--clear-paper", action="store_true", help="reset paper V2 state from clean template")
    parser.add_argument("--clear-live", action="store_true", help="reset live V2 state from clean template")
    parser.add_argument("--restore-paper", action="store_true", help="restore paper active state from template")
    parser.add_argument("--restore-live", action="store_true", help="restore live active state from template")
    parser.add_argument("--gps-check", action="store_true", help="print compact GPS/log readiness status")
    parser.add_argument("--reset", action="store_true", help="legacy V1 control; unavailable in V2 status")
    parser.add_argument("--reset-highwind", action="store_true", help="legacy V1 control; unavailable in V2 status")
    parser.add_argument("--rescale", action="store_true", help="legacy V1 control; unavailable in V2 status")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        cfg = load_runtime_config()

        if args.reset or args.reset_highwind or args.rescale:
            unavailable_control("--reset/--reset-highwind/--rescale")
            return 2

        if args.verify:
            verify_states(cfg)
            print("V2 config/state verification: OK")
            return 0

        if args.gps_check:
            force_mode = "live" if args.live == "__view__" else "paper" if args.paper else None
            mode, path = state_file_for_mode(cfg, force_mode)
            state = load_validated_state(cfg, mode)
            display_gps_check(cfg, state, mode, path)
            return 0

        if args.clear_live and not confirm_live_reset():
            print("Aborted.")
            return 1

        if args.clear_paper:
            path = reset_one_state(cfg, "paper")
            verify_states(cfg)
            print(f"wrote clean paper state: {path}")
            return 0

        if args.clear_live:
            path = reset_one_state(cfg, "live")
            verify_states(cfg)
            print(f"wrote clean live state: {path}")
            return 0

        if args.restore_paper:
            print(f"restored paper: {restore_state_from_template(cfg, 'paper')}")
            return 0
        if args.restore_live:
            if not confirm_live_reset():
                print("Aborted.")
                return 1
            print(f"restored live: {restore_state_from_template(cfg, 'live')}")
            return 0

        if args.live and args.live != "__view__":
            state = set_symbol_mode(cfg, "live", args.live, "live")
            display(cfg, state, "live", cfg.get_state_file("live"))
            return 0

        if args.shadow:
            state = set_symbol_mode(cfg, "paper", args.shadow, "paper")
            display(cfg, state, "paper", cfg.get_state_file("paper"))
            return 0

        if args.disable:
            mode = args.state or ("live" if args.live == "__view__" else "paper")
            state = set_symbol_mode(cfg, mode, args.disable, "disabled")
            display(cfg, state, mode, cfg.get_state_file(mode))
            return 0

        force_mode = None
        if args.paper:
            force_mode = "paper"
        if args.live == "__view__":
            force_mode = "live"

        mode, path = state_file_for_mode(cfg, force_mode)
        state = load_validated_state(cfg, mode)
        display(cfg, state, mode, path)
        return 0
    except ConfigError as exc:
        print(f"{RED}V2 status error:{RESET} {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
