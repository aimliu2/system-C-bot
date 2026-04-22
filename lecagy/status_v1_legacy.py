"""
status_v1_legacy.py — System C EOD Status + Manual Controls
v1.0 · April 2026

Usage:
  python3 status.py                     # view current mode's state (auto-detect)
  python3 status.py --paper             # view paper state
  python3 status.py --live              # view live state
  python3 status.py --reset             # clear CB, Rule2, hypothesis_states, pivot_arrays
  python3 status.py --reset-highwind    # clear above + instrument_highwind windows
  python3 status.py --rescale           # rescale base_equity to current equity
  python3 status.py --clear-paper       # wipe paper state → fresh template
  python3 status.py --clear-live        # wipe live state → fresh template (CONFIRM req)
  python3 status.py --shadow EURUSD     # set instrument_modes.EURUSD = "paper"
  python3 status.py --live EURUSD       # set instrument_modes.EURUSD = "live"
  python3 status.py --disable EURUSD    # set instrument_modes.EURUSD = "disabled"

Instrument mode commands override the runtime state (state["instrument_modes"]).
They persist across bot restarts. Use --live EURUSD to restore after Highwind halt.

--rescale: tries native MT5 first (VPS), falls back to rpyc (macOS), then manual input.
"""

import json
import os
import sys
import shutil
import yaml
from datetime import datetime, timezone
from typing import Optional, Tuple

# ── ANSI colors ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
GRAY   = "\033[90m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

VALID_MODES = ("live", "paper", "disabled")

# ── Highwind seed helper ──────────────────────────────────────────────────────

def _hw_seed_entry(symbol: str, cfg: dict) -> dict:
    """Build seeded Highwind window + level from config. Mirrors logic in run_orders."""
    hw_global  = cfg.get("highwind", {})
    hw_inst    = cfg.get("instruments", {}).get(symbol, {}).get("highwind", {}) or {}
    hw_cfg     = {**hw_global, **hw_inst}
    seed_wins  = hw_cfg.get("seed_wins",   0)
    seed_loss  = hw_cfg.get("seed_losses", 0)
    window     = [1] * seed_wins + [0] * seed_loss
    halt_thr   = hw_cfg.get("halt_threshold", 0.32)
    l2_thr     = hw_cfg.get("l2_threshold",   0.36)
    l1_thr     = hw_cfg.get("l1_threshold",   0.40)
    wr         = sum(window) / len(window) if window else 1.0
    if wr >= l1_thr:    level = "NORMAL"
    elif wr >= l2_thr:  level = "L1"
    elif wr >= halt_thr: level = "L2"
    else:               level = "HALT"
    return {"window": window, "level": level, "halt_since": None}


# ── Fresh state template ──────────────────────────────────────────────────────

def _fresh_state(symbols: list, cfg: dict = None) -> dict:
    cfg = cfg or {}
    return {
        "_comment"            : "System C state file",
        "_version"            : "1.0",
        "open_trades"         : [],
        "instrument_modes"    : {s: "paper" for s in symbols},
        "instrument_highwind" : {s: _hw_seed_entry(s, cfg) for s in symbols},
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
        "rule2"               : {"base_equity": None, "triggered_today": False,
                                 "trigger_date": None},
        "last_bar_times"      : {s: {"m15": None, "h1": None, "entry": None, "context": None} for s in symbols},
        "session_state"       : {s: {"session": None, "session_bar": -1} for s in symbols},
        "london_session_summary": {s: {"trades": 0, "wins": 0, "pnl_r": 0.0} for s in symbols},
        "ny_session_summary"    : {s: {"trades": 0, "wins": 0, "pnl_r": 0.0} for s in symbols},
        "_reset_log"          : [],
    }


# ── Config + state file resolution ───────────────────────────────────────────

def load_config() -> dict:
    try:
        with open("config.yaml") as f:
            return yaml.safe_load(f)
    except Exception:
        return {"paper_mode": True,
                "state_file_paper": "state_paper.json",
                "state_file_live":  "state_live.json",
                "instruments"     : {"EURUSD": {"mode": "paper", "pip_size": 0.0001}}}


def get_symbols(cfg: dict) -> list:
    return list(cfg.get("instruments", {"EURUSD": {}}).keys())


