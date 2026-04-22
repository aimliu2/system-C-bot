"""Execution layer for System C bot V2.

Live order placement is intentionally guarded while the V2 engine is being
validated. Paper execution writes coherent open trade state and trade rows with
the final trade-log schema.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable

from runtime.adapters import Mt5Adapter
from runtime.config import RuntimeConfig
from runtime.logging import RuntimeLogger
from runtime.notifications import RuntimeNotifier


class ExecutionEngine:
    def __init__(
        self,
        cfg: RuntimeConfig,
        logger: RuntimeLogger,
        adapter: Mt5Adapter | None = None,
        state_saver: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.cfg = cfg
        self.logger = logger
        self.adapter = adapter
        self.state_saver = state_saver
        self.notifier = RuntimeNotifier(cfg, logger)

    def execute(self, accepted: list[dict[str, Any]], state: dict[str, Any], *, mode: str, live_enabled: bool) -> None:
        for candidate in accepted:
            if mode == "paper" or not live_enabled:
                self._paper_open(candidate, state)
            else:
                self._live_open(candidate, state)

    def _paper_open(self, candidate: dict[str, Any], state: dict[str, Any]) -> None:
        portfolio = state["portfolio"]
        ticket = int(portfolio.get("next_paper_ticket", -1))
        portfolio["next_paper_ticket"] = ticket - 1
        now = datetime.now(timezone.utc).isoformat()
        trade = {
            "ticket": ticket,
            "mode": "paper",
            "symbol": candidate["symbol"],
            "candidate_id": candidate["candidate_id"],
            "hypothesis": candidate["hypothesis"],
            "direction": candidate["direction"],
            "session": candidate.get("session", ""),
            "entry_price": candidate.get("entry_price"),
            "sl_price": candidate.get("sl"),
            "tp_price": candidate.get("tp"),
            "lot_size": candidate.get("lot_size", ""),
            "open_time": now,
            "bar_time": candidate.get("bar_time"),
            "bars_held": 0,
        }
        state.setdefault("open_trades", []).append(trade)
        self.logger.event("ORDER_PAPER", symbol=candidate["symbol"], detail=candidate["candidate_id"])
        self.logger.trade({
            "open_time": now,
            "close_time": "",
            "symbol": candidate["symbol"],
            "candidate_id": candidate["candidate_id"],
            "hypothesis": candidate["hypothesis"],
            "direction": candidate["direction"],
            "session": candidate.get("session", ""),
            "entry_price": candidate.get("entry_price", ""),
            "exit_price": "",
            "sl": candidate.get("sl", ""),
            "tp": candidate.get("tp", ""),
            "r_result": "",
            "risk_pct": self.cfg.base_risk_pct,
            "decision": "paper_open",
            "portfolio_open_count": len(state.get("open_trades", [])),
            "exit_reason": "",
            "ticket": ticket,
            "mode": "paper",
            "lot_size": candidate.get("lot_size", ""),
        })
        self.notifier.trade_opened(trade, mode="paper")

    def _live_open(self, candidate: dict[str, Any], state: dict[str, Any]) -> None:
        if self.adapter is None:
            self.logger.event("ORDER_FAIL", symbol=candidate.get("symbol", ""), detail="missing MT5 adapter")
            return

        symbol = candidate["symbol"]
        tick = self.adapter.symbol_info_tick(symbol)
        info = self.adapter.symbol_info(symbol)
        account = self.adapter.account_info()
        if not tick or not info or not account:
            self.logger.event("ORDER_FAIL", symbol=symbol, detail="missing tick/symbol/account info")
            return

        direction = candidate["direction"]
        price = float(_get(tick, "ask")) if direction == "long" else float(_get(tick, "bid"))
        digits = int(_get(info, "digits", 5))
        sl_price = float(candidate["sl"])
        tp_price = float(candidate["tp"])
        if not self._stop_distance_ok(info, price, sl_price, tp_price):
            self.logger.event("SLTP_TOO_CLOSE", symbol=symbol, detail=f"price={price} sl={sl_price} tp={tp_price}")
            return

        lot = self._lot_size(symbol, info, account, sl_price, price)
        if lot is None:
            self.logger.event("LOT_SIZE_FAIL", symbol=symbol, detail="risk lot calculation returned none")
            return

        mt5 = getattr(self.adapter, "mt5", None)
        request = {
            "action": _const(mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": symbol,
            "volume": lot,
            "type": _const(mt5, "ORDER_TYPE_BUY", 0) if direction == "long" else _const(mt5, "ORDER_TYPE_SELL", 1),
            "price": price,
            "sl": round(sl_price, digits),
            "tp": round(tp_price, digits),
            "deviation": int(self.cfg.raw.get("execution", {}).get("deviation_points", 10)),
            "magic": self.cfg.magic_number,
            "comment": self._comment(candidate),
            "type_filling": self._filling_mode(info),
            "type_time": _const(mt5, "ORDER_TIME_GTC", 0),
        }
        result = self.adapter.order_send(request)
        retcode = int(_get(result, "retcode", -1))
        if retcode != int(_const(mt5, "TRADE_RETCODE_DONE", 10009)):
            self.logger.event("ORDER_FAIL", symbol=symbol, detail=f"retcode={retcode} result={result}")
            return

        fill_price = float(_get(result, "price", 0.0) or 0.0)
        if fill_price == 0.0 and self.cfg.raw.get("execution", {}).get("fill_price_fallback") == "requested_price":
            fill_price = price
        ticket = int(_get(result, "order", _get(result, "deal", 0)) or 0)
        if ticket <= 0:
            self.logger.event("ORDER_FAIL", symbol=symbol, detail=f"retcode={retcode} missing ticket result={result}")
            return
        now = datetime.now(timezone.utc).isoformat()
        trade = {
            "ticket": ticket,
            "mode": "live",
            "symbol": symbol,
            "candidate_id": candidate["candidate_id"],
            "hypothesis": candidate["hypothesis"],
            "direction": direction,
            "session": candidate.get("session", ""),
            "entry_price": fill_price,
            "sl_price": round(sl_price, digits),
            "tp_price": round(tp_price, digits),
            "lot_size": lot,
            "open_time": now,
            "bar_time": candidate.get("bar_time"),
            "bars_held": 0,
        }
        state.setdefault("open_trades", []).append(trade)
        self._save_after_live_open(state, symbol=symbol, ticket=ticket)
        self.logger.event("ORDER_LIVE", symbol=symbol, detail=f"ticket={ticket} candidate={candidate['candidate_id']}")
        self.logger.trade({
            "open_time": now,
            "close_time": "",
            "symbol": symbol,
            "candidate_id": candidate["candidate_id"],
            "hypothesis": candidate["hypothesis"],
            "direction": direction,
            "session": candidate.get("session", ""),
            "entry_price": fill_price,
            "exit_price": "",
            "sl": round(sl_price, digits),
            "tp": round(tp_price, digits),
            "r_result": "",
            "risk_pct": self.cfg.base_risk_pct,
            "decision": "live_open",
            "portfolio_open_count": len(state.get("open_trades", [])),
            "exit_reason": "",
            "ticket": ticket,
            "mode": "live",
            "lot_size": lot,
        })
        self.notifier.trade_opened(trade, mode="live")

    def _save_after_live_open(self, state: dict[str, Any], *, symbol: str, ticket: int) -> None:
        if self.state_saver is None:
            return
        try:
            self.state_saver(state)
        except Exception as exc:
            self.logger.event("LIVE_STATE_SAVE_FAILED", symbol=symbol, detail=f"ticket={ticket} error={exc}")
            raise

    def _lot_size(self, symbol: str, info: dict[str, Any], account: dict[str, Any], sl_price: float, entry_price: float) -> float | None:
        equity = float(_get(account, "equity", 0.0))
        risk_usd = equity * self.cfg.base_risk_pct / 100.0
        pip_size = 0.01 if symbol.endswith("JPY") else 0.0001
        sl_pips = abs(entry_price - sl_price) / pip_size
        tick_size = float(_get(info, "trade_tick_size", 0.0))
        tick_value = float(_get(info, "trade_tick_value", 0.0))
        if equity <= 0 or sl_pips <= 0 or tick_size <= 0 or tick_value <= 0:
            return None
        pip_value_per_lot = (pip_size / tick_size) * tick_value
        raw_lots = risk_usd / (sl_pips * pip_value_per_lot)
        step = float(_get(info, "volume_step", 0.01))
        lot = math.floor(raw_lots / step) * step
        if lot < float(_get(info, "volume_min", 0.01)):
            return None
        lot = min(float(_get(info, "volume_max", lot)), lot)
        return round(lot, _decimal_places(step))

    def _filling_mode(self, info: dict[str, Any]) -> int:
        mt5 = getattr(self.adapter, "mt5", None)
        filling_mode = int(_get(info, "filling_mode", 1))
        if filling_mode & 1:
            return int(_const(mt5, "ORDER_FILLING_FOK", 0))
        if filling_mode & 2:
            return int(_const(mt5, "ORDER_FILLING_IOC", 1))
        return int(_const(mt5, "ORDER_FILLING_RETURN", 2))

    @staticmethod
    def _stop_distance_ok(info: dict[str, Any], entry_price: float, sl_price: float, tp_price: float) -> bool:
        min_dist = float(_get(info, "trade_stops_level", 0.0)) * float(_get(info, "point", 0.0))
        if min_dist <= 0:
            return True
        return abs(entry_price - sl_price) >= min_dist and abs(entry_price - tp_price) >= min_dist

    def _comment(self, candidate: dict[str, Any]) -> str:
        session = str(candidate.get("session") or "NA")
        hypothesis = str(candidate.get("hypothesis") or "NA")
        symbol = str(candidate.get("symbol") or "")
        return f"SysC-{symbol}-{session}-{hypothesis}"[:31]


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _const(mt5: Any, name: str, default: Any) -> Any:
    if mt5 is None:
        return default
    return getattr(mt5, name, default)


def _decimal_places(step: float) -> int:
    text = f"{step:.10f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])
