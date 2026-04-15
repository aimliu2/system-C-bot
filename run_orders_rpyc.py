"""
run_orders_rpyc.py — System C Order Execution (macOS / rpyc bridge)
v1.0 · April 2026

macOS fallback — connects to MT5 running under Wine via rpyc bridge.
Feature-parity with run_orders_vps.py (upgraded from SystemB run_orders.py).

Key differences from run_orders_vps.py:
  - Uses rpyc.classic.connect() instead of native MetaTrader5 import
  - All MT5 proxy attribute accesses use float()/int()/str() casts
  - rpyc.utils.classic.obtain() used for array results (copy_rates, positions)
  - 5× reconnect backoff retained (needed for remote rpyc connection)
  - MT5_PATH not used (MT5 managed by start_bridge.sh under Wine)

Features identical to run_orders_vps.py:
  - get_filling_mode() — dynamic RETURN→FOK→IOC (not hardcoded)
  - STOP file kill switch
  - stale_idle soft mode (60s idle, NOT hard exit)
  - Retcode 10027/10030 warnings
  - All hypothesis logic (A1, A2, B), cooldown, Highwind, CB, Rule 2

Run: python3 run_orders_rpyc.py
Requires: start_bridge.sh running on macOS (starts rpyc server + Wine MT5)
Kill: create a file named STOP in the same directory
"""

import rpyc
import rpyc.utils.classic
import json
import math
import os
import sys
import shutil
import time
import platform
import subprocess
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config_loader import (
    config, secrets,
    get_hyp_config, get_active_symbols, get_pip_size,
    get_regime_config, get_highwind_config,
    get_trading_hours,
    is_paper_mode, get_state_file,
    validate_config, print_config_summary,
)
from run_data import build_data_bundle, DataBundle
import notifier

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

KILL_FILE           = "STOP"
STALE_WARN_MINUTES  = 30
STALE_IDLE_MINUTES  = 60
IDLE_POLL_SECONDS   = 60
PAPER_TICKET_BASE   = 99_999_000

# ---------------------------------------------------------------------------
# RPYC CONNECTION — global, reassignable on reconnect
# ---------------------------------------------------------------------------

conn = None   # rpyc connection object
mt5  = None   # remote MetaTrader5 module (proxy)


def connect():
    """
    Connect to rpyc bridge and initialize MT5.
    Returns (conn, mt5_module).
    Raises RuntimeError on failure.
    """
    host = secrets.get("rpyc_host", "localhost")
    port = int(secrets.get("rpyc_port", 18812))
    c = rpyc.classic.connect(host, port)
    m = c.modules.MetaTrader5
    ok = m.initialize(
        login    = secrets["mt5_login"],
        password = secrets["mt5_password"],
        server   = secrets["mt5_server"],
    )
    if not ok:
        raise RuntimeError(f"MT5 initialize failed: {m.last_error()}")
    return c, m


def reconnect(max_attempts: int = 5):
    """
    5× exponential backoff reconnect. Updates global conn and mt5.
    Returns True on success, False after all attempts fail.
    """
    global conn, mt5
    for attempt in range(1, max_attempts + 1):
        wait = min(30 * attempt, 120)
        print(f"  Reconnecting in {wait}s (attempt {attempt}/{max_attempts})...")
        time.sleep(wait)
        try:
            try:
                conn.close()
            except Exception:
                pass
            conn, mt5 = connect()
            print("  ✅ Reconnected to rpyc bridge")
            return True
        except Exception as e:
            print(f"  ❌ Reconnect attempt {attempt} failed: {e}")
    return False


# ---------------------------------------------------------------------------
# INTERNET CHECK
# ---------------------------------------------------------------------------

def has_internet() -> bool:
    try:
        if platform.system() == "Windows":
            r = subprocess.run(
                ["ping", "-n", "1", "-w", "2000", "8.8.8.8"],
                capture_output=True, timeout=5,
            )
        else:
            r = subprocess.run(
                ["ping", "-c", "1", "-W", "2", "8.8.8.8"],
                capture_output=True, timeout=5,
            )
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HIGHWIND SEEDING HELPERS  (must be defined before fresh_state uses them)
# ---------------------------------------------------------------------------

def _hw_seed_entry(symbol: str) -> dict:
    """Build seeded Highwind window + level from config. Used in fresh_state and migration."""
    hw_cfg    = get_highwind_config(symbol)
    seed_wins = hw_cfg.get("seed_wins",   0)
    seed_loss = hw_cfg.get("seed_losses", 0)
    window    = [1] * seed_wins + [0] * seed_loss
    halt_thr  = hw_cfg.get("halt_threshold", 0.32)
    l2_thr    = hw_cfg.get("l2_threshold",   0.36)
    l1_thr    = hw_cfg.get("l1_threshold",   0.40)
    wr        = sum(window) / len(window) if window else 1.0
    if wr >= l1_thr:    level = "NORMAL"
    elif wr >= l2_thr:  level = "L1"
    elif wr >= halt_thr: level = "L2"
    else:               level = "HALT"
    return {"window": window, "level": level, "halt_since": None}


def print_guards_status(state: dict):
    """Print CB and Rule 2 armed status to terminal. Called at every startup."""
    cb     = state.get("cb_anchor", {})
    r2     = state.get("rule2", {})
    cfg_cb = config.get("cb_anchor", {})
    peak   = cb.get("peak")
    anchor = cb.get("anchor")
    trig   = round(anchor * cfg_cb.get("next_trigger_pct", 0.92), 2) if anchor else None
    armed  = "✅ ARMED" if peak else "⚠️  NOT ARMED"
    base   = r2.get("base_equity")
    floor_ = round(base * config.get("rule2", {}).get("floor_pct", 0.85), 2) if base else None

    print(f"\n  ── Guards ──────────────────────────────────")
    print(f"  CB      : {armed}")
    if peak:
        print(f"            peak   = ${peak:,.2f}")
        print(f"            anchor = ${anchor:,.2f}  (×{cfg_cb.get('recovery_buffer', 0.97)})")
        print(f"            trigger= ${trig:,.2f}  ← CB fires here")
    if base:
        print(f"  Rule 2  : base   = ${base:,.2f}")
        print(f"            floor  = ${floor_:,.2f}  ← trading stops here")
    print(f"  ────────────────────────────────────────────\n")


def ensure_symbol_state(state: dict, symbol: str):
    """
    Initialize all per-symbol state dicts for a newly added instrument.
    Idempotent — setdefault never overwrites existing data.
    Must be called at startup whenever a symbol is promoted from disabled → paper/live
    after the state file was first created (fresh_state only runs once).
    """
    state.setdefault("last_bar_times", {}).setdefault(
        symbol, {"m15": None, "h1": None})
    state.setdefault("session_state", {}).setdefault(
        symbol, {"session": None, "session_bar": -1})
    state.setdefault("london_session_summary", {}).setdefault(
        symbol, {"trades": 0, "wins": 0, "pnl_r": 0.0})
    state.setdefault("ny_session_summary", {}).setdefault(
        symbol, {"trades": 0, "wins": 0, "pnl_r": 0.0})
    state.setdefault("pivot_arrays", {}).setdefault(symbol, [])
    state.setdefault("hypothesis_states", {}).setdefault(symbol, {
        "choch_confirmed" : False,
        "choch_direction" : None,
        "choch_level"     : None,
        "in_cooldown"     : False,
        "bars_since_sl"   : 0,
        "new_extreme_flag": False,
        "sb_used"         : False,
    })
    state.setdefault("instrument_modes", {}).setdefault(
        symbol, config.get("instruments", {}).get(symbol, {}).get("mode", "paper"))


def ensure_highwind_ready(state: dict, symbol: str):
    """Migrate old binary structure and seed empty windows on first run or new instrument."""
    ihw   = state.setdefault("instrument_highwind", {})
    entry = ihw.setdefault(symbol, {})
    if "halted" in entry:
        was_halted = entry.pop("halted")
        entry.pop("halt_time", None)
        entry.setdefault("level",      "HALT" if was_halted else "NORMAL")
        entry.setdefault("halt_since", None)
    if not entry.get("window"):
        seeded = _hw_seed_entry(symbol)
        entry.update(seeded)
        wins = sum(seeded["window"])
        n    = len(seeded["window"])
        wr   = wins / n if n else 0.0
        print(f"  📊 HW seed {symbol}: {seeded['level']}  ({wins}/{n} = {wr:.1%})")


# ---------------------------------------------------------------------------
# STATE — load / save / fresh template
# ---------------------------------------------------------------------------

def fresh_state() -> dict:
    symbols = list(config.get("instruments", {}).keys())
    state = {
        "_comment"            : "System C state file",
        "_version"            : "1.0",
        "open_trades"         : [],
        "instrument_modes"    : {s: config["instruments"][s].get("mode", "paper") for s in symbols},
        "instrument_highwind" : {s: _hw_seed_entry(s) for s in symbols},
        "hypothesis_states"   : {s: {
            "choch_confirmed" : False,
            "choch_direction" : None,
            "choch_level"     : None,
            "in_cooldown"     : False,
            "bars_since_sl"   : 0,
            "new_extreme_flag": False,
            "sb_used"         : False,
        } for s in symbols},
        "pivot_arrays"        : {s: [] for s in symbols},
        "cb_anchor"           : {"peak": None, "anchor": None,
                                 "triggered_session": False, "last_trigger_time": None},
        "rule2"               : {"base_equity": None, "triggered_today": False, "trigger_date": None},
        "last_bar_times"      : {s: {"m15": None, "h1": None} for s in symbols},
        "session_state"       : {s: {"session": None, "session_bar": -1} for s in symbols},
        "london_session_summary": {s: {"trades": 0, "wins": 0, "pnl_r": 0.0} for s in symbols},
        "ny_session_summary"    : {s: {"trades": 0, "wins": 0, "pnl_r": 0.0} for s in symbols},
        "_reset_log"          : [],
    }
    return state


