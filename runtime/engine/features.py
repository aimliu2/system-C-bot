# features.py — precompute safe indicator features from raw OHLCV data
#
# Every column produced here contains ONLY information available at that
# bar's close. No future bars, no unfinished higher-TF candles, no trade outcomes.
#
# Output columns per timeframe:
#   open, high, low, close, volume  (passthrough)
#   close_prev, ema3_prev, ema20_prev
#   ema3, ema20
#   atr10, atr50
#   rsi30
#   st_line, st_dir              SuperTrend(12, 3.0)
#   st_flip                      bool: direction changed vs prev bar
#   st_step_count                ST line value steps since last flip
#   session                      str: named session (asian/london/london_ny/ny/overnight)
#   avail_time                   = index (bar close time, UTC)
#
# Regime label is NOT computed here. It requires both entry-TF and context-TF
# features to be aligned first. Call compute_regime(df_aligned) after align().
#
from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path
from typing import Optional

from runtime.engine.indicator import ema, atr, rsi, supertrend
from runtime.engine.numba_kernels import st_stable_kernel, st_step_count_kernel

# Session boundaries (UTC hours, half-open [start, end))
# Matches the YAML session definitions used across all instruments.
_SESSION_BOUNDARIES = [
    ('asian',     0,  7),
    ('london',    7, 12),
    ('london_ny', 12, 17),
    ('ny',        17, 21),
    ('overnight', 21, 24),
]

# SuperTrend default params
_ST_PERIOD = 12
_ST_MULT   = 3.0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def prepare_ohlcv(
    df: pd.DataFrame,
    bar_duration: str | pd.Timedelta,
    timestamp_col: str = 'timestamp',
) -> pd.DataFrame:
    """
    Normalise raw OHLCV data to the engine's bar-close timestamp convention.

    SystemC raw parquet files store `timestamp` as the bar OPEN time. The
    forward engine makes decisions only after a bar closes, so the DataFrame
    index and `avail_time` must be bar close time: timestamp + bar_duration.
    """
    out = df.copy()

    if timestamp_col in out.columns:
        open_time = pd.to_datetime(out[timestamp_col], utc=True)
        out = out.drop(columns=[timestamp_col])
    elif isinstance(out.index, pd.DatetimeIndex):
        open_time = pd.to_datetime(out.index, utc=True)
    else:
        raise ValueError(
            f"Expected a `{timestamp_col}` column or DatetimeIndex with bar-open times."
        )

    out.index = open_time + pd.Timedelta(bar_duration)
    out.index.name = 'bar_close_time'

    if 'volume' not in out.columns and 'tick_vol' in out.columns:
        out = out.rename(columns={'tick_vol': 'volume'})

    return out.sort_index()


def build_features(
    df: pd.DataFrame,
    bar_duration: Optional[str | pd.Timedelta] = None,
    st_period: int = _ST_PERIOD,
    st_mult: float = _ST_MULT,
) -> pd.DataFrame:
    """
    Compute all safe features for one timeframe from raw OHLCV data.

    Parameters
    ----------
    df : pd.DataFrame
        Must have columns: open, high, low, close, volume
        Index: DatetimeIndex (UTC bar close times).

    Returns
    -------
    pd.DataFrame
        Original columns + all feature columns. Index unchanged.
        NaN rows during warm-up are kept (do not drop here — the cursor
        handles warm-up by skipping NaN rows).
    """
    if bar_duration is not None:
        df = prepare_ohlcv(df, bar_duration=bar_duration)
    else:
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True)

    # --- Lagged prices ---
    df['close_prev'] = df['close'].shift(1)

    # --- Moving averages ---
    df['ema3']      = ema(df['close'], period=3)
    df['ema20']     = ema(df['close'], period=20)
    df['ema3_prev'] = df['ema3'].shift(1)
    df['ema3_lag1'] = df['ema3'].shift(1)
    df['ema3_lag2'] = df['ema3'].shift(2)
    df['ema3_lag3'] = df['ema3'].shift(3)
    df['ema20_prev']= df['ema20'].shift(1)

    # --- ATR (regime + ST) ---
    df['atr10'] = atr(df['high'], df['low'], df['close'], period=10)
    df['atr50'] = atr(df['high'], df['low'], df['close'], period=50)

    # --- RSI(30) — A1 V2 gate ---
    df['rsi30'] = rsi(df['close'], period=30)

    # --- SuperTrend(12, 3.0) ---
    df['st_line'], df['st_dir'] = supertrend(
        df['high'], df['low'], df['close'],
        period=st_period, multiplier=st_mult,
    )

    # --- ST flip flag ---
    df['st_flip'] = (df['st_dir'] != df['st_dir'].shift(1)) & df['st_dir'].notna()
    df['st_stable_3'] = _compute_st_stable(df['st_dir'].to_numpy(dtype=float), bars=3)

    # --- ST step count since last flip ---
    df['st_step_count'] = _compute_st_step_count(
        df['st_line'].to_numpy(dtype=float),
        df['st_dir'].to_numpy(dtype=float),
    )

    # --- Session label ---
    df['session'] = df.index.map(_in_session)

    # --- Available time = bar close time (= index) ---
    df['avail_time'] = df.index

    return df


# ---------------------------------------------------------------------------
# Regime label (call AFTER align() has merged entry + context TF)
# ---------------------------------------------------------------------------

