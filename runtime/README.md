# Runtime Wiring

This directory is the active System C Bot V2 runtime. The root runners
`run_orders_vps.py` and `run_orders_rpyc.py` are thin entrypoints into this
package.

The runtime shape is sequential at the portfolio level:

```text
load config/state
connect adapter
warm closed-bar cache
reconcile broker positions against state
for each symbol:
  update closed bars
  rebuild features
  evaluate engine
  stage proposed symbol state
portfolio reducer accepts/rejects candidates
execution opens paper/live trades sequentially
state/logs/GPS/notifications/console heartbeat update after decisions
```

## Main Flow

```text
run_orders_vps.py / run_orders_rpyc.py
  -> runtime.runner.run_with_adapter()
    -> runtime.config.load_runtime_config()
    -> runtime.state_store.load_state()
    -> runtime.adapters.NativeMt5Adapter or RpycMt5Adapter
    -> runtime.broker_time.detect_broker_utc_offset()
    -> runtime.data_cache.BarCache
    -> runtime.engine_bridge.EngineBridge
    -> runtime.portfolio.PortfolioReducer
    -> runtime.reconciliation.BrokerReconciler
    -> runtime.execution.ExecutionEngine
    -> runtime.logging.RuntimeLogger
    -> runtime.gps.write_reports()
```

The runner is the coordinator. Other modules should remain focused and not
reach across ownership boundaries.

## Module Summary

### `config.py`

Loads and validates the V2 deployment config:

```text
bot/config/config.yaml
bot/config/<SYMBOL>.yaml
bot/.ennv
```

It intentionally ignores legacy root `bot/config.yaml`.

It verifies:

```text
Option A symbol order and branch setup
portfolio cap
CB / Highwind disabled + monitor-only
closed-bar clock rules
data cache rules
Numba indicator settings
notification flags
required .ennv keys
```

Important helpers:

```text
cfg.get_state_file()
cfg.get_log_paths()
cfg.notifications_enabled_for(mode)
```

### `state_store.py`

Owns V2 state shape, atomic writes, templates, and validation.

Active states:

```text
bot/state_live_portfolio_v2.json
bot/state_paper_portfolio_v2.json
```

Templates:

```text
bot/state/state_live_portfolio_v2.template.json
bot/state/state_paper_portfolio_v2.template.json
```

State is not migrated from V1. Each symbol owns its own engine state:

```text
state.symbols.<SYMBOL>.engine_state
```

Portfolio-level open trades are owned only by:

```text
state.open_trades
```

### `adapters.py`

Defines the MT5 adapter boundary and two implementations:

```text
NativeMt5Adapter
RpycMt5Adapter
```

Both expose the same methods:

```text
connect / close
account_info / positions_get / orders_get / history_deals_get
symbol_info / symbol_info_tick
copy_rates_from_pos
order_send
timeframe_value
```

The rest of the runtime talks to this protocol, not directly to `MetaTrader5`.

### `data_cache.py`

Owns closed-bar polling and in-memory bar cache.

Responsibilities:

```text
normalize timeframe labels
convert native MT5 structured arrays or tuple rows to pandas frames indexed by UTC bar close time
normalize broker-server timestamps by detected UTC offset
request MT5 start_pos=1 so the forming bar is excluded
warm 500+ bars on startup
probe latest closed bar with closed-bar positions
fetch closed-bar deltas with overlap
retain bounded history per timeframe
```

The cache is intentionally in memory for launch:

```text
data.cache.persist_to_disk: false
```

There is no persistent runtime bar cache to clear on deployment. Restarting the
process rebuilds the cache from MT5. The state file persists diagnostics and open
trades, but not the in-memory bar frames.

The native MT5 VPS path returns numpy structured arrays from
`copy_rates_from_pos`. `data_cache.py` preserves field names when present and
can recover standard unnamed MT5 tuple rows using the known rate column order.

### `broker_time.py`

Detects the broker-server clock offset from UTC using the latest MT5 tick time.
This matters because many MT5 brokers encode bar timestamps in server time
instead of true UTC, and the server offset can switch between UTC+2 and UTC+3
with DST.

Responsibilities:

```text
read symbol_info_tick("EURUSD").time
compare broker timestamp against wall-clock UTC
round to an integer UTC offset
log/print BROKER_TIME_OFFSET on startup
feed the offset into BarCache timestamp normalization
```

The offset is a diagnostic and normalization input. Closed-bar selection still
uses MT5 bar position, not local-clock filtering.

