# strategy.py — fundamental signal elements for SystemC hypotheses
#
# This module contains ONLY pure signal functions:
#   - context validity checks
#   - entry triggers
#   - SL / TP calculations
#   - session filter
#
# No routing, no stacking, no policy logic.
# All routing/priority belongs in policy.py.
#
# Expected feature_row columns (precomputed by features.py):
#   close, high, low, open
#   st_dir          — entry TF SuperTrend direction (+1 / -1)
#   st_line         — entry TF SuperTrend line value
#   st_step_count   — ST line value changes since last flip (precomputed)
#   ema3            — EMA(3) current value
#   ema3_prev       — EMA(3) previous bar value
#   ema3_lag1/2/3   — EMA(3) lagged values for configurable trajectory checks
#   ema20           — EMA(20) current value
#   ema20_prev      — EMA(20) previous bar value
#   close_prev      — close of previous bar
#   rsi30           — RSI(30) current value
#   regime          — regime label string (from regime classifier)
#   session         — bool: bar is inside London+NY session window
#
# Expected context_row columns (1H, as-of merged):
#   st_dir          — 1H SuperTrend direction (+1 / -1)
#
from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.engine.engine import InstrumentEngine, PivotArray

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------

ST_PERIOD               = 12
ST_MULT                 = 3.0

# Default session boundaries (UTC hours, half-open [start, end)).
# Override via top-level 'sessions:' block in symbol YAML.
DEFAULT_SESSIONS: dict[str, tuple[int, int]] = {
    "asian":     (0,  7),   # 00–07 UTC
    "london":    (7,  12),  # 07–12 UTC
    "london_ny": (12, 17),  # 12–17 UTC
    "ny":        (17, 21),  # 17–21 UTC
}

# A1
HYP_A1_ST_MIN_STEPS     = 2
A1_SL_FIXED             = 0.0012    # 12 pip fixed SL for Phase2 A1
A1_SL_MIN               = 0.0012    # 12 pip floor
A1_SL_MAX               = 0.0020    # 20 pip hard cap
HYP_A1_RR               = 1.5
A1_ST_TOUCH_EPSILON     = 0.0003    # 3 pip epsilon for st_touch mode (matches V7)

EMA20_SPAN              = 20
RSI_PERIOD              = 30
RSI_BULL_GATE           = 55.0
RSI_BEAR_GATE           = 48.0
# A2
HYP_A2_SL_MIN           = 0.0015   # 15 pip floor
HYP_A2_SL_MAX           = 0.0020   # 20 pip cap
HYP_A2_SL_EPSILON       = 0.0003   # 3 pip buffer beyond pivot
HYP_A2_RR               = 1.5
HYP_A2_PD_LEVEL         = 0.618
OF_DEPTH_A2             = 4
OF_MIN_A2               = 2

# B
HYP_B_SL_FIXED          = 0.0020   # 20 pip fixed SL
HYP_B_RR                = 2.0

# Cooldown
COOLDOWN_BARS           = 6
PIVOT_MAXLEN            = 8


# ---------------------------------------------------------------------------
# Session filter
# ---------------------------------------------------------------------------

def _sessions_cfg(config: dict) -> dict[str, tuple[int, int]]:
    """
    Returns session hour boundaries from config['sessions'] if present,
    otherwise falls back to DEFAULT_SESSIONS.

    YAML format (top-level):
        sessions:
          asian:     [0,  7]
          london:    [7,  12]
          london_ny: [12, 17]
          ny:        [17, 21]
    """
    raw = config.get("sessions", {})
    if not raw:
        return DEFAULT_SESSIONS
    return {label: tuple(bounds) for label, bounds in raw.items()}


def session_label_for(bar_time: pd.Timestamp, sessions: dict[str, tuple[int, int]]) -> str:
    """
    Maps a bar timestamp to its session label by checking UTC hour against
    each [start, end) window.  Falls back to 'asian' (catch-all) if no
    explicit match — handles wrap-around hours like 21–00.
    """
    h = bar_time.hour
    for label, (start, end) in sessions.items():
        if _hour_in_range(h, start, end):
            return label
    return "asian"