def compute_regime(df_aligned: pd.DataFrame) -> pd.Series:
    """
    Compute systemC-regime labels on the aligned DataFrame.

    Requires columns (produced by build_features + align):
        st_dir          — entry TF (15m) direction
        st_flip         — entry TF flip flag
        atr10, atr50    — entry TF ATR ratio
        ctx_st_dir      — context TF (1H) direction
        ctx_st_flip     — context TF flip flag

    Classifier precedence:
        1. TRANSITION  — context TF recently flipped (within flip_lookback bars)
        2. CHAOTIC     — ATR10/ATR50 >= atr_spike threshold
        3. STEADY_TREND — context stable, entry aligned, ATR10/ATR50 < atr_expand
        4. ACTIVE_TREND — context stable, entry aligned or OF active, ATR elevated
        5. RANGE        — context stable, direction mixed or non-sequential

    Parameters (EURUSD v1 calibration):
        stable_bars   = 3     bars of consistent context direction
        flip_lookback = 20    bars to look back for a recent context flip
        atr_expand    = 1.30
        atr_spike     = 1.50

    Returns
    -------
    pd.Series of regime label strings, same index as df_aligned.
    """
    STABLE_BARS   = 3
    FLIP_LOOKBACK = 20
    ATR_EXPAND    = 1.30
    ATR_SPIKE     = 1.50

    n      = len(df_aligned)
    labels = np.full(n, 'UNKNOWN', dtype=object)

    ctx_dir  = df_aligned['ctx_st_dir'].to_numpy(dtype=float)
    ent_dir  = df_aligned['st_dir'].to_numpy(dtype=float)
    atr10    = df_aligned['atr10'].to_numpy(dtype=float)
    atr50    = df_aligned['atr50'].to_numpy(dtype=float)

    for i in range(n):
        if np.isnan(ctx_dir[i]) or np.isnan(ent_dir[i]) or np.isnan(atr10[i]):
            labels[i] = 'UNKNOWN'
            continue

        atr_ratio = atr10[i] / atr50[i] if atr50[i] != 0 else 1.0

        # --- Layer 1: TRANSITION ---
        # Context TF flipped within the last FLIP_LOOKBACK entry-TF bars
        start        = max(0, i - FLIP_LOOKBACK)
        window_ctx   = ctx_dir[start: i + 1]
        recent_flip  = _has_recent_flip(window_ctx)

        # Also check stability: last STABLE_BARS context bars same direction
        ctx_window   = ctx_dir[max(0, i - STABLE_BARS + 1): i + 1]
        ctx_stable   = (
            len(ctx_window) >= STABLE_BARS and
            np.all(ctx_window == ctx_window[-1]) and
            not np.any(np.isnan(ctx_window))
        )

        if recent_flip or not ctx_stable:
            labels[i] = 'TRANSITION'
            continue

        # --- Layer 2: CHAOTIC ---
        if atr_ratio >= ATR_SPIKE:
            labels[i] = 'CHAOTIC'
            continue

        # --- Layers 3 & 4: directional ---
        ctx_d = ctx_dir[i]
        ent_d = ent_dir[i]
        aligned = (ctx_d == ent_d)

        if aligned:
            if atr_ratio < ATR_EXPAND:
                labels[i] = 'STEADY_TREND'
            else:
                labels[i] = 'ACTIVE_TREND'
        else:
            # Entry TF direction differs from context — retracement or range
            if atr_ratio >= ATR_EXPAND:
                labels[i] = 'ACTIVE_TREND'   # large swings, structure active
            else:
                labels[i] = 'RANGE'

    return pd.Series(labels, index=df_aligned.index, name='regime')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _in_session(ts: pd.Timestamp) -> str:
    h = ts.hour
    for name, start, end in _SESSION_BOUNDARIES:
        if start <= h < end:
            return name
    return 'overnight'  # hour == 0 edge already caught by asian; fallback


def _has_recent_flip(arr: np.ndarray) -> bool:
    """True if arr contains at least one direction change."""
    valid = arr[~np.isnan(arr)]
    if len(valid) < 2:
        return False
    return bool(np.any(np.diff(valid) != 0))


def _compute_st_stable(st_dir: np.ndarray, bars: int = 3) -> np.ndarray:
    """True when the last `bars` ST directions are all present and equal."""
    return st_stable_kernel(st_dir, bars)


def _compute_st_step_count(
    st_line: np.ndarray,
    st_dir:  np.ndarray,
) -> np.ndarray:
    """
    For each bar i, count how many times the ST line value changed in the
    trend direction since the last ST direction flip.

    Bullish episode: count bars where st_line[j] > st_line[j-1]
    Bearish episode: count bars where st_line[j] < st_line[j-1]

    Window = [first bar of current episode .. i]
    Resets automatically when a new flip is detected.

    Returns int32 array, same length as inputs.
    """
    return st_step_count_kernel(st_line, st_dir)


# ---------------------------------------------------------------------------
# Parquet I/O helpers
# ---------------------------------------------------------------------------

def save_features(df: pd.DataFrame, path: str | Path) -> None:
    """Write feature DataFrame to parquet."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)


def load_features(path: str | Path) -> pd.DataFrame:
    """Load feature DataFrame from parquet."""
    return pd.read_parquet(path)