### `engine_bridge.py`

Connects cached V2 bars to the bot-local copied backtester engine.

Responsibilities:

```text
select symbol branch phases from bot/config/<SYMBOL>.yaml
rebuild Numba-backed features from cached bars
align entry/context frames as closed-bar asof
hydrate InstrumentEngine from symbol state
run engine.on_bar()
run policy_3()
resolve SL/TP for accepted symbol candidate
return a candidate plus proposed symbol engine state
```

It does not commit state directly. It returns staged state to the runner.

### `engine/`

Bot-local copy of the needed backtester engine surface:

```text
engine.py
strategy.py
policy.py
features.py
indicator.py
numba_kernels.py
config_loader.py
align.py
```

V2 runtime imports this local copy, not the external `backtester` symlink.

### `portfolio.py`

Owns portfolio-level candidate reduction.

Current rules:

```text
sort candidates deterministically
reject when Rule 2 is triggered
reject when portfolio cap is full
reject when symbol already has an open trade
accept up to remaining portfolio slots
```

Priority order for same-time decisions:

```text
B -> A2 -> A1
```

It returns:

```text
Reduction(accepted=[...], rejected=[...])
```

### `execution.py`

Owns paper and live trade opening.

Paper path:

```text
append open trade to state
decrement next_paper_ticket
write ORDER_PAPER event
write trade log row
optionally notify if paper notifications are enabled
```

Live path:

```text
read tick, symbol info, account info
calculate risk-based lot size
check broker stop-distance
build MT5 market order request
send order_send
append open trade to state only after successful retcode
save state immediately after appending live trade
write ORDER_LIVE event
write trade log row
notify live trade open if enabled
```

Live order sending requires all of these:

```text
paper_mode: false
execution.live_order_enabled: true
runner not in --dry-run
accepted candidate from reducer
```

### `reconciliation.py`

Owns broker/state reconciliation for live positions.

Responsibilities:

```text
filter broker positions by System C magic number
compare broker open tickets against state.open_trades tickets
detect state-known trades that disappeared from broker positions
query MT5 history with history_deals_get(position=ticket)
infer exit reason, exit price, close time, and historical entry price
compute R result from entry, exit, direction, and SL distance
return close rows for trade logging
detect broker orphan positions missing from state
```

Manual broker closes are handled the same way as SL/TP closes: if the state
contains the ticket and MT5 history reports a client/mobile/web/expert close
reason, reconciliation emits `exit_reason=MANUAL`, clears the open trade from
state, and writes a `decision=broker_closed` trade row.

Broker orphan positions are not silently adopted or traded around. The runner
logs `BROKER_ORPHAN_POSITION`, records diagnostics, saves state, and blocks
new entries until the position is reviewed.

### `logging.py`

Owns all CSV log schemas and append operations.

Logs:

```text
events_<YYYYMM>.csv
signals_<YYYYMM>.csv
candidates_<YYYYMM>.csv
reducer_<YYYYMM>.csv
snapshot_<YYYYMM>.csv
timing_<YYYYMM>.csv
state_audit_<YYYYMM>.csv
trades_<YYYYMM>.csv
```

Base directory:

```text
logs/portfolio_option_a_202604/
```

`RuntimeLogger.ensure_headers()` creates header-only files so status checks can
verify contracts before the first trade.

### `gps.py`

Owns portfolio GPS report generation from closed trade rows.

Input:

```text
logs/portfolio_option_a_202604/trades_<YYYYMM>.csv
```

Only rows with both `close_time` and `r_result` are treated as closed trades.

Outputs:

```text
logs/portfolio_option_a_202604/gps/rolling_window_metrics.csv
logs/portfolio_option_a_202604/gps/rolling_gps_report.md
logs/portfolio_option_a_202604/gps/frontier_drift_report.md
```

Metrics:

```text
3m / 6m / 12m / 24m / full windows
trade count
total R
max DD R
worst month R
monthly std R
symbol contribution
```

Status classes:

```text
GRAY
GREEN
YELLOW
RED
```

### `notifications.py`

Owns the V2 Telegram boundary.

It reads Telegram secrets from the already-loaded runtime config:

```text
TELEGRAM_TOKEN
TELEGRAM_CHAT_ID
```

Notifications are sent only if config allows them:

```yaml
notifications:
  enabled: true
  paper_trades: false
  live_trades: true
  daily_status: true
  daily_status_utc_hour: 7
```

Execution calls trade-open notifications only after state and trade logs are
updated. Live execution also persists state before the notification path,
shrinking the order-send crash window.

