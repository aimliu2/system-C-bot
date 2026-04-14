"""
config_loader.py — System C configuration loader
v1.0 · April 2026

Loads .env (secrets) and config.yaml (trade config).
All scripts import from here — single source of truth.

Key difference from SystemB:
  get_hyp_config(symbol, hyp) merges global hypothesis defaults with
  per-instrument overrides. All hypothesis logic reads from one call
  without needing to know where each value came from.

Usage:
    from config_loader import config, secrets
    from config_loader import get_hyp_config, get_pip_size, validate_config
"""

import yaml
import os
from dotenv import load_dotenv
from pathlib import Path

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"
ENV_FILE    = BASE_DIR / ".ennv"

# ---------------------------------------------------------------------------
# LOAD SECRETS FROM .env
# ---------------------------------------------------------------------------

load_dotenv(ENV_FILE)

secrets = {
    "mt5_login"    : int(os.getenv("MT5_LOGIN", 0)),
    "mt5_password" : os.getenv("MT5_PASSWORD", ""),
    "mt5_server"   : os.getenv("MT5_SERVER", ""),
    "mt5_path"     : os.getenv("MT5_PATH", ""),    # optional — path to terminal64.exe
    "rpyc_host"    : os.getenv("RPYC_HOST", "localhost"),
    "rpyc_port"    : int(os.getenv("RPYC_PORT", 18812)),
}

# ---------------------------------------------------------------------------
# LOAD TRADE CONFIG FROM config.yaml
# ---------------------------------------------------------------------------

with open(CONFIG_FILE, encoding="utf-8") as f:
    config = yaml.safe_load(f)

# ---------------------------------------------------------------------------
# CORE ACCESSORS
# ---------------------------------------------------------------------------

def get_hyp_config(symbol: str, hyp: str) -> dict:
    """
    Return effective hypothesis config for a given symbol.

    Merges global hyp_<hyp> defaults with instruments.<symbol>.overrides.hyp_<hyp>.
    Per-instrument override keys win over global defaults.

    Example:
        EURUSD has no overrides → returns global hyp_a1 unchanged
        GBPUSD has overrides.hyp_a1.enabled=false → returns {enabled: false, ...rest global}
        GBPUSD has overrides.hyp_a1.ema_touch_span=30 → only that key is overridden
    """
    global_defaults = dict(config.get(f"hyp_{hyp}", {}))
    instrument_cfg  = config.get("instruments", {}).get(symbol, {})
    overrides       = instrument_cfg.get("overrides", {}) or {}
    hyp_overrides   = overrides.get(f"hyp_{hyp}", {}) or {}
    return {**global_defaults, **hyp_overrides}


def get_active_symbols() -> list:
    """Return symbols where mode != 'disabled'."""
    instruments = config.get("instruments", {})
    return [sym for sym, cfg in instruments.items()
            if cfg.get("mode", "disabled") != "disabled"]


def get_instrument_mode(symbol: str) -> str:
    """
    Return mode from config (initial setting).
    Note: runtime state can override this — always use
    get_instrument_mode_runtime(symbol, config, state) in run_orders.
    """
    return config.get("instruments", {}).get(symbol, {}).get("mode", "disabled")


def get_pip_size(symbol: str) -> float:
    return config["instruments"][symbol]["pip_size"]


def get_regime_config() -> dict:
    return config["regime"]


def get_highwind_config(symbol: str = None) -> dict:
    """
    Return effective Highwind config for a given symbol.

    Merges global highwind defaults with instruments.<symbol>.highwind overrides.
    Per-instrument keys (halt_threshold, l2_threshold, l1_threshold, seed_wins, seed_losses)
    win over global defaults. When symbol=None, returns global defaults only.

    Example:
        EURUSD has only seed_wins/seed_losses → thresholds remain global (0.32/0.36/0.40)
        GBPUSD overrides halt/l2/l1 thresholds + seeds
        GBPJPY overrides all thresholds + seeds (higher break-even)
    """
    global_defaults = dict(config.get("highwind", {}))
    if symbol is None:
        return global_defaults
    instrument_cfg = config.get("instruments", {}).get(symbol, {})
    hw_overrides   = instrument_cfg.get("highwind", {}) or {}
    return {**global_defaults, **hw_overrides}


def get_session_hours(session: str) -> tuple:
    return tuple(config["sessions"][session])


def is_paper_mode() -> bool:
    """Global paper override — shadows ALL instruments when True."""
    return config.get("paper_mode", False)


def get_state_file() -> str:
    """Returns correct state file path based on global paper_mode."""
    if config.get("paper_mode", False):
        return config.get("state_file_paper", "state_paper.json")
    return config.get("state_file_live", "state_live.json")


def is_trail_enabled() -> bool:
    return config.get("trail", {}).get("enabled", False)


def get_st_config(symbol: str) -> dict:
    """
    Return effective SuperTrend params for a given symbol.
    Merges global st_period/st_multiplier with instruments.<symbol>.overrides
    (st_period or st_multiplier keys at the instrument level, not under hyp_*).
    """
    base = {
        "st_period"     : config.get("st_period", 12),
        "st_multiplier" : config.get("st_multiplier", 3.0),
    }
    overrides = (config.get("instruments", {})
                       .get(symbol, {})
                       .get("overrides", {}) or {})
    base.update({k: v for k, v in overrides.items()
                 if k in ("st_period", "st_multiplier")})
    return base