def resolve_state_file(cfg: dict, force: Optional[str]) -> Tuple[str, str]:
    """Returns (path, mode_label)."""
    paper_file = cfg.get("state_file_paper", "state_paper.json")
    live_file  = cfg.get("state_file_live",  "state_live.json")
    if force == "paper":
        return paper_file, "PAPER"
    if force == "live":
        return live_file, "LIVE"
    if cfg.get("paper_mode", True):
        return paper_file, "PAPER"
    return live_file, "LIVE"


# ── State I/O ─────────────────────────────────────────────────────────────────

def load_state(path: str, symbols: list) -> dict:
    if not os.path.exists(path):
        bak = path + ".bak"
        if os.path.exists(bak):
            print(f"  ⚠️  {path} missing — loading backup {bak}")
            path = bak
        else:
            print(f"  ℹ️  No state file at {path} — showing empty state")
            return _fresh_state(symbols)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠️  Could not read state: {e}")
        return _fresh_state(symbols)


def save_state(state: dict, path: str):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, path)
    shutil.copy(path, path + ".bak")
    print(f"  ✅ {path} saved  ({path}.bak backed up)")


# ── Helpers ───────────────────────────────────────────────────────────────────

def color_pct(pct: float, good_above: float = 40.0) -> str:
    if pct >= good_above:
        return f"{GREEN}{pct:.1f}%{RESET}"
    elif pct >= good_above - 10:
        return f"{YELLOW}{pct:.1f}%{RESET}"
    else:
        return f"{RED}{pct:.1f}%{RESET}"


def progress_bar(wr: Optional[float], width: int = 20) -> str:
    if wr is None:
        return f"{GRAY}{'─' * width} (empty){RESET}"
    filled = int(wr / 100 * width)
    return "█" * filled + "░" * (width - filled)


def confirm(prompt: str) -> bool:
    return input(f"{YELLOW}{prompt}{RESET} [yes/no]: ").strip().lower() == "yes"


def _mode_color(mode: str) -> str:
    return {
        "live"    : f"{RED}LIVE{RESET}",
        "paper"   : f"{YELLOW}PAPER{RESET}",
        "disabled": f"{GRAY}DISABLED{RESET}",
    }.get(mode, mode)


# ── Display ───────────────────────────────────────────────────────────────────

