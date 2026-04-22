"""Config loading helpers for the SystemC backtester."""

from __future__ import annotations

from pathlib import Path
from copy import deepcopy
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path(__file__).with_name("EURUSD.yaml")


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load backtester YAML config."""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def load_instrument_config(symbol: str, config_dir: str | Path | None = None) -> dict[str, Any]:
    """Load an instrument-specific profile such as `EURUSD.yaml`."""
    root = Path(config_dir) if config_dir is not None else Path(__file__).parent
    return load_config(root / f"{symbol.upper()}.yaml")


def enabled_hypotheses(config: dict[str, Any]) -> list[str]:
    """Return enabled hypotheses in canonical order."""
    hypotheses = config.get("hypotheses", {})
    return [
        hyp for hyp in ("A1", "A2", "B")
        if hypotheses.get(hyp, {}).get("enabled", True)
    ]


def phase_config(config: dict[str, Any], phase_name: str | None = None) -> dict[str, Any]:
    """Return one architecture phase from the instrument profile."""
    selected = phase_name or config.get("deployment", {}).get("selected_phase")
    phases = config.get("architecture_phases", {})
    if selected not in phases:
        raise KeyError(f"Unknown architecture phase: {selected}")
    return phases[selected]


def combo_config(config: dict[str, Any], combo_name: str) -> dict[str, Any]:
    """Return a copied config with hypotheses enabled according to a named combo."""
    combos = config.get("combos", {})
    if combo_name not in combos:
        raise KeyError(f"Unknown combo: {combo_name}")

    active = set(combos[combo_name])
    copied = deepcopy(config)
    copied["hypotheses"] = {
        hyp: {**settings, "enabled": hyp in active}
        for hyp, settings in config.get("hypotheses", {}).items()
    }
    copied["run_combo"] = combo_name
    return copied


def apply_phase_config(config: dict[str, Any], phase_name: str | None = None) -> dict[str, Any]:
    """Return a copied config with selected architecture phase expanded."""
    phase = phase_config(config, phase_name)
    copied = deepcopy(config)
    copied["active_phase"] = phase
    copied["timeframes"] = {
        "entry": phase["entry_timeframe"],
        "context": phase["context_timeframe"],
        "execution": copied.get("timeframes", {}).get("execution", "1min"),
    }
    copied["features"] = {
        **copied.get("features", {}),
        "entry_supertrend": phase["entry_st"],
        "context_supertrend": phase["context_st"],
    }
    return copied


def cross_phase_branches(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return enabled cross-phase branch definitions keyed by hypothesis."""
    cross_phase = config.get("cross_phase", {})
    branches = cross_phase.get("branches", {})
    return {
        hyp: branch
        for hyp, branch in branches.items()
        if branch.get("enabled", True)
    }


def branch_config(config: dict[str, Any], hypothesis: str, phase_name: str) -> dict[str, Any]:
    """
    Return a copied config for one split-run branch.

    A branch owns one hypothesis and one architecture phase. The split cursor
    combines several branch configs under one shared symbol execution layer.
    """
    copied = apply_phase_config(config, phase_name)
    copied["hypotheses"] = {
        hyp: {**settings, "enabled": hyp == hypothesis}
        for hyp, settings in copied.get("hypotheses", {}).items()
    }
    copied["branch"] = {
        "hypothesis": hypothesis,
        "phase": phase_name,
        "entry_timeframe": copied["active_phase"]["entry_timeframe"],
        "context_timeframe": copied["active_phase"]["context_timeframe"],
    }
    return copied
