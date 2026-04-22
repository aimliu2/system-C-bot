# declare indicator here or use a python library like ta-lib or pandas_ta
import numpy as np
import pandas as pd
from typing import Tuple

from runtime.engine.numba_kernels import (
    atr_kernel,
    ema_kernel,
    rsi_kernel,
    supertrend_kernel,
)


def sma(close: pd.Series, period: int = 50) -> pd.Series:
    """
    Simple Moving Average (SMA).

    SMA = sum(close, N) / N

    Parameters
    ----------
    close : pd.Series
        Closing prices.
    period : int
        Lookback window (default 50).

    Returns
    -------
    pd.Series
        SMA values, NaN for the first `period - 1` rows (warm-up).

    Usage
    -----
    df['sma50'] = sma(df['close'])
    df['sma20'] = sma(df['close'], period=20)
    """
    result = close.rolling(window=period, min_periods=period).mean()
    result.name = f"sma_{period}"
    return result


def ema(close: pd.Series, period: int = 20) -> pd.Series:
    """
    Exponential Moving Average (EMA).

    Multiplier = 2 / (N + 1)
    EMA = (Close - EMA_prev) * Multiplier + EMA_prev

    Seeded with the SMA of the first `period` bars.
    NaN for the first `period - 1` rows (warm-up).

    Parameters
    ----------
    close : pd.Series
        Closing prices.
    period : int
        Lookback window (default 20).

    Returns
    -------
    pd.Series
        EMA values.

    Usage
    -----
    df['ema20'] = ema(df['close'])
    df['ema50'] = ema(df['close'], period=50)
    """
    prices = close.to_numpy(dtype=float)
    result = ema_kernel(prices, period)
    return pd.Series(result, index=close.index, name=f"ema_{period}")


def kama(close: pd.Series, er_period: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    """
    Kaufman's Adaptive Moving Average (KAMA).

    Parameters
    ----------
    close : pd.Series
        Closing prices.
    er_period : int
        Lookback for Efficiency Ratio (default 10).
    fast : int
        Fast EMA period (default 2).
    slow : int
        Slow EMA period (default 30).

    Returns
    -------
    pd.Series
        KAMA values, NaN for the first `er_period - 1` rows (warm-up).

    Usage
    -----
    df['kama'] = kama(df['close'])
    df['kama_20'] = kama(df['close'], er_period=20)
    """
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)

    prices = close.to_numpy(dtype=float)
    n = len(prices)
    result = np.full(n, np.nan)

    # Seed: first KAMA value at index er_period - 1
    seed = er_period - 1
    result[seed] = prices[seed]

    for i in range(seed + 1, n):
        # Efficiency Ratio
        direction = abs(prices[i] - prices[i - er_period])
        volatility = np.sum(np.abs(np.diff(prices[i - er_period: i + 1])))
        er = direction / volatility if volatility != 0 else 0.0

        # Smoothing Constant
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2

        result[i] = result[i - 1] + sc * (prices[i] - result[i - 1])

    return pd.Series(result, index=close.index, name=f"kama_{er_period}_{fast}_{slow}")


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    Average True Range (ATR).

    True Range = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR = RMA(TR, period)   [Wilder's smoothed MA — same as EMA with alpha=1/N]

    Parameters
    ----------
    high, low, close : pd.Series
        OHLCV columns.
    period : int
        Smoothing period (default 14).

    Returns
    -------
    pd.Series
        ATR values. NaN for the first `period` rows (warm-up).

    Usage
    -----
    df['atr14'] = atr(df['high'], df['low'], df['close'])
    df['atr10'] = atr(df['high'], df['low'], df['close'], period=10)
    df['atr50'] = atr(df['high'], df['low'], df['close'], period=50)
    """
    result = atr_kernel(
        high.to_numpy(dtype=float),
        low.to_numpy(dtype=float),
        close.to_numpy(dtype=float),
        period,
    )
    return pd.Series(result, index=close.index, name=f"atr_{period}")


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 12,
    multiplier: float = 3.0,
) -> Tuple[pd.Series, pd.Series]:
    """
    SuperTrend indicator — SystemC default: ST(12, 3.0).

    Algorithm
    ---------
    HL2        = (high + low) / 2
    basic_ub   = HL2 + multiplier * ATR(period)
    basic_lb   = HL2 - multiplier * ATR(period)
    final_ub / final_lb are adjusted so the bands only tighten, never widen,
    while price stays on the same side.
    Direction flips when close crosses the active band.

    Parameters
    ----------
    high, low, close : pd.Series
    period : int
        ATR period (default 12).
    multiplier : float
        ATR multiplier (default 3.0).

    Returns
    -------
    st_line : pd.Series
        SuperTrend line value (the active support/resistance level).
    st_dir : pd.Series
        Direction: +1 = bullish (price above ST), -1 = bearish (price below ST).
        NaN during warm-up.

    Usage
    -----
    df['st_line'], df['st_dir'] = supertrend(df['high'], df['low'], df['close'])
    df['st_line_1h'], df['st_dir_1h'] = supertrend(
        df['high'], df['low'], df['close'], period=12, multiplier=3.0
    )
    """
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    st_line, st_dir = supertrend_kernel(h, l, c, period, multiplier)

    return (
        pd.Series(st_line, index=close.index, name=f"st_line_{period}_{multiplier}"),
        pd.Series(st_dir,  index=close.index, name=f"st_dir_{period}_{multiplier}"),
    )


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (RSI) — Wilder's smoothed method.

    RS  = avg_gain / avg_loss  (Wilder RMA over `period` bars)
    RSI = 100 - 100 / (1 + RS)

    SystemC A1 V2 gate uses RSI(30):
        long  entries require RSI > 55
        short entries require RSI < 48

    Parameters
    ----------
    close : pd.Series
        Closing prices.
    period : int
        Lookback period (default 14; use 30 for SystemC A1).

    Returns
    -------
    pd.Series
        RSI values in [0, 100]. NaN for the first `period` rows (warm-up).

    Usage
    -----
    df['rsi30'] = rsi(df['close'], period=30)
    df['rsi14'] = rsi(df['close'])
    """
    prices = close.to_numpy(dtype=float)
    result = rsi_kernel(prices, period)
    return pd.Series(result, index=close.index, name=f"rsi_{period}")
