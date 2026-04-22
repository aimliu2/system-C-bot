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