def display(state: dict, state_file: str, mode_label: str, cfg: dict):
    now     = datetime.now(timezone.utc).replace(tzinfo=None)
    symbols = get_symbols(cfg)

    mode_color = YELLOW if mode_label == "PAPER" else RED
    print(f"\n{'═'*62}")
    print(f"  {BOLD}System C — Status{RESET}  "
          f"[{mode_color}{mode_label}{RESET}]  {state_file}")
    print(f"  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═'*62}")

    # ── Open Trades ──────────────────────────────────────────────────────────
    trades = state.get("open_trades", [])
    print(f"\n{BOLD}Open Trades{RESET}  ({len(trades)})")
    if not trades:
        print(f"  {GRAY}None{RESET}")
    else:
        for t in trades:
            ticket   = t.get("ticket", 0)
            hyp      = t.get("hypothesis", "?")
            t_mode   = t.get("mode", "live")
            mod_tag  = f"[{YELLOW}paper{RESET}]" if t_mode == "paper" else ""
            digs     = 5
            print(f"  {t.get('symbol')} {t.get('direction','').upper()} "
                  f"[{hyp}] [{t.get('session','')}] {mod_tag}  "
                  f"entry={t.get('entry_price', 0):.{digs}f}  "
                  f"SL={t.get('sl_price', 0):.{digs}f}  "
                  f"TP={t.get('tp_price', 0):.{digs}f}")
            print(f"  {'':4}bars={t.get('bars_held', 0)}/32  "
                  f"ticket={ticket}  regime={t.get('regime','?')}")

    # ── Instrument Modes + Highwind ──────────────────────────────────────────
    print(f"\n{BOLD}Instrument Modes + Highwind{RESET}  (rolling WR per instrument)")
    print(f"  {'Symbol':<8} {'Mode':<10} {'WR':>8}  {'Bar':^20}  N  Status")
    print(f"  {'─'*62}")

    inst_modes = state.get("instrument_modes", {})
    inst_hw    = state.get("instrument_highwind", {})

    # size_mult lookup for level display
    size_mults = cfg.get("highwind", {}).get("size_mult", {
        "NORMAL": 1.00, "L1": 0.75, "L2": 0.50, "HALT": 0.00
    })

    for sym in symbols:
        raw_mode = inst_modes.get(sym, cfg.get("instruments", {}).get(sym, {}).get("mode", "?"))
        # global paper_mode overrides
        eff_mode = "paper" if cfg.get("paper_mode", False) else raw_mode

        hw       = inst_hw.get(sym, {})
        window   = hw.get("window", [])
        level    = hw.get("level", "NORMAL")
        wr       = (sum(window) / len(window) * 100) if window else None
        n        = len(window)

        # Per-instrument l1_threshold for color threshold
        hw_global = cfg.get("highwind", {})
        hw_inst   = cfg.get("instruments", {}).get(sym, {}).get("highwind", {}) or {}
        hw_cfg    = {**hw_global, **hw_inst}
        l1_thr_pct = hw_cfg.get("l1_threshold", 0.40) * 100

        wr_str   = color_pct(wr, l1_thr_pct) if wr is not None else f"{GRAY}N/A{RESET}"

        # Level display
        mult = size_mults.get(level, 1.00)
        if level == "NORMAL":
            level_str = f"{GREEN}NORMAL{RESET}"
        elif level == "L1":
            level_str = f"{YELLOW}L1 ({mult:.0%}){RESET}"
        elif level == "L2":
            level_str = f"{YELLOW}L2 ({mult:.0%}){RESET}"
        else:  # HALT
            level_str = f"{RED}{BOLD}HALT (shadow){RESET}"

        if eff_mode == "disabled":
            status = f"{GRAY}DISABLED{RESET}"
        elif eff_mode == "paper" and level != "HALT":
            status = f"{YELLOW}Shadow{RESET}  {level_str}"
        else:
            status = level_str

        print(f"  {sym:<8} {_mode_color(eff_mode):<10}  {wr_str:>8}  "
              f"{progress_bar(wr)}  {n}  {status}")

        if level == "HALT":
            halt_since = hw.get("halt_since", "?")
            print(f"  {'':8}  Halted since: {halt_since}")
            print(f"  {'':8}  Restore:      python3 status.py --live {sym}")

    # ── Hypothesis States ─────────────────────────────────────────────────────
    print(f"\n{BOLD}Hypothesis States{RESET}  (per instrument)")
    hs_all = state.get("hypothesis_states", {})
    pa_all = state.get("pivot_arrays", {})

    for sym in symbols:
        hs = hs_all.get(sym, {})
        pa = pa_all.get(sym, [])

        print(f"\n  {BOLD}{sym}{RESET}")

        # ChoCh
        choch_ok  = hs.get("choch_confirmed", False)
        choch_dir = hs.get("choch_direction")
        choch_lvl = hs.get("choch_level")
        if choch_ok:
            dir_str = "bullish (long)" if choch_dir == 1 else "bearish (short)"
            print(f"  {'':4}ChoCh      : {GREEN}CONFIRMED{RESET} — {dir_str} "
                  f"level={choch_lvl}")
        else:
            print(f"  {'':4}ChoCh      : {GRAY}not confirmed{RESET}")

        # Cooldown
        in_cd   = hs.get("in_cooldown", False)
        bars_sl = hs.get("bars_since_sl", 0)
        cooldown_bars = cfg.get("cooldown_bars", 6)
        if in_cd:
            print(f"  {'':4}Cooldown   : {RED}active — {bars_sl}/{cooldown_bars} bars{RESET}")
        else:
            print(f"  {'':4}Cooldown   : {GREEN}clear{RESET}")

        # Other flags
        ne_flag = hs.get("new_extreme_flag", False)
        sb_used = hs.get("sb_used", False)
        ne_str  = f"{GREEN}True{RESET}" if ne_flag else f"{GRAY}False{RESET}"
        sb_str  = f"{YELLOW}True{RESET}" if sb_used else f"{GRAY}False{RESET}"
        print(f"  {'':4}New extreme: {ne_str}   SB used: {sb_str}")

        # Pivot array
        n_pivots = len(pa)
        if n_pivots == 0:
            print(f"  {'':4}Pivot array: {GRAY}empty{RESET}")
        else:
            print(f"  {'':4}Pivot array: {n_pivots} pivots (most recent first)")
            for pv in reversed(pa[-6:]):   # show up to last 6
                ptype = pv.get("type", "?")
                pprice = pv.get("price", 0)
                ptime  = pv.get("time", "?")
                tag = f"{GREEN}H{RESET}" if ptype == "H" else f"{RED}L{RESET}"
                print(f"  {'':8}  {tag}  {pprice:.5f}  ({ptime})")

    # ── CB Anchor ─────────────────────────────────────────────────────────────
    print(f"\n{BOLD}CB Anchor{RESET}  (8% DD → skip session)")
    cb        = state.get("cb_anchor", {})
    peak      = cb.get("peak")
    anchor    = cb.get("anchor")
    triggered = cb.get("triggered_session", False)
    if peak is None:
        print(f"  {GRAY}Not initialized — no trades yet{RESET}")
    else:
        print(f"  Peak        : {peak:.2f}")
        print(f"  Anchor      : {anchor:.2f}  (peak × 0.97)")
        trig_pct  = cfg.get("cb_anchor", {}).get("trigger_pct", 0.08)
        next_trig = cfg.get("cb_anchor", {}).get("next_trigger_pct", 0.92)
        print(f"  Trigger at  : {peak * (1 - trig_pct):.2f}  (peak × {1 - trig_pct:.2f})")
        if anchor:
            print(f"  Next trigger: {anchor * next_trig:.2f}  (anchor × {next_trig})")
        status = (f"{RED}TRIGGERED — session skipped{RESET}" if triggered
                  else f"{GREEN}Armed{RESET}")
        print(f"  Status      : {status}")
        if triggered:
            print(f"  Triggered at: {cb.get('last_trigger_time', '?')}")

    # ── Rule 2 ────────────────────────────────────────────────────────────────
    print(f"\n{BOLD}Rule 2 — Hard Floor{RESET}  (15% DD → stop day)")
    r2          = state.get("rule2", {})
    base_equity = r2.get("base_equity")
    trig_r2     = r2.get("triggered_today", False)
    trig_date   = r2.get("trigger_date")
    floor_pct   = cfg.get("rule2", {}).get("floor_pct", 0.85)
    if base_equity is None:
        print(f"  {GRAY}Not initialized{RESET}")
    else:
        print(f"  Base equity : {base_equity:.2f}")
        print(f"  Floor at    : {base_equity * floor_pct:.2f}  (base × {floor_pct})")
        if trig_r2:
            print(f"  Status      : {RED}TRIGGERED {trig_date} — no trading today{RESET}")
        else:
            print(f"  Status      : {GREEN}Active{RESET}")

    # ── Last Processed ────────────────────────────────────────────────────────
    print(f"\n{BOLD}Last Processed{RESET}")
    last_times = state.get("last_bar_times", {})
    sess_state = state.get("session_state", {})
    for sym in symbols:
        lt  = last_times.get(sym, {})
        ss  = sess_state.get(sym, {})
        inst_tf = cfg.get("instruments", {}).get(sym, {}).get("timeframes", {}) or {}
        entry_tf = str(inst_tf.get("entry", cfg.get("entry_timeframe", "M15"))).upper()
        context_tf = str(inst_tf.get("context", cfg.get("context_timeframe", "H1"))).upper()
        entry_time = lt.get("entry", lt.get("m15", "N/A"))
        context_time = lt.get("context", lt.get("h1", "N/A"))
        ses = ss.get("session", "N/A")
        bar = ss.get("session_bar", -1)
        print(f"  {sym:<8}  {entry_tf}: {entry_time}  |  {context_tf}: {context_time}")
        print(f"  {'':8}  Session: {ses}  bar {bar}")

    print(f"\n{'═'*62}\n")


