"""
run_data.py — System C: Dual-Timeframe Data + Indicators
v1.0 · April 2026

Pulls 15m (entry TF) and 1H (context TF) bars from MT5.
Computes:
  15m: SuperTrend(12,3), EMA20, EMA3, RSI30, ATR ratio, EMA20/60
  1H:  SuperTrend(12,3), EMA50, EMA200, RSI30
  Regime: 7-regime classifier (1H EMA group + 15m ATR/RSI sub-state)

No orders. No state. Math must match backtest study exactly.

Run: python3 run_data.py
Requires: MT5 running (VPS) or rpyc bridge (macOS)
"""

import pandas as pd
import numpy as np
from datetime import datetime, timezone
from collections import namedtuple
from numba import njit

from config_loader import config, get_regime_config, get_st_config

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

WEEKDAYS = {0, 1, 2, 3, 4}  # Mon=0 ... Fri=4

DataBundle = namedtuple("DataBundle", ["df_15m", "df_1h"])

# ---------------------------------------------------------------------------
# BROKER OFFSET DETECTION
# ---------------------------------------------------------------------------

def detect_broker_offset(mt5) -> int:
    """
    Detect broker UTC offset by comparing broker tick timestamp to wall-clock UTC.
    Returns integer hour offset (e.g. 2 for UTC+2, 3 for UTC+3).
    """
    tick = mt5.symbol_info_tick("EURUSD")
    broker_utc_dt = datetime.fromtimestamp(tick.time, tz=timezone.utc).replace(tzinfo=None)
    utc_dt        = datetime.now(timezone.utc).replace(tzinfo=None)
    offset = round((broker_utc_dt - utc_dt).total_seconds() / 3600)
    print(f"  Broker UTC offset: UTC+{offset}")
    return offset


# ---------------------------------------------------------------------------
# SUPERTREND — EWM ATR (must match backtest study exactly)
# ---------------------------------------------------------------------------