def load_state(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            print(f"  ⚠️  State load failed: {e}")
    bak = path + ".bak"
    if os.path.exists(bak):
        print(f"  ⚠️  Loading backup: {bak}")
        try:
            with open(bak) as f:
                return json.load(f)
        except Exception:
            pass
    print("  ℹ️  No state found — starting fresh")
    return fresh_state()


def save_state(state: dict, path: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, path)
    shutil.copy(path, path + ".bak")


# ---------------------------------------------------------------------------
# INSTRUMENT MODE RESOLUTION
# ---------------------------------------------------------------------------

def get_instrument_mode_rt(symbol: str, state: dict) -> str:
    """
    Resolve effective mode at runtime.
    Priority: global paper_mode > state override > config initial mode.
    """
    if config.get("paper_mode", False):
        return "paper"
    return state.get("instrument_modes", {}).get(
        symbol,
        config.get("instruments", {}).get(symbol, {}).get("mode", "paper"),
    )


# ---------------------------------------------------------------------------
# HIGHWIND — per-instrument overall win rate
# ---------------------------------------------------------------------------

def ihw_update(state: dict, symbol: str, exit_reason: str, pnl_r: float):
    """
    Update rolling WR window + adjust Highwind level.

    Outcome mapping:
      TP              → WIN  (1)
      SL              → LOSS (0)
      MANUAL/TIMEOUT pnl_r < 0  → LOSS (0)
      MANUAL/TIMEOUT pnl_r >= 0 → EXCLUDE
      UNKNOWN         → EXCLUDE

    Level ladder (per-instrument thresholds from config):
      NORMAL → L1 → L2 → HALT   (step-down on WR decline)
      HALT   → shadow mode (instrument_modes set to "paper")
      HALT auto-recovery: WR ≥ l1_threshold while in HALT → L2 + restore live
      Step-up (L2/L1): auto when WR ≥ l1_threshold (one level per confirmed window)
    """
    hw_cfg   = get_highwind_config(symbol)
    win_n    = hw_cfg.get("window", 30)
    halt_thr = hw_cfg.get("halt_threshold", 0.32)
    l2_thr   = hw_cfg.get("l2_threshold",   0.36)
    l1_thr   = hw_cfg.get("l1_threshold",   0.40)

    ihw = state.setdefault("instrument_highwind", {}).setdefault(
        symbol, {"window": [], "level": "NORMAL", "halt_since": None}
    )

    if exit_reason == "TP":
        ihw["window"].append(1)
    elif exit_reason == "SL":
        ihw["window"].append(0)
    elif exit_reason in ("MANUAL", "TIMEOUT"):
        if pnl_r < 0:
            ihw["window"].append(0)

    if len(ihw["window"]) > win_n:
        ihw["window"] = ihw["window"][-win_n:]

    n = len(ihw["window"])
    if n < win_n:
        print(f"  📊 HW {symbol}  window {n}/{win_n} filling → {ihw['level']} (hold)")
        return

    wr      = sum(ihw["window"]) / win_n
    current = ihw["level"]

    if current == "HALT" and wr >= l1_thr:
        new_level = "L2"
        cfg_mode = config.get("instruments", {}).get(symbol, {}).get("mode", "paper")
        if cfg_mode == "live":
            state.setdefault("instrument_modes", {})[symbol] = "live"
            print(f"  ✅ HW {symbol} recovered HALT→L2 — restored to live (0.50× risk)")
        ihw["halt_since"] = None
    elif current == "HALT":
        new_level = "HALT"
    elif wr >= l1_thr:
        step_up   = {"L2": "L1", "L1": "NORMAL", "NORMAL": "NORMAL"}
        new_level = step_up.get(current, "NORMAL")
        if new_level != current:
            ihw["halt_since"] = None
    elif wr < halt_thr:
        new_level = "HALT"
        if current != "HALT":
            ihw["halt_since"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            state.setdefault("instrument_modes", {})[symbol] = "paper"
            log_event("INSTRUMENT_HALTED",
                      f"{symbol} WR={wr:.1%} ({sum(ihw['window'])}/{win_n}) → shadow mode")
    elif wr < l2_thr:
        new_level = "L2"
    else:
        new_level = "L1"

    ihw["level"] = new_level
    print(f"  📊 HW {symbol}  WR={wr:.1%} ({sum(ihw['window'])}/{win_n})  {current} → {new_level}")


# ---------------------------------------------------------------------------
# CIRCUIT BREAKER (CB ANCHOR) — portfolio level
# ---------------------------------------------------------------------------

def cb_init(state: dict):
    cb = state.get("cb_anchor", {})
    if cb.get("peak") is None:
        eq = float(mt5.account_info().equity)
        cb["peak"]              = eq
        cb["anchor"]            = eq * config["cb_anchor"]["recovery_buffer"]
        cb["triggered_session"] = False
        cb["last_trigger_time"] = None
        state["cb_anchor"]      = cb
        print(f"  CB anchor init  peak={eq:.2f}")


def cb_update_peak(state: dict):
    eq = float(mt5.account_info().equity)
    cb = state["cb_anchor"]
    if cb.get("peak") is None or eq > cb["peak"]:
        cb["peak"]   = eq
        cb["anchor"] = eq * config["cb_anchor"]["recovery_buffer"]


def cb_check(state: dict) -> bool:
    """Returns True if CB triggered (skip session)."""
    eq = float(mt5.account_info().equity)
    cb = state["cb_anchor"]
    if cb.get("triggered_session", False):
        return True
    peak   = cb.get("peak")
    anchor = cb.get("anchor")
    if peak is None:
        return False
    trig_pct  = config["cb_anchor"]["trigger_pct"]
    next_trig = config["cb_anchor"]["next_trigger_pct"]
    if anchor and eq <= anchor * next_trig:
        cb["triggered_session"] = True
        cb["last_trigger_time"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        old_anchor = anchor
        new_anchor = round(old_anchor * config["cb_anchor"]["recovery_buffer"], 2)
        cb["peak"]   = old_anchor
        cb["anchor"] = new_anchor
        print(f"     CB ratchet: peak={old_anchor:.2f}  anchor={new_anchor:.2f}"
              f"  next_trig={new_anchor * next_trig:.2f}")
        return True
    if eq <= peak * (1 - trig_pct):
        cb["triggered_session"] = True
        cb["last_trigger_time"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        new_anchor = round(peak * (1 - trig_pct) * config["cb_anchor"]["recovery_buffer"], 2)
        cb["anchor"] = new_anchor
        print(f"     CB ratchet (first hit): peak={peak:.2f}  anchor={new_anchor:.2f}"
              f"  next_trig={new_anchor * next_trig:.2f}")
        return True
    return False


def cb_reset_session(state: dict):
    """Call at each new session start to allow trading again."""
    state["cb_anchor"]["triggered_session"] = False


# ---------------------------------------------------------------------------
# RULE 2 — hard equity floor (15% DD from base)
# ---------------------------------------------------------------------------

def r2_init(state: dict):
    r2 = state.get("rule2", {})
    if r2.get("base_equity") is None:
        eq = float(mt5.account_info().equity)
        r2["base_equity"]     = eq
        r2["triggered_today"] = False
        r2["trigger_date"]    = None
        state["rule2"]        = r2
        print(f"  Rule 2 init  base={eq:.2f}")


def r2_check(state: dict) -> bool:
    """Returns True if hard floor triggered (stop day)."""
    r2    = state.get("rule2", {})
    today = datetime.now(timezone.utc).date().isoformat()

    if r2.get("triggered_today") and r2.get("trigger_date") == today:
        return True
    if r2.get("trigger_date") and r2["trigger_date"] != today:
        r2["triggered_today"] = False

    base = r2.get("base_equity")
    if base is None:
        return False
    eq    = float(mt5.account_info().equity)
    floor = base * config["rule2"]["floor_pct"]
    if eq <= floor:
        r2["triggered_today"] = True
        r2["trigger_date"]    = today
        return True
    return False


# ---------------------------------------------------------------------------
# BAR-BASED COOLDOWN — per instrument
# ---------------------------------------------------------------------------

def cd_activate(state: dict, symbol: str):
    hs = state["hypothesis_states"][symbol]
    hs["in_cooldown"]      = True
    hs["bars_since_sl"]    = 0
    hs["new_extreme_flag"] = False


def cd_tick(state: dict, symbol: str) -> str:
    """
    Called on each new 15m bar when in cooldown.
    Returns: 'waiting' | 'reassess' | 'active'
    """
    hs = state["hypothesis_states"][symbol]
    if not hs["in_cooldown"]:
        return "active"
    hs["bars_since_sl"] += 1
    if hs["bars_since_sl"] >= config.get("cooldown_bars", 6):
        hs["in_cooldown"] = False
        return "reassess"
    return "waiting"


def cd_reassess(state: dict, symbol: str, st_1h_dir: int, bar_close: float) -> str | None:
    """
    After cooldown — re-enter only if context still valid.
    Returns: 'B' if valid for re-entry, None otherwise.
    """
    hs = state["hypothesis_states"][symbol]
    if not hs.get("choch_confirmed"):
        return None
    if hs.get("choch_direction") != st_1h_dir:
        hs["choch_confirmed"] = False
        return None
    choch_dir   = hs["choch_direction"]
    choch_level = hs["choch_level"]
    if choch_level is not None:
        if choch_dir == 1 and bar_close <= choch_level:
            hs["choch_confirmed"] = False
            return None
        if choch_dir == -1 and bar_close >= choch_level:
            hs["choch_confirmed"] = False
            return None
    return "B"


# ---------------------------------------------------------------------------
# PIVOT ARRAY — per instrument, persisted in state
# ---------------------------------------------------------------------------

def pa_add_pivot(state: dict, symbol: str, pivot_type: str,
                 price: float, time_str: str):
    """Add confirmed swing pivot to state. Trim to pivot_maxlen."""
    if pivot_type == "H":
        pivot_type = "high"
    elif pivot_type == "L":
        pivot_type = "low"
    arr = state["pivot_arrays"].setdefault(symbol, [])
    arr.append({"type": pivot_type, "price": price, "time": time_str})
    maxlen = config.get("pivot_maxlen", 8)
    if len(arr) > maxlen:
        state["pivot_arrays"][symbol] = arr[-maxlen:]


def pa_last_high(state: dict, symbol: str) -> float | None:
    for p in reversed(state["pivot_arrays"].get(symbol, [])):
        if p["type"] in ("high", "H"):
            return p["price"]
    return None


def pa_last_low(state: dict, symbol: str) -> float | None:
    for p in reversed(state["pivot_arrays"].get(symbol, [])):
        if p["type"] in ("low", "L"):
            return p["price"]
    return None


def pa_on_new_flip(state: dict, symbol: str):
    """Reset sb_used and new_extreme_flag on every 15m ST flip."""
    hs = state["hypothesis_states"][symbol]
    hs["sb_used"]          = False
    hs["new_extreme_flag"] = False


# ---------------------------------------------------------------------------
# HYPOTHESIS A1 — ST Compression Bounce
# ---------------------------------------------------------------------------

def count_st_steps_since_flip(st_line_vals, st_dirs, bar_idx: int,
                               direction: str) -> int:
    """Count how many times ST line changed value since last flip."""
    current_dir = 1 if direction == "long" else -1
    steps = 0
    prev_val = None
    for i in range(bar_idx, -1, -1):
        if int(st_dirs[i]) != current_dir:
            break
        if prev_val is not None and st_line_vals[i] != prev_val:
            steps += 1
        prev_val = st_line_vals[i]
    return steps


def is_a1_context(st_line_vals, st_dirs, bar_idx: int,
                  direction: str, min_steps: int) -> bool:
    return count_st_steps_since_flip(st_line_vals, st_dirs, bar_idx, direction) >= min_steps


def ema3_ok(ema_traj_vals, idx: int, direction: str) -> bool:
    """EMA3 slope filter: rising for long, falling for short."""
    if idx < 1:
        return True
    slope = ema_traj_vals[idx] - ema_traj_vals[idx - 1]
    return slope > 0 if direction == "long" else slope < 0


def hyp_a1_trigger(bar_low: float, bar_high: float,
                   ema_touch_i: float, ema_touch_prev: float,
                   close_prev: float,
                   rsi_i: float, ema_traj_vals, idx: int,
                   direction: str, touch_allowed: bool,
                   a1_cfg: dict) -> bool:
    """
    A1 V2 trigger: EMA touch + EMA3 trajectory + RSI gate.
    touch_allowed: False suppresses flicker re-entry when position already open.
    """
    eps      = a1_cfg.get("touch_epsilon", 0.0003)
    bull_rsi = a1_cfg.get("rsi_bull_gate", 55.0)
    bear_rsi = a1_cfg.get("rsi_bear_gate", 48.0)

    # RSI gate
    if direction == "long" and rsi_i <= bull_rsi:
        return False
    if direction == "short" and rsi_i >= bear_rsi:
        return False

    # EMA3 trajectory
    if not ema3_ok(ema_traj_vals, idx, direction):
        return False

    # EMA touch
    if direction == "long":
        touched = bar_low <= ema_touch_i + eps
    else:
        touched = bar_high >= ema_touch_i - eps

    if not touched or not touch_allowed:
        return False

    return True


def get_a1_sl(direction: str, entry_price: float,
              ema_touch: float, st_line: float, a1_cfg: dict) -> float:
    sl_min  = a1_cfg.get("sl_min", 0.0012)
    sl_max  = a1_cfg.get("sl_max", 0.0020)
    pocket  = abs(ema_touch - st_line)
    sl_dist = max(sl_min, min(pocket, sl_max))
    return entry_price - sl_dist if direction == "long" else entry_price + sl_dist


# ---------------------------------------------------------------------------
# HYPOTHESIS A2 — Pivot Retracement (Premium-Discount)
# ---------------------------------------------------------------------------

def hyp_a2_of_gate(state: dict, symbol: str, direction: str,
                   a2_cfg: dict) -> bool:
    """
    Lax 4/2 Order Flow gate: last of_depth pivots contain ≥ of_min
    sequential higher-highs + higher-lows (bull) or lower-lows + lower-highs (bear).
    """
    depth = a2_cfg.get("of_depth", 4)
    arr = state["pivot_arrays"].get(symbol, [])[-depth:]
    if len(arr) < a2_cfg.get("of_min", 2):
        return False

    highs = [p["price"] for p in arr if p["type"] in ("high", "H")]
    lows  = [p["price"] for p in arr if p["type"] in ("low", "L")]

    if len(highs) < 2 or len(lows) < 2:
        return False

    if direction == "long":
        hh_pairs = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i - 1])
        hl_pairs = sum(1 for i in range(1, len(lows))  if lows[i]  > lows[i - 1])
        return hh_pairs >= 1 and hl_pairs >= 1
    else:
        ll_pairs = sum(1 for i in range(1, len(lows))  if lows[i]  < lows[i - 1])
        lh_pairs = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i - 1])
        return ll_pairs >= 1 and lh_pairs >= 1