# ── Actions ───────────────────────────────────────────────────────────────────

def do_reset(state: dict, symbols: list) -> dict:
    print(f"\n{BOLD}-- RESET --{RESET}")
    print("Clears: CB Anchor, Rule 2 triggered flag,")
    print("        hypothesis_states (ChoCh, cooldown, flags),")
    print("        pivot_arrays for all instruments")
    print("Keeps : instrument_highwind windows (real trade history)")
    if not confirm("Confirm reset?"):
        print("Aborted.")
        return state

    state["cb_anchor"] = {"peak": None, "anchor": None,
                          "triggered_session": False, "last_trigger_time": None}
    state["rule2"]["triggered_today"] = False
    state["rule2"]["trigger_date"]    = None

    for sym in symbols:
        state.setdefault("hypothesis_states", {})[sym] = {
            "choch_confirmed" : False,
            "choch_direction" : None,
            "choch_level"     : None,
            "in_cooldown"     : False,
            "bars_since_sl"   : 0,
            "new_extreme_flag": False,
            "sb_used"         : False,
        }
        state.setdefault("pivot_arrays", {})[sym] = []

    state.setdefault("_reset_log", []).append({
        "action": "reset",
        "time"  : datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    })

    print(f"  {GREEN}✅ CB Anchor cleared{RESET}")
    print(f"  {GREEN}✅ Rule 2 flag cleared{RESET}")
    print(f"  {GREEN}✅ hypothesis_states cleared (ChoCh, cooldown, flags){RESET}")
    print(f"  {GREEN}✅ pivot_arrays cleared{RESET}")
    print(f"  {YELLOW}⚠️  instrument_highwind windows unchanged{RESET}")
    return state


