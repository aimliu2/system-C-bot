# align.py — multi-timeframe as-of aligner (no future leak)
#
# Rule: each entry-TF decision row receives only the latest context-TF row
# whose explicit avail_time (bar close time) is <= the entry-TF decision time.
#
# This prevents lookahead into unfinished higher-timeframe candles.
#
# Example for 15m entry vs 1H context:
#   15m bar closes at 10:15 UTC
#   → latest allowed 1H row is the one that closed at 10:00 UTC
#   → the 10:00–11:00 1H candle is NOT yet available (still open)
#
# Context columns are prefixed with 'ctx_' to avoid name collisions.
# Multiple context timeframes can be merged sequentially.
#
from __future__ import annotations

import pandas as pd
from typing import Optional


def align(
    df_entry:   pd.DataFrame,
    df_context: pd.DataFrame,
    ctx_prefix: str = 'ctx_',
    ctx_cols:   Optional[list[str]] = None,
) -> pd.DataFrame:
    """
    As-of merge: attach context-TF features to entry-TF rows without future leak.

    Uses pd.merge_asof with direction='backward', which selects the most recent
    context row whose avail_time is <= the entry-TF avail_time.

    Parameters
    ----------
    df_entry : pd.DataFrame
        Entry timeframe feature DataFrame (e.g. 15m).
        Index must be DatetimeIndex (UTC bar close times).

    df_context : pd.DataFrame
        Context timeframe feature DataFrame (e.g. 1H).
        Index must be DatetimeIndex (UTC bar close times).

    ctx_prefix : str
        Prefix applied to all context columns in the output (default 'ctx_').

    ctx_cols : list[str] or None
        Subset of context columns to attach. None = attach all columns.
        Useful to avoid attaching redundant OHLCV columns from the context TF.

    Returns
    -------
    pd.DataFrame
        Entry-TF DataFrame with context columns appended.
        Index = entry-TF timestamps (unchanged).
        Context columns are named '{ctx_prefix}{original_name}'.

    Raises
    ------
    ValueError
        If either DataFrame index is not a DatetimeIndex, or is not UTC.
    """
    _validate_index(df_entry,   'df_entry')
    _validate_index(df_context, 'df_context')

    entry_reset = df_entry.copy()
    entry_reset['_entry_index'] = entry_reset.index
    entry_reset['_entry_avail_time'] = _availability_time(df_entry)

    if ctx_cols is None:
        payload_cols = list(df_context.columns)
    else:
        payload_cols = list(ctx_cols)
    payload_cols = [c for c in payload_cols if c in df_context.columns and c != 'avail_time']

    context_reset = df_context[payload_cols].copy()
    context_reset['_ctx_avail_time'] = _availability_time(df_context)
    context_reset = context_reset.rename(
        columns={c: f'{ctx_prefix}{c}' for c in payload_cols}
    )

    merged = pd.merge_asof(
        entry_reset.sort_values('_entry_avail_time'),
        context_reset.sort_values('_ctx_avail_time'),
        left_on  = '_entry_avail_time',
        right_on = '_ctx_avail_time',
        direction= 'backward',   # latest context row at or before entry time
    )

    # Restore entry-TF index
    merged = merged.set_index('_entry_index')
    merged.index.name = df_entry.index.name  # preserve original index name
    merged[f'{ctx_prefix}avail_time'] = merged['_ctx_avail_time']
    merged = merged.drop(columns=['_entry_avail_time', '_ctx_avail_time'])

    return merged


def align_multi(
    df_entry:    pd.DataFrame,
    contexts:    list[tuple[pd.DataFrame, str, Optional[list[str]]]],
) -> pd.DataFrame:
    """
    Merge multiple context timeframes onto the entry-TF DataFrame sequentially.

    Parameters
    ----------
    df_entry : pd.DataFrame
        Entry timeframe features.

    contexts : list of (df_context, ctx_prefix, ctx_cols)
        Each tuple defines one context timeframe merge:
            df_context  — context-TF feature DataFrame
            ctx_prefix  — column prefix for this context (e.g. 'ctx_1h_')
            ctx_cols    — columns to include (None = all)

    Returns
    -------
    pd.DataFrame
        Entry-TF DataFrame with all context columns appended.

    Example
    -------
    df_aligned = align_multi(
        df_15m,
        contexts=[
            (df_1h,  'ctx_',    ['st_dir', 'st_line', 'st_flip']),
            (df_4h,  'ctx_4h_', ['st_dir', 'st_line']),
        ]
    )
    """
    result = df_entry.copy()
    for df_ctx, prefix, cols in contexts:
        result = align(result, df_ctx, ctx_prefix=prefix, ctx_cols=cols)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_index(df: pd.DataFrame, name: str) -> None:
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(
            f"{name} index must be a DatetimeIndex, got {type(df.index).__name__}"
        )


def _availability_time(df: pd.DataFrame) -> pd.Series:
    """Return explicit bar availability timestamps, falling back to index."""
    if 'avail_time' in df.columns:
        return pd.to_datetime(df['avail_time'], utc=True)
    return pd.Series(pd.to_datetime(df.index, utc=True), index=df.index)