def calculate_pd_levels(state: dict, symbol: str, direction: str,
                        a2_cfg: dict) -> dict | None:
    """Calculate 61.8% premium/discount retracement zone."""
    if direction == "long":
        swing_high = pa_last_high(state, symbol)
        swing_low  = pa_last_low(state, symbol)
    else:
        swing_high = pa_last_high(state, symbol)
        swing_low  = pa_last_low(state, symbol)

    if swing_high is None or swing_low is None:
        return None

    range_sz   = swing_high - swing_low
    pd_level   = a2_cfg.get("pd_level", 0.618)

    if direction == "long":
        threshold = swing_low + range_sz * (1 - pd_level)
        return {"threshold": threshold, "direction": "long",
                "swing_high": swing_high, "swing_low": swing_low}
    else:
        threshold = swing_high - range_sz * (1 - pd_level)
        return {"threshold": threshold, "direction": "short",
                "swing_high": swing_high, "swing_low": swing_low}


def is_in_discount_zone(bar_close: float, pd_levels: dict | None,
                        direction: str) -> bool:
    if pd_levels is None:
        return False
    if direction == "long":
        return bar_close <= pd_levels["threshold"]
    return bar_close >= pd_levels["threshold"]


def track_new_extreme(bar_close: float, state: dict, symbol: str,
                      direction: str) -> bool:
    """Step 2 of A2: set new_extreme_flag if new extreme beyond last pivot."""
    hs = state["hypothesis_states"][symbol]
    if direction == "short":
        last_low = pa_last_low(state, symbol)
        if last_low and bar_close < last_low:
            hs["new_extreme_flag"] = True
    elif direction == "long":
        last_high = pa_last_high(state, symbol)
        if last_high and bar_close > last_high:
            hs["new_extreme_flag"] = True
    return hs["new_extreme_flag"]


def hyp_a2_trigger(bar_close: float, state: dict, symbol: str,
                   direction: str, a2_cfg: dict) -> bool:
    """Step 3: fires when new extreme confirmed + 61.8% discount zone reached."""
    hs = state["hypothesis_states"][symbol]
    if not hs.get("new_extreme_flag"):
        return False
    pd_levels = calculate_pd_levels(state, symbol, direction, a2_cfg)
    return is_in_discount_zone(bar_close, pd_levels, direction)


def get_a2_sl(direction: str, entry_price: float,
              state: dict, symbol: str, a2_cfg: dict) -> float:
    sl_min  = a2_cfg.get("sl_min", 0.0015)
    sl_max  = a2_cfg.get("sl_max", 0.0020)
    epsilon = a2_cfg.get("sl_epsilon", 0.0003)
    if direction == "short":
        pivot = pa_last_high(state, symbol)
    else:
        pivot = pa_last_low(state, symbol)
    if pivot is None:
        sl_dist = sl_min
    else:
        raw_dist = abs(entry_price - pivot) + epsilon
        sl_dist  = max(raw_dist, sl_min)
        sl_dist  = min(sl_dist, sl_max)
    return entry_price - sl_dist if direction == "long" else entry_price + sl_dist


# ---------------------------------------------------------------------------
# HYPOTHESIS B — ChoCh Structural Break
# ---------------------------------------------------------------------------

def detect_choch(bar_close: float, state: dict, symbol: str,
                 st_1h_dir: int, st_15m_dir: int) -> bool:
    """
    Bullish ChoCh (1H bullish): entry TF was bearish, closes above last swing HIGH.
    Bearish ChoCh (1H bearish): entry TF was bullish, closes below last swing LOW.
    Fake-out filter: entry TF ST must still be opposite.
    """
    hs = state["hypothesis_states"][symbol]
    if st_1h_dir == 1:
        last_high = pa_last_high(state, symbol)
        if last_high and bar_close > last_high and st_15m_dir == -1:
            hs["choch_confirmed"] = True
            hs["choch_direction"] = 1
            hs["choch_level"]     = last_high
            return True
    elif st_1h_dir == -1:
        last_low = pa_last_low(state, symbol)
        if last_low and bar_close < last_low and st_15m_dir == 1:
            hs["choch_confirmed"] = True
            hs["choch_direction"] = -1
            hs["choch_level"]     = last_low
            return True
    return False


def is_choch_valid(bar_close: float, state: dict, symbol: str) -> bool:
    """ChoCh invalidated if price closes back through the ChoCh level."""
    hs = state["hypothesis_states"][symbol]
    if not hs.get("choch_confirmed"):
        return False
    choch_dir   = hs["choch_direction"]
    choch_level = hs["choch_level"]
    if choch_level is None:
        return True
    if choch_dir == 1:
        return bar_close > choch_level
    return bar_close < choch_level


def hyp_b_trigger(bar_close: float, state: dict, symbol: str,
                  direction: str) -> bool:
    """
    Structural break: price closes beyond last confirmed swing extreme.
    One entry per confirmed pivot (sb_used resets on each ST flip).
    """
    hs = state["hypothesis_states"][symbol]
    if hs.get("sb_used"):
        return False
    if direction == "long":
        last_high = pa_last_high(state, symbol)
        if last_high and bar_close > last_high:
            hs["sb_used"] = True
            return True
    else:
        last_low = pa_last_low(state, symbol)
        if last_low and bar_close < last_low:
            hs["sb_used"] = True
            return True
    return False


# ---------------------------------------------------------------------------
# HYPOTHESIS CLASSIFICATION
# ---------------------------------------------------------------------------