def do_reset_highwind(state: dict, symbols: list, cfg: dict = None) -> dict:
    cfg = cfg or {}
    state = do_reset(state, symbols)
    print(f"\n{RED}{BOLD}-- RESET HIGHWIND --{RESET}")
    print("Reseeds ALL instrument_highwind windows from config seed values.")
    print(f"{RED}This erases real trade win-rate history.{RESET}")
    if not confirm("Are you SURE?"):
        print("Highwind reset aborted.")
        return state
    if input(f"{RED}Type RESET to confirm: {RESET}").strip() != "RESET":
        print("Highwind reset aborted.")
        return state

    for sym in symbols:
        seeded = _hw_seed_entry(sym, cfg)
        state.setdefault("instrument_highwind", {})[sym] = seeded
        wr_val = sum(seeded["window"]) / len(seeded["window"]) if seeded["window"] else 0.0
        print(f"  {GREEN}✅ {sym} reseeded → {seeded['level']}  "
              f"({sum(seeded['window'])}/{len(seeded['window'])} = {wr_val:.1%}){RESET}")
        # Restore instrument_modes to config default if currently paper due to HALT
        config_mode = cfg.get("instruments", {}).get(sym, {}).get("mode", "paper")
        if (state.get("instrument_modes", {}).get(sym) == "paper"
                and config_mode not in ("paper", "disabled")):
            state["instrument_modes"][sym] = config_mode
            print(f"  {YELLOW}Note: {sym} mode restored to config default ({config_mode}){RESET}")

    state.setdefault("_reset_log", []).append({
        "action": "reset-highwind",
        "time"  : datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    })
    print(f"  {GREEN}✅ All instrument_highwind windows reseeded (not cleared){RESET}")
    return state


