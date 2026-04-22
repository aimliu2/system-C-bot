"""Runtime config loader for System C bot architecture v2.

This loader intentionally ignores the legacy ``bot/config.yaml`` file. The V2
runtime reads portfolio settings from ``bot/config/config.yaml`` and selected
symbol configs from ``bot/config/<SYMBOL>.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


BOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BOT_DIR / "config"
RUNTIME_CONFIG_FILE = CONFIG_DIR / "config.yaml"

REQUIRED_ENV_KEYS = (
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "RPYC_HOST",
    "RPYC_PORT",
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID",
)

OPTION_A_EXPECTED_SETUP = {
    "AUDUSD": {
        "selected_phase": "phase3",
        "selected_combo": "A1+A2+B",
        "cross_phase_enabled": True,
        "branches": {"A1": "phase3", "A2": "phase2", "B": "phase2"},
        "session_gates": {"A1": False, "A2": True, "B": True},
    },
    "EURJPY": {
        "selected_phase": "phase4",
        "selected_combo": "A1+A2+B",
        "cross_phase_enabled": True,
        "branches": {"A1": "phase4", "A2": "phase4", "B": "phase5"},
        "session_gates": {"A1": True, "A2": True, "B": True},
    },
    "EURUSD": {
        "selected_phase": "phase3",
        "selected_combo": "A1+A2+B",
        "cross_phase_enabled": False,
        "branches": {"A1": "phase3", "A2": "phase3", "B": "phase3"},
        "session_gates": {"A1": True, "A2": True, "B": True},
    },
    "USDJPY": {
        "selected_phase": "phase3",
        "selected_combo": "A1+A2+B",
        "cross_phase_enabled": True,
        "branches": {"A1": "phase3", "A2": "phase3", "B": "phase7"},
        "session_gates": {"A1": True, "A2": True, "B": True},
    },
}


class ConfigError(ValueError):
    """Raised when the V2 runtime config is incomplete or inconsistent."""


@dataclass(frozen=True)
class SymbolRuntimeConfig:
    symbol: str
    path: Path
    raw: dict[str, Any]
    selected_phase: str
    selected_combo: str
    cross_phase_enabled: bool
    branch_phases: dict[str, str]
    session_gates: dict[str, bool]
    allowed_sessions: dict[str, list[str]]

    @property
    def enabled_hypotheses(self) -> list[str]:
        combo = self.raw.get("combos", {}).get(self.selected_combo)
        if combo:
            return list(combo)
        return [part.strip() for part in self.selected_combo.split("+") if part.strip()]


@dataclass(frozen=True)
class RuntimeConfig:
    bot_dir: Path
    config_path: Path
    raw: dict[str, Any]
    secrets: dict[str, str]
    symbols: dict[str, SymbolRuntimeConfig]

    @property
    def deployment_id(self) -> str:
        return str(self.raw["deployment"]["id"])

    @property
    def deployment_symbols(self) -> list[str]:
        return list(self.raw["deployment"]["symbols"])

    @property
    def paper_mode(self) -> bool:
        return bool(self.raw.get("paper_mode", True))

    @property
    def portfolio_cap(self) -> int:
        return int(self.raw["portfolio"]["max_concurrent_live_trades"])

    @property
    def base_risk_pct(self) -> float:
        return float(self.raw["portfolio"]["base_risk_pct"])

    @property
    def magic_number(self) -> int:
        return int(self.raw["mt5"]["magic_number"])

    @property
    def env_file(self) -> Path:
        return resolve_bot_path(self.bot_dir, self.raw["mt5"]["env_file"])

    def get_state_file(self, mode: str | None = None) -> Path:
        selected_mode = mode or ("paper" if self.paper_mode else "live")
        if selected_mode not in {"paper", "live"}:
            raise ConfigError(f"state mode must be 'paper' or 'live', got {selected_mode!r}")
        key = "paper_file" if selected_mode == "paper" else "live_file"
        return resolve_bot_path(self.bot_dir, self.raw["state"][key])

    def get_state_template_file(self, mode: str) -> Path:
        if mode not in {"paper", "live"}:
            raise ConfigError(f"state template mode must be 'paper' or 'live', got {mode!r}")
        return self.bot_dir / "state" / f"state_{mode}_portfolio_v2.template.json"

    def get_log_dir(self) -> Path:
        return resolve_bot_path(self.bot_dir, self.raw["logs"]["base_dir"])

    def get_log_paths(self, yyyymm: str | None = None) -> dict[str, Path]:
        month = yyyymm or datetime.now(timezone.utc).strftime("%Y%m")
        base = self.get_log_dir()
        logs = self.raw["logs"]
        names = {
            "trade": "trade_log",
            "event": "event_log",
            "signal": "signal_log",
            "candidate": "candidate_log",
            "reducer": "reducer_log",
            "snapshot": "snapshot_log",
            "timing": "timing_log",
            "state_audit": "state_audit_log",
        }
        return {
            label: base / str(logs[key]).replace("{YYYYMM}", month)
            for label, key in names.items()
        }

    def is_highwind_enabled(self) -> bool:
        return bool(self.raw["intervention"]["highwind"]["enabled"])

    def is_highwind_monitor_only(self) -> bool:
        return bool(self.raw["intervention"]["highwind"]["monitor_only"])

    def is_cb_enabled(self) -> bool:
        return bool(self.raw["intervention"]["cb_anchor"]["enabled"])

    def is_cb_monitor_only(self) -> bool:
        return bool(self.raw["intervention"]["cb_anchor"]["monitor_only"])

    def is_rule2_enabled(self) -> bool:
        return bool(self.raw["intervention"]["rule2"]["enabled"])

    def notifications_enabled_for(self, mode: str) -> bool:
        notifications = self.raw.get("notifications") or {}
        if not notifications.get("enabled", False):
            return False
        if mode == "paper":
            return bool(notifications.get("paper_trades", False))
        if mode == "live":
            return bool(notifications.get("live_trades", False))
        return False


def resolve_bot_path(bot_dir: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == "bot":
        return bot_dir.joinpath(*path.parts[1:])
    if path.parts and path.parts[0] == bot_dir.name:
        return bot_dir.parent / path
    return bot_dir / path


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing YAML file: {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"YAML root must be a mapping: {path}")
    return data


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise ConfigError(f"missing env file: {path}")

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_symbol_config(path: Path) -> SymbolRuntimeConfig:
    raw = load_yaml(path)
    symbol = str(raw.get("symbol") or path.stem).upper()
    deployment = raw.get("deployment") or {}
    selected_phase = deployment.get("selected_phase")
    selected_combo = deployment.get("selected_combo")
    if not selected_phase or not selected_combo:
        raise ConfigError(f"{symbol}: deployment.selected_phase and selected_combo are required")

    cross_phase = raw.get("cross_phase") or {}
    branches = cross_phase.get("branches") or {}
    cross_phase_enabled = bool(cross_phase.get("enabled", False))
    hypotheses = raw.get("hypotheses") or {}

    enabled_hypotheses = raw.get("combos", {}).get(selected_combo)
    if not enabled_hypotheses:
        enabled_hypotheses = [part.strip() for part in str(selected_combo).split("+") if part.strip()]

    branch_phases: dict[str, str] = {}
    session_gates: dict[str, bool] = {}
    allowed_sessions: dict[str, list[str]] = {}
    for hyp in enabled_hypotheses:
        hyp_cfg = hypotheses.get(hyp) or {}
        session_gates[hyp] = bool(hyp_cfg.get("session_gate", False))
        allowed_sessions[hyp] = list(hyp_cfg.get("allowed_sessions") or [])
        if cross_phase_enabled:
            branch = branches.get(hyp) or {}
            branch_phases[hyp] = str(branch.get("phase") or selected_phase)
        else:
            branch_phases[hyp] = str(selected_phase)

    return SymbolRuntimeConfig(
        symbol=symbol,
        path=path,
        raw=raw,
        selected_phase=str(selected_phase),
        selected_combo=str(selected_combo),
        cross_phase_enabled=cross_phase_enabled,
        branch_phases=branch_phases,
        session_gates=session_gates,
        allowed_sessions=allowed_sessions,
    )


def load_runtime_config(config_path: Path = RUNTIME_CONFIG_FILE) -> RuntimeConfig:
    raw = load_yaml(config_path)
    bot_dir = config_path.resolve().parents[1]

    validate_runtime_config_shape(raw)
    secrets = load_env_file(resolve_bot_path(bot_dir, raw["mt5"]["env_file"]))

    symbol_dir = resolve_bot_path(bot_dir, raw["deployment"].get("symbol_config_dir", "config"))
    symbols: dict[str, SymbolRuntimeConfig] = {}
    for symbol in raw["deployment"]["symbols"]:
        symbol_path = symbol_dir / f"{symbol}.yaml"
        symbols[symbol] = load_symbol_config(symbol_path)

    cfg = RuntimeConfig(
        bot_dir=bot_dir,
        config_path=config_path,
        raw=raw,
        secrets=secrets,
        symbols=symbols,
    )
    verify_runtime_config(cfg)
    return cfg


def validate_runtime_config_shape(raw: dict[str, Any]) -> None:
    required_sections = ("deployment", "portfolio", "state", "logs", "runtime", "mt5", "intervention", "notifications")
    missing = [section for section in required_sections if section not in raw]
    if missing:
        raise ConfigError(f"missing runtime config sections: {', '.join(missing)}")

    symbols = raw["deployment"].get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ConfigError("deployment.symbols must be a non-empty list")

    if int(raw["portfolio"].get("max_concurrent_live_trades", 0)) < 1:
        raise ConfigError("portfolio.max_concurrent_live_trades must be >= 1")

    if raw["state"].get("migrate_legacy_state"):
        raise ConfigError("state.migrate_legacy_state must remain false for V2 launch")

    notifications = raw.get("notifications") or {}
    for key in ("enabled", "paper_trades", "live_trades"):
        if key not in notifications:
            raise ConfigError(f"notifications.{key} is required")
        if not isinstance(notifications[key], bool):
            raise ConfigError(f"notifications.{key} must be boolean")


def verify_runtime_config(cfg: RuntimeConfig) -> None:
    deployment_symbols = cfg.deployment_symbols
    if deployment_symbols != list(OPTION_A_EXPECTED_SETUP):
        raise ConfigError(
            "deployment.symbols must match Option A order: "
            f"{list(OPTION_A_EXPECTED_SETUP)}, got {deployment_symbols}"
        )

    missing_env = [key for key in REQUIRED_ENV_KEYS if key not in cfg.secrets or cfg.secrets[key] == ""]
    if missing_env:
        raise ConfigError(f"missing required .ennv keys: {', '.join(missing_env)}")

    if cfg.portfolio_cap != 2:
        raise ConfigError(f"Option A launch portfolio cap must be 2, got {cfg.portfolio_cap}")

    if cfg.is_highwind_enabled() or not cfg.is_highwind_monitor_only():
        raise ConfigError("Highwind must be disabled and monitor_only for launch")

    if cfg.is_cb_enabled() or not cfg.is_cb_monitor_only():
        raise ConfigError("CB anchor must be disabled and monitor_only for launch")

    if cfg.raw["runtime"].get("parallel_symbol_eval"):
        raise ConfigError("parallel_symbol_eval must stay false for first V2 launch")

    if cfg.raw["clocks"].get("minimum_resolution") != "1min":
        raise ConfigError("clocks.minimum_resolution must be 1min")

    if cfg.raw["clocks"].get("context_merge") != "asof_closed_bar_only":
        raise ConfigError("clocks.context_merge must be asof_closed_bar_only")

    if int(cfg.raw["clocks"].get("latest_closed_bar_probe_bars", 0)) < 2:
        raise ConfigError("clocks.latest_closed_bar_probe_bars must be >= 2")

    if cfg.raw["clocks"].get("latest_closed_bar_position") != "penultimate":
        raise ConfigError("clocks.latest_closed_bar_position must be penultimate")

    data = cfg.raw.get("data") or {}
    cache = data.get("cache") or {}
    if not cache.get("enabled"):
        raise ConfigError("data.cache.enabled must be true")
    if cache.get("update_mode") != "closed_bar_delta":
        raise ConfigError("data.cache.update_mode must be closed_bar_delta")
    if not cache.get("rebuild_features_from_cache"):
        raise ConfigError("data.cache.rebuild_features_from_cache must be true")
    if cache.get("persist_to_disk"):
        raise ConfigError("data.cache.persist_to_disk must be false for launch")
    if data.get("include_forming_bar_in_decisions"):
        raise ConfigError("data.include_forming_bar_in_decisions must be false")

    min_by_tf = data.get("min_bars_by_timeframe") or {}
    for timeframe in ("1min", "5m", "15m", "1h", "4h"):
        actual = int(min_by_tf.get(timeframe, 0))
        if actual < 500:
            raise ConfigError(f"data.min_bars_by_timeframe.{timeframe} must be >= 500, got {actual}")

    indicators = cfg.raw.get("indicators") or {}
    if indicators.get("backend") != "numba":
        raise ConfigError("indicators.backend must be numba")
    if indicators.get("supertrend_backend") != "numba":
        raise ConfigError("indicators.supertrend_backend must be numba")
    if indicators.get("fallback_to_python"):
        raise ConfigError("indicators.fallback_to_python must be false")

    for symbol, expected in OPTION_A_EXPECTED_SETUP.items():
        if symbol not in cfg.symbols:
            raise ConfigError(f"missing selected symbol config: {symbol}")
        actual = cfg.symbols[symbol]
        checks = {
            "selected_phase": actual.selected_phase,
            "selected_combo": actual.selected_combo,
            "cross_phase_enabled": actual.cross_phase_enabled,
            "branches": actual.branch_phases,
            "session_gates": actual.session_gates,
        }
        for key, got in checks.items():
            want = expected[key]
            if got != want:
                raise ConfigError(f"{symbol}: {key} mismatch, got {got!r}, want {want!r}")


def sanitized_summary(cfg: RuntimeConfig) -> str:
    lines = [
        f"deployment_id={cfg.deployment_id}",
        f"symbols={','.join(cfg.deployment_symbols)}",
        f"portfolio_cap={cfg.portfolio_cap}",
        f"base_risk_pct={cfg.base_risk_pct}",
        f"paper_mode={cfg.paper_mode}",
        f"state_live={cfg.get_state_file('live')}",
        f"state_paper={cfg.get_state_file('paper')}",
        f"env_file={cfg.env_file}",
        f"env_keys_present={','.join(key for key in REQUIRED_ENV_KEYS if key in cfg.secrets)}",
        (
            "intervention="
            f"highwind(enabled={cfg.is_highwind_enabled()},monitor_only={cfg.is_highwind_monitor_only()}),"
            f"cb(enabled={cfg.is_cb_enabled()},monitor_only={cfg.is_cb_monitor_only()}),"
            f"rule2(enabled={cfg.is_rule2_enabled()})"
        ),
        (
            "data_cache="
            f"enabled={cfg.raw['data']['cache']['enabled']},"
            f"update={cfg.raw['data']['cache']['update_mode']},"
            f"startup_entry={cfg.raw['data']['startup_bars']['entry']},"
            f"startup_context={cfg.raw['data']['startup_bars']['context']},"
            f"startup_execution_1min={cfg.raw['data']['startup_bars']['execution_1min']}"
        ),
        (
            "indicators="
            f"backend={cfg.raw['indicators']['backend']},"
            f"supertrend={cfg.raw['indicators']['supertrend_backend']},"
            f"compile_on_startup={cfg.raw['indicators']['compile_on_startup']}"
        ),
        (
            "notifications="
            f"enabled={cfg.raw['notifications']['enabled']},"
            f"paper_trades={cfg.raw['notifications']['paper_trades']},"
            f"live_trades={cfg.raw['notifications']['live_trades']}"
        ),
    ]
    for symbol in cfg.deployment_symbols:
        sym = cfg.symbols[symbol]
        lines.append(
            f"{symbol}: phase={sym.selected_phase} combo={sym.selected_combo} "
            f"cross_phase={sym.cross_phase_enabled} branches={sym.branch_phases} "
            f"session_gates={sym.session_gates}"
        )
    return "\n".join(lines)


def main() -> None:
    cfg = load_runtime_config()
    print(sanitized_summary(cfg))
    print("V2 runtime config verification: OK")


if __name__ == "__main__":
    main()