def classify_hypothesis(state: dict, symbol: str, df_15m, df_1h,
                        bar_idx: int, st_1h_dir: int) -> tuple:
    """
    Legacy context-only classifier.

    Execution uses trigger-first dispatch in detect_signal() instead, so A1/A2
    priority is applied only when signals fire on the same bar.

    Returns (hypothesis: str|None, direction: str|None, allow_stack: bool).

    Priority:
    P1: ChoCh confirmed + hyp_b_enabled           → B (B stacking rules)
    P2: A1 valid AND A2 valid (conflict resolution)
    P3: A2 valid only                              → A2 (stack allowed)
    P4: A1 valid only                              → A1 (stack allowed)
    None valid                                     → skip
    """
    a1_cfg = get_hyp_config(symbol, "a1")
    a2_cfg = get_hyp_config(symbol, "a2")
    b_cfg  = get_hyp_config(symbol, "b")

    st_dirs  = df_15m["st_direction"].values
    st_lines = df_15m["st_line"].values
    st_15m_dir = int(st_dirs[bar_idx])

    entry_dir_a1 = "long" if st_15m_dir == 1 else "short"
    hs = state["hypothesis_states"][symbol]

    # Priority 1: ChoCh (Hyp B)
    if b_cfg.get("enabled", True) and hs.get("choch_confirmed"):
        b_dir_int = hs.get("choch_direction", st_1h_dir)
        b_dir     = "long" if b_dir_int == 1 else "short"
        return ("B", b_dir, False)

    trend_dir_1h = "long" if st_1h_dir == 1 else "short"

    # Determine A1 eligibility (trend-following only — A1 never fires counter-1H)
    min_steps = a1_cfg.get("st_min_steps", 2)
    a1_valid  = (
        a1_cfg.get("enabled", True)
        and is_a1_context(st_lines, st_dirs, bar_idx, entry_dir_a1, min_steps)
        and (entry_dir_a1 == trend_dir_1h)
    )

    # Determine A2 eligibility (A2 direction = 1H direction, independent of 15m ST)
    a2_of_dir = trend_dir_1h
    a2_valid  = (
        a2_cfg.get("enabled", True)
        and hyp_a2_of_gate(state, symbol, a2_of_dir, a2_cfg)
    )

    a2_dir = a2_of_dir

    # Priority 2: Conflict resolution (both valid)
    if a1_valid and a2_valid:
        if entry_dir_a1 == a2_dir:
            return ("A1", entry_dir_a1, False)
        a1_aligned = (entry_dir_a1 == trend_dir_1h)
        a2_aligned = (a2_dir       == trend_dir_1h)
        if a1_aligned and not a2_aligned:
            return ("A1", entry_dir_a1, False)
        if a2_aligned and not a1_aligned:
            return ("A2", a2_dir, False)
        return (None, None, False)

    # Priority 3: A2 alone
    if a2_valid:
        return ("A2", a2_dir, True)

    # Priority 4: A1 alone
    if a1_valid:
        return ("A1", entry_dir_a1, True)

    return (None, None, False)


def resolve_a1_a2_priority(a1_fired: bool, a1_dir: str,
                           a2_fired: bool, a2_dir: str,
                           trend_dir: str) -> tuple:
    """
    Resolve A1/A2 only after their current-bar triggers fire.

    This keeps A1 context from pre-empting A2. The priority table is still used,
    but only for real same-bar signal conflicts.
    """
    if a1_fired and a2_fired:
        if a1_dir == a2_dir:
            return ("A1", a1_dir, False)
        a1_aligned = (a1_dir == trend_dir)
        a2_aligned = (a2_dir == trend_dir)
        if a1_aligned and not a2_aligned:
            return ("A1", a1_dir, False)
        if a2_aligned and not a1_aligned:
            return ("A2", a2_dir, False)
        return (None, None, False)
    if a2_fired:
        return ("A2", a2_dir, True)
    if a1_fired:
        return ("A1", a1_dir, True)
    return (None, None, False)


# ---------------------------------------------------------------------------
# 1H STABILITY GATE
# ---------------------------------------------------------------------------

def is_1h_stable(df_1h, cfg: dict) -> tuple:
    """
    Returns (is_stable: bool, st_dir: int).
    Stable = last stable_bars_1h 1H bars all same ST direction.
    """
    n = cfg.get("stable_bars_1h", 3)
    if len(df_1h) < n:
        return False, 0
    recent = df_1h["st_direction"].values[-n:]
    if len(set(int(d) for d in recent)) != 1:
        return False, 0
    return True, int(recent[-1])


# ---------------------------------------------------------------------------
# SL / TP UNIFIED
# ---------------------------------------------------------------------------

def get_sl(hypothesis: str, direction: str, entry_price: float,
           state: dict, symbol: str, df_15m, bar_idx: int) -> float | None:
    cfg = get_hyp_config(symbol, hypothesis.lower())
    if hypothesis == "A1":
        ema_touch = float(df_15m["ema_touch"].iloc[bar_idx])
        st_line   = float(df_15m["st_line"].iloc[bar_idx])
        return get_a1_sl(direction, entry_price, ema_touch, st_line, cfg)
    elif hypothesis == "A2":
        return get_a2_sl(direction, entry_price, state, symbol, cfg)
    elif hypothesis == "B":
        sl_fixed = cfg.get("sl_fixed", 0.0020)
        return entry_price - sl_fixed if direction == "long" else entry_price + sl_fixed
    return None


def get_tp(entry_price: float, sl_price: float, hypothesis: str,
           symbol: str) -> float:
    cfg     = get_hyp_config(symbol, hypothesis.lower())
    rr      = cfg.get("rr", 1.5)
    sl_dist = abs(entry_price - sl_price)
    if entry_price > sl_price:  # long
        return entry_price + sl_dist * rr
    return entry_price - sl_dist * rr


# ---------------------------------------------------------------------------
# STACKING CHECK
# ---------------------------------------------------------------------------

def can_stack(hypothesis: str, allow_stack_flag: bool,
              open_trades: list, direction: str) -> bool:
    if hypothesis == "B":
        if not open_trades:
            return True
        return all(t["direction"].lower() == direction for t in open_trades)
    if not allow_stack_flag:
        return False
    if not open_trades:
        return True
    return all(t["direction"].lower() == direction for t in open_trades)


# ---------------------------------------------------------------------------
# SIGNAL DETECTION — 8-step bar decision tree
# ---------------------------------------------------------------------------

def detect_signal(state: dict, symbol: str, bundle: DataBundle,
                  bar_idx: int) -> dict | None:
    """
    Implements the full 8-step decision tree from systemC-overview.md.
    Returns signal dict if entry should be placed, None otherwise.
    """
    df_15m = bundle.df_15m
    df_1h  = bundle.df_1h

    bar = df_15m.iloc[bar_idx]

    skip_reason  = None
    st_15m_dir   = int(bar["st_direction"])
    bar_regime   = str(bar.get("regime", "UNKNOWN"))
    bar_session  = str(bar.get("session", "none"))

    # Step 1: Session gate — per-instrument trading hours
    t_start, t_end = get_trading_hours(symbol)
    bar_hour = bar["time_utc"].hour
    if not (t_start <= bar_hour < t_end):
        skip_reason = "SESSION_SKIP"
        _log_signal_replay(symbol, bar, None, None, None, False, skip_reason, df_1h)
        return None

    # Step 2: 1H stability gate
    stable, st_1h_dir = is_1h_stable(df_1h, config)
    if not stable:
        skip_reason = "1H_STABILITY_FAIL"
        _log_signal_replay(symbol, bar, None, None, None, False, skip_reason, df_1h)
        return None

    # Step 3: Cooldown state
    cd_status = cd_tick(state, symbol)
    if cd_status == "waiting":
        skip_reason = "COOLDOWN_ACTIVE"
        _log_signal_replay(symbol, bar, None, None, None, False, skip_reason, df_1h)
        return None
    if cd_status == "reassess":
        re_hyp = cd_reassess(state, symbol, st_1h_dir, float(bar["close"]))
        if re_hyp is None:
            skip_reason = "COOLDOWN_REASSESS_FAIL"
            _log_signal_replay(symbol, bar, None, None, None, False, skip_reason, df_1h)
            return None

    # Step 4: Pullback filter removed — A1 pullback gate is handled during trigger-first dispatch.
    # (A1 invalid when entry_dir_a1 ≠ trend_dir_1h; A2 uses 1H direction so it can
    #  fire in the 1H direction even when 15m is counter-trend)

    # Step 5: Trigger-first hypothesis dispatch.
    # B keeps top priority, matching the phase2-rework A2+B baseline.
    hyp = direction = None
    allow_stack = False
    b_cfg = get_hyp_config(symbol, "b")
    hs = state["hypothesis_states"][symbol]
    if b_cfg.get("enabled", True) and hs.get("choch_confirmed"):
        b_dir_int = hs.get("choch_direction", st_1h_dir)
        direction = "long" if b_dir_int == 1 else "short"
        if not is_choch_valid(float(bar["close"]), state, symbol):
            hs["choch_confirmed"] = False
            skip_reason = "CHOCH_INVALIDATED"
            _log_signal_replay(symbol, bar, "B", direction, allow_stack, False, skip_reason, df_1h)
            return None
        if hyp_b_trigger(float(bar["close"]), state, symbol, direction):
            hyp = "B"
            allow_stack = False
        else:
            skip_reason = "TRIGGER_NOT_MET"
            _log_signal_replay(symbol, bar, "B", direction, allow_stack, False, skip_reason, df_1h)
            return None

    # A1/A2 priority is resolved only among triggers that actually fired on this bar.
    touch_allowed = len([t for t in state.get("open_trades", []) if t["symbol"] == symbol]) == 0  # A1 flicker suppression — per symbol

    if hyp is None:
        trend_dir_1h = "long" if st_1h_dir == 1 else "short"
        entry_dir_a1 = "long" if st_15m_dir == 1 else "short"
        a2_dir = trend_dir_1h

        a1_fired = False
        a1_cfg = get_hyp_config(symbol, "a1")
        blocked_regimes = a1_cfg.get("blocked_regimes", [])
        if (
            a1_cfg.get("enabled", True)
            and bar_regime not in blocked_regimes
            and entry_dir_a1 == trend_dir_1h
            and is_a1_context(
                df_15m["st_line"].values,
                df_15m["st_direction"].values,
                bar_idx,
                entry_dir_a1,
                a1_cfg.get("st_min_steps", 2),
            )
        ):
            ema_t = df_15m["ema_touch"].values
            ema_tr = df_15m["ema_traj"].values
            a1_fired = hyp_a1_trigger(
                bar_low=float(bar["low"]),
                bar_high=float(bar["high"]),
                ema_touch_i=float(ema_t[bar_idx]),
                ema_touch_prev=float(ema_t[bar_idx - 1]) if bar_idx > 0 else float(ema_t[bar_idx]),
                close_prev=float(df_15m["close"].iloc[bar_idx - 1]) if bar_idx > 0 else 0,
                rsi_i=float(bar["rsi"]),
                ema_traj_vals=ema_tr,
                idx=bar_idx,
                direction=entry_dir_a1,
                touch_allowed=touch_allowed,
                a1_cfg=a1_cfg,
            )

        a2_fired = False
        a2_cfg = get_hyp_config(symbol, "a2")
        if a2_cfg.get("enabled", True) and hyp_a2_of_gate(state, symbol, a2_dir, a2_cfg):
            track_new_extreme(float(bar["close"]), state, symbol, a2_dir)
            a2_fired = hyp_a2_trigger(float(bar["close"]), state, symbol, a2_dir, a2_cfg)

        hyp, direction, allow_stack = resolve_a1_a2_priority(
            a1_fired, entry_dir_a1, a2_fired, a2_dir, trend_dir_1h
        )
        if hyp is None:
            skip_reason = "TRIGGER_NOT_MET"
            _log_signal_replay(symbol, bar, hyp, direction, None, False, skip_reason, df_1h)
            return None

    # Step 8: Stack check — filter to this symbol only (prevent cross-symbol suppression)
    sym_open = [t for t in state.get("open_trades", []) if t["symbol"] == symbol]
    if not can_stack(hyp, allow_stack, sym_open, direction):
        skip_reason = "STACK_BLOCKED"
        _log_signal_replay(symbol, bar, hyp, direction, allow_stack, False, skip_reason, df_1h)
        return None

    _log_signal_replay(symbol, bar, hyp, direction, allow_stack, True, None, df_1h)

    return {
        "symbol"      : symbol,
        "hypothesis"  : hyp,
        "direction"   : direction,
        "allow_stack" : allow_stack,
        "regime"      : bar_regime,
        "session"     : bar_session,
        "st_1h_dir"   : st_1h_dir,
        "is_pullback" : (direction == "short" and st_1h_dir == 1)
                        or (direction == "long" and st_1h_dir == -1),
        "bar_time"    : str(bar["time_utc"]),
        "bar_idx"     : bar_idx,
    }


