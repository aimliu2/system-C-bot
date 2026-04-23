"""Bridge V2 cached bars into the bot-local System C engine."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pandas as pd

from runtime.config import RuntimeConfig
from runtime.data_cache import BarCache, normalize_timeframe
from runtime.engine.align import align
from runtime.engine.config_loader import apply_phase_config, branch_config, combo_config
from runtime.engine.engine import InstrumentEngine
from runtime.engine.features import build_features, compute_regime
from runtime.engine.policy import policy_3
from runtime.engine.strategy import resolve_sl_tp


@dataclass
class SymbolEvalResult:
    symbol: str
    evaluated: bool
    candidate: dict[str, Any] | None = None
    proposed_engine_state: dict[str, Any] | None = None
    reason: str = ""


def serialize_engine(engine: InstrumentEngine) -> dict[str, Any]:
    return json_safe({
        "snapshot": engine.snapshot(),
        "pivot_array": list(engine.pivot_array.pivots),
        "choch": {
            "confirmed": engine.choch.confirmed,
            "direction": engine.choch.direction,
            "level": engine.choch.level,
            "confirmed_time": str(engine.choch.confirmed_time) if engine.choch.confirmed_time is not None else None,
        },
        "cooldowns": {
            hyp: {"active": state.active, "bars_elapsed": state.bars_elapsed}
            for hyp, state in engine.cooldowns.items()
        },
        "hypothesis_states": {
            hyp: {"new_extreme_flag": state.new_extreme_flag, "sb_used": state.sb_used}
            for hyp, state in engine.hypothesis_states.items()
        },
        "last_st_dir": engine._last_st_dir,
        "episode_high": engine._episode_high,
        "episode_low": engine._episode_low,
        "episode_high_time": str(engine._episode_high_time) if engine._episode_high_time is not None else None,
        "episode_low_time": str(engine._episode_low_time) if engine._episode_low_time is not None else None,
    })


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def hydrate_engine(engine: InstrumentEngine, payload: dict[str, Any] | None) -> InstrumentEngine:
    if not payload:
        return engine
    engine.pivot_array.pivots = list(payload.get("pivot_array", []))
    choch = payload.get("choch", {})
    engine.choch.confirmed = bool(choch.get("confirmed", False))
    engine.choch.direction = choch.get("direction")
    engine.choch.level = choch.get("level")
    if choch.get("confirmed_time"):
        engine.choch.confirmed_time = pd.Timestamp(choch["confirmed_time"])

    for hyp, state in (payload.get("cooldowns") or {}).items():
        if hyp in engine.cooldowns:
            engine.cooldowns[hyp].active = bool(state.get("active", False))
            engine.cooldowns[hyp].bars_elapsed = int(state.get("bars_elapsed", 0))
    for hyp, state in (payload.get("hypothesis_states") or {}).items():
        if hyp in engine.hypothesis_states:
            engine.hypothesis_states[hyp].new_extreme_flag = bool(state.get("new_extreme_flag", False))
            engine.hypothesis_states[hyp].sb_used = bool(state.get("sb_used", False))

    engine._last_st_dir = payload.get("last_st_dir")
    engine._episode_high = payload.get("episode_high")
    engine._episode_low = payload.get("episode_low")
    if payload.get("episode_high_time"):
        engine._episode_high_time = pd.Timestamp(payload["episode_high_time"])
    if payload.get("episode_low_time"):
        engine._episode_low_time = pd.Timestamp(payload["episode_low_time"])
    return engine


class EngineBridge:
    def __init__(self, cfg: RuntimeConfig, cache: BarCache):
        self.cfg = cfg
        self.cache = cache

    def evaluate_symbol(self, symbol: str, symbol_state: dict[str, Any]) -> SymbolEvalResult:
        sym_cfg = self.cfg.symbols[symbol]
        engine_state = symbol_state.setdefault("engine_state", {})
        branches = self._branch_specs(symbol)
        candidates = []
        proposed_state = deepcopy(engine_state)

        for branch_key, hyp, phase_name, branch_cfg in branches:
            phase = branch_cfg["active_phase"]
            entry_tf = normalize_timeframe(phase["entry_timeframe"])
            context_tf = normalize_timeframe(phase["context_timeframe"])
            if not self.cache.has_frame(symbol, entry_tf) or not self.cache.has_frame(symbol, context_tf):
                return SymbolEvalResult(symbol, False, reason=f"missing_cache:{entry_tf}/{context_tf}")

            aligned = self._aligned_features(symbol, entry_tf, context_tf, branch_cfg)
            if aligned.empty:
                return SymbolEvalResult(symbol, False, reason="empty_aligned_features")

            feature_row = aligned.iloc[-1].copy()
            context_row = self._context_row(feature_row)
            if pd.isna(feature_row.get("st_dir")) or pd.isna(context_row.get("st_dir")):
                return SymbolEvalResult(symbol, False, reason="warmup_not_ready")

            engine = hydrate_engine(
                InstrumentEngine(symbol, branch_cfg),
                engine_state.get(branch_key),
            )
            fired = engine.on_bar(feature_row, context_row) or []
            accepted = policy_3(fired, engine, branch_cfg)
            proposed_state[branch_key] = serialize_engine(engine)
            if accepted:
                entry_price = float(feature_row["close"])
                candidate = resolve_sl_tp(accepted, entry_price)
                candidate.update({
                    "symbol": symbol,
                    "bar_time": feature_row.name.isoformat(),
                    "candidate_id": self._candidate_id(symbol, candidate["hypothesis"], feature_row.name),
                    "source_phase": phase_name,
                    "entry_timeframe": entry_tf,
                    "context_timeframe": context_tf,
                })
                candidates.append(candidate)

        if not candidates:
            return SymbolEvalResult(symbol, True, proposed_engine_state=proposed_state, reason="no_signal")

        priority = {"B": 0, "A2": 1, "A1": 2}
        candidates.sort(key=lambda c: (c["bar_time"], priority.get(c["hypothesis"], 99)))
        return SymbolEvalResult(symbol, True, candidates[0], proposed_engine_state=proposed_state, reason="candidate")

    def _branch_specs(self, symbol: str) -> list[tuple[str, str, str, dict[str, Any]]]:
        sym_cfg = self.cfg.symbols[symbol]
        raw = sym_cfg.raw
        if sym_cfg.cross_phase_enabled:
            specs = []
            for hyp in sym_cfg.enabled_hypotheses:
                phase_name = sym_cfg.branch_phases[hyp]
                specs.append((hyp, hyp, phase_name, branch_config(raw, hyp, phase_name)))
            return specs
        cfg = apply_phase_config(combo_config(raw, sym_cfg.selected_combo), sym_cfg.selected_phase)
        return [("main", "main", sym_cfg.selected_phase, cfg)]

    def _aligned_features(self, symbol: str, entry_tf: str, context_tf: str, engine_cfg: dict[str, Any]) -> pd.DataFrame:
        entry_st = engine_cfg["features"]["entry_supertrend"]
        context_st = engine_cfg["features"]["context_supertrend"]
        entry = build_features(
            self.cache.frame(symbol, entry_tf),
            st_period=int(entry_st["period"]),
            st_mult=float(entry_st["multiplier"]),
        )
        context = build_features(
            self.cache.frame(symbol, context_tf),
            st_period=int(context_st["period"]),
            st_mult=float(context_st["multiplier"]),
        )
        aligned = align(entry, context)
        aligned["regime"] = compute_regime(aligned)
        return aligned

    @staticmethod
    def _context_row(feature_row: pd.Series) -> pd.Series:
        values = {}
        for key, value in feature_row.items():
            if key.startswith("ctx_"):
                values[key.removeprefix("ctx_")] = value
        return pd.Series(values)

    @staticmethod
    def _candidate_id(symbol: str, hyp: str, bar_time: pd.Timestamp) -> str:
        return f"{symbol}-{hyp}-{bar_time.strftime('%Y%m%dT%H%M%S')}"