def do_rescale(state: dict) -> dict:
    """Rescale base_equity to current equity. Tries MT5 native → rpyc → manual."""
    print(f"\n{BOLD}-- RESCALE --{RESET}")
    print("Rescales base_equity and Rule 2 floor to current equity.")
    print("Use after deposit OR withdrawal.\n")

    current_equity = None

    # Try native MetaTrader5 first (VPS)
    try:
        import MetaTrader5 as mt5
        from config_loader import secrets
        ok = mt5.initialize(
            login    = secrets["mt5_login"],
            password = secrets["mt5_password"],
            server   = secrets["mt5_server"],
            path     = secrets.get("mt5_path") or None,
        )
        if ok:
            current_equity = float(mt5.account_info().equity)
            print(f"  MT5 (native) current equity: {current_equity:.2f}")
            mt5.shutdown()
    except ImportError:
        pass
    except Exception as e:
        print(f"  {YELLOW}⚠️  MT5 native: {e}{RESET}")

    # Fall back to rpyc (macOS)
    if current_equity is None:
        try:
            import rpyc
            from config_loader import secrets
            conn = rpyc.classic.connect(
                secrets.get("rpyc_host", "localhost"),
                int(secrets.get("rpyc_port", 18812)),
            )
            mt5r = conn.modules.MetaTrader5
            ok   = mt5r.initialize(
                login    = secrets["mt5_login"],
                password = secrets["mt5_password"],
                server   = secrets["mt5_server"],
            )
            if ok:
                current_equity = float(mt5r.account_info().equity)
                print(f"  MT5 (rpyc) current equity: {current_equity:.2f}")
                mt5r.shutdown()
            conn.close()
        except Exception as e:
            print(f"  {YELLOW}⚠️  rpyc: {e}{RESET}")

    # Manual fallback
    if current_equity is None:
        try:
            current_equity = float(input("  Enter current equity manually: ").strip())
        except (ValueError, KeyboardInterrupt):
            print("  Invalid or cancelled. Aborted.")
            return state

    old_base  = state.get("rule2", {}).get("base_equity")
    floor_pct = 0.85
    new_floor = current_equity * floor_pct

    print(f"  Old base : {old_base:.2f}" if old_base else "  Old base : N/A")
    print(f"  New base : {current_equity:.2f}")
    print(f"  New floor: {new_floor:.2f}  (× {floor_pct})")

    if not confirm("Confirm rescale?"):
        print("Aborted.")
        return state

    state.setdefault("rule2", {})
    state["rule2"]["base_equity"]     = current_equity
    state["rule2"]["triggered_today"] = False
    state["rule2"]["trigger_date"]    = None
    state.setdefault("_reset_log", []).append({
        "action"  : "rescale",
        "time"    : datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "old_base": old_base,
        "new_base": current_equity,
    })
    print(f"  {GREEN}✅ base_equity → {current_equity:.2f}{RESET}")
    print(f"  {GREEN}✅ Rule 2 floor → {new_floor:.2f}{RESET}")
    print(f"  {YELLOW}Note: run --reset to also clear CB Anchor if needed{RESET}")
    return state


def do_clear(path: str, mode_label: str, symbols: list):
    """Wipe state file and replace with fresh template."""
    print(f"\n{BOLD}-- CLEAR {mode_label} STATE --{RESET}")
    print(f"  File: {path}")
    print(f"  {RED}This wipes ALL state: trades, Highwind, CB, Rule2.{RESET}")

    if mode_label == "LIVE":
        print(f"  {RED}{BOLD}⚠️  This is the LIVE state — real trade history will be lost.{RESET}")
        if input(f"{RED}Type CONFIRM to proceed: {RESET}").strip() != "CONFIRM":
            print("Aborted.")
            return
    else:
        if not confirm(f"Clear {mode_label} state?"):
            print("Aborted.")
            return

    if os.path.exists(path):
        ts  = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")
        bak = path + f".backup_{ts}"
        shutil.copy(path, bak)
        print(f"  Backup saved: {bak}")

    fresh = _fresh_state(symbols)
    if mode_label == "LIVE":
        for sym in symbols:
            fresh["instrument_modes"][sym] = "live"

    with open(path, "w") as f:
        json.dump(fresh, f, indent=2)
    shutil.copy(path, path + ".bak")
    print(f"  {GREEN}✅ {path} reset to fresh template{RESET}")