def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Exponentially-weighted ATR. Same as SystemB — do not change."""
    high       = df["high"]
    low        = df["low"]
    close      = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


@njit
def _st_bands(close, upper, lower):
    """JIT-compiled SuperTrend band carry-forward and direction logic."""
    n         = len(close)
    st_upper  = upper.copy()
    st_lower  = lower.copy()
    direction = np.ones(n, dtype=np.int32)
    for i in range(1, n):
        # Upper band — only tighten, never widen
        if upper[i] < st_upper[i - 1] or close[i - 1] > st_upper[i - 1]:
            st_upper[i] = upper[i]
        else:
            st_upper[i] = st_upper[i - 1]
        # Lower band — only tighten, never widen
        if lower[i] > st_lower[i - 1] or close[i - 1] < st_lower[i - 1]:
            st_lower[i] = lower[i]
        else:
            st_lower[i] = st_lower[i - 1]
        # Direction flip
        if direction[i - 1] == -1 and close[i] > st_upper[i]:
            direction[i] = 1
        elif direction[i - 1] == 1 and close[i] < st_lower[i]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    return st_upper, st_lower, direction


def compute_supertrend(df: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """
    SuperTrend with EWM ATR. Matches backtest implementation exactly.
    Returns DataFrame with: st_upper, st_lower, st_direction, st_line.
    """
    atr   = compute_atr(df, period)
    hl2   = (df["high"] + df["low"]) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    st_upper_arr, st_lower_arr, direction_arr = _st_bands(
        df["close"].values, upper.values.copy(), lower.values.copy()
    )
    st_upper  = pd.Series(st_upper_arr,  index=df.index)
    st_lower  = pd.Series(st_lower_arr,  index=df.index)
    direction = pd.Series(direction_arr, index=df.index)

    # Active ST line: lower band when bullish, upper band when bearish
    st_line = pd.Series(
        np.where(direction == 1, st_lower, st_upper),
        index=df.index,
    )

    return pd.DataFrame({
        "st_upper"    : st_upper,
        "st_lower"    : st_lower,
        "st_direction": direction,
        "st_line"     : st_line,
    })


# ---------------------------------------------------------------------------
# EMA and RSI
# ---------------------------------------------------------------------------

def compute_ema(series: pd.Series, span: int) -> pd.Series:
    """Exponentially-weighted moving average."""
    return series.ewm(span=span, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int) -> pd.Series:
    """
    RSI using Wilder's smoothing (EWM with alpha=1/period).
    Matches standard RSI implementations used in backtesting.
    """
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)  # neutral fill on warm-up


# ---------------------------------------------------------------------------
# 15m INDICATORS
# ---------------------------------------------------------------------------

def add_15m_indicators(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Add all 15m indicators required for hypothesis detection.

      ST(12,3)         → st_line, st_direction, st_upper, st_lower
      EMA(touch_span)  → ema_touch  (A1 bounce target; span from config)
      EMA(traj_span)   → ema_traj   (A1 trajectory filter)
      RSI(30)          → rsi        (A1 gate + regime sub-state)
      ATR ratio        → atr_ratio  (regime sub-state)
      EMA20 / EMA60    → ema20_15m, ema60_15m (regime reference)
    """
    from config_loader import get_hyp_config
    df = df.copy()

    # SuperTrend — per-instrument params (supports override in config.yaml)
    st_cfg = get_st_config(symbol, "entry")
    period = st_cfg["st_period"]
    mult   = st_cfg["st_multiplier"]
    st     = compute_supertrend(df, period, mult)
    df["st_line"]      = st["st_line"]
    df["st_direction"] = st["st_direction"]
    df["st_upper"]     = st["st_upper"]
    df["st_lower"]     = st["st_lower"]

    # A1 EMA indicators (span from effective config — supports per-instrument override)
    a1_cfg         = get_hyp_config(symbol, "a1")
    touch_span     = a1_cfg.get("ema_touch_span", 20)
    traj_span      = a1_cfg.get("ema_traj_span",  3)
    df["ema_touch"] = compute_ema(df["close"], touch_span)
    df["ema_traj"]  = compute_ema(df["close"], traj_span)

    # RSI(30) — A1 gate + regime sub-state
    regime_cfg    = get_regime_config()
    rsi_period    = regime_cfg.get("rsi_period", 30)
    df["rsi"]     = compute_rsi(df["close"], rsi_period)

    # ATR ratio for regime sub-state  (ATR_short / ATR_long)
    atr_short_p   = regime_cfg.get("atr_short", 10)
    atr_long_p    = regime_cfg.get("atr_long",  50)
    atr_s         = compute_atr(df, atr_short_p)
    atr_l         = compute_atr(df, atr_long_p)
    df["atr_ratio"] = atr_s / atr_l.replace(0, float("nan"))

    # 15m EMA20/60 — regime sub-state reference only
    df["ema20_15m"] = compute_ema(df["close"], regime_cfg.get("ema_fast_15m", 20))
    df["ema60_15m"] = compute_ema(df["close"], regime_cfg.get("ema_slow_15m", 60))

    return df


# ---------------------------------------------------------------------------
# 1H INDICATORS
# ---------------------------------------------------------------------------

