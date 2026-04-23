"""Shared sequential portfolio runner for System C bot V2."""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime.adapters import Mt5Adapter
from runtime.broker_time import detect_broker_utc_offset
from runtime.config import RuntimeConfig, load_runtime_config
from runtime.data_cache import BarCache, CacheUpdate, normalize_timeframe
from runtime.engine_bridge import EngineBridge
from runtime.execution import ExecutionEngine
from runtime.gps import write_reports
from runtime.logging import RuntimeLogger
from runtime.notifications import RuntimeNotifier
from runtime.portfolio import PortfolioReducer
from runtime.reconciliation import BrokerReconciler, ReconciliationResult
from runtime.state_store import atomic_write_json, load_state, validate_state_shape


@dataclass
class LoopSummary:
    loop_id: str
    evaluated_symbols: int = 0
    skipped_disabled: int = 0
    skipped_no_new_bar: int = 0
    skipped_data_error: int = 0
    skipped_engine_not_ready: int = 0
    candidates: int = 0
    accepted: int = 0
    rejected: int = 0
    entry_bar_updates: int = 0


class SequentialPortfolioRunner:
    def __init__(self, cfg: RuntimeConfig, adapter: Mt5Adapter | None, *, dry_run: bool = False):
        self.cfg = cfg
        self.adapter = adapter
        self.dry_run = dry_run
        self.logger = RuntimeLogger(cfg)
        self.cache = BarCache(cfg)
        self.engine = EngineBridge(cfg, self.cache)
        self.reducer = PortfolioReducer(cfg)
        self.execution = ExecutionEngine(cfg, self.logger, adapter, state_saver=self.save_state)
        self.notifier = RuntimeNotifier(cfg, self.logger)
        self.reconciler = BrokerReconciler(cfg, adapter) if adapter is not None else None
        self._last_heartbeat_monotonic = 0.0

    def _state_mode(self) -> str:
        return "paper" if self.cfg.paper_mode else "live"

    def _state_path(self) -> Path:
        return self.cfg.get_state_file(self._state_mode())

    def load_state(self) -> dict[str, Any]:
        mode = self._state_mode()
        state = load_state(self.cfg.get_state_file(mode))
        validate_state_shape(self.cfg, state, mode)
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        if self.dry_run:
            return
        atomic_write_json(self._state_path(), state)

    def startup(self) -> dict[str, Any]:
        state = self.load_state()
        mode = self._state_mode()
        detail = f"adapter={self.adapter.name if self.adapter else 'none'} dry_run={self.dry_run} mode={mode}"
        self._print_startup_banner(state)
        self.logger.ensure_headers()
        self.logger.event("BOT_STARTED", detail=detail)
        self._log_deferred_performance_modes()
        if self.adapter is not None and not self.dry_run:
            self.adapter.connect()
            terminal = self.adapter.terminal_info()
            self.logger.event("MT5_CONNECTED", detail=str(terminal or {}))
            print("MT5 connected", flush=True)
            offset = detect_broker_utc_offset(self.adapter)
            self.cache.set_broker_utc_offset(offset.offset_hours)
            if self.reconciler is not None:
                self.reconciler.set_broker_utc_offset(offset.offset_hours)
            offset_detail = (
                f"offset_hours={offset.offset_hours} status={offset.status} "
                f"source_symbol={offset.source_symbol} broker_time={offset.broker_time} "
                f"utc_now={offset.utc_now} detail={offset.detail}"
            )
            self.logger.event("BROKER_TIME_OFFSET", detail=offset_detail)
            print(f"Broker UTC offset: UTC{offset.offset_hours:+d} ({offset.status})", flush=True)
            updates = self.cache.warm_start(self.adapter)
            warmed = sum(1 for update in updates if update.updated)
            failed = len(updates) - warmed
            for update in updates:
                event = "CACHE_WARMED" if update.updated else "CACHE_WARM_FAILED"
                detail = f"timeframe={update.timeframe} rows={update.rows}"
                if update.error:
                    detail = f"{detail} error={update.error}"
                self.logger.event(event, symbol=update.symbol, detail=detail)
                if update.error:
                    print(f"CACHE warning {update.symbol} {update.timeframe}: {update.error}", flush=True)
            print(f"Cache warmup complete: warmed={warmed} failed={failed}", flush=True)
            if self._seed_market_data_freshness_from_warmup(state, updates):
                self.save_state(state)
        else:
            self.logger.event("DRY_RUN_NO_MT5", detail="adapter connection skipped")
            print("DRY RUN: MT5 adapter connection skipped", flush=True)
        print("Main loop started. Create STOP file to exit cleanly.", flush=True)
        return state

    def broker_snapshot(self) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        if self.adapter is None or self.dry_run:
            return None, []
        account = self.adapter.account_info()
        positions = self.adapter.positions_get()
        return account, positions

    def run_once(self, state: dict[str, Any]) -> dict[str, Any]:
        loop_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        loop_started = time.perf_counter()
        summary = LoopSummary(loop_id=loop_id)
        account, broker_positions = self.broker_snapshot()
        self._print_heartbeat(
            state,
            loop_id=loop_id,
            account=account,
            broker_positions=broker_positions,
        )
        candidates: list[dict[str, Any]] = []
        proposed_engine_states: dict[str, dict[str, Any]] = {}
        updated_bar_times: dict[str, dict[str, str]] = {}
        reconciliation_blocked = self._reconcile_broker(state, broker_positions, loop_id)
        reconciliation_closed = bool(state.get("diagnostics", {}).pop("_reconciliation_closed_this_loop", False))

        for symbol in ([] if reconciliation_blocked else self.cfg.deployment_symbols):
            symbol_started = time.perf_counter()
            sym_state = state["symbols"][symbol]
            if sym_state.get("mode") == "disabled":
                summary.skipped_disabled += 1
                self.logger.event("SYMBOL_SKIPPED_DISABLED", loop_id=loop_id, symbol=symbol)
                continue

            if self.adapter is None or self.dry_run:
                summary.skipped_no_new_bar += 1
                self.logger.event("SYMBOL_SKIPPED_NO_NEW_BAR", loop_id=loop_id, symbol=symbol, detail="no MT5 adapter")
                self._log_signal(loop_id, symbol, "not_due", "skipped", "no MT5 adapter")
                continue

            updates = self._refresh_symbol_cache(symbol)
            failed_updates = [update for update in updates if update.error]
            if failed_updates:
                summary.skipped_data_error += 1
                self.logger.event(
                    "SYMBOL_DATA_ERROR",
                    loop_id=loop_id,
                    symbol=symbol,
                    detail="; ".join(f"{u.timeframe}:{u.error}" for u in failed_updates),
                )
                self._log_signal(loop_id, symbol, "data_error", "skipped", failed_updates[0].error)
                continue

            entry_timeframes = self._entry_timeframes(symbol)
            entry_updated = any(update.updated and update.timeframe in entry_timeframes for update in updates)
            if entry_updated:
                summary.entry_bar_updates += 1
            if not entry_updated:
                summary.skipped_no_new_bar += 1
                self.logger.event("SYMBOL_SKIPPED_NO_NEW_BAR", loop_id=loop_id, symbol=symbol)
                self._log_signal(loop_id, symbol, "not_due", "skipped", "no_new_closed_entry_bar")
                continue

            updated_bar_times[symbol] = {
                update.timeframe: update.latest_closed_bar.isoformat()
                for update in updates
                if update.latest_closed_bar is not None
            }

            try:
                result = self.engine.evaluate_symbol(symbol, sym_state)
            except Exception as exc:
                summary.skipped_data_error += 1
                self.logger.event("SYMBOL_EVAL_ERROR", loop_id=loop_id, symbol=symbol, detail=str(exc))
                self._log_signal(loop_id, symbol, "error", "skipped", str(exc))
                continue

            if not result.evaluated:
                summary.skipped_data_error += 1
                self.logger.event("SYMBOL_EVAL_NOT_READY", loop_id=loop_id, symbol=symbol, detail=result.reason)
                self._log_signal(loop_id, symbol, "not_ready", "skipped", result.reason)
                continue

            summary.evaluated_symbols += 1
            if result.proposed_engine_state is not None:
                proposed_engine_states[symbol] = result.proposed_engine_state

            if result.candidate:
                summary.candidates += 1
                candidates.append(result.candidate)
                self.logger.candidate({
                    **result.candidate,
                    "loop_id": loop_id,
                    "status": "candidate",
                    "detail": result.reason,
                })
                self._log_signal(loop_id, symbol, "evaluated", "candidate", result.reason, result.candidate)
            else:
                self._log_signal(loop_id, symbol, "evaluated", "no_signal", result.reason)

            symbol_duration_ms = int((time.perf_counter() - symbol_started) * 1000)
            self.logger.timing({
                "loop_id": loop_id,
                "stage": "symbol_evaluation",
                "duration_ms": symbol_duration_ms,
                "detail": symbol,
            })
            self._warn_if_slow("symbol_evaluation", symbol_duration_ms, loop_id=loop_id, symbol=symbol)

        reduction = self.reducer.reduce(candidates, state)
        summary.accepted = len(reduction.accepted)
        summary.rejected = len(reduction.rejected)
        for row in reduction.accepted + reduction.rejected:
            self.logger.reducer({"loop_id": loop_id, **row})

        for symbol, proposed in proposed_engine_states.items():
            state["symbols"][symbol]["engine_state"] = proposed
            state["symbols"][symbol].setdefault("last_bar_times", {}).update(updated_bar_times.get(symbol, {}))
            self.logger.state_audit({
                "loop_id": loop_id,
                "symbol": symbol,
                "action": "commit_symbol_engine_state",
                "state_version": state["portfolio"].get("state_version", 0),
                "detail": ",".join(sorted(proposed)),
            })

        self.execution.execute(
            reduction.accepted,
            state,
            mode=self._state_mode(),
            live_enabled=(
                not self.dry_run
                and self._state_mode() == "live"
                and bool(self.cfg.raw.get("execution", {}).get("live_order_enabled", False))
            ),
        )

        state.setdefault("diagnostics", {})
        state["diagnostics"]["last_loop_id"] = loop_id
        state["diagnostics"]["last_snapshot_time"] = datetime.now(timezone.utc).isoformat()
        if not reconciliation_blocked:
            state["diagnostics"]["last_invariant_status"] = "OK"
        self._update_market_data_freshness(state, summary, loop_id)
        state["portfolio"]["state_version"] = int(state["portfolio"].get("state_version", 0)) + 1

        self.logger.snapshot(
            loop_id=loop_id,
            mode=self._state_mode(),
            open_trades=len(state.get("open_trades", [])),
            portfolio_cap=self.cfg.portfolio_cap,
            evaluated_symbols=summary.evaluated_symbols,
            skipped_disabled=summary.skipped_disabled,
            skipped_no_new_bar=summary.skipped_no_new_bar,
            skipped_data_error=summary.skipped_data_error,
            skipped_engine_not_ready=summary.skipped_engine_not_ready,
        )
        loop_duration_ms = int((time.perf_counter() - loop_started) * 1000)
        self.logger.timing({
            "loop_id": loop_id,
            "stage": "portfolio_loop",
            "duration_ms": loop_duration_ms,
            "detail": f"candidates={summary.candidates} accepted={summary.accepted} rejected={summary.rejected}",
        })
        self._warn_if_slow("portfolio_loop", loop_duration_ms, loop_id=loop_id)
        self.logger.event(
            "SNAPSHOT_OK",
            loop_id=loop_id,
            detail=(
                f"account={bool(account)} broker_positions={len(broker_positions)} "
                f"candidates={summary.candidates} accepted={summary.accepted} rejected={summary.rejected}"
            ),
        )
        self._maybe_write_gps(state, loop_id, force=reconciliation_closed)
        self._maybe_send_daily_status(
            state,
            loop_id=loop_id,
            account=account,
            broker_positions=broker_positions,
        )
        self.save_state(state)
        return state

    def _reconcile_broker(self, state: dict[str, Any], broker_positions: list[dict[str, Any]], loop_id: str) -> bool:
        if self.reconciler is None or self.dry_run or self._state_mode() != "live":
            return False

        result = self.reconciler.reconcile(state, broker_positions)
        self._log_reconciliation(result, loop_id)

        if result.changed:
            state["portfolio"]["state_version"] = int(state["portfolio"].get("state_version", 0)) + 1
            state.setdefault("diagnostics", {})
            state["diagnostics"]["_reconciliation_closed_this_loop"] = True
            state["diagnostics"]["last_reconciliation_time"] = datetime.now(timezone.utc).isoformat()
            state["diagnostics"]["last_reconciliation_closed_tickets"] = sorted(result.closed_tickets)
            if result.history_errors:
                state["diagnostics"]["last_reconciliation_status"] = "DEGRADED_HISTORY_MISSING"
                state["diagnostics"]["last_reconciliation_history_errors"] = list(result.history_errors)
                state["diagnostics"]["last_review_action"] = (
                    "Review broker history for UNKNOWN close rows before trusting GPS."
                )
                self.logger.event(
                    "BROKER_RECONCILIATION_DEGRADED",
                    loop_id=loop_id,
                    detail="broker close history missing; state cap cleared with review marker",
                )
            else:
                state["diagnostics"]["last_reconciliation_status"] = "OK"
            self.save_state(state)

        if result.orphan_positions:
            state.setdefault("diagnostics", {})
            state["diagnostics"]["last_invariant_status"] = "BROKER_ORPHAN_POSITION"
            state["diagnostics"]["last_reconciliation_time"] = datetime.now(timezone.utc).isoformat()
            state["diagnostics"]["last_orphan_positions"] = [
                self._position_detail(position) for position in result.orphan_positions
            ]
            state["portfolio"]["state_version"] = int(state["portfolio"].get("state_version", 0)) + 1
            self.save_state(state)
            self.logger.event(
                "PORTFOLIO_ENTRY_BLOCKED",
                loop_id=loop_id,
                detail="broker orphan position detected; review state before new entries",
            )
            return True

        return False

    def _maybe_write_gps(self, state: dict[str, Any], loop_id: str, *, force: bool = False) -> None:
        gps_cfg = self.cfg.raw.get("gps", {})
        diagnostics = state.setdefault("diagnostics", {})
        if not gps_cfg.get("enabled"):
            diagnostics["last_gps_skip_reason"] = "disabled"
            return

        now = datetime.now(timezone.utc)
        last_run = _parse_utc(diagnostics.get("last_gps_run_time"))
        interval = int(gps_cfg.get("loop_interval_seconds", 300))
        due = last_run is None or (now - last_run).total_seconds() >= interval
        forced_by_close = force and bool(gps_cfg.get("run_on_trade_close", True))
        if not due and not forced_by_close:
            diagnostics["last_gps_skip_reason"] = f"not_due interval_seconds={interval}"
            return

        reason = "trade_close" if forced_by_close else "interval_due"
        try:
            outputs = write_reports(self.cfg)
            diagnostics["last_gps_status"] = "GRAY"
            diagnostics["last_gps_run_time"] = now.isoformat()
            diagnostics["last_gps_run_reason"] = reason
            diagnostics["last_gps_skip_reason"] = ""
            self.logger.event("GPS_REPORTS_WRITTEN", loop_id=loop_id, detail=f"reason={reason} outputs={outputs}")
        except Exception as exc:
            diagnostics["last_gps_skip_reason"] = "failed"
            self.logger.event("GPS_REPORTS_FAILED", loop_id=loop_id, detail=str(exc))

    def _maybe_send_daily_status(
        self,
        state: dict[str, Any],
        *,
        loop_id: str,
        account: dict[str, Any] | None,
        broker_positions: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> None:
        if self.dry_run or self.adapter is None:
            return
        if not self.cfg.daily_status_notifications_enabled():
            return

        now = now or datetime.now(timezone.utc)
        notifications = self.cfg.raw.get("notifications", {})
        send_hour = int(notifications.get("daily_status_utc_hour", 7))
        if now.hour < send_hour:
            return

        diagnostics = state.setdefault("diagnostics", {})
        today = now.date().isoformat()
        if diagnostics.get("last_daily_status_date") == today:
            return

        payload = self._daily_status_payload(state, account, broker_positions, now=now)
        sent = self.notifier.daily_status(payload)
        diagnostics["last_daily_status_attempt_time"] = now.isoformat()
        if sent:
            diagnostics["last_daily_status_date"] = today
            diagnostics["last_daily_status_time"] = now.isoformat()
            self.logger.event("DAILY_STATUS_SENT", loop_id=loop_id, detail=f"date={today}")

    def _daily_status_payload(
        self,
        state: dict[str, Any],
        account: dict[str, Any] | None,
        broker_positions: list[dict[str, Any]],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        diagnostics = state.get("diagnostics", {})
        return {
            "date_utc": now.date().isoformat(),
            "time_utc": now.strftime("%Y-%m-%d %H:%M"),
            "mode": self._state_mode(),
            "deployment": self.cfg.deployment_id,
            "symbols": self.cfg.deployment_symbols,
            "equity": _fmt_money(_get_any(account, "equity")) if account else "N/A",
            "balance": _fmt_money(_get_any(account, "balance")) if account else "N/A",
            "open_trades": len(state.get("open_trades", [])),
            "portfolio_cap": self.cfg.portfolio_cap,
            "broker_positions": len(broker_positions),
            "market_data": diagnostics.get("last_market_data_status", "UNKNOWN"),
            "latest_entry_bar": diagnostics.get("last_warmup_entry_bar_time")
            or self._latest_state_bar_time(state)
            or "N/A",
            "gps": diagnostics.get("last_gps_status", "UNKNOWN"),
        }

    @staticmethod
    def _latest_state_bar_time(state: dict[str, Any]) -> str:
        latest: datetime | None = None
        latest_text = ""
        for symbol_state in (state.get("symbols") or {}).values():
            for value in (symbol_state.get("last_bar_times") or {}).values():
                parsed = _parse_utc(value)
                if parsed is not None and (latest is None or parsed > latest):
                    latest = parsed
                    latest_text = parsed.isoformat()
        return latest_text

    def _warn_if_slow(self, stage: str, duration_ms: int, *, loop_id: str, symbol: str = "") -> None:
        perf_cfg = self.cfg.raw.get("performance", {})
        threshold_key = {
            "symbol_evaluation": "slow_symbol_eval_ms",
            "portfolio_loop": "slow_portfolio_loop_ms",
        }.get(stage)
        if not threshold_key:
            return
        threshold = int(perf_cfg.get(threshold_key, 0) or 0)
        if threshold <= 0 or duration_ms < threshold:
            return
        self.logger.event(
            "PERF_SLOW_STAGE",
            loop_id=loop_id,
            symbol=symbol,
            detail=(
                f"stage={stage} duration_ms={duration_ms} threshold_ms={threshold} "
                "deferred_watch=incremental_feature_rebuild"
            ),
        )

    def _log_deferred_performance_modes(self) -> None:
        perf_cfg = self.cfg.raw.get("performance", {})
        self.logger.event(
            "PERF_DEFERRED_MODES",
            detail=(
                "incremental_feature_rebuild=false "
                f"slow_symbol_eval_ms={int(perf_cfg.get('slow_symbol_eval_ms', 2000))} "
                f"slow_portfolio_loop_ms={int(perf_cfg.get('slow_portfolio_loop_ms', 5000))} "
                f"csv_fsync_enabled={bool(perf_cfg.get('csv_fsync_enabled', False))} "
                f"csv_fsync_deferred_logs={','.join(perf_cfg.get('csv_fsync_deferred_logs', []))} "
                "source_of_truth=state+mt5_history"
            ),
        )

    def _update_market_data_freshness(self, state: dict[str, Any], summary: LoopSummary, loop_id: str) -> None:
        diagnostics = state.setdefault("diagnostics", {})
        now = datetime.now(timezone.utc)
        if summary.entry_bar_updates > 0:
            was_stale = diagnostics.get("last_market_data_status") == "STALE"
            diagnostics["last_entry_bar_update_time"] = now.isoformat()
            diagnostics["last_market_data_status"] = "OK"
            diagnostics["last_market_data_stale_warning_time"] = None
            diagnostics["last_market_data_stale_minutes"] = 0
            if was_stale:
                message = "MARKET_DATA_RESUMED closed entry bars are updating again; returning to normal poll"
                print(message, flush=True)
                self.logger.event("MARKET_DATA_RESUMED", loop_id=loop_id, detail=message)
            return

        last_update = _parse_utc(diagnostics.get("last_entry_bar_update_time"))
        if last_update is None:
            diagnostics["last_entry_bar_update_time"] = now.isoformat()
            diagnostics["last_market_data_status"] = "UNKNOWN"
            return

        stale_minutes = int(self.cfg.raw.get("runtime", {}).get("market_data_stale_minutes", 180))
        age_minutes = int((now - last_update).total_seconds() // 60)
        if age_minutes < stale_minutes:
            diagnostics["last_market_data_status"] = "OK"
            return

        heartbeat_seconds = int(self.cfg.raw.get("runtime", {}).get("heartbeat_minutes", 15)) * 60
        last_warning = _parse_utc(diagnostics.get("last_market_data_stale_warning_time"))
        should_emit = last_warning is None or (now - last_warning).total_seconds() >= heartbeat_seconds
        diagnostics["last_market_data_status"] = "STALE"
        diagnostics["last_market_data_stale_minutes"] = age_minutes
        if should_emit:
            message = (
                f"MARKET_DATA_STALE no closed entry bars for {age_minutes} minutes; "
                "possible holiday/weekend/feed issue"
            )
            print(message, flush=True)
            self.logger.event("MARKET_DATA_STALE", loop_id=loop_id, detail=message)
            diagnostics["last_market_data_stale_warning_time"] = now.isoformat()

    def _seed_market_data_freshness_from_warmup(self, state: dict[str, Any], updates: list[CacheUpdate]) -> bool:
        latest_entry_bar: Any = None
        for update in updates:
            if update.error or update.latest_closed_bar is None:
                continue
            if update.timeframe not in self._entry_timeframes(update.symbol):
                continue
            candidate = pd_timestamp_to_utc(update.latest_closed_bar)
            if candidate is not None and (latest_entry_bar is None or candidate > latest_entry_bar):
                latest_entry_bar = candidate

        if latest_entry_bar is None:
            return False

        now = datetime.now(timezone.utc)
        stale_minutes = int(self.cfg.raw.get("runtime", {}).get("market_data_stale_minutes", 180))
        age_minutes = int((now - latest_entry_bar).total_seconds() // 60)
        if latest_entry_bar > now:
            self.logger.event(
                "MARKET_DATA_FUTURE_AFTER_OFFSET",
                detail=f"latest={latest_entry_bar.isoformat()} now={now.isoformat()}",
            )
            return False
        if age_minutes >= stale_minutes:
            return False

        diagnostics = state.setdefault("diagnostics", {})
        changed = diagnostics.get("last_market_data_status") != "OK"
        diagnostics["last_entry_bar_update_time"] = now.isoformat()
        diagnostics["last_market_data_status"] = "OK"
        diagnostics["last_market_data_stale_warning_time"] = None
        diagnostics["last_market_data_stale_minutes"] = 0
        diagnostics["last_warmup_entry_bar_time"] = latest_entry_bar.isoformat()
        message = (
            f"MARKET_DATA_RESUMED warmup found current closed entry bars; "
            f"latest={latest_entry_bar.isoformat()} age_minutes={age_minutes}"
        )
        self.logger.event("MARKET_DATA_RESUMED", detail=message)
        print(message, flush=True)
        return changed

    def _next_sleep_seconds(self, state: dict[str, Any]) -> int:
        runtime_cfg = self.cfg.raw.get("runtime", {})
        if state.get("diagnostics", {}).get("last_market_data_status") == "STALE":
            return int(runtime_cfg.get("market_data_stale_poll_seconds", 60))
        return int(runtime_cfg.get("poll_interval_seconds", 5))

    def _print_startup_banner(self, state: dict[str, Any]) -> None:
        mode = self._state_mode().upper()
        live_enabled = bool(self.cfg.raw.get("execution", {}).get("live_order_enabled", False))
        notifications = self.cfg.raw.get("notifications", {})
        print("", flush=True)
        print("System C V2 bot", flush=True)
        print("=" * 60, flush=True)
        print(f"mode={mode} dry_run={self.dry_run}", flush=True)
        print(f"deployment={self.cfg.deployment_id}", flush=True)
        print(f"symbols={','.join(self.cfg.deployment_symbols)}", flush=True)
        print(f"portfolio_cap={self.cfg.portfolio_cap} base_risk_pct={self.cfg.base_risk_pct}", flush=True)
        print(f"live_order_enabled={live_enabled}", flush=True)
        print(
            "notifications="
            f"enabled={bool(notifications.get('enabled', False))} "
            f"paper={bool(notifications.get('paper_trades', False))} "
            f"live={bool(notifications.get('live_trades', False))} "
            f"daily={bool(notifications.get('daily_status', False))}",
            flush=True,
        )
        print(f"state_file={self._state_path()}", flush=True)
        print(f"open_trades={len(state.get('open_trades', []))}/{self.cfg.portfolio_cap}", flush=True)
        print(f"kill_file={self.cfg.bot_dir / self.cfg.raw['runtime']['kill_file']}", flush=True)
        print("=" * 60, flush=True)

    def _print_heartbeat(
        self,
        state: dict[str, Any],
        *,
        loop_id: str,
        account: dict[str, Any] | None,
        broker_positions: list[dict[str, Any]],
    ) -> None:
        interval = int(self.cfg.raw.get("runtime", {}).get("heartbeat_minutes", 15)) * 60
        now_monotonic = time.monotonic()
        if self._last_heartbeat_monotonic and now_monotonic - self._last_heartbeat_monotonic < interval:
            return
        self._last_heartbeat_monotonic = now_monotonic

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        equity = _fmt_money(_get_any(account, "equity")) if account else "N/A"
        balance = _fmt_money(_get_any(account, "balance")) if account else "N/A"
        diagnostics = state.get("diagnostics", {})
        print(
            (
                f"[{now}] ALIVE_START loop={loop_id} mode={self._state_mode()} "
                f"equity={equity} balance={balance} "
                f"open={len(state.get('open_trades', []))}/{self.cfg.portfolio_cap} "
                f"broker_positions={len(broker_positions)} "
                f"last_loop={diagnostics.get('last_loop_id')} "
                f"last_snapshot={diagnostics.get('last_snapshot_time')} "
                f"invariant={diagnostics.get('last_invariant_status')} "
                f"market_data={diagnostics.get('last_market_data_status')} "
                f"gps={diagnostics.get('last_gps_status')} "
                f"log_write_failures={self.logger.write_failures}"
            ),
            flush=True,
        )

    def _log_reconciliation(self, result: ReconciliationResult, loop_id: str) -> None:
        for row in result.close_rows:
            self.logger.trade(row)
            self.logger.event(
                "BROKER_CLOSED_TRADE",
                loop_id=loop_id,
                symbol=str(row.get("symbol", "")),
                detail=(
                    f"ticket={row.get('ticket')} reason={row.get('exit_reason')} "
                    f"r={row.get('r_result')} close_time={row.get('close_time')}"
                ),
            )
        for detail in result.history_errors:
            self.logger.event("BROKER_CLOSE_HISTORY_ERROR", loop_id=loop_id, detail=detail)
        for position in result.orphan_positions:
            detail = self._position_detail(position)
            self.logger.event(
                "BROKER_ORPHAN_POSITION",
                loop_id=loop_id,
                symbol=detail.get("symbol", ""),
                detail=str(detail),
            )

    @staticmethod
    def _position_detail(position: dict[str, Any]) -> dict[str, Any]:
        def get(key: str, default: Any = "") -> Any:
            if isinstance(position, dict):
                return position.get(key, default)
            return getattr(position, key, default)

        return {
            "ticket": get("ticket", get("identifier", "")),
            "symbol": get("symbol", ""),
            "volume": get("volume", ""),
            "type": get("type", ""),
            "magic": get("magic", ""),
        }

    def _refresh_symbol_cache(self, symbol: str) -> list[CacheUpdate]:
        if self.adapter is None:
            return []
        updates = []
        for timeframe in sorted(self.cache.required_timeframes(symbol)):
            update = self.cache.update_delta(self.adapter, symbol, timeframe)
            updates.append(update)
            if update.updated:
                self.logger.event(
                    "CACHE_UPDATED",
                    symbol=symbol,
                    detail=f"timeframe={update.timeframe} latest={update.latest_closed_bar} rows={update.rows}",
                )
        return updates

    def _entry_timeframes(self, symbol: str) -> set[str]:
        sym_cfg = self.cfg.symbols[symbol]
        phases = sym_cfg.raw.get("architecture_phases", {})
        if sym_cfg.cross_phase_enabled:
            return {
                normalize_timeframe(phases[phase_name]["entry_timeframe"])
                for phase_name in sym_cfg.branch_phases.values()
            }
        return {normalize_timeframe(phases[sym_cfg.selected_phase]["entry_timeframe"])}

    def _log_signal(
        self,
        loop_id: str,
        symbol: str,
        eval_status: str,
        signal_status: str,
        reason: str,
        candidate: dict[str, Any] | None = None,
    ) -> None:
        candidate = candidate or {}
        self.logger.signal({
            "loop_id": loop_id,
            "bar_time": candidate.get("bar_time", ""),
            "symbol": symbol,
            "entry_timeframe": candidate.get("entry_timeframe", ""),
            "context_timeframe": candidate.get("context_timeframe", ""),
            "eval_status": eval_status,
            "signal_status": signal_status,
            "reason": reason,
            "hypothesis": candidate.get("hypothesis", ""),
            "direction": candidate.get("direction", ""),
            "candidate_id": candidate.get("candidate_id", ""),
        })

    def run(self, *, once: bool = False) -> None:
        state = self.startup()
        kill_file = self.cfg.bot_dir / self.cfg.raw["runtime"]["kill_file"]
        try:
            while True:
                if kill_file.exists():
                    self.logger.event("BOT_STOPPED", detail=f"kill file present: {kill_file}")
                    break
                state = self.run_once(state)
                if once:
                    break
                time.sleep(self._next_sleep_seconds(state))
        finally:
            if self.adapter is not None and not self.dry_run:
                self.adapter.close()


def run_with_adapter(adapter: Mt5Adapter | None, *, dry_run: bool = False, once: bool = False) -> None:
    cfg = load_runtime_config()
    runner = SequentialPortfolioRunner(cfg, adapter, dry_run=dry_run)
    runner.run(once=once)


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pd_timestamp_to_utc(value: Any) -> datetime | None:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return _parse_utc(str(value))


def _get_any(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _fmt_money(value: Any) -> str:
    try:
        if value in (None, ""):
            return "N/A"
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def build_common_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--dry-run", action="store_true", help="skip MT5 connection and order execution")
    parser.add_argument("--once", action="store_true", help="run a single sequential loop")
    parser.add_argument(
        "--probe-market-data",
        action="store_true",
        help="connect to MT5, probe required symbol/timeframe bars, write a separate probe log, then exit",
    )
    parser.add_argument(
        "--probe-bars",
        type=int,
        default=20,
        help="number of latest MT5 bars to fetch per symbol/timeframe during --probe-market-data",
    )
    return parser