def _hour_in_range(h: int, start: int, end: int) -> bool:
    """
    Half-open [start, end) hour check with midnight wrap-around support.
    Normal:    _hour_in_range(h, 7, 21)  → True for h in [7, 20]
    Overnight: _hour_in_range(h, 21, 5)  → True for h>=21 OR h<5
    """
    if start <= end:
        return start <= h < end
    return h >= start or h < end


def _session_allowed(config: dict, hypothesis: str, bar_time: pd.Timestamp) -> bool:
    """
    Returns True when either:
      - session_gate is false/off  → all hours pass
      - bar's UTC hour falls in an allowed window

    Two YAML formats supported per hypothesis:

    1. Raw hour ranges (inline, no label definitions needed):
           allowed_hours: [[7, 12], [17, 21]]
       Each sub-list is [start, end) — union of windows.
       Single-hour window: [12, 13]

    2. Named session labels (readable, reusable):
           allowed_sessions: [london, london_ny, ny]
       Labels are resolved against the top-level 'sessions:' block
       (or DEFAULT_SESSIONS if absent).

    allowed_hours takes priority if both are present.
    """
    cfg  = _hypothesis_cfg(config, hypothesis)
    gate = cfg.get("session_gate", config.get("session_gate", True))
    if not gate:
        return True

    h = bar_time.hour

    # Format 1 — raw hour ranges: [[7, 12], [17, 21]]
    # Wrap-around supported: [21, 5] means 21:00–05:00 crossing midnight.
    allowed_hours = cfg.get("allowed_hours")
    if allowed_hours is not None:
        return any(_hour_in_range(h, int(s), int(e)) for s, e in allowed_hours)

    # Format 2 — named labels: [london, london_ny, ny]
    sessions = _sessions_cfg(config)
    label    = session_label_for(bar_time, sessions)
    allowed  = cfg.get("allowed_sessions", list(sessions.keys()))
    return label in allowed


# ---------------------------------------------------------------------------
# Hypothesis A1 — ST/EMA Compression Bounce
# ---------------------------------------------------------------------------

def a1_context_valid(feature_row: pd.Series) -> bool:
    """
    A1 context is valid when the ST line has stepped (changed value) in the
    trend direction at least HYP_A1_ST_MIN_STEPS times since the last flip.

    Reads precomputed column `st_step_count` from feature_row.
    This column is produced by features.py by scanning each bar's ST line
    history back to the last direction change.
    """
    return int(feature_row.get('st_step_count', 0)) >= HYP_A1_ST_MIN_STEPS


def a1_ema3_toward(
    feature_row: pd.Series,
    direction: str,
    slope_mode: str = 'lax',
) -> bool:
    """
    EMA3 trajectory gate for A1.

    lax:
       long  -> EMA3 two bars ago > current EMA3
       short -> EMA3 two bars ago < current EMA3

    strict:
       long  -> EMA3 two bars ago > previous EMA3 > current EMA3
       short -> EMA3 two bars ago < previous EMA3 < current EMA3
    """
    ema3      = float(feature_row['ema3'])
    ema3_lag1 = float(feature_row['ema3_lag1'])
    ema3_lag2 = float(feature_row['ema3_lag2'])

    if np.isnan(ema3) or np.isnan(ema3_lag1) or np.isnan(ema3_lag2):
        return False

    if slope_mode == 'lax':
        return ema3_lag2 > ema3 if direction == 'long' else ema3_lag2 < ema3

    if slope_mode == 'strict':
        return (
            ema3_lag2 > ema3_lag1 > ema3
            if direction == 'long'
            else ema3_lag2 < ema3_lag1 < ema3
        )

    raise ValueError(f"Unknown A1 EMA3 slope mode: {slope_mode}")


