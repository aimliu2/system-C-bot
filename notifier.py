"""
notifier.py — Telegram notification sender for System C
v1.0 · April 2026

All functions are fire-and-forget.
A notification failure NEVER crashes the bot.

Bot token + chat ID read from .ennv (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID).

Message types:
  trade_opened  — real-time, every trade (paper + live)
  trade_closed  — real-time, every trade (TP / SL / TIMEOUT)
  london_open   — 07:00 UTC morning briefing + batched overnight NY summary
  cb_triggered  — circuit-breaker triggered (session skipped)
"""

import os
import requests

_TOKEN   = None
_CHAT_ID = None


def _init():
    global _TOKEN, _CHAT_ID
    if _TOKEN is None:
        _TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
        _CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send(text: str) -> None:
    """Send a plain-text message. HTML tags supported. Fire-and-forget."""
    _init()
    if not _TOKEN or not _CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            json={
                "chat_id"   : _CHAT_ID,
                "text"      : text,
                "parse_mode": "HTML",
            },
            timeout=5,
        )
    except Exception:
        pass  # never propagate — notification is non-critical


# ─────────────────────────────────────────────────────────────────────────────
# Message formatters
# ─────────────────────────────────────────────────────────────────────────────

def trade_opened(
    symbol: str,
    direction: str,
    hypothesis: str,
    session: str,
    regime: str,
    entry: float,
    sl: float,
    tp: float,
    lot: float,
    mode: str = "live",
) -> None:
    """
    Sent immediately after a trade is placed (paper or live).

    Args:
        direction  : "long" or "short"
        hypothesis : "A1", "A2", or "B"
        session    : "London" or "NY"
        regime     : e.g. "BULLISH", "BEARISH", "ACCUMULATION"
        mode       : "live" or "paper"
    """
    dir_upper = direction.upper()
    emoji     = "🟢" if direction == "long" else "🔴"
    mode_tag  = "  <i>[paper]</i>" if mode == "paper" else ""
    digs      = 3 if "JPY" in symbol else 5

    send(
        f"{emoji} <b>{dir_upper} {symbol}</b> — {session}  [{hypothesis}]{mode_tag}\n"
        f"Entry  : {entry:.{digs}f}\n"
        f"SL     : {sl:.{digs}f}\n"
        f"TP     : {tp:.{digs}f}\n"
        f"Lot    : {lot:.2f}  |  Regime: {regime}"
    )


def trade_closed(
    symbol: str,
    direction: str,
    hypothesis: str,
    session: str,
    exit_reason: str,
    pnl_r: float,
    hw_window: list,
) -> None:
    """
    Sent immediately after a trade is closed.

    Args:
        hw_window : list of 1/0 outcomes — used to compute running WR display
    """
    if exit_reason == "TP":
        emoji = "✅"
        label = "TP HIT"
    elif exit_reason == "SL":
        emoji = "❌"
        label = "SL HIT"
    elif exit_reason == "TIMEOUT":
        emoji = "⏱"
        label = "TIMEOUT"
    else:
        emoji = "🔲"
        label = f"CLOSED ({exit_reason})"

    sign = "+" if pnl_r >= 0 else ""

    # Running WR from window
    if hw_window:
        wr_pct  = sum(hw_window) / len(hw_window) * 100
        wr_line = f"\nHW WR  : {wr_pct:.1f}%  ({sum(hw_window)}/{len(hw_window)})"
    else:
        wr_line = ""

    send(
        f"{emoji} <b>{label} — {symbol} {session} [{hypothesis}]</b>\n"
        f"{direction.upper()}  P&amp;L: <b>{sign}{pnl_r:.2f}R</b>"
        f"{wr_line}"
    )


def london_open(state: dict) -> None:
    """
    Sent at London Open (07:00 UTC).
    Batches overnight NY session summary from state["ny_session_summary"].
    Reads state["instrument_highwind"] for per-instrument HW level + WR.
    Reads state["rule2"] and state["cb_anchor"] for guard status.
    After sending, ny_session_summary is NOT cleared here — caller resets it on NY open.
    """
    # ── NY summary (all instruments combined) ────────────────────────────────
    ny_data    = state.get("ny_session_summary", {})
    ny_trades  = sum(v.get("trades", 0) for v in ny_data.values() if isinstance(v, dict))
    ny_wins    = sum(v.get("wins",   0) for v in ny_data.values() if isinstance(v, dict))
    ny_pnl     = sum(v.get("pnl_r", 0.0) for v in ny_data.values() if isinstance(v, dict))
    ny_losses  = ny_trades - ny_wins

    # ── HW summary per instrument ─────────────────────────────────────────────
    inst_hw    = state.get("instrument_highwind", {})
    hw_parts   = []
    for sym, hw in inst_hw.items():
        level  = hw.get("level", "NORMAL")
        window = hw.get("window", [])
        if window:
            wr_str = f"{sum(window)/len(window):.1%}"
        else:
            wr_str = "N/A"
        hw_parts.append(f"{sym}: {level} ({wr_str})")
    hw_summary = "\n  ".join(hw_parts) if hw_parts else "N/A"

    # ── Guards ────────────────────────────────────────────────────────────────
    cb   = state.get("cb_anchor", {})
    cb_ok = not cb.get("triggered_session", False)
    r2   = state.get("rule2", {})
    r2_ok = not r2.get("triggered_today", False)

    lines = ["🌅 <b>London Open — System C</b>", "━━━━━━━━━━━━━━━━━━━"]

    if ny_trades > 0:
        sign = "+" if ny_pnl >= 0 else ""
        lines.append(
            f"🇺🇸 <b>NY (overnight):</b>\n"
            f"  Trades: {ny_trades}  TP:{ny_wins}  SL:{ny_losses}\n"
            f"  P&amp;L: <b>{sign}{ny_pnl:.3f}R</b>"
        )
        lines.append("━━━━━━━━━━━━━━━━━━━")

    lines.append(
        f"⚙️ <b>Bot Status:</b>\n"
        f"  {hw_summary}\n"
        f"  Rule2: {'✅' if r2_ok else '🔴'}  CB: {'✅' if cb_ok else '🔴'}"
    )
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("London session: 07:00–13:00 UTC  (14:00–20:00 BKK)")

    send("\n".join(lines))


def cb_triggered(equity: float, peak: float, anchor: float) -> None:
    """
    Sent when the circuit-breaker fires and the session is skipped.

    Args:
        equity : current account equity
        peak   : CB peak (high-water mark for this session)
        anchor : CB anchor (equity level reset was last anchored to)
    """
    send(
        f"⛔ <b>Circuit Breaker — Session Skipped</b>\n"
        f"Equity : ${equity:,.2f}\n"
        f"Peak   : ${peak:,.2f}\n"
        f"Anchor : ${anchor:,.2f}\n"
        f"<i>All instruments skipped until next session.</i>"
    )
