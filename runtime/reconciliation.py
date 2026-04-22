"""Broker/state reconciliation for System C bot V2."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from runtime.adapters import Mt5Adapter
from runtime.config import RuntimeConfig


@dataclass
class ReconciliationResult:
    closed_tickets: set[int] = field(default_factory=set)
    close_rows: list[dict[str, Any]] = field(default_factory=list)
    orphan_positions: list[dict[str, Any]] = field(default_factory=list)
    history_errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.closed_tickets)


class BrokerReconciler:
    def __init__(self, cfg: RuntimeConfig, adapter: Mt5Adapter):
        self.cfg = cfg
        self.adapter = adapter
        self.mt5 = getattr(adapter, "mt5", None)

    def reconcile(self, state: dict[str, Any], broker_positions: list[dict[str, Any]]) -> ReconciliationResult:
        result = ReconciliationResult()
        broker_magic_positions = [
            position for position in broker_positions
            if _int(_get(position, "magic")) == self.cfg.magic_number
        ]
        broker_by_ticket = {
            ticket: position
            for position in broker_magic_positions
            for ticket in [_ticket(position)]
            if ticket is not None
        }

        state_live_trades = [
            trade for trade in state.get("open_trades", [])
            if str(trade.get("mode", state.get("mode", ""))) == "live"
            and _ticket(trade) is not None
            and int(_ticket(trade) or 0) > 0
        ]
        state_by_ticket = {int(_ticket(trade) or 0): trade for trade in state_live_trades}

        for ticket in sorted(set(state_by_ticket) - set(broker_by_ticket)):
            trade = state_by_ticket[ticket]
            close_info = self._close_info(ticket)
            if close_info.get("history_error"):
                result.history_errors.append(f"{ticket}:{close_info['history_error']}")
            result.close_rows.append(self._close_row(trade, close_info, state))
            result.closed_tickets.add(ticket)

        for ticket in sorted(set(broker_by_ticket) - set(state_by_ticket)):
            result.orphan_positions.append(broker_by_ticket[ticket])

        if result.closed_tickets:
            state["open_trades"] = [
                trade for trade in state.get("open_trades", [])
                if int(_ticket(trade) or 0) not in result.closed_tickets
            ]

        return result

    def _close_info(self, ticket: int) -> dict[str, Any]:
        try:
            deals = self.adapter.history_deals_get(position=ticket) or []
        except Exception as exc:
            return {
                "exit_reason": "UNKNOWN",
                "exit_price": "",
                "close_time": datetime.now(timezone.utc).isoformat(),
                "entry_price_hist": None,
                "history_error": str(exc),
            }
        if not deals:
            return {
                "exit_reason": "UNKNOWN",
                "exit_price": "",
                "close_time": datetime.now(timezone.utc).isoformat(),
                "entry_price_hist": None,
                "history_error": "no history deals",
            }

        entry_price_hist = None
        exit_deal = None
        for deal in deals:
            entry = _int(_get(deal, "entry"))
            price = _float(_get(deal, "price"))
            if entry == 0 and price not in (None, 0.0):
                entry_price_hist = price
            if entry in {1, 3}:
                exit_deal = deal

        if exit_deal is None:
            return {
                "exit_reason": "UNKNOWN",
                "exit_price": "",
                "close_time": datetime.now(timezone.utc).isoformat(),
                "entry_price_hist": entry_price_hist,
                "history_error": "no exit deal",
            }

        return {
            "exit_reason": self._exit_reason(exit_deal),
            "exit_price": _float(_get(exit_deal, "price"), ""),
            "close_time": _deal_time(exit_deal),
            "entry_price_hist": entry_price_hist,
            "history_error": "",
        }

    def _exit_reason(self, deal: Any) -> str:
        reason = _get(deal, "reason")
        reason_int = _int(reason)
        if reason_int is None:
            text = str(reason or "").upper()
            if "SL" in text:
                return "SL"
            if "TP" in text:
                return "TP"
            if any(token in text for token in ("CLIENT", "MOBILE", "WEB", "EXPERT", "MANUAL")):
                return "MANUAL"
            return "UNKNOWN"

        if reason_int == _const_int(self.mt5, "DEAL_REASON_SL"):
            return "SL"
        if reason_int == _const_int(self.mt5, "DEAL_REASON_TP"):
            return "TP"
        manual_codes = {
            _const_int(self.mt5, "DEAL_REASON_CLIENT"),
            _const_int(self.mt5, "DEAL_REASON_MOBILE"),
            _const_int(self.mt5, "DEAL_REASON_WEB"),
            _const_int(self.mt5, "DEAL_REASON_EXPERT"),
        }
        if reason_int in manual_codes:
            return "MANUAL"
        return "UNKNOWN"

    def _close_row(self, trade: dict[str, Any], close_info: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        entry = _float(close_info.get("entry_price_hist"))
        if entry is None:
            entry = _float(trade.get("entry_price"), 0.0)
        exit_price = close_info.get("exit_price")
        r_result = ""
        if exit_price not in ("", None):
            r_result = _r_result(
                direction=str(trade.get("direction", "")),
                entry_price=float(entry or 0.0),
                exit_price=float(exit_price),
                sl_price=_float(trade.get("sl_price"), _float(trade.get("sl"), 0.0)) or 0.0,
            )

        return {
            "open_time": trade.get("open_time", ""),
            "close_time": close_info.get("close_time", ""),
            "symbol": trade.get("symbol", ""),
            "candidate_id": trade.get("candidate_id", ""),
            "hypothesis": trade.get("hypothesis", ""),
            "direction": trade.get("direction", ""),
            "session": trade.get("session", ""),
            "entry_price": entry,
            "exit_price": exit_price,
            "sl": trade.get("sl_price", trade.get("sl", "")),
            "tp": trade.get("tp_price", trade.get("tp", "")),
            "r_result": f"{r_result:.4f}" if isinstance(r_result, float) else "",
            "risk_pct": trade.get("risk_pct", self.cfg.base_risk_pct),
            "decision": "broker_closed",
            "portfolio_open_count": len(state.get("open_trades", [])) - 1,
            "exit_reason": close_info.get("exit_reason", "UNKNOWN"),
            "ticket": trade.get("ticket", ""),
            "mode": trade.get("mode", "live"),
            "lot_size": trade.get("lot_size", ""),
        }


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        if value in ("", None):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: Any = None) -> float | Any:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _ticket(value: Any) -> int | None:
    ticket = _int(_get(value, "ticket"))
    if ticket is not None:
        return ticket
    return _int(_get(value, "identifier"))


def _const_int(mt5: Any, name: str) -> int | None:
    if mt5 is None:
        return None
    return _int(getattr(mt5, name, None))


def _deal_time(deal: Any) -> str:
    time_msc = _int(_get(deal, "time_msc"))
    if time_msc:
        return datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc).isoformat()
    timestamp = _int(_get(deal, "time"))
    if timestamp:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _r_result(direction: str, entry_price: float, exit_price: float, sl_price: float) -> float:
    sl_dist = abs(entry_price - sl_price)
    if sl_dist == 0:
        return 0.0
    if direction == "long":
        return (exit_price - entry_price) / sl_dist
    return (entry_price - exit_price) / sl_dist