def do_set_instrument_mode(state: dict, symbol: str, new_mode: str,
                           symbols: list, cfg: dict) -> dict:
    """Set instrument_modes[symbol] = new_mode in state."""
    print(f"\n{BOLD}-- SET INSTRUMENT MODE --{RESET}")

    if symbol not in symbols:
        print(f"  {RED}Unknown symbol: {symbol}{RESET}")
        print(f"  Available: {', '.join(symbols)}")
        return state

    if new_mode not in VALID_MODES:
        print(f"  {RED}Invalid mode: {new_mode}{RESET}")
        print(f"  Valid modes: {', '.join(VALID_MODES)}")
        return state

    old_mode = state.get("instrument_modes", {}).get(symbol, "?")

    if new_mode == "live":
        print(f"  {RED}{BOLD}⚠️  This sets {symbol} to LIVE mode — real orders will be placed.{RESET}")
        # Check if Highwind halted
        hw = state.get("instrument_highwind", {}).get(symbol, {})
        if hw.get("level", "NORMAL") == "HALT":
            wr_window = hw.get("window", [])
            wr = sum(wr_window) / len(wr_window) * 100 if wr_window else 0
            print(f"  {YELLOW}Note: {symbol} is Highwind-HALT (WR={wr:.1f}%). "
                  f"Force-restoring: window reseeded, level set to L1 (0.75×).{RESET}")

    action_map = {
        "live"    : f"--live {symbol}",
        "paper"   : f"--shadow {symbol}",
        "disabled": f"--disable {symbol}",
    }
    print(f"  Command  : {action_map.get(new_mode, '')}")
    print(f"  Change   : {symbol}  {old_mode} → {new_mode}")

    if not confirm(f"Set {symbol} to {new_mode}?"):
        print("Aborted.")
        return state

    state.setdefault("instrument_modes", {})[symbol] = new_mode

    # Force-restore from HALT: reseed window, set level to L1
    if new_mode == "live":
        hw = state.setdefault("instrument_highwind", {}).setdefault(
            symbol, _hw_seed_entry(symbol, cfg)
        )
        if hw.get("level", "NORMAL") == "HALT":
            hw_cfg  = {**cfg.get("highwind", {}),
                       **(cfg.get("instruments", {}).get(symbol, {}).get("highwind", {}) or {})}
            win_n   = hw_cfg.get("window", 30)
            seeded  = _hw_seed_entry(symbol, cfg)
            hw.update(seeded)
            hw["level"]      = "L1"
            hw["halt_since"] = None
            print(f"  {YELLOW}⚠️  {symbol} force-restored to L1 — skipped shadow recovery period{RESET}")
            print(f"  {YELLOW}⚠️  Window reseeded. Earn NORMAL after {win_n} live trades above l1_threshold.{RESET}")

    state.setdefault("_reset_log", []).append({
        "action"  : f"set-mode-{new_mode}",
        "symbol"  : symbol,
        "old_mode": old_mode,
        "time"    : datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    })
    print(f"  {GREEN}✅ {symbol} mode → {new_mode}{RESET}")
    return state


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args    = sys.argv[1:]
    cfg     = load_config()
    symbols = get_symbols(cfg)

    # determine force mode from args (view-only flags)
    force = None
    if "--paper" in args and "--shadow" not in args and "--live" not in args:
        # --paper as view flag (not --live SYMBOL)
        # Distinguish: "--live" alone = view live, "--live SYMBOL" = set mode
        if len(args) == 1 or (len(args) >= 1 and args[0] == "--paper"):
            force = "paper"
    if "--live" in args and len(args) == 1:
        force = "live"

    # Handle --live SYMBOL / --shadow SYMBOL / --disable SYMBOL
    # These take a symbol argument
    for cmd, new_mode in [("--live", "live"), ("--shadow", "paper"), ("--disable", "disabled")]:
        if cmd in args:
            idx = args.index(cmd)
            if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
                symbol = args[idx + 1].upper()
                state_file, mode_label = resolve_state_file(cfg, force)
                state = load_state(state_file, symbols)
                state = do_set_instrument_mode(state, symbol, new_mode, symbols, cfg)
                save_state(state, state_file)
                display(state, state_file, mode_label, cfg)
                return
            elif cmd == "--live" and len(args) > 1:
                pass  # --live with no symbol → view live state below
            elif cmd == "--live" and len(args) == 1:
                force = "live"
                break

    state_file, mode_label = resolve_state_file(cfg, force)

    # clear actions — don't load state
    if "--clear-paper" in args:
        pf, _ = resolve_state_file(cfg, "paper")
        do_clear(pf, "PAPER", symbols)
        return

    if "--clear-live" in args:
        lf, _ = resolve_state_file(cfg, "live")
        do_clear(lf, "LIVE", symbols)
        return

    # load state for all other actions
    state = load_state(state_file, symbols)

    if "--reset-highwind" in args:
        state = do_reset_highwind(state, symbols, cfg)
        save_state(state, state_file)
        display(state, state_file, mode_label, cfg)

    elif "--reset" in args:
        state = do_reset(state, symbols)
        save_state(state, state_file)
        display(state, state_file, mode_label, cfg)

    elif "--rescale" in args:
        state = do_rescale(state)
        save_state(state, state_file)
        display(state, state_file, mode_label, cfg)

    else:
        display(state, state_file, mode_label, cfg)


if __name__ == "__main__":
    main()
