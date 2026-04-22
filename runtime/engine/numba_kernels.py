"""Numba-accelerated numeric kernels for the backtester."""

from __future__ import annotations

import numpy as np
from numba import njit


@njit
def ema_kernel(prices: np.ndarray, period: int) -> np.ndarray:
    n = len(prices)
    result = np.empty(n, dtype=np.float64)
    result[:] = np.nan
    if n < period:
        return result

    seed = period - 1
    total = 0.0
    for i in range(period):
        total += prices[i]
    result[seed] = total / period

    multiplier = 2.0 / (period + 1.0)
    for i in range(seed + 1, n):
        result[i] = (prices[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


@njit
def atr_kernel(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    n = len(close)
    tr = np.empty(n, dtype=np.float64)
    tr[:] = np.nan
    if n == 0:
        return tr

    tr[0] = high[0] - low[0]
    for i in range(1, n):
        v1 = high[i] - low[i]
        v2 = abs(high[i] - close[i - 1])
        v3 = abs(low[i] - close[i - 1])
        tr[i] = max(v1, v2, v3)

    result = np.empty(n, dtype=np.float64)
    result[:] = np.nan
    if n < period:
        return result

    total = 0.0
    for i in range(period):
        total += tr[i]
    result[period - 1] = total / period

    alpha = 1.0 / period
    for i in range(period, n):
        result[i] = result[i - 1] + alpha * (tr[i] - result[i - 1])
    return result


@njit
def rsi_kernel(prices: np.ndarray, period: int) -> np.ndarray:
    n = len(prices)
    result = np.empty(n, dtype=np.float64)
    result[:] = np.nan
    if n < period + 1:
        return result

    avg_gain = 0.0
    avg_loss = 0.0
    for i in range(1, period + 1):
        delta = prices[i] - prices[i - 1]
        if delta > 0.0:
            avg_gain += delta
        else:
            avg_loss -= delta
    avg_gain /= period
    avg_loss /= period

    if avg_loss == 0.0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - 100.0 / (1.0 + rs)

    alpha = 1.0 / period
    for i in range(period + 1, n):
        delta = prices[i] - prices[i - 1]
        gain = delta if delta > 0.0 else 0.0
        loss = -delta if delta < 0.0 else 0.0
        avg_gain = avg_gain + alpha * (gain - avg_gain)
        avg_loss = avg_loss + alpha * (loss - avg_loss)
        if avg_loss == 0.0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100.0 - 100.0 / (1.0 + rs)
    return result


@njit
def supertrend_kernel(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int,
    multiplier: float,
) -> tuple[np.ndarray, np.ndarray]:
    n = len(close)
    st_line = np.empty(n, dtype=np.float64)
    st_dir = np.empty(n, dtype=np.float64)
    final_ub = np.empty(n, dtype=np.float64)
    final_lb = np.empty(n, dtype=np.float64)
    st_line[:] = np.nan
    st_dir[:] = np.nan
    final_ub[:] = np.nan
    final_lb[:] = np.nan

    atr_vals = atr_kernel(high, low, close, period)
    start = period - 1
    if start >= n:
        return st_line, st_dir

    hl2 = (high[start] + low[start]) / 2.0
    final_ub[start] = hl2 + multiplier * atr_vals[start]
    final_lb[start] = hl2 - multiplier * atr_vals[start]

    if close[start] <= final_ub[start]:
        st_dir[start] = 1.0
        st_line[start] = final_lb[start]
    else:
        st_dir[start] = -1.0
        st_line[start] = final_ub[start]

    for i in range(start + 1, n):
        hl2 = (high[i] + low[i]) / 2.0
        basic_ub = hl2 + multiplier * atr_vals[i]
        basic_lb = hl2 - multiplier * atr_vals[i]

        if basic_ub < final_ub[i - 1] or close[i - 1] > final_ub[i - 1]:
            final_ub[i] = basic_ub
        else:
            final_ub[i] = final_ub[i - 1]

        if basic_lb > final_lb[i - 1] or close[i - 1] < final_lb[i - 1]:
            final_lb[i] = basic_lb
        else:
            final_lb[i] = final_lb[i - 1]

        if st_dir[i - 1] == 1.0:
            if close[i] < final_lb[i]:
                st_dir[i] = -1.0
                st_line[i] = final_ub[i]
            else:
                st_dir[i] = 1.0
                st_line[i] = final_lb[i]
        else:
            if close[i] > final_ub[i]:
                st_dir[i] = 1.0
                st_line[i] = final_lb[i]
            else:
                st_dir[i] = -1.0
                st_line[i] = final_ub[i]
    return st_line, st_dir


@njit
def st_stable_kernel(st_dir: np.ndarray, bars: int) -> np.ndarray:
    n = len(st_dir)
    stable = np.zeros(n, dtype=np.bool_)
    for i in range(bars - 1, n):
        last = st_dir[i]
        if np.isnan(last):
            continue
        ok = True
        for j in range(i - bars + 1, i):
            if np.isnan(st_dir[j]) or st_dir[j] != last:
                ok = False
                break
        stable[i] = ok
    return stable


@njit
def st_step_count_kernel(st_line: np.ndarray, st_dir: np.ndarray) -> np.ndarray:
    n = len(st_dir)
    counts = np.zeros(n, dtype=np.int32)
    for i in range(1, n):
        d = st_dir[i]
        if np.isnan(d) or np.isnan(st_line[i]):
            continue
        if st_dir[i - 1] != d:
            counts[i] = 0
            continue
        prev = st_line[i - 1]
        curr = st_line[i]
        step = 0
        if not (np.isnan(prev) or np.isnan(curr)):
            if d == 1.0 and curr > prev:
                step = 1
            elif d == -1.0 and curr < prev:
                step = 1
        counts[i] = counts[i - 1] + step
    return counts