def a1_trigger_ema20_touch(
    feature_row: pd.Series,
    rsi_gate_enabled: bool = False,
    ema3_slope_mode: str = 'lax',
    rsi_bull_gate: float = RSI_BULL_GATE,
    rsi_bear_gate: float = RSI_BEAR_GATE,
) -> bool:
    """
    A1 EMA20 touch-and-bounce trigger.

    1. Clean approach:   prev close was cleanly outside the EMA20 line
                         (close_prev > ema20_prev for long, < for short)
    2. EMA3 trajectory:  EMA3 net-moving toward EMA20
                         selected by ema3_slope_mode: lax or strict
    3. Touch:            bar low <= EMA20 (long) or bar high >= EMA20 (short)
    4. Bounce/intact:    close ends back on the trend side of EMA20
    5. Optional RSI gate: RSI(30) > 55 (long) or < 48 (short)
    """
    st_dir     = int(feature_row['st_dir'])
    close_prev = float(feature_row['close_prev'])
    ema20      = float(feature_row['ema20'])
    ema20_prev = float(feature_row['ema20_prev'])
    rsi        = float(feature_row['rsi30'])
    low        = float(feature_row['low'])
    high       = float(feature_row['high'])

    if st_dir == +1:  # long
        direction      = 'long'
        clean_approach = close_prev > ema20_prev
        ema3_toward    = a1_ema3_toward(feature_row, direction, ema3_slope_mode)
        touched        = low  <= ema20
        bounced        = float(feature_row['close']) > ema20
        rsi_ok         = rsi  >  rsi_bull_gate if rsi_gate_enabled else True
    else:             # short
        direction      = 'short'
        clean_approach = close_prev < ema20_prev
        ema3_toward    = a1_ema3_toward(feature_row, direction, ema3_slope_mode)
        touched        = high >= ema20
        bounced        = float(feature_row['close']) < ema20
        rsi_ok         = rsi  <  rsi_bear_gate if rsi_gate_enabled else True

    return clean_approach and ema3_toward and touched and bounced and rsi_ok


def a1_trigger_st_touch(
    feature_row: pd.Series,
    ema3_slope_mode: str = 'lax',
) -> bool:
    """
    A1 ST line touch-and-bounce trigger. Replicates V7 phase4 behavior.

    1. Touch:   bar low  <= ST line + 3 pip epsilon  (long)
                bar high >= ST line - 3 pip epsilon  (short)
    2. Intact:  close ends on the trend side of ST line
    3. EMA3:    EMA3 net-moving toward ST line (ema3_slope_mode)

    No clean_approach filter (V7 does not require prev close outside ST line).
    The ST line is deeper than EMA20 — fewer bars touch it, so volume is
    lower than ema20_touch. Expect shallower-pullback trades vs ema20_touch.

    Volume expectation vs ema20_touch:
      st_touch    → lower volume (ST line is harder to reach)
      ema20_touch → higher volume (EMA20 is closer to price)
    """
    st_dir  = int(feature_row['st_dir'])
    st_line = float(feature_row['st_line'])
    low     = float(feature_row['low'])
    high    = float(feature_row['high'])
    close   = float(feature_row['close'])

    if st_dir == +1:
        direction = 'long'
        touched   = low  <= st_line + A1_ST_TOUCH_EPSILON
        intact    = close > st_line
    else:
        direction = 'short'
        touched   = high >= st_line - A1_ST_TOUCH_EPSILON
        intact    = close < st_line

    return touched and intact and a1_ema3_toward(feature_row, direction, ema3_slope_mode)


def a1_trigger(
    feature_row: pd.Series,
    mode: str = 'ema20_touch',
    rsi_gate_enabled: bool = False,
    ema3_slope_mode: str = 'lax',
    rsi_bull_gate: float = RSI_BULL_GATE,
    rsi_bear_gate: float = RSI_BEAR_GATE,
) -> bool:
    """
    Dispatch A1 trigger mode.

    Modes:
      ema20_touch  — touch EMA20 + clean_approach + EMA3 trajectory + bounce (default)
      ema20_rsi_v2 — ema20_touch with RSI gate enabled
      st_touch     — touch ST line ± 3 pip epsilon + EMA3 trajectory (V7 style, no clean_approach)
    """
    if mode == 'ema20_touch':
        return a1_trigger_ema20_touch(
            feature_row,
            rsi_gate_enabled=rsi_gate_enabled,
            ema3_slope_mode=ema3_slope_mode,
            rsi_bull_gate=rsi_bull_gate,
            rsi_bear_gate=rsi_bear_gate,
        )
    if mode == 'ema20_rsi_v2':
        return a1_trigger_ema20_touch(
            feature_row,
            rsi_gate_enabled=True,
            ema3_slope_mode=ema3_slope_mode,
            rsi_bull_gate=rsi_bull_gate,
            rsi_bear_gate=rsi_bear_gate,
        )
    if mode == 'st_touch':
        return a1_trigger_st_touch(feature_row, ema3_slope_mode=ema3_slope_mode)
    raise ValueError(f"Unknown A1 trigger mode: {mode}")