def add_1h_indicators(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Add all 1H indicators required for context and regime group.

      ST(12,3)     → st_line, st_direction
      EMA50/200    → ema50_1h, ema200_1h  (regime group)
    """
    df = df.copy()

    # Per-instrument ST params (supports override in config.yaml)
    st_cfg = get_st_config(symbol, "context")
    period = st_cfg["st_period"]
    mult   = st_cfg["st_multiplier"]
    st     = compute_supertrend(df, period, mult)
    df["st_line"]      = st["st_line"]
    df["st_direction"] = st["st_direction"]
    df["st_upper"]     = st["st_upper"]
    df["st_lower"]     = st["st_lower"]

    regime_cfg      = get_regime_config()
    df["ema50_1h"]  = compute_ema(df["close"], regime_cfg.get("ema_fast_1h", 50))
    df["ema200_1h"] = compute_ema(df["close"], regime_cfg.get("ema_slow_1h", 200))

    return df


# ---------------------------------------------------------------------------
# SESSION ASSIGNMENT
# ---------------------------------------------------------------------------

def assign_session(ts: pd.Series) -> pd.Series:
    """
    Assign session label based on UTC hour.
    Sessions from config: London [7,16], NY [13,21], Asia [0,7].
    Overlapping London/NY window (13–16 UTC) → NY (NY takes precedence).
    """
    hour = ts.dt.hour
    sess = config.get("sessions", {})
    l_start, l_end = sess.get("London", [7, 16])
    ny_start, ny_end = sess.get("NY", [13, 21])
    a_start, a_end   = sess.get("Asia", [0, 7])

    conditions = [
        (hour >= ny_start) & (hour < ny_end),
        (hour >= l_start)  & (hour < l_end),
        (hour >= a_start)  & (hour < a_end),
    ]
    choices = ["NY", "London", "Asia"]
    return pd.Series(
        np.select(conditions, choices, default="none"),
        index=ts.index,
    )


# ---------------------------------------------------------------------------
# REGIME INT ENCODING  (used by @njit functions — cannot pass strings to numba)
# 0=UNKNOWN  1=BULLISH  2=ACCUMULATION  3=RECOVERY
# 4=BEARISH  5=DISTRIBUTION  6=CORRECTION
# ---------------------------------------------------------------------------

_REGIME_ENC = {
    "UNKNOWN": 0, "BULLISH": 1, "ACCUMULATION": 2, "RECOVERY": 3,
    "BEARISH": 4, "DISTRIBUTION": 5, "CORRECTION": 6,
}
_REGIME_DEC = {v: k for k, v in _REGIME_ENC.items()}


@njit
def _regime_group_confirm(raw_cross, confirm_n):
    """
    JIT-compiled EMA group confirmation.
    raw_cross: int32 array (1=bull, -1=bear)
    Returns int32 array: 1=BULLISH group, -1=BEARISH group, 0=undecided
    """
    n             = len(raw_cross)
    confirmed     = np.zeros(n, dtype=np.int32)
    current_group = np.int32(0)
    streak        = np.int32(0)
    streak_dir    = np.int32(0)
    for i in range(n):
        c = raw_cross[i]
        if c == streak_dir:
            streak += 1
        else:
            streak    = np.int32(1)
            streak_dir = c
        if streak >= confirm_n:
            current_group = streak_dir
        confirmed[i] = current_group
    return confirmed


@njit
def _regime_substate(confirmed_group, rsi, atr_ratio,
                     rsi_corr, rsi_bear, rsi_neut_hi, rsi_bull, rsi_dist,
                     atr_compress, atr_expand, atr_spike):
    """
    JIT-compiled regime sub-state carry-forward.
    Returns int32 array encoded as:
      0=UNKNOWN  1=BULLISH  2=ACCUMULATION  3=RECOVERY
      4=BEARISH  5=DISTRIBUTION  6=CORRECTION
    Caller decodes via _REGIME_DEC.
    """
    n      = len(confirmed_group)
    regime = np.zeros(n, dtype=np.int32)   # default UNKNOWN=0
    prev   = np.int32(0)
    for i in range(n):
        grp = confirmed_group[i]
        r   = rsi[i]
        a   = atr_ratio[i]
        r_p = rsi[i - 1]       if i > 0 else np.float64(np.nan)
        a_p = atr_ratio[i - 1] if i > 0 else np.float64(np.nan)
        sub = np.int32(-1)     # -1 = no match → hold previous

        if grp == 1:   # BULLISH group
            nan_safe = (r_p == r_p) and (a_p == a_p)   # NaN check: NaN != NaN
            # RECOVERY: RSI crossing 48↑ with ATR declining
            if nan_safe and r_p < rsi_bear and r >= rsi_bear and a <= a_p:
                sub = np.int32(3)   # RECOVERY
            # BULLISH: strong momentum, ATR not spiking
            elif r >= rsi_bull and a < atr_expand:
                sub = np.int32(1)   # BULLISH
            # ACCUMULATION: neutral RSI, ATR compressed (below parity)
            elif rsi_bear <= r < rsi_neut_hi and a < atr_compress:
                sub = np.int32(2)   # ACCUMULATION

        elif grp == -1:   # BEARISH group
            # CORRECTION: RSI oversold + ATR spike (genuine panic)
            if r < rsi_corr and a >= atr_spike:
                sub = np.int32(6)   # CORRECTION
            # DISTRIBUTION: RSI overbought, diverging ↓, ATR mild expansion
            elif r >= rsi_dist:
                nan_safe = (r_p == r_p)
                if nan_safe and r_p > r and atr_compress <= a < atr_expand:
                    sub = np.int32(5)   # DISTRIBUTION
            # BEARISH: persistent bear, ATR above compression
            elif rsi_corr <= r < rsi_bear and a >= atr_compress:
                sub = np.int32(4)   # BEARISH

        if sub >= 0:
            regime[i] = sub
            prev = sub
        else:
            regime[i] = prev   # UNKNOWN: hold previous

    return regime


# ---------------------------------------------------------------------------
# 7-REGIME CLASSIFIER
# ---------------------------------------------------------------------------

def compute_regime_7(df_15m: pd.DataFrame, df_1h: pd.DataFrame) -> pd.Series:
    """
    7-regime classifier. Per-bar on df_15m index.

    Step 1 — Group (1H EMA50/200 crossover, with ema_confirm_bars buffer):
        EMA50 > EMA200  (confirmed N bars)  →  BULLISH group
        EMA50 < EMA200  (confirmed N bars)  →  BEARISH group
        Transitioning                        →  hold previous

    Step 2 — Sub-state (15m ATR ratio + RSI30):
        BULLISH group:
            RSI 48–55, ATR < 1.0 (compressed)   →  ACCUMULATION
            RSI > 55,  ATR < 1.3                 →  BULLISH
            RSI crossing 48↑, ATR declining      →  RECOVERY
        BEARISH group:
            RSI > 60 diverging ↓, ATR 1.0–1.3   →  DISTRIBUTION
            RSI 40–48, ATR ≥ 1.0                 →  BEARISH
            RSI < 40,  ATR > 1.5 (spike)         →  CORRECTION

    Fallthrough → UNKNOWN (hold previous regime)

    Returns: pd.Series of regime labels aligned to df_15m.index
    """
    rc = get_regime_config()

    ema_fast_col = "ema50_1h"
    ema_slow_col = "ema200_1h"
    confirm_n    = rc.get("ema_confirm_bars", 3)

    atr_compress = rc.get("atr_compress", 1.0)
    atr_expand   = rc.get("atr_expand",  1.3)
    atr_spike    = rc.get("atr_spike",   1.5)

    rsi_corr     = rc.get("rsi_correction", 40.0)
    rsi_bear     = rc.get("rsi_bear_band",  48.0)
    rsi_neut_hi  = rc.get("rsi_neutral_hi", 55.0)
    rsi_bull     = rc.get("rsi_bull_band",  55.0)
    rsi_dist     = rc.get("rsi_dist_entry", 60.0)

    # ── Step 1: 1H group assignment ──────────────────────────────────────
    # Reindex 1H onto 15m timestamps (forward-fill — each 15m bar sees
    # the most recent closed 1H bar's indicators)
    df_1h_ri = df_1h.set_index("time_utc")[[ema_fast_col, ema_slow_col, "st_direction"]].copy()
    df_1h_ri = df_1h_ri[~df_1h_ri.index.duplicated(keep="last")]
    df_15m_times = df_15m["time_utc"]

    ema_fast_1h = df_1h_ri[ema_fast_col].reindex(df_15m_times, method="ffill").values
    ema_slow_1h = df_1h_ri[ema_slow_col].reindex(df_15m_times, method="ffill").values
    st_dir_1h   = df_1h_ri["st_direction"].reindex(df_15m_times, method="ffill").values

    n = len(df_15m)
    group = np.full(n, "", dtype=object)  # "BULLISH" | "BEARISH" | ""

    # Compute raw crossover signal (1 = bull cross, -1 = bear cross, 0 = unclear)
    raw_cross = np.where(ema_fast_1h > ema_slow_1h, 1, -1)

    # Apply confirmation buffer: require confirm_n consecutive bars before flipping group
    raw_cross_int   = raw_cross.astype(np.int32)
    confirmed_group = _regime_group_confirm(raw_cross_int, np.int32(confirm_n))

    # ── Step 2: sub-state from 15m ATR ratio + RSI ──────────────────────
    atr_ratio = df_15m["atr_ratio"].values
    rsi       = df_15m["rsi"].values

    regime_int = _regime_substate(
        confirmed_group,
        rsi.astype(np.float64), atr_ratio.astype(np.float64),
        float(rsi_corr), float(rsi_bear), float(rsi_neut_hi),
        float(rsi_bull), float(rsi_dist),
        float(atr_compress), float(atr_expand), float(atr_spike),
    )
    regime = np.vectorize(_REGIME_DEC.get)(regime_int)

    return pd.Series(regime, index=df_15m.index, name="regime")


# ---------------------------------------------------------------------------
# DATA PULL — VPS (native MT5)
# ---------------------------------------------------------------------------

def pull_bars_15m(mt5, symbol: str, n_bars: int) -> pd.DataFrame:
    tf    = mt5.TIMEFRAME_M15
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n_bars)
    df    = pd.DataFrame(rates)
    df["time_utc"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)
    return df


def pull_bars_1h(mt5, symbol: str, n_bars: int) -> pd.DataFrame:
    tf    = mt5.TIMEFRAME_H1
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n_bars)
    df    = pd.DataFrame(rates)
    df["time_utc"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)
    return df


# ---------------------------------------------------------------------------
# DATA PULL — rpyc (macOS, via bridge)
# ---------------------------------------------------------------------------

def pull_bars_15m_rpyc(mt5, symbol: str, n_bars: int) -> pd.DataFrame:
    import rpyc.utils.classic
    tf    = mt5.TIMEFRAME_M15
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n_bars)
    local = rpyc.utils.classic.obtain(rates)
    df    = pd.DataFrame(local)
    df["time_utc"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)
    return df


def pull_bars_1h_rpyc(mt5, symbol: str, n_bars: int) -> pd.DataFrame:
    import rpyc.utils.classic
    tf    = mt5.TIMEFRAME_H1
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n_bars)
    local = rpyc.utils.classic.obtain(rates)
    df    = pd.DataFrame(local)
    df["time_utc"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)
    return df


# ---------------------------------------------------------------------------
# FULL PIPELINE — called by run_orders on each data refresh
# ---------------------------------------------------------------------------

def build_data_bundle(mt5, symbol: str, is_startup: bool,
                      rpyc_mode: bool = False) -> DataBundle:
    """
    Pull and process both timeframes. Called on each 15m bar close.

    is_startup=True  → pull full history (bars_to_pull_*)
    is_startup=False → pull running history (bars_running_*)

    Returns DataBundle(df_15m, df_1h) with all indicators computed.
    """
    n_15m = config["bars_to_pull_15m"] if is_startup else config["bars_running_15m"]
    n_1h  = config["bars_to_pull_1h"]  if is_startup else config["bars_running_1h"]

    if rpyc_mode:
        df_15m = pull_bars_15m_rpyc(mt5, symbol, n_15m)
        df_1h  = pull_bars_1h_rpyc(mt5, symbol, n_1h)
    else:
        df_15m = pull_bars_15m(mt5, symbol, n_15m)
        df_1h  = pull_bars_1h(mt5, symbol, n_1h)

    df_15m = add_15m_indicators(df_15m, symbol)
    df_1h  = add_1h_indicators(df_1h, symbol)

    df_15m["session"] = assign_session(df_15m["time_utc"])
    df_15m["regime"]  = compute_regime_7(df_15m, df_1h)

    return DataBundle(df_15m=df_15m, df_1h=df_1h)


# ---------------------------------------------------------------------------
# MAIN — standalone verification
# ---------------------------------------------------------------------------

def main():
    print("System C — run_data.py  (standalone verification)")
    print("=" * 60)

    import MetaTrader5 as mt5

    if not mt5.initialize(
        login    = int(os.getenv("MT5_LOGIN", 0)),
        password = os.getenv("MT5_PASSWORD", ""),
        server   = os.getenv("MT5_SERVER", ""),
    ):
        print("❌ MT5 initialize failed")
        return

    print("✅ MT5 connected")

    broker_offset = detect_broker_offset(mt5)

    for symbol in ["EURUSD"]:
        print(f"\n{'─'*60}")
        print(f"  {symbol}")
        print(f"{'─'*60}")

        bundle = build_data_bundle(mt5, symbol, is_startup=True, rpyc_mode=False)
        df15   = bundle.df_15m
        df1h   = bundle.df_1h

        print(f"  15m bars: {len(df15)}  |  1H bars: {len(df1h)}")
        print(f"  15m last: {df15['time_utc'].iloc[-1]}")
        print(f"  1H  last: {df1h['time_utc'].iloc[-1]}")

        last15 = df15.iloc[-1]
        last1h = df1h.iloc[-1]

        print(f"\n  15m current bar:")
        print(f"    time_utc    : {last15['time_utc']}")
        print(f"    close       : {last15['close']:.6f}")
        print(f"    st_line     : {last15['st_line']:.6f}")
        print(f"    st_dir      : {int(last15['st_direction'])} ({'BULL' if last15['st_direction']==1 else 'BEAR'})")
        print(f"    ema_touch   : {last15['ema_touch']:.6f}")
        print(f"    ema_traj    : {last15['ema_traj']:.6f}")
        print(f"    rsi         : {last15['rsi']:.2f}")
        print(f"    atr_ratio   : {last15['atr_ratio']:.4f}")
        print(f"    session     : {last15['session']}")
        print(f"    regime      : {last15['regime']}")

        print(f"\n  1H current bar:")
        print(f"    time_utc    : {last1h['time_utc']}")
        print(f"    close       : {last1h['close']:.6f}")
        print(f"    st_line     : {last1h['st_line']:.6f}")
        print(f"    st_dir      : {int(last1h['st_direction'])} ({'BULL' if last1h['st_direction']==1 else 'BEAR'})")
        print(f"    ema50       : {last1h['ema50_1h']:.6f}")
        print(f"    ema200      : {last1h['ema200_1h']:.6f}")

        pd.set_option("display.float_format", lambda x: f"{x:.6f}")
        cols15 = ["time_utc", "close", "st_line", "st_direction",
                  "ema_touch", "rsi", "atr_ratio", "session", "regime"]
        print(f"\n  Last 10 × 15m bars:")
        print(df15[cols15].tail(10).to_string(index=False))

        # Regime distribution summary
        print(f"\n  Regime distribution (last 200 bars):")
        dist = df15["regime"].tail(200).value_counts()
        for reg, cnt in dist.items():
            pct = cnt / min(200, len(df15)) * 100
            print(f"    {reg:<15} {cnt:>4}  ({pct:.1f}%)")

    mt5.shutdown()
    print(f"\n{'='*60}")
    print("run_data.py verification complete.")


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv(".ennv")
    main()
