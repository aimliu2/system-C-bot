"""Portfolio reducer for System C bot V2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runtime.config import RuntimeConfig


@dataclass
class Reduction:
    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]


class PortfolioReducer:
    def __init__(self, cfg: RuntimeConfig):
        self.cfg = cfg

    def reduce(self, candidates: list[dict[str, Any]], state: dict[str, Any]) -> Reduction:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        open_trades = state.get("open_trades", [])
        open_count = len(open_trades)
        cap = self.cfg.portfolio_cap
        priority = {"B": 0, "A2": 1, "A1": 2}
        ordered = sorted(
            candidates,
            key=lambda c: (c.get("bar_time", ""), c.get("symbol", ""), priority.get(c.get("hypothesis"), 99)),
        )

        for candidate in ordered:
            symbol = candidate["symbol"]
            symbol_open_count = sum(1 for trade in open_trades if trade.get("symbol") == symbol)
            record = {
                **candidate,
                "portfolio_open_count_before": open_count + len(accepted),
                "symbol_open_count_before": symbol_open_count,
            }
            if state.get("portfolio", {}).get("rule2", {}).get("triggered_today"):
                rejected.append({**record, "decision": "rejected", "reject_reason": "rule2_block"})
                continue
            if open_count + len(accepted) >= cap:
                rejected.append({**record, "decision": "rejected", "reject_reason": "portfolio_cap_full"})
                continue
            if symbol_open_count > 0:
                rejected.append({**record, "decision": "rejected", "reject_reason": "symbol_cap_full"})
                continue
            accepted.append({**record, "decision": "accepted", "reject_reason": ""})

        return Reduction(accepted=accepted, rejected=rejected)