def a1_sl_tp(
    direction: str,
    entry_price: float,
    ema20: float,
    st_line: float,
    mode: str = 'fixed_phase2',
    rr: float = HYP_A1_RR,
    sl_min: float | None = None,
    sl_max: float | None = None,
) -> tuple[float, float]:
    """
    Phase2 default uses a fixed 12-pip stop. The alternate EMA20-ST pocket
    mode grounds SL to the EMA20-ST gap at the signal bar and clamps it to
    [A1_SL_MIN, A1_SL_MAX].

    sl_min / sl_max override A1_SL_MIN / A1_SL_MAX when provided — required
    for JPY-scale instruments where the default constants (0.0012, 0.0020)
    are too small by 100×.

    TP      = entry ± SL_dist * rr
    """
    if mode in {'fixed_phase2', 'fixed_12pip'}:
        sl_dist = A1_SL_FIXED
    elif mode == 'fixed_15pip':
        sl_dist = 0.0015
    elif mode == 'ema20_st_pocket':
        raw_dist = abs(ema20 - st_line)
        _sl_min = sl_min if sl_min is not None else A1_SL_MIN
        _sl_max = sl_max if sl_max is not None else A1_SL_MAX
        sl_dist = float(np.clip(raw_dist, _sl_min, _sl_max))
    else:
        raise ValueError(f"Unknown A1 SL mode: {mode}")

    if direction == 'long':
        sl = entry_price - sl_dist
        tp = entry_price + sl_dist * rr
    else:
        sl = entry_price + sl_dist
        tp = entry_price - sl_dist * rr

    return sl, tp


# ---------------------------------------------------------------------------
# Hypothesis A2 — Pivot Retracement (Premium-Discount)
# ---------------------------------------------------------------------------

def a2_of_gate(
    pivot_array: PivotArray,
    direction: str,
    depth: int = OF_DEPTH_A2,
    min_count: int = OF_MIN_A2,
) -> bool:
    """
    Lax 4/2 orderflow gate.

    Last `depth` pivots must contain at least 1 ascending high+low pair (bullish)
    or 1 descending high+low pair (bearish).
    Returns False if fewer than `min_count` pivots exist.
    depth/min_count default to OF_DEPTH_A2/OF_MIN_A2 but can be overridden via
    mechanics.of_depth_a2 / mechanics.of_min_a2 in YAML.
    """
    recent = list(pivot_array.pivots)[-depth:]
    if len(recent) < min_count:
        return False

    highs = [p['price'] for p in recent if p['type'] == 'high']
    lows  = [p['price'] for p in recent if p['type'] == 'low']

    if direction == 'long':
        bull_highs = sum(1 for i in range(len(highs) - 1) if highs[i] < highs[i + 1])
        bull_lows  = sum(1 for i in range(len(lows)  - 1) if lows[i]  < lows[i + 1])
        return bull_highs >= 1 and bull_lows >= 1

    if direction == 'short':
        bear_highs = sum(1 for i in range(len(highs) - 1) if highs[i] > highs[i + 1])
        bear_lows  = sum(1 for i in range(len(lows)  - 1) if lows[i]  > lows[i + 1])
        return bear_highs >= 1 and bear_lows >= 1

    return False