# ---------------------------------------------------------------------------
# LOT SIZING
# ---------------------------------------------------------------------------

def compute_lot_size(symbol: str, sl_price: float, entry_price: float,
                     hw_level: str = "NORMAL") -> float | None:
    """
    Risk-based lot sizing with Highwind size multiplier.
    Explicit float() casts on all rpyc proxy attributes.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return None

    equity      = float(mt5.account_info().equity)
    size_mult   = config["highwind"]["size_mult"].get(hw_level, 1.00)
    risk_usd    = equity * config["base_risk_pct"] / 100.0 * size_mult
    pip_size    = get_pip_size(symbol)
    sl_dist     = abs(entry_price - sl_price)
    sl_pips     = sl_dist / pip_size

    if sl_pips <= 0:
        return None

    tick_size  = float(info.trade_tick_size)
    tick_value = float(info.trade_tick_value)
    if tick_size <= 0:
        return None
    pip_value_per_lot = (pip_size / tick_size) * tick_value

    raw_lots = risk_usd / (sl_pips * pip_value_per_lot)
    step     = float(info.volume_step)
    lot      = math.floor(raw_lots / step) * step
    if lot < float(info.volume_min):
        print(f"  ⚠️  Lot below minimum after floor: {symbol} floored={lot:.4f} — SKIP")
        return None
    lot = min(float(info.volume_max), lot)
    return round(lot, 2)


# ---------------------------------------------------------------------------
# FILLING MODE  (dynamic — not hardcoded RETURN)
# ---------------------------------------------------------------------------

def get_filling_mode(symbol: str) -> int:
    """
    Auto-detect broker filling mode for symbol.

    IMPORTANT: info.filling_mode is a bitmask using SYMBOL_FILLING_* values:
      SYMBOL_FILLING_FOK = 1  (bit 0)
      SYMBOL_FILLING_IOC = 2  (bit 1)
      neither bit set (fm==0) → RETURN only

    ORDER_FILLING_* are separate constants used in the order request:
      ORDER_FILLING_FOK    = 0
      ORDER_FILLING_IOC    = 1
      ORDER_FILLING_RETURN = 2

    rpyc: int() casts required for proxy object comparison.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_FOK
    fm = int(info.filling_mode)
    if fm & 1:   # bit 0 = FOK supported (SYMBOL_FILLING_FOK=1, not in Python API)
        return mt5.ORDER_FILLING_FOK
    if fm & 2:   # bit 1 = IOC supported (SYMBOL_FILLING_IOC=2, not in Python API)
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN        # fm==0 → RETURN only


# ---------------------------------------------------------------------------
# MIN STOP DISTANCE CHECK
# ---------------------------------------------------------------------------

def check_min_stop_distance(symbol: str, entry_price: float,
                             sl_price: float, tp_price: float) -> bool:
    """Returns True if SL/TP distance meets broker minimum."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return True
    min_dist = float(info.trade_stops_level) * float(info.point)
    if min_dist <= 0:
        return True
    return (abs(entry_price - sl_price) >= min_dist and
            abs(entry_price - tp_price) >= min_dist)


# ---------------------------------------------------------------------------
# PLACE ORDER
# ---------------------------------------------------------------------------

def place_order(symbol: str, direction: str, lot: float,
                sl_price: float, tp_price: float,
                hypothesis: str, session: str, regime: str = "") -> dict | None:
    """
    Place market order with SL+TP.
    rpyc: all attribute reads use explicit float()/int() casts.
    Retcode 10027/10030 shown to user.
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None

    order_type = mt5.ORDER_TYPE_BUY if direction == "long" else mt5.ORDER_TYPE_SELL
    price      = float(tick.ask) if direction == "long" else float(tick.bid)
    digits     = int(mt5.symbol_info(symbol).digits)

    if not check_min_stop_distance(symbol, price, sl_price, tp_price):
        log_event("SLTP_TOO_CLOSE", f"{symbol} {direction} price={price:.{digits}f} "
                                    f"sl={sl_price:.{digits}f} tp={tp_price:.{digits}f}")
        return None

    regime_tag = regime[:8] if regime else ""
    comment    = f"SysC-{symbol}-{session}-{hypothesis}-{regime_tag}"

    request = {
        "action"       : mt5.TRADE_ACTION_DEAL,
        "symbol"       : symbol,
        "volume"       : lot,
        "type"         : order_type,
        "price"        : price,
        "sl"           : round(sl_price, digits),
        "tp"           : round(tp_price, digits),
        "deviation"    : 10,
        "magic"        : config["magic_number"],
        "comment"      : comment,
        "type_filling" : get_filling_mode(symbol),
        "type_time"    : mt5.ORDER_TIME_GTC,
    }

    result  = mt5.order_send(request)
    retcode = int(result.retcode) if result else -1
    if result is None or retcode != int(mt5.TRADE_RETCODE_DONE):
        if retcode == 10027:
            log_event("ORDER_FAIL", f"{symbol} retcode={retcode} — Algo Trading DISABLED in MT5")
        elif retcode == 10030:
            info = mt5.symbol_info(symbol)
            log_event("ORDER_FAIL", f"{symbol} retcode={retcode} — filling_mode mismatch "
                                    f"broker_bitmask={int(info.filling_mode) if info else '?'}")
        else:
            log_event("ORDER_FAIL", f"{symbol} {direction} retcode={retcode}")
        return None

    ticket     = int(result.order)
    fill_price = float(result.price)
    if fill_price == 0.0:
        time.sleep(0.3)
        deals = mt5.history_deals_get(position=ticket)
        deals_local = rpyc.utils.classic.obtain(deals) if deals else []
        open_deals  = [d for d in deals_local if int(d.entry) == 0]
        if open_deals:
            fill_price = float(open_deals[-1].price)
            print(f"  ℹ️  result.price=0 — fill from history: {fill_price:.{digits}f}")
        else:
            print(f"  ⚠️  result.price=0 and no history deal — entry_price unreliable")

    return {
        "ticket"     : ticket,
        "symbol"     : symbol,
        "direction"  : direction,
        "entry_price": fill_price,
        "sl_price"   : round(sl_price, digits),
        "tp_price"   : round(tp_price, digits),
        "lot_size"   : lot,
    }


# ---------------------------------------------------------------------------
# CLOSE ORDER
# ---------------------------------------------------------------------------

