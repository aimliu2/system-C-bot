# System C State Backups

This folder holds local V2 clean-state templates used to recover the active
portfolio state files if they become corrupted. V2 does not migrate V1 state.

Generated clean templates:

```text
state_live_portfolio_v2.template.json
state_paper_portfolio_v2.template.json
```

The active V2 state files are at the bot repo root because
`bot/config/config.yaml` currently declares:

```text
state_live_portfolio_v2.json
state_paper_portfolio_v2.json
```

## Recovery

Verify both active states and templates:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m runtime.state_store --verify
```

Restore from a clean template:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m runtime.state_store --restore live
PYTHONDONTWRITEBYTECODE=1 python3 -m runtime.state_store --restore paper
```

The status wrapper also exposes restore commands:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 status.py --restore-live
PYTHONDONTWRITEBYTECODE=1 python3 status.py --restore-paper
```

Live restore asks for confirmation.

## Ownership

Symbol engine memory is separated by symbol:

```text
state.symbols.<SYMBOL>.engine_state
```

Portfolio-owned live/paper open trades are stored only in:

```text
state.open_trades
```

The runner stages symbol engine changes during a loop and commits them only
after portfolio reduction/execution. This is the V2 guard against the old shared
single-state failure.

## Diagnostics

State diagnostics are operational, not strategy memory. Important fields include:

```text
last_loop_id
last_snapshot_time
last_invariant_status
last_reconciliation_time
last_reconciliation_status
last_gps_status
last_gps_run_time
last_gps_run_reason
last_gps_skip_reason
last_entry_bar_update_time
last_market_data_status
last_market_data_stale_minutes
last_market_data_stale_warning_time
```

`last_market_data_status` can be:

```text
UNKNOWN  startup/no entry bar observed yet
OK       closed entry bars are updating or stale threshold has not been reached
STALE    no closed entry bars for runtime.market_data_stale_minutes
```

When `STALE`, the runner slows polling from 5 seconds to 60 seconds. It returns
to 5-second polling when any deployed symbol produces a new closed entry bar.

## Notes

- Active state is the source of truth for bot memory, together with MT5 broker
  history for live position reconciliation.
- Trade CSV logs are append-only observability and GPS input.
- Do not edit active state by hand while the bot is running.
- If state JSON is corrupted, stop the bot, restore from template, then restart
  and let broker reconciliation detect any live broker positions.