def a2_pd_levels(pivot_array: PivotArray, direction: str, pd_level: float = HYP_A2_PD_LEVEL) -> Optional[dict]:
    """
    Calculate the premium-discount entry level.

    Bullish:  entry_level = last_high - pd_level * pd_range
              entry fires when close <= entry_level
    Bearish:  entry_level = last_low  + pd_level * pd_range
              entry fires when close >= entry_level

    pd_level is configurable (hypotheses.A2.pd_level in YAML). Default 0.618.
    Returns None if pivots are insufficient or pd_range <= 0.
    """
    last_high = pivot_array.last_high()
    last_low  = pivot_array.last_low()

    if last_high is None or last_low is None:
        return None

    pd_range = last_high - last_low
    if pd_range <= 0:
        return None

    if direction == 'long':
        entry_level = last_high - pd_range * pd_level
    elif direction == 'short':
        entry_level = last_low  + pd_range * pd_level
    else:
        return None

    return {
        'entry_level': entry_level,
        'pd_range':    pd_range,
        'last_high':   last_high,
        'last_low':    last_low,
    }


def a2_in_discount_zone(close: float, pd_levels: dict, direction: str) -> bool:
    """True when close has breached into the 61.8% discount zone."""
    if direction == 'long':
        return close <= pd_levels['entry_level']
    if direction == 'short':
        return close >= pd_levels['entry_level']
    return False


def a2_trigger(
    engine: InstrumentEngine,
    feature_row: pd.Series,
    direction: str,
) -> bool:
    """
    A2 trigger: Step 2 + Step 3 of sequential entry.

    Step 2: new_extreme_flag must be armed (price already closed beyond
            last confirmed pivot in the OF direction).
    Step 3: close is now inside the configured discount zone (hypotheses.A2.pd_level).

    Returns False if retrace is too shallow — skip this setup.
    """
    if not engine.state_for('A2').new_extreme_flag:
        return False

    close  = float(feature_row['close'])
    a2_cfg = engine.config.get('hypotheses', {}).get('A2', {})
    pd_lvl = float(a2_cfg.get('pd_level', HYP_A2_PD_LEVEL))

    pd_levels = a2_pd_levels(engine.pivot_array, direction, pd_level=pd_lvl)
    if pd_levels is None:
        return False

    return a2_in_discount_zone(close, pd_levels, direction)


def a2_sl_tp(
    direction: str,
    entry_price: float,
    pivot_array: PivotArray,
    sl_min: float = HYP_A2_SL_MIN,
    sl_max: float = HYP_A2_SL_MAX,
    sl_epsilon: float = HYP_A2_SL_EPSILON,
    rr: float = HYP_A2_RR,
) -> tuple[float, float]:
    """
    pivot_clamped mode:
      SL = |entry − last pivot| + sl_epsilon, clamped to [sl_min, sl_max].
      TP = sl_dist × rr.
    All params configurable via hypotheses.A2 in YAML;
    defaults to EURUSD constants (15/20 pip, 3 pip ε, 1.5R).
    """
    if direction == 'short':
        pivot_price = pivot_array.last_high()
    else:
        pivot_price = pivot_array.last_low()

    if pivot_price is None:
        sl_dist = sl_min
    else:
        raw_dist = abs(entry_price - pivot_price) + sl_epsilon
        sl_dist  = float(np.clip(raw_dist, sl_min, sl_max))

    if direction == 'long':
        sl = entry_price - sl_dist
        tp = entry_price + sl_dist * rr
    else:
        sl = entry_price + sl_dist
        tp = entry_price - sl_dist * rr

    return sl, tp


# ---------------------------------------------------------------------------
# Hypothesis B — ChoCh Structural Break
# ---------------------------------------------------------------------------

