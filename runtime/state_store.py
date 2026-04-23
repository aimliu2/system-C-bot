"""State helpers for System C bot architecture v2."""

from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    from runtime.config import ConfigError, RuntimeConfig, load_runtime_config
except ModuleNotFoundError:  # pragma: no cover - useful when run as a file.
    from config import ConfigError, RuntimeConfig, load_runtime_config


STATE_VERSION = "2.0"


def build_clean_state(cfg: RuntimeConfig, mode: str) -> dict[str, Any]:
    if mode not in {"paper", "live"}:
        raise ConfigError(f"state mode must be 'paper' or 'live', got {mode!r}")

    symbol_mode = "paper" if mode == "paper" else "live"
    return {
        "_comment": f"System C portfolio state v2 - {mode}",
        "_version": STATE_VERSION,
        "deployment_id": cfg.deployment_id,
        "fresh_deployment": True,
        "mode": mode,
        "portfolio": {
            "base_equity": None,
            "peak_equity": None,
            "rule2": {
                "enabled": cfg.is_rule2_enabled(),
                "base_equity": None,
                "triggered_today": False,
                "trigger_date": None,
            },
            "cb_anchor": {
                "enabled": cfg.is_cb_enabled(),
                "monitor_only": cfg.is_cb_monitor_only(),
                "peak": None,
                "anchor": None,
                "would_trigger": False,
                "last_would_trigger_time": None,
            },
            "highwind": {
                "enabled": cfg.is_highwind_enabled(),
                "monitor_only": cfg.is_highwind_monitor_only(),
            },
            "next_paper_ticket": -1,
            "state_version": 0,
            "seen_candidate_ids": [],
        },
        "symbols": {
            symbol: {
                "mode": symbol_mode,
                "engine_state": {},
                "last_bar_times": {},
                "session_state": {"session": None, "session_bar": -1},
                "session_summary": {},
                "highwind_monitor_state": {},
            }
            for symbol in cfg.deployment_symbols
        },
        "open_trades": [],
        "diagnostics": {
            "last_loop_id": None,
            "last_snapshot_time": None,
            "last_gps_status": "GRAY",
            "last_invariant_status": None,
        },
        "_reset_log": [],
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"state file is corrupted JSON: {path}: {exc}") from exc


def validate_state_shape(cfg: RuntimeConfig, state: dict[str, Any], mode: str) -> None:
    if state.get("_version") != STATE_VERSION:
        raise ConfigError(f"{mode} state has wrong version: {state.get('_version')!r}")
    if state.get("deployment_id") != cfg.deployment_id:
        raise ConfigError(f"{mode} state deployment_id mismatch: {state.get('deployment_id')!r}")
    if state.get("mode") != mode:
        raise ConfigError(f"{mode} state mode mismatch: {state.get('mode')!r}")

    symbols = state.get("symbols")
    if not isinstance(symbols, dict):
        raise ConfigError(f"{mode} state symbols must be a mapping")
    if sorted(symbols) != sorted(cfg.deployment_symbols):
        raise ConfigError(f"{mode} state symbols mismatch: {sorted(symbols)}")

    open_trades = state.get("open_trades")
    if not isinstance(open_trades, list):
        raise ConfigError(f"{mode} state open_trades must be a list")

    portfolio = state.get("portfolio") or {}
    if portfolio.get("cb_anchor", {}).get("enabled") != cfg.is_cb_enabled():
        raise ConfigError(f"{mode} state CB enabled flag differs from config")
    if portfolio.get("cb_anchor", {}).get("monitor_only") != cfg.is_cb_monitor_only():
        raise ConfigError(f"{mode} state CB monitor_only flag differs from config")
    if portfolio.get("highwind", {}).get("enabled") != cfg.is_highwind_enabled():
        raise ConfigError(f"{mode} state Highwind enabled flag differs from config")
    if portfolio.get("highwind", {}).get("monitor_only") != cfg.is_highwind_monitor_only():
        raise ConfigError(f"{mode} state Highwind monitor_only flag differs from config")


def write_templates(cfg: RuntimeConfig) -> list[Path]:
    written = []
    for mode in ("live", "paper"):
        path = cfg.get_state_template_file(mode)
        atomic_write_json(path, build_clean_state(cfg, mode))
        written.append(path)
    return written


def reset_active_states(cfg: RuntimeConfig) -> list[Path]:
    written = []
    for mode in ("live", "paper"):
        path = cfg.get_state_file(mode)
        atomic_write_json(path, build_clean_state(cfg, mode))
        written.append(path)
    return written


def restore_state_from_template(cfg: RuntimeConfig, mode: str) -> Path:
    template = cfg.get_state_template_file(mode)
    if not template.exists():
        raise ConfigError(f"missing state template: {template}")
    state = deepcopy(load_state(template))
    validate_state_shape(cfg, state, mode)
    target = cfg.get_state_file(mode)
    atomic_write_json(target, state)
    return target


def verify_states(cfg: RuntimeConfig) -> None:
    for mode in ("live", "paper"):
        template = cfg.get_state_template_file(mode)
        active = cfg.get_state_file(mode)
        if not template.exists():
            raise ConfigError(f"missing {mode} state template: {template}")
        if not active.exists():
            raise ConfigError(f"missing {mode} active state: {active}")
        validate_state_shape(cfg, load_state(template), mode)
        validate_state_shape(cfg, load_state(active), mode)


def main() -> None:
    parser = argparse.ArgumentParser(description="System C V2 state helper")
    parser.add_argument("--write-templates", action="store_true")
    parser.add_argument("--reset-active", action="store_true")
    parser.add_argument("--restore", choices=("live", "paper"))
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    cfg = load_runtime_config()
    if args.write_templates:
        for path in write_templates(cfg):
            print(f"wrote template: {path}")
    if args.reset_active:
        for path in reset_active_states(cfg):
            print(f"wrote active state: {path}")
    if args.restore:
        print(f"restored {args.restore}: {restore_state_from_template(cfg, args.restore)}")
    if args.verify or not any((args.write_templates, args.reset_active, args.restore)):
        verify_states(cfg)
        print("V2 state verification: OK")


if __name__ == "__main__":
    main()