def get_trading_hours(symbol: str) -> tuple:
    """
    Return (start_utc, end_utc) for per-instrument trading session gate.
    Default [7, 21] = London + NY (EURUSD).
    Override per instrument via instruments.<symbol>.trading_hours: [start, end].
    """
    inst  = config.get("instruments", {}).get(symbol, {})
    hours = inst.get("trading_hours", [7, 21])
    return int(hours[0]), int(hours[1])


# ---------------------------------------------------------------------------
# VALIDATION — catch config errors at startup
# ---------------------------------------------------------------------------

def validate_config():
    errors = []

    # Instrument section
    instruments = config.get("instruments", {})
    if not instruments:
        errors.append("instruments section is empty — at least one instrument required")

    for symbol, inst_cfg in instruments.items():
        if inst_cfg.get("mode", "disabled") == "disabled":
            continue  # skip validation for disabled instruments

        if "pip_size" not in inst_cfg:
            errors.append(f"instruments.{symbol}: pip_size missing")

        # Validate effective hypothesis configs
        for hyp in ("a1", "a2", "b"):
            hyp_cfg = get_hyp_config(symbol, hyp)
            if not hyp_cfg.get("enabled", True):
                continue

            if hyp in ("a1", "a2"):
                rr = hyp_cfg.get("rr", 0)
                if rr <= 0:
                    errors.append(f"instruments.{symbol}.hyp_{hyp}: rr must be > 0, got {rr}")
                sl_min = hyp_cfg.get("sl_min", 0)
                sl_max = hyp_cfg.get("sl_max", 0)
                if sl_min >= sl_max:
                    errors.append(f"instruments.{symbol}.hyp_{hyp}: sl_min ({sl_min}) must be < sl_max ({sl_max})")

            if hyp == "b":
                sl_fixed = hyp_cfg.get("sl_fixed", 0)
                if sl_fixed <= 0:
                    errors.append(f"instruments.{symbol}.hyp_b: sl_fixed must be > 0, got {sl_fixed}")
                rr = hyp_cfg.get("rr", 0)
                if rr <= 0:
                    errors.append(f"instruments.{symbol}.hyp_b: rr must be > 0, got {rr}")

    # Global params
    stable = config.get("stable_bars_1h", 0)
    if stable < 1:
        errors.append(f"stable_bars_1h must be >= 1, got {stable}")

    cooldown = config.get("cooldown_bars", -1)
    if cooldown < 0:
        errors.append(f"cooldown_bars must be >= 0, got {cooldown}")

    pivot_maxlen = config.get("pivot_maxlen", 0)
    if pivot_maxlen < 2:
        errors.append(f"pivot_maxlen must be >= 2, got {pivot_maxlen}")

    # Regime classifier
    regime = config.get("regime", {})
    if regime.get("ema_fast_1h", 0) >= regime.get("ema_slow_1h", 0):
        errors.append("regime.ema_fast_1h must be < regime.ema_slow_1h")

    # Risk
    base_risk = config.get("base_risk_pct", 0)
    if base_risk <= 0 or base_risk > 5:
        errors.append(f"base_risk_pct out of range: {base_risk}")

    # Secrets
    if secrets["mt5_login"] == 0:
        errors.append("MT5_LOGIN not set in .env")
    if not secrets["mt5_password"]:
        errors.append("MT5_PASSWORD not set in .env")
    if not secrets["mt5_server"]:
        errors.append("MT5_SERVER not set in .env")

    if errors:
        print("❌ Config validation errors:")
        for e in errors:
            print(f"   {e}")
        raise ValueError("Fix config errors before running bot")

    print("✅ Config validated")
    return True


# ---------------------------------------------------------------------------
# SUMMARY — print at startup
# ---------------------------------------------------------------------------

def print_config_summary():
    print(f"\nSystem C — Config Summary")
    print(f"{'─'*50}")
    print(f"  Global paper mode : {config.get('paper_mode', False)}")
    print(f"  Base risk         : {config.get('base_risk_pct')}%")
    print(f"  Max hold bars     : {config.get('max_hold_bars')} × 15m")
    print(f"  Cooldown bars     : {config.get('cooldown_bars')} bars (after SL)")
    print(f"  Stable 1H bars    : {config.get('stable_bars_1h')}")
    print(f"  ST params         : period={config.get('st_period')} mult={config.get('st_multiplier')}")
    print(f"  MT5 server        : {secrets['mt5_server']}")

    print(f"\n  Instruments:")
    for symbol, inst_cfg in config.get("instruments", {}).items():
        mode = inst_cfg.get("mode", "disabled")
        pip  = inst_cfg.get("pip_size", "?")
        if mode == "disabled":
            print(f"    {symbol:<8} DISABLED")
            continue
        a1 = get_hyp_config(symbol, "a1")
        a2 = get_hyp_config(symbol, "a2")
        b  = get_hyp_config(symbol, "b")
        a1_str = f"A1({'ON' if a1.get('enabled') else 'OFF'})"
        a2_str = f"A2({'ON' if a2.get('enabled') else 'OFF'})"
        b_str  = f"B({'ON' if b.get('enabled') else 'OFF'})"
        t_start, t_end = get_trading_hours(symbol)
        st_cfg = get_st_config(symbol)
        print(f"    {symbol:<8} {mode.upper():<6}  pip={pip}  {a1_str} {a2_str} {b_str}"
              f"  hours={t_start:02d}-{t_end:02d}UTC"
              f"  ST({st_cfg['st_period']},{st_cfg['st_multiplier']})")
    print(f"{'─'*50}\n")


if __name__ == "__main__":
    validate_config()
    print_config_summary()