def b_trigger(engine: InstrumentEngine, feature_row: pd.Series) -> bool:
    """
    Structural break entry.

    Fires when:
      - ChoCh is confirmed in engine.choch
      - Price closes beyond the last confirmed swing extreme
        in the ChoCh direction (same logic, one entry per pivot)
      - sb_used flag is not yet set for this pivot episode

    One structural break entry per confirmed pivot.
    sb_used resets on each ST flip via pivot_state.reset().
    """
    if not engine.choch.confirmed:
        return False
    if engine.state_for('B').sb_used:
        return False
    if engine.choch.confirmed_time is not None and feature_row.name <= engine.choch.confirmed_time:
        return False

    choch_dir = engine.choch.direction   # +1 | -1
    close     = float(feature_row['close'])

    if choch_dir == +1:
        last_high = engine.pivot_array.last_high()
        if last_high and close > last_high:
            return True

    elif choch_dir == -1:
        last_low = engine.pivot_array.last_low()
        if last_low and close < last_low:
            return True

    return False


def b_sl_tp(
    direction: str,
    entry_price: float,
    sl_fixed: float = HYP_B_SL_FIXED,
    rr: float = HYP_B_RR,
) -> tuple[float, float]:
    """
    Fixed SL mode: flat stop at sl_fixed distance, TP = sl_fixed × rr.
    Configurable via hypotheses.B.sl_fixed / hypotheses.B.rr in YAML.
    Default: 20 pip SL, 2.0R TP (EURUSD; other symbols use 25 pip).
    """
    if direction == 'long':
        sl = entry_price - sl_fixed
        tp = entry_price + sl_fixed * rr
    else:
        sl = entry_price + sl_fixed
        tp = entry_price - sl_fixed * rr

    return sl, tp


# ---------------------------------------------------------------------------
# Orderflow direction helper
# ---------------------------------------------------------------------------

def of_direction_from_pivot(pivot_array: PivotArray) -> Optional[int]:
    """
    Derive orderflow direction from the pivot array.

    Checks the most recent high-low pair sequence:
      Bullish (HH + HL): last high > prev high AND last low > prev low → +1
      Bearish (LH + LL): last high < prev high AND last low < prev low → -1
      Inconclusive → None
    """
    highs = [p['price'] for p in pivot_array.pivots if p['type'] == 'high']
    lows  = [p['price'] for p in pivot_array.pivots if p['type'] == 'low']

    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1] > highs[-2]
        hl = lows[-1]  > lows[-2]
        lh = highs[-1] < highs[-2]
        ll = lows[-1]  < lows[-2]

        if hh and hl:
            return +1
        if lh and ll:
            return -1

    return None


# ---------------------------------------------------------------------------
# evaluate_hypotheses — called by InstrumentEngine._evaluate()
# ---------------------------------------------------------------------------