def close_order(ticket: int, symbol: str, direction: str, lot: float) -> bool:
    """Force close position by ticket (timeout / manual)."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    order_type = mt5.ORDER_TYPE_SELL if direction == "long" else mt5.ORDER_TYPE_BUY
    price      = float(tick.bid) if direction == "long" else float(tick.ask)
    digits     = int(mt5.symbol_info(symbol).digits)

    request = {
        "action"       : mt5.TRADE_ACTION_DEAL,
        "symbol"       : symbol,
        "volume"       : lot,
        "type"         : order_type,
        "position"     : ticket,
        "price"        : price,
        "deviation"    : 10,
        "magic"        : config["magic_number"],
        "comment"      : "SysC-timeout",
        "type_filling" : get_filling_mode(symbol),
        "type_time"    : mt5.ORDER_TIME_GTC,
    }
    result = mt5.order_send(request)
    return result is not None and int(result.retcode) == int(mt5.TRADE_RETCODE_DONE)


# ---------------------------------------------------------------------------
# POSITION MONITORING
# ---------------------------------------------------------------------------

def get_open_tickets() -> set:
    """Return set of open tickets belonging to System C (by magic number)."""
    positions = rpyc.utils.classic.obtain(mt5.positions_get() or [])
    return {int(p.ticket) for p in positions if int(p.magic) == config["magic_number"]}


def get_close_reason(ticket: int, symbol: str) -> tuple:
    """
    Look up how a position was closed.
    Returns (exit_reason, exit_price, exit_time, entry_price_hist) or ('UNKNOWN', 0.0, None, None).
    entry_price_hist: fill price from opening deal (entry==0), used to correct entry_price=0 bug.
    """
    history          = rpyc.utils.classic.obtain(mt5.history_deals_get(ticket=ticket) or [])
    entry_price_hist = None
    exit_reason      = "UNKNOWN"
    exit_price       = 0.0
    exit_time_str    = None
    for deal in history:
        if int(deal.entry) == 0:   # opening fill
            p = float(deal.price)
            if p != 0.0:
                entry_price_hist = p
        if int(deal.entry) == int(mt5.DEAL_ENTRY_OUT):
            reason_code = int(getattr(deal, "reason", 0))
            if reason_code == int(mt5.DEAL_REASON_SL):
                exit_reason = "SL"
            elif reason_code == int(mt5.DEAL_REASON_TP):
                exit_reason = "TP"
            elif reason_code in (int(mt5.DEAL_REASON_CLIENT), int(mt5.DEAL_REASON_MOBILE),
                                 int(mt5.DEAL_REASON_WEB), int(mt5.DEAL_REASON_EXPERT)):
                exit_reason = "MANUAL"
            else:
                exit_reason = "UNKNOWN"
            exit_price    = float(deal.price)
            exit_time_str = datetime.fromtimestamp(
                int(deal.time), tz=timezone.utc).replace(tzinfo=None).isoformat()
    return exit_reason, exit_price, exit_time_str, entry_price_hist


def compute_pnl_r(direction: str, entry_price: float,
                  exit_price: float, sl_price: float,
                  entry_override: float = None) -> float:
    entry   = entry_override if entry_override is not None else entry_price
    sl_dist = abs(entry - sl_price)
    if sl_dist == 0:
        return 0.0
    if direction == "long":
        return (exit_price - entry) / sl_dist
    return (entry - exit_price) / sl_dist


# ---------------------------------------------------------------------------
# SL/TP VERIFICATION AND REPAIR
# ---------------------------------------------------------------------------

def verify_sltp(trade: dict) -> bool:
    """Check that broker position still has SL and TP set."""
    positions = rpyc.utils.classic.obtain(mt5.positions_get(ticket=trade["ticket"]) or [])
    if not positions:
        return True
    p = positions[0]
    return float(p.sl) != 0.0 and float(p.tp) != 0.0


def fix_sltp(trade: dict) -> bool:
    """Re-apply SL/TP if missing (broker glitch recovery)."""
    info   = mt5.symbol_info(trade["symbol"])
    digits = int(info.digits) if info else 5
    request = {
        "action"   : mt5.TRADE_ACTION_SLTP,
        "symbol"   : trade["symbol"],
        "position" : trade["ticket"],
        "sl"       : round(trade["sl_price"], digits),
        "tp"       : round(trade["tp_price"], digits),
    }
    result = mt5.order_send(request)
    return result is not None and int(result.retcode) == int(mt5.TRADE_RETCODE_DONE)


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def _log_path(template: str) -> str:
    month = datetime.now().strftime("%Y%m")
    return template.replace("{YYYYMM}", month)


def log_trade(trade: dict, exit_reason: str, exit_price: float,
              exit_time: str, pnl_r: float, pnl_usd: float,
              hypothesis: str, mode: str,
              st_context_dir: int, allow_stack: bool, is_pullback: bool):
    path = _log_path(config["logging"]["trade_log"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_header = not os.path.exists(path)
    with open(path, "a") as f:
        if write_header:
            f.write("ticket,symbol,direction,hypothesis,mode,session,regime,"
                    "entry_time,entry_price,exit_time,exit_price,"
                    "sl_price,tp_price,sl_pips,rr,exit_reason,"
                    "pnl_r,pnl_usd,lot_size,"
                    "st_context_dir,allow_stack,is_pullback\n")
        pip_size = get_pip_size(trade["symbol"])
        sl_pips  = abs(trade["entry_price"] - trade["sl_price"]) / pip_size
        rr       = abs(trade["tp_price"] - trade["entry_price"]) / abs(trade["tp_price"] - trade["sl_price"]) if abs(trade["tp_price"] - trade["sl_price"]) > 0 else 0
        f.write(f"{trade['ticket']},{trade['symbol']},{trade['direction']},"
                f"{hypothesis},{mode},{trade.get('session','')},{trade.get('regime','')},"
                f"{trade.get('entry_time','')},{trade['entry_price']},"
                f"{exit_time},{exit_price},"
                f"{trade['sl_price']},{trade['tp_price']},{sl_pips:.1f},{rr:.2f},"
                f"{exit_reason},{pnl_r:.4f},{pnl_usd:.2f},{trade['lot_size']},"
                f"{st_context_dir},{allow_stack},{is_pullback}\n")


def log_event(event_type: str, detail: str):
    path = _log_path(config["logging"]["event_log"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_header = not os.path.exists(path)
    ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    print(f"  [{event_type}] {detail}")
    with open(path, "a") as f:
        if write_header:
            f.write("timestamp,event_type,detail\n")
        safe = detail.replace(",", ";")
        f.write(f"{ts},{event_type},{safe}\n")


def _log_signal_replay(symbol: str, bar, hypothesis, direction,
                        allow_stack, order_placed: bool, skip_reason, df_1h):
    path = config["logging"]["signal_log"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_header = not os.path.exists(path)
    with open(path, "a") as f:
        if write_header:
            f.write("signal_time,symbol,session,regime,st_15m_dir,st_1h_dir,"
                    "hypothesis,direction,allow_stack,is_pullback,"
                    "cooldown_active,order_placed,skip_reason,"
                    "entry_bar_15m,entry_bar_1h\n")
        st_15m = int(bar.get("st_direction", 0)) if bar is not None else 0
        st_1h  = int(df_1h["st_direction"].iloc[-1]) if df_1h is not None and len(df_1h) else 0
        hyp_str  = hypothesis or ""
        dir_str  = direction  or ""
        stack_s  = str(allow_stack) if allow_stack is not None else ""
        pullback = ((dir_str == "short" and st_1h == 1) or
                    (dir_str == "long"  and st_1h == -1))
        bar_15m_time = str(bar["time_utc"]) if bar is not None else ""
        bar_1h_time  = str(df_1h["time_utc"].iloc[-1]) if df_1h is not None and len(df_1h) else ""
        skip_s = skip_reason or ""
        f.write(f"{bar_15m_time},{symbol},"
                f"{bar.get('session','') if bar is not None else ''},"
                f"{bar.get('regime','') if bar is not None else ''},"
                f"{st_15m},{st_1h},"
                f"{hyp_str},{dir_str},{stack_s},{pullback},"
                f"False,{order_placed},{skip_s},"
                f"{bar_15m_time},{bar_1h_time}\n")


# ---------------------------------------------------------------------------
# PAPER TRADE SIMULATION
# ---------------------------------------------------------------------------

def paper_fake_ticket(state: dict) -> int:
    """Assign incrementing fake ticket > PAPER_TICKET_BASE."""
    used = {t["ticket"] for t in state.get("open_trades", [])
            if t.get("ticket", 0) >= PAPER_TICKET_BASE}
    return max(used, default=PAPER_TICKET_BASE - 1) + 1


def paper_check_exit(trade: dict) -> tuple:
    """
    Check if fake paper trade has been closed by SL or TP via live price.
    Returns (exit_reason, exit_price) or (None, None).
    """
    symbol    = trade["symbol"]
    direction = trade["direction"]
    tick      = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None, None

    sl = trade["sl_price"]
    tp = trade["tp_price"]
    bid, ask = float(tick.bid), float(tick.ask)

    if direction == "long":
        if bid <= sl:
            return "SL", sl
        if ask >= tp:
            return "TP", tp
    else:
        if ask >= sl:
            return "SL", sl
        if bid <= tp:
            return "TP", tp
    return None, None


# ---------------------------------------------------------------------------
# STARTUP RECOVERY
# ---------------------------------------------------------------------------

def startup_recovery(state: dict, path: str):
    """
    Reconcile state.json vs broker positions on startup.
    Orphan positions (in broker, not in state): close them.
    Offline closes (in state as open, closed in broker): log trade, update.
    """
    log_event("STARTUP_RECOVERY", "reconciling state vs broker")

    broker_tickets = get_open_tickets()
    state_tickets  = {t["ticket"] for t in state.get("open_trades", [])}
    paper_tickets  = {t["ticket"] for t in state.get("open_trades", [])
                      if t.get("ticket", 0) >= PAPER_TICKET_BASE}

    # Close orphans
    orphans = broker_tickets - state_tickets
    for ticket in orphans:
        positions = rpyc.utils.classic.obtain(mt5.positions_get(ticket=ticket) or [])
        if not positions:
            continue
        p = positions[0]
        log_event("ORPHAN_CLOSE", f"ticket={ticket} {str(p.symbol)} — closing")
        close_order(int(ticket), str(p.symbol),
                    "long" if int(p.type) == int(mt5.ORDER_TYPE_BUY) else "short",
                    float(p.volume))

    # Detect offline closes
    live_in_state = [t for t in state.get("open_trades", [])
                     if t.get("ticket", 0) not in paper_tickets]
    still_open = []
    for trade in live_in_state:
        if trade["ticket"] in broker_tickets:
            still_open.append(trade)
            continue
        reason, exit_price, exit_time, entry_hist = get_close_reason(
            trade["ticket"], trade["symbol"]
        )
        if entry_hist and trade.get("entry_price", 0) == 0.0:
            trade["entry_price"] = entry_hist
        pnl_r   = compute_pnl_r(trade["direction"], trade["entry_price"],
                                 exit_price, trade["sl_price"],
                                 entry_override=entry_hist)
        pnl_usd = pnl_r * abs(trade["entry_price"] - trade["sl_price"]) / \
                  get_pip_size(trade["symbol"]) * \
                  (trade.get("lot_size", 0.01) * 10)
        log_event("OFFLINE_CLOSE",
                  f"ticket={trade['ticket']} {trade['symbol']} "
                  f"reason={reason} pnl_r={pnl_r:.3f}")
        log_trade(
            trade, reason, exit_price, exit_time or "",
            pnl_r, pnl_usd,
            trade.get("hypothesis", "?"), trade.get("mode", "live"),
            trade.get("st_context_dir", 0),
            trade.get("allow_stack", True),
            trade.get("is_pullback", False),
        )
        sym = trade["symbol"]
        ihw_update(state, sym, reason, pnl_r)
        if reason == "SL":
            cd_activate(state, sym)

    state["open_trades"] = [t for t in state.get("open_trades", [])
                             if t.get("ticket", 0) in paper_tickets] + still_open
    save_state(state, path)


# ---------------------------------------------------------------------------
# STALE BAR DETECTION  (soft idle — NOT hard exit)
# ---------------------------------------------------------------------------

def check_stale(symbol: str, state: dict) -> str:
    """Returns: 'ok' | 'warn' | 'stale_idle'"""
    last_m15 = state.get("last_bar_times", {}).get(symbol, {}).get("m15")
    if last_m15 is None:
        return "ok"
    try:
        last_dt = datetime.fromisoformat(str(last_m15))
    except Exception:
        return "ok"
    age_min = (datetime.now(timezone.utc).replace(tzinfo=None) - last_dt).total_seconds() / 60
    if age_min >= STALE_IDLE_MINUTES:
        return "stale_idle"
    if age_min >= STALE_WARN_MINUTES:
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# CHEAP 15m BAR CHECK (rpyc: obtain() to materialise proxy)
# ---------------------------------------------------------------------------

def pull_15m_latest(symbol: str) -> str | None:
    """Single-bar pull to detect new 15m bar without full data pull."""
    tf    = mt5.TIMEFRAME_M15
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, 1)
    local = rpyc.utils.classic.obtain(rates)
    if local is None or len(local) == 0:
        return None
    ts = datetime.fromtimestamp(int(local[0]["time"]), tz=timezone.utc).replace(tzinfo=None)
    return str(ts)


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def main():
    global conn, mt5

    print("\nSystem C — run_orders_rpyc.py")
    print("=" * 60)

    validate_config()
    print_config_summary()

    active_symbols = get_active_symbols()
    if not active_symbols:
        print("❌ No active instruments. Check config.yaml instruments section.")
        sys.exit(1)

    # Connect to rpyc bridge
    print(f"\n  Connecting to rpyc bridge "
          f"({secrets.get('rpyc_host','localhost')}:{secrets.get('rpyc_port',18812)})...")
    try:
        conn, mt5 = connect()
    except Exception as e:
        print(f"❌ Cannot connect: {e}")
        print(f"   Is start_bridge.sh running?")
        sys.exit(1)
    print("✅ Connected to MT5 via rpyc")

    # Algo Trading check
    try:
        term_info = mt5.terminal_info()
        if term_info and not bool(term_info.trade_allowed):
            print("⚠️  MT5: Algo Trading is DISABLED — enable in terminal toolbar")
            log_event("ALGO_TRADING_DISABLED", "Enable in MT5 toolbar before running")
    except Exception:
        pass

    # Filling mode info
    for sym in active_symbols:
        try:
            fm   = get_filling_mode(sym)
            info = mt5.symbol_info(sym)
            bitmask = int(info.filling_mode) if info else "?"
            print(f"  {sym} filling mode: {fm}  (broker bitmask: {bitmask})")
        except Exception:
            pass

    # Load state
    state_path = get_state_file()
    state      = load_state(state_path)

    # Migrate + seed state dicts for all active symbols (handles new instruments)
    for sym in active_symbols:
        ensure_symbol_state(state, sym)
        ensure_highwind_ready(state, sym)

    # Startup recovery
    startup_recovery(state, state_path)

    # Init CB and Rule 2
    cb_init(state)
    r2_init(state)
    print_guards_status(state)
    save_state(state, state_path)

    # Initial data pull
    print("\n  Pulling initial data (startup)...")
    bundles = {}
    for sym in active_symbols:
        try:
            bundles[sym] = build_data_bundle(mt5, sym, is_startup=True, rpyc_mode=True)
            df15 = bundles[sym].df_15m
            df1h = bundles[sym].df_1h
            if len(df15):
                state["last_bar_times"][sym]["m15"] = str(df15["time_utc"].iloc[-1])
            if len(df1h):
                state["last_bar_times"][sym]["h1"]  = str(df1h["time_utc"].iloc[-1])
            print(f"  {sym}: {len(df15)} × 15m  |  {len(df1h)} × 1H")
        except Exception as e:
            log_event("DATA_PULL_ERROR", f"{sym} startup: {e}")
    save_state(state, state_path)

    log_event("BOT_STARTED", f"symbols={active_symbols} mode={'paper' if is_paper_mode() else 'live'} via=rpyc")
    print("\n  Main loop started. Create STOP file to exit cleanly.\n")

    idle_mode          = False
    poll_secs          = config.get("poll_interval_seconds", 5)
    _hb_mins           = config.get("heartbeat_minutes", 15)
    _last_hb           = datetime.now(timezone.utc).replace(tzinfo=None)
    reconnect_attempts = 0

    while True:
        try:
            # STOP file kill switch
            if os.path.exists(KILL_FILE):
                log_event("SHUTDOWN_KILL_FILE", "STOP file detected — exiting")
                os.remove(KILL_FILE)
                break

            if idle_mode:
                time.sleep(IDLE_POLL_SECONDS)
                for sym in active_symbols:
                    stale = check_stale(sym, state)
                    if stale != "stale_idle":
                        idle_mode = False
                        log_event("IDLE_RESUMED", sym)
                        bundles[sym] = build_data_bundle(mt5, sym, is_startup=False, rpyc_mode=True)
                continue

            time.sleep(poll_secs)

            # ── Heartbeat ────────────────────────────────────────────────────
            if _hb_mins > 0:
                _now = datetime.now(timezone.utc).replace(tzinfo=None)
                if (_now - _last_hb).total_seconds() >= _hb_mins * 60:
                    _last_hb = _now
                    _eq      = mt5.account_info().equity
                    _cb      = state.get("cb_anchor", {})
                    _trig    = (round(_cb["anchor"] * config["cb_anchor"].get("next_trigger_pct", 0.92), 2)
                                if _cb.get("anchor") else None)
                    _n_open  = len(state.get("open_trades", []))
                    _ihw     = state.get("instrument_highwind", {})
                    _hw_str  = "  ".join(f"{s}:{_ihw.get(s, {}).get('level', '?')}"
                                         for s in active_symbols)
                    _cb_str  = f"CB trig=${_trig:,.0f}" if _trig else "CB unarmed"
                    print(f"  [{_now.strftime('%H:%M')} UTC] ALIVE  "
                          f"eq=${_eq:,.2f}  {_cb_str}  open={_n_open}  {_hw_str}")

            for sym in active_symbols:
                mode = get_instrument_mode_rt(sym, state)
                if mode == "disabled":
                    continue

                # Check stale
                stale = check_stale(sym, state)
                if stale == "warn":
                    log_event("STALE_BAR_WARN", f"{sym} no new bar for {STALE_WARN_MINUTES}+ min")
                    if not has_internet():
                        log_event("INTERNET_DOWN", sym)
                elif stale == "stale_idle":
                    log_event("STALE_BAR_IDLE", f"{sym} → entering idle mode")
                    idle_mode = True
                    break

                # Cheap new-bar check
                try:
                    latest_m15 = pull_15m_latest(sym)
                    if latest_m15 is None:
                        continue
                except Exception:
                    continue

                last_m15 = state["last_bar_times"][sym].get("m15")
                if latest_m15 == last_m15:
                    continue  # no new bar

                # New 15m bar — full data pull
                try:
                    bundles[sym] = build_data_bundle(mt5, sym, is_startup=False, rpyc_mode=True)
                except Exception as e:
                    log_event("DATA_PULL_ERROR", f"{sym}: {e}")
                    continue

                df15 = bundles[sym].df_15m
                df1h = bundles[sym].df_1h
                if df15 is None or len(df15) == 0:
                    log_event("EMPTY_DATA", sym)
                    continue

                # Update bar times
                state["last_bar_times"][sym]["m15"] = str(df15["time_utc"].iloc[-1])
                state["last_bar_times"][sym]["h1"]  = str(df1h["time_utc"].iloc[-1])

                # Update session state
                last_bar    = df15.iloc[-1]
                new_session = str(last_bar.get("session", "none"))
                old_session = state["session_state"][sym].get("session")
                if new_session != old_session:
                    state["session_state"][sym]["session"]     = new_session
                    state["session_state"][sym]["session_bar"] = 0
                    cb_reset_session(state)
                    log_event("SESSION_CHANGE", f"{sym} {old_session} → {new_session}")
                    if new_session == "London" and sym == active_symbols[0]:
                        notifier.london_open(state)
                else:
                    sb = state["session_state"][sym].get("session_bar", -1)
                    state["session_state"][sym]["session_bar"] = sb + 1

                # CB / Rule 2 checks
                if r2_check(state):
                    log_event("RULE2_TRIGGERED", "equity floor reached — no trading today")
                    save_state(state, state_path)
                    continue

                cb_update_peak(state)
                if cb_check(state):
                    log_event("CB_TRIGGERED", f"{sym} session skipped")
                    notifier.cb_triggered(
                        equity=float(mt5.account_info().equity),
                        peak=state["cb_anchor"].get("peak", 0),
                        anchor=state["cb_anchor"].get("anchor", 0),
                    )
                    save_state(state, state_path)
                    continue

                # Monitor open trades for this symbol
                sym_trades = [t for t in state.get("open_trades", [])
                              if t["symbol"] == sym]
                for trade in list(sym_trades):
                    t_mode = trade.get("mode", "live")
                    ex_reason = None
                    ex_price  = 0.0

                    if t_mode == "paper":
                        ex_reason, ex_price = paper_check_exit(trade)
                    else:
                        if trade["ticket"] not in get_open_tickets():
                            ex_reason, ex_price, _, entry_hist_rt = get_close_reason(
                                trade["ticket"], sym
                            )
                            if entry_hist_rt and trade.get("entry_price", 0) == 0.0:
                                trade["entry_price"] = entry_hist_rt
                        else:
                            trade["bars_held"] = trade.get("bars_held", 0) + 1
                            if trade["bars_held"] >= config.get("max_hold_bars", 32):
                                if close_order(trade["ticket"], sym,
                                               trade["direction"], trade["lot_size"]):
                                    ex_reason = "TIMEOUT"
                                    tick = mt5.symbol_info_tick(sym)
                                    if tick:
                                        ex_price = float(tick.bid) if trade["direction"] == "long" else float(tick.ask)
                            else:
                                if not verify_sltp(trade):
                                    log_event("SLTP_MISSING", f"ticket={trade['ticket']}")
                                    if fix_sltp(trade):
                                        log_event("SLTP_FIXED", f"ticket={trade['ticket']}")
                                continue

                    if ex_reason is None:
                        continue

                    # Trade closed
                    pnl_r   = compute_pnl_r(trade["direction"], trade["entry_price"],
                                             ex_price, trade["sl_price"])
                    equity  = float(mt5.account_info().equity)
                    pip_sz   = get_pip_size(sym)
                    sl_pips  = abs(trade["entry_price"] - trade["sl_price"]) / pip_sz
                    _info_sym = mt5.symbol_info(sym)
                    if _info_sym and _info_sym.trade_tick_size > 0:
                        _pip_val = (pip_sz / _info_sym.trade_tick_size) * _info_sym.trade_tick_value
                    else:
                        _pip_val = pip_sz * 100000  # USD-quoted fallback
                    pnl_usd  = pnl_r * sl_pips * _pip_val * trade.get("lot_size", 0.01)

                    log_trade(
                        trade, ex_reason, ex_price,
                        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                        pnl_r, pnl_usd,
                        trade.get("hypothesis", "?"), t_mode,
                        trade.get("st_context_dir", 0),
                        trade.get("allow_stack", True),
                        trade.get("is_pullback", False),
                    )
                    ihw_update(state, sym, ex_reason, pnl_r)
                    if ex_reason == "SL":
                        cd_activate(state, sym)
                        log_event("COOLDOWN_ACTIVATED",
                                  f"{sym} after SL — {config.get('cooldown_bars',6)} bars")

                    sess_key = f"{trade.get('session','').lower()}_session_summary"
                    if sess_key in state:
                        ss = state[sess_key].setdefault(sym, {"trades": 0, "wins": 0, "pnl_r": 0.0})
                        ss["trades"] += 1
                        if ex_reason == "TP":
                            ss["wins"] += 1
                        ss["pnl_r"] = round(ss["pnl_r"] + pnl_r, 4)

                    notifier.trade_closed(
                        symbol=sym,
                        direction=trade["direction"],
                        hypothesis=trade.get("hypothesis", "?"),
                        session=trade.get("session", ""),
                        exit_reason=ex_reason,
                        pnl_r=pnl_r,
                        hw_window=state.get("instrument_highwind", {}).get(sym, {}).get("window", []),
                    )

                    state["open_trades"] = [t for t in state["open_trades"]
                                            if t.get("ticket") != trade["ticket"]]

                # Signal detection — penultimate bar (last confirmed close)
                bar_idx = len(df15) - 2 if len(df15) > 1 else len(df15) - 1
                if bar_idx < 0:
                    continue

                # ── Pivot tracking — detect ST flip on confirmed bar ──────────────
                # Must run before detect_signal so A2 OF gate and B trigger have pivots.
                if bar_idx > 1:
                    prev_dir   = int(df15["st_direction"].iloc[bar_idx - 1])
                    cur_dir    = int(df15["st_direction"].iloc[bar_idx])
                    if prev_dir != cur_dir:
                        flip_bar   = df15.iloc[bar_idx]
                        pivot_type = "high" if prev_dir == 1 else "low"
                        pivot_px   = float(flip_bar["high" if prev_dir == 1 else "low"])
                        pa_add_pivot(state, sym, pivot_type, pivot_px,
                                     str(flip_bar["time_utc"]))
                        pa_on_new_flip(state, sym)
                        log_event("PIVOT_ADDED",
                                  f"{sym} {pivot_type}={pivot_px:.5f} at {flip_bar['time_utc']}")

                # ── ChoCh detection — runs BEFORE detect_signal (bypasses pullback filter) ──
                # ChoCh bar has st_15m ≠ st_1h by definition; detect_signal pullback gate
                # would block it if detection were inside detect_signal.
                stable_pre, st_1h_dir_pre = is_1h_stable(df1h, config)
                if stable_pre and bar_idx > 0:
                    choch_bar = df15.iloc[bar_idx]
                    detect_choch(
                        float(choch_bar["close"]), state, sym,
                        st_1h_dir_pre,
                        int(choch_bar["st_direction"]),
                    )

                signal = detect_signal(state, sym, bundles[sym], bar_idx)
                if signal is None:
                    save_state(state, state_path)
                    continue

                # Compute entry price, SL, TP
                tick = mt5.symbol_info_tick(sym)
                if tick is None:
                    continue
                entry_price = float(tick.ask) if signal["direction"] == "long" else float(tick.bid)
                info_digs   = int(mt5.symbol_info(sym).digits)

                sl_price = get_sl(signal["hypothesis"], signal["direction"],
                                  entry_price, state, sym, df15, bar_idx)
                if sl_price is None:
                    log_event("SL_CALC_FAIL", f"{sym} {signal['hypothesis']}")
                    save_state(state, state_path)
                    continue
                tp_price = get_tp(entry_price, sl_price, signal["hypothesis"], sym)

                hw_level = state.get("instrument_highwind", {}).get(sym, {}).get("level", "NORMAL")
                eff_mode = get_instrument_mode_rt(sym, state)
                hw_level_for_lot = hw_level if eff_mode == "live" else "NORMAL"
                lot = compute_lot_size(sym, sl_price, entry_price, hw_level=hw_level_for_lot)
                if lot is None:
                    log_event("LOT_SIZE_FAIL", f"{sym} sl too small")
                    save_state(state, state_path)
                    continue

                if eff_mode == "paper":
                    ticket = paper_fake_ticket(state)
                    trade_entry = {
                        "ticket"        : ticket,
                        "symbol"        : sym,
                        "direction"     : signal["direction"],
                        "hypothesis"    : signal["hypothesis"],
                        "mode"          : "paper",
                        "session"       : signal["session"],
                        "regime"        : signal["regime"],
                        "entry_time"    : datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                        "entry_price"   : round(entry_price, info_digs),
                        "sl_price"      : round(sl_price, info_digs),
                        "tp_price"      : round(tp_price, info_digs),
                        "sl_pips"       : abs(entry_price - sl_price) / get_pip_size(sym),
                        "rr"            : get_hyp_config(sym, signal["hypothesis"].lower()).get("rr", 1.5),
                        "bars_held"     : 0,
                        "lot_size"      : lot,
                        "allow_stack"   : signal["allow_stack"],
                        "is_pullback"   : signal["is_pullback"],
                        "st_context_dir": signal["st_1h_dir"],
                    }
                    state["open_trades"].append(trade_entry)
                    log_event("ORDER_PAPER",
                              f"{sym} {signal['direction']} {signal['hypothesis']} "
                              f"ticket={ticket} entry={entry_price:.{info_digs}f} "
                              f"sl={sl_price:.{info_digs}f} tp={tp_price:.{info_digs}f} "
                              f"regime={signal['regime']}")
                    notifier.trade_opened(
                        symbol=sym, direction=signal["direction"],
                        hypothesis=signal["hypothesis"], session=signal["session"],
                        regime=signal["regime"], entry=round(entry_price, info_digs),
                        sl=round(sl_price, info_digs), tp=round(tp_price, info_digs),
                        lot=lot, mode="paper",
                    )

                else:  # live
                    result = place_order(sym, signal["direction"], lot,
                                         sl_price, tp_price,
                                         signal["hypothesis"], signal["session"],
                                         regime=signal["regime"])
                    if result is None:
                        save_state(state, state_path)
                        continue
                    trade_entry = {
                        "ticket"        : result["ticket"],
                        "symbol"        : sym,
                        "direction"     : signal["direction"],
                        "hypothesis"    : signal["hypothesis"],
                        "mode"          : "live",
                        "session"       : signal["session"],
                        "regime"        : signal["regime"],
                        "entry_time"    : datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                        "entry_price"   : result["entry_price"],
                        "sl_price"      : result["sl_price"],
                        "tp_price"      : result["tp_price"],
                        "sl_pips"       : abs(result["entry_price"] - result["sl_price"]) / get_pip_size(sym),
                        "rr"            : get_hyp_config(sym, signal["hypothesis"].lower()).get("rr", 1.5),
                        "bars_held"     : 0,
                        "lot_size"      : result["lot_size"],
                        "allow_stack"   : signal["allow_stack"],
                        "is_pullback"   : signal["is_pullback"],
                        "st_context_dir": signal["st_1h_dir"],
                    }
                    state["open_trades"].append(trade_entry)
                    log_event("ORDER_PLACED",
                              f"{sym} {signal['direction']} {signal['hypothesis']} "
                              f"ticket={result['ticket']} "
                              f"entry={result['entry_price']:.{info_digs}f} "
                              f"sl={result['sl_price']:.{info_digs}f} "
                              f"regime={signal['regime']}")
                    notifier.trade_opened(
                        symbol=sym, direction=signal["direction"],
                        hypothesis=signal["hypothesis"], session=signal["session"],
                        regime=signal["regime"], entry=result["entry_price"],
                        sl=result["sl_price"], tp=result["tp_price"],
                        lot=lot, mode="live",
                    )

                save_state(state, state_path)

            reconnect_attempts = 0  # reset on clean cycle

        except KeyboardInterrupt:
            log_event("SHUTDOWN_KEYBOARD", "Ctrl+C — exiting cleanly")
            break

        except (ConnectionRefusedError, EOFError) as e:
            # rpyc bridge dropped — attempt reconnect
            log_event("RPYC_DISCONNECTED", f"{type(e).__name__}: {e}")
            print(f"\n  ❌ rpyc bridge lost: {e}")
            reconnect_attempts += 1
            if reconnect_attempts >= 5:
                log_event("RECONNECT_FAIL", "5 consecutive failures — exiting")
                print("  5 consecutive failures — exiting.")
                break
            if reconnect(max_attempts=1):
                reconnect_attempts = 0
                log_event("RECONNECT_OK", f"bridge restored after {reconnect_attempts} attempt(s)")
                startup_recovery(state, state_path)
                cb_init(state)
                r2_init(state)
                save_state(state, state_path)

        except Exception as e:
            reconnect_attempts += 1
            log_event("LOOP_ERROR", f"attempt {reconnect_attempts}: {type(e).__name__}: {e}")
            print(f"\n  ❌ Error (attempt {reconnect_attempts}): {e}")

            if reconnect_attempts >= 5:
                log_event("RECONNECT_FAIL", "5 consecutive failures — exiting")
                print("  5 consecutive failures — exiting.")
                break

            wait = min(30 * reconnect_attempts, 120)
            print(f"  Reconnecting in {wait}s...")
            time.sleep(wait)
            try:
                try:
                    conn.close()
                except Exception:
                    pass
                conn, mt5 = connect()
                log_event("RECONNECT_OK", f"after {reconnect_attempts} attempt(s)")
                print("  ✅ Reconnected")
                startup_recovery(state, state_path)
                cb_init(state)
                r2_init(state)
                save_state(state, state_path)
            except Exception as e2:
                log_event("RECONNECT_FAIL", str(e2))
                print(f"  ❌ Reconnect failed: {e2}")

    try:
        mt5.shutdown()
        conn.close()
    except Exception:
        pass

    print("\nSystem C stopped. Open positions protected by SL/TP.")


if __name__ == "__main__":
    main()
