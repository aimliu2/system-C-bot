"""Notification boundary for System C bot V2."""

from __future__ import annotations

from typing import Any

import requests

from runtime.config import RuntimeConfig
from runtime.logging import RuntimeLogger


class RuntimeNotifier:
    def __init__(self, cfg: RuntimeConfig, logger: RuntimeLogger):
        self.cfg = cfg
        self.logger = logger

    def trade_opened(self, trade: dict[str, Any], *, mode: str) -> None:
        if not self.cfg.notifications_enabled_for(mode):
            return
        try:
            self._send(self._trade_opened_text(trade, mode=mode))
            self.logger.event("NOTIFY_TRADE_OPENED", symbol=str(trade.get("symbol", "")), detail=f"mode={mode}")
        except Exception as exc:
            self.logger.event("NOTIFY_FAILED", symbol=str(trade.get("symbol", "")), detail=str(exc))

    def daily_status(self, payload: dict[str, Any]) -> bool:
        if not self.cfg.daily_status_notifications_enabled():
            return False
        try:
            self._send(self._daily_status_text(payload))
            self.logger.event("NOTIFY_DAILY_STATUS", detail=f"date={payload.get('date_utc', '')}")
            return True
        except Exception as exc:
            self.logger.event("NOTIFY_FAILED", detail=f"daily_status: {exc}")
            return False

    def _send(self, text: str) -> None:
        token = self.cfg.secrets.get("TELEGRAM_TOKEN", "")
        chat_id = self.cfg.secrets.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            self.logger.event("NOTIFY_SKIPPED", detail="missing telegram token/chat id")
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=5,
        )

    @staticmethod
    def _trade_opened_text(trade: dict[str, Any], *, mode: str) -> str:
        symbol = str(trade.get("symbol", ""))
        direction = str(trade.get("direction", ""))
        hypothesis = str(trade.get("hypothesis", ""))
        session = str(trade.get("session", ""))
        entry = float(trade.get("entry_price") or 0.0)
        sl = float(trade.get("sl_price") or 0.0)
        tp = float(trade.get("tp_price") or 0.0)
        lot = float(trade.get("lot_size") or 0.0)
        direction_label = direction.upper()
        side_label = "LONG" if direction == "long" else "SHORT"
        mode_tag = "  <i>[paper]</i>" if mode == "paper" else ""
        digits = 3 if "JPY" in symbol else 5
        return (
            f"<b>{side_label} {symbol}</b> - {session} [{hypothesis}]{mode_tag}\n"
            f"Direction: {direction_label}\n"
            f"Entry: {entry:.{digits}f}\n"
            f"SL: {sl:.{digits}f}\n"
            f"TP: {tp:.{digits}f}\n"
            f"Lot: {lot:.2f}"
        )

    @staticmethod
    def _daily_status_text(payload: dict[str, Any]) -> str:
        symbols = payload.get("symbols", [])
        if isinstance(symbols, (list, tuple)):
            symbols_text = ", ".join(str(symbol) for symbol in symbols)
        else:
            symbols_text = str(symbols)
        return (
            "<b>System C V2 Daily Status</b>\n"
            f"Time: {payload.get('time_utc', '')} UTC\n"
            f"Mode: {payload.get('mode', '')}\n"
            f"Deployment: {payload.get('deployment', '')}\n"
            f"Symbols: {symbols_text}\n"
            f"Equity: {payload.get('equity', 'N/A')}  Balance: {payload.get('balance', 'N/A')}\n"
            f"Open trades: {payload.get('open_trades', 0)}/{payload.get('portfolio_cap', 0)} "
            f"Broker positions: {payload.get('broker_positions', 0)}\n"
            f"Market data: {payload.get('market_data', 'UNKNOWN')}\n"
            f"Latest entry bar: {payload.get('latest_entry_bar', 'N/A')}\n"
            f"GPS: {payload.get('gps', 'UNKNOWN')}"
        )