def evaluate_hypotheses(
    engine:          InstrumentEngine,
    feature_row:     pd.Series,
    context_row:     pd.Series,
    cooldown_status: str,
) -> Optional[list[dict]]:
    """
    Check all hypothesis triggers and return a list of candidates that fired.

    Returns
    -------
    list[dict] — one entry per fired hypothesis, or None if nothing fired.

    Each candidate dict contains:
        hypothesis      'A1' | 'A2' | 'B'
        direction       'long' | 'short'
        entry_price     None  (filled by cursor at next bar open)
        sl              float
        tp              float
        trigger_type    str
        a1_ctx_valid    bool   — A1 context flag (used by policy)
        a2_of_gate      bool   — A2 OF gate result (used by policy)
        of_direction    int | None  — derived OF direction
        context_dir     int    — 1H ST direction

    The policy layer selects which candidate (if any) to trade.
    Entry price is set to None here — the cursor fills it at next bar open.
    """
    bar_time = feature_row.name

    st_dir    = int(feature_row['st_dir'])
    ctx_dir   = int(context_row['st_dir'])
    if _execution_cfg(engine.config).get('require_context_stable', engine.config.get('require_context_stable', True)):
        if not bool(context_row.get('st_stable_3', True)):
            return None

    direction = 'long' if st_dir == +1 else 'short'
    b_dir     = 'long' if (engine.choch.direction or 0) == +1 else 'short'

    # Precompute shared inputs
    a1_ctx = a1_context_valid(feature_row)

    # A2 OF direction: lax = trust context TF (phase-agnostic 1H/4H/etc)
    #                  strict = must be self-proved by pivot HH+HL pattern
    mechanics = engine.config.get('mechanics', {})
    a2_cfg    = _hypothesis_cfg(engine.config, 'A2')
    of_mode   = a2_cfg.get('of_direction_mode', 'strict')
    if of_mode == 'lax':
        of_dir = ctx_dir
    else:
        of_dir = of_direction_from_pivot(engine.pivot_array)

    a2_direction = 'long' if of_dir == +1 else 'short' if of_dir == -1 else None
    a2_gate   = (
        a2_of_gate(
            engine.pivot_array,
            a2_direction,
            depth=int(mechanics.get('of_depth_a2', OF_DEPTH_A2)),
            min_count=int(mechanics.get('of_min_a2', OF_MIN_A2)),
        )
        if engine.pivot_array.has_recent_flip() and a2_direction is not None
        else False
    )

    fired: list[dict] = []

    # --- Hypothesis A1 ---
    a1_cfg  = _hypothesis_cfg(engine.config, 'A1')
    a1_mode = a1_cfg.get('trigger_mode', engine.config.get('a1_trigger_mode', 'ema20_touch'))
    if (
        _hypothesis_enabled(engine.config, 'A1') and
        _session_allowed(engine.config, 'A1', bar_time) and
        engine.cooldown_status_for('A1') != 'waiting' and
        a1_ctx and
        _flicker_allowed(engine, 'A1') and
        a1_trigger(
            feature_row,
            mode=a1_mode,
            rsi_gate_enabled=bool(a1_cfg.get('rsi_gate_enabled', False)),
            ema3_slope_mode=a1_cfg.get('ema3_slope_mode', 'lax'),
            rsi_bull_gate=float(a1_cfg.get('rsi_bull_gate', RSI_BULL_GATE)),
            rsi_bear_gate=float(a1_cfg.get('rsi_bear_gate', RSI_BEAR_GATE)),
        )
    ):
        fired.append({
            'hypothesis':   'A1',
            'direction':    direction,
            'entry_price':  None,
            'trigger_type': a1_mode,
            'a1_ctx_valid': True,
            'a2_of_gate':   a2_gate,
            'of_direction': of_dir,
            'context_dir':  ctx_dir,
            'regime':       feature_row.get('regime', 'UNKNOWN'),
            'session':      feature_row.get('session', ''),
            # SL inputs — resolve_sl_tp uses these at fill time with real entry price
            '_ema20':       float(feature_row['ema20']),
            '_st_line':     float(feature_row['st_line']),
            '_a1_sl_mode':  a1_cfg.get('sl_mode', engine.config.get('a1_sl_mode', 'fixed_12pip')),
            '_a1_rr':       float(a1_cfg.get('rr', HYP_A1_RR)),
            '_a1_sl_min':   float(a1_cfg['a1_sl_min']) if 'a1_sl_min' in a1_cfg else None,
            '_a1_sl_max':   float(a1_cfg['a1_sl_max']) if 'a1_sl_max' in a1_cfg else None,
        })

    # --- Hypothesis A2 ---
    if (
        _hypothesis_enabled(engine.config, 'A2') and
        _session_allowed(engine.config, 'A2', bar_time) and
        engine.cooldown_status_for('A2') != 'waiting' and
        _flicker_allowed(engine, 'A2') and
        a2_gate and
        a2_trigger(engine, feature_row, a2_direction)
    ):
        fired.append({
            'hypothesis':   'A2',
            'direction':    a2_direction,
            'entry_price':  None,
            'trigger_type': 'pd_discount',
            'a1_ctx_valid': a1_ctx,
            'a2_of_gate':   True,
            'of_direction': of_dir,
            'context_dir':  ctx_dir,
            'regime':       feature_row.get('regime', 'UNKNOWN'),
            'session':      feature_row.get('session', ''),
            # SL inputs — resolve_sl_tp uses these at fill time with real entry price
            '_pivot_array':   engine.pivot_array,
            '_a2_sl_min':     float(a2_cfg.get('sl_min',     HYP_A2_SL_MIN)),
            '_a2_sl_max':     float(a2_cfg.get('sl_max',     HYP_A2_SL_MAX)),
            '_a2_sl_epsilon': float(a2_cfg.get('sl_epsilon', HYP_A2_SL_EPSILON)),
            '_a2_rr':         float(a2_cfg.get('rr',         HYP_A2_RR)),
        })

    # --- Hypothesis B ---
    b_cfg = _hypothesis_cfg(engine.config, 'B')
    if (
        _hypothesis_enabled(engine.config, 'B') and
        _session_allowed(engine.config, 'B', bar_time) and
        engine.cooldown_status_for('B') != 'waiting' and
        _flicker_allowed(engine, 'B') and
        b_trigger(engine, feature_row)
    ):
        fired.append({
            'hypothesis':   'B',
            'direction':    b_dir,
            'entry_price':  None,
            'trigger_type': 'structural_break',
            'a1_ctx_valid': a1_ctx,
            'a2_of_gate':   a2_gate,
            'of_direction': of_dir,
            'context_dir':  ctx_dir,
            'regime':       feature_row.get('regime', 'UNKNOWN'),
            'session':      feature_row.get('session', ''),
            # SL inputs — resolve_sl_tp uses these at fill time with real entry price
            '_b_sl_fixed':  float(b_cfg.get('sl_fixed', HYP_B_SL_FIXED)),
            '_b_rr':        float(b_cfg.get('rr',       HYP_B_RR)),
        })

    return fired if fired else None