The runner sends one daily Telegram status update at or after
`daily_status_utc_hour`. It is an alive/status message, not a trade event. It
includes mode, deployment, symbols, account equity/balance, open trade count,
broker position count, market-data status, latest entry bar, and GPS status.
It intentionally excludes Highwind, Rule2, and CB.

### `runner.py`

Owns the sequential portfolio loop.

Key responsibilities:

```text
load selected state from paper_mode
connect adapter unless dry-run
warm cache
loop symbols sequentially
stage proposed engine states
collect candidates
reduce candidates once per portfolio loop
execute accepted entries
commit staged symbol state
update diagnostics
write snapshot/timing/events
write GPS reports when cadence is due or broker close happened
print console startup/heartbeat/status messages
save state atomically
honor STOP kill file
```

Dry-run behavior:

```text
no MT5 connection
no cache polling
no orders
no state save
logs still append observation rows
```

## State Ownership

The V2 guard against V1-style shared-state failure is this split:

```text
symbol engine memory:
  state.symbols.<SYMBOL>.engine_state

portfolio open trades:
  state.open_trades

portfolio controls:
  state.portfolio

diagnostics:
  state.diagnostics
```

During a loop, symbol evaluation writes only to a staged dictionary:

```text
proposed_engine_states[symbol]
```

The runner commits those staged states only after reducer/execution has run.

## Clock Model

The runtime uses closed-bar clocks:

```text
minimum resolution: 1min
poll interval: 5 seconds
terminal heartbeat: 15 minutes
indicator rebuild: on entry bar close
entry decision: closed entry bar
context merge: asof closed bar only
execution monitor: broker poll
```

The data cache avoids future leak by using closed bars only and ignoring the
forming bar. It requests MT5 bars from `start_pos=1`, then normalizes broker
server timestamps to UTC using the detected broker offset. This avoids false
future-bar/stale behavior when the broker clock is UTC+2 or UTC+3.

Startup prints/logs the detected offset:

```text
Broker UTC offset: UTC+3 (OK)
```

The 5-second poll is the signal-detection clock. The 15-minute heartbeat is only
console output, so it does not delay 5-minute bar detection.

Market-data stale behavior:

```text
market_data_stale_minutes: 180
market_data_stale_poll_seconds: 60
```

If no deployed symbol produces a new closed entry bar for 180 minutes, the
runner prints/logs `MARKET_DATA_STALE` and backs off loop sleep to 60 seconds.
When a closed entry bar appears again, it prints/logs `MARKET_DATA_RESUMED` and
returns to 5-second polling.

One-shot market-data probe:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_vps.py --probe-market-data --probe-bars 20
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_rpyc.py --probe-market-data --probe-bars 20
```

The probe writes `market_probe_{YYYYMM}.csv` under the deployment log folder and
does not mutate state, send orders, or notify.

GPS cadence:

```text
gps.loop_interval_seconds: 300
gps.run_on_trade_close: true
```

GPS is trade-log based. It runs every 5 minutes and is forced immediately after
broker reconciliation writes a closed trade row.

## Live Gates

Current deployment is live-enabled:

```yaml
paper_mode: false

execution:
  live_order_enabled: true

notifications:
  enabled: true
  paper_trades: false
  live_trades: true
  daily_status: true
  daily_status_utc_hour: 7
```

Current risk:

```yaml
portfolio:
  max_concurrent_live_trades: 2
  base_risk_pct: 0.5
```

To shadow-test instead:

```yaml
paper_mode: true

execution:
  live_order_enabled: false
```

## Typical Commands

Validate runtime config:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m runtime.config
```

Verify states:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m runtime.state_store --verify
```

Native MT5 dry-run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_vps.py --dry-run --once
```

RPyC dry-run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_rpyc.py --dry-run --once
```

Generate GPS reports:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m runtime.gps
```

View status:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 status.py --live
PYTHONDONTWRITEBYTECODE=1 python3 status.py --gps-check
```

## What Not To Do

- Do not import from the external `backtester` symlink in runtime code.
- Do not use legacy root `config.yaml` or `config_loader.py`.
- Do not mutate symbol engine state directly inside reducer or execution.
- Do not send Telegram notifications from symbol evaluation.
- Do not include forming bars in decisions.
- Do not migrate V1 state into V2 state.
- Do not treat the GPS seed-baseline `base_risk_pct: 0.4` as live order risk;
  live risk is `portfolio.base_risk_pct`.