def _hypothesis_cfg(config: dict, hypothesis: str) -> dict:
    return config.get('hypotheses', {}).get(hypothesis, {})


def _execution_cfg(config: dict) -> dict:
    return config.get('execution', {})


def _hypothesis_enabled(config: dict, hypothesis: str) -> bool:
    cfg = _hypothesis_cfg(config, hypothesis)
    return bool(cfg.get('enabled', config.get(f'hyp_{hypothesis.lower()}_enabled', True)))


def _flicker_allowed(engine: InstrumentEngine, hypothesis: str) -> bool:
    cfg = _hypothesis_cfg(engine.config, hypothesis)
    mode = cfg.get('flicker_suppression', 'own_hypothesis')
    if mode in (False, None, 'none', 'off'):
        return True
    if mode in (True, 'own_hypothesis'):
        return not engine.has_open_hypothesis(hypothesis)
    if mode == 'global':
        return not engine.open_trades
    raise ValueError(f"Unknown flicker_suppression mode for {hypothesis}: {mode}")


def resolve_sl_tp(candidate: dict, entry_price: float) -> dict:
    """
    Called by the cursor after the next bar opens.
    Fills in the real entry_price, sl, and tp for a chosen candidate.

    Returns the candidate dict with entry_price, sl, tp populated.
    """
    hyp       = candidate['hypothesis']
    direction = candidate['direction']

    if hyp == 'A1':
        sl, tp = a1_sl_tp(direction, entry_price,
                          candidate['_ema20'], candidate['_st_line'],
                          candidate.get('_a1_sl_mode', 'fixed_phase2'),
                          rr=candidate.get('_a1_rr', HYP_A1_RR),
                          sl_min=candidate.get('_a1_sl_min'),
                          sl_max=candidate.get('_a1_sl_max'))

    elif hyp == 'A2':
        sl, tp = a2_sl_tp(direction, entry_price, candidate['_pivot_array'],
                          sl_min=candidate.get('_a2_sl_min', HYP_A2_SL_MIN),
                          sl_max=candidate.get('_a2_sl_max', HYP_A2_SL_MAX),
                          sl_epsilon=candidate.get('_a2_sl_epsilon', HYP_A2_SL_EPSILON),
                          rr=candidate.get('_a2_rr', HYP_A2_RR))

    elif hyp == 'B':
        sl, tp = b_sl_tp(direction, entry_price,
                         sl_fixed=candidate.get('_b_sl_fixed', HYP_B_SL_FIXED),
                         rr=candidate.get('_b_rr', HYP_B_RR))

    else:
        raise ValueError(f"Unknown hypothesis: {hyp}")

    return {**candidate, 'entry_price': entry_price, 'sl': sl, 'tp': tp}
