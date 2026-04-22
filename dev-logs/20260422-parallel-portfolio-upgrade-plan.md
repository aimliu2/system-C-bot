# System C Bot Parallel Portfolio Upgrade Plan

_Prepared 2026-04-22_

## Goal

Upgrade the current bot from a sequential single-state runner into the portfolio
runtime described in `bot/sys-C-bot-architecture-v2.md`.

The upgraded bot must support both live entrypoints:

```text
bot/run_orders_vps.py
bot/run_orders_rpyc.py
```

Both versions should share one portfolio/symbol runtime and differ only in the
MT5 connection adapter.

## Source Decisions

Architecture source:

```text
bot/sys-C-bot-architecture-v2.md
```

Portfolio deployment source:

```text
docs/portfolio-study/202604-portfolio_deployment.md
```

Portfolio GPS / rolling review source:

```text
docs/systemC-portfolio-frontier-bystep.md
Step 8 - Portfolio GPS / Rolling Review
```

First deployment shape:

```text
symbols:
  AUDUSD
  EURJPY
  EURUSD
  USDJPY

portfolio cap:
  N = 2 concurrent live trades

risk per trade:
  0.4% to 0.5% account risk

intervention:
  no Highwind resizing/halt at launch
  CB monitor-only at launch
```

Backtester symbol config and engine source:

```text
backtester/AUDUSD.yaml
backtester/EURJPY.yaml
backtester/EURUSD.yaml
backtester/USDJPY.yaml
backtester/engine.py
backtester/strategy.py
backtester/policy.py
```

Runtime cutover decision:

```text
old state is not migrated
old active logs are not migrated
new portfolio deployment starts fresh
```

## Current Runtime Observations

The current bot has two large runner files:

```text
bot/run_orders_vps.py
bot/run_orders_rpyc.py
```

They mostly duplicate the same strategy, state, risk, logging, and execution
logic. The main difference is MT5 access:

```text
VPS: native MetaTrader5 module
RPyC: remote MT5 module through rpyc, with obtain() for remote objects
```

Current state is one JSON file selected by global paper/live mode:

```text
state_file_paper
state_file_live
```

The state already has per-symbol dictionaries, but the loop still processes one
symbol at a time and applies account checks inside that symbol loop. There is no
portfolio candidate collection and reducer step yet.

Treat the current `bot/config.yaml` as stale legacy config. It is useful as a
reference for runtime knobs and secrets-adjacent paths, but it is not a source
of truth for the new portfolio deployment. Do not infer the new deployment
symbol list, strategy shape, risk model, or intervention policy from the current
YAML.

## Target Runtime Shape

Target layers:

```text
MT5 adapter layer
  native VPS adapter or RPyC adapter

Data snapshot layer
  one sequential MT5 read phase
  candle bundles, ticks, symbol info, positions, account info

Symbol evaluation layer
  one engine/state/config per symbol
  no MT5 calls
  no file writes
  returns candidate plus proposed symbol-state transition

Portfolio reducer layer
  portfolio cap
  account risk guards
  deterministic admission
  latest open-position conflict checks

Execution layer
  one sequential MT5 order lane
  live/paper execution
  state/log/notifier after coherent decision
```

## Proposed File Layout

Keep both existing entrypoint filenames, but make them thin wrappers.

```text
bot/run_orders_vps.py
  native MT5 bootstrap
  creates NativeMt5Adapter
  calls shared runner

bot/run_orders_rpyc.py
  RPyC bootstrap/reconnect
  creates RpycMt5Adapter
  calls shared runner

bot/runtime/adapters.py
  NativeMt5Adapter
  RpycMt5Adapter
  normalizes MT5 return values

bot/runtime/state_store.py
  fresh portfolio state v2
  atomic save/load
  no migration from old deployment state by default

bot/runtime/config_bridge.py
  reads portfolio deployment settings
  reads copied symbol YAMLs from bot/config/
  builds live runtime symbol configs

bot/runtime/symbol_runtime.py
  per-symbol evaluation wrapper
  converts state snapshots to/from backtester InstrumentEngine shape

bot/runtime/portfolio.py
  candidate model
  portfolio reducer
  portfolio cap N=2
  account guardrails

bot/runtime/execution.py
  live order placement
  paper order placement
  SL/TP repair
  timeout close
  MT5 comment format contract

bot/runtime/logging.py
  fresh portfolio logs
  trade/event/signal/candidate/reducer/snapshot/timing/state-audit CSV writers

bot/runtime/gps.py
  rolling portfolio GPS metrics
  green/yellow/red/gray status classification
  rolling review report inputs

bot/status.py
  update for config v2 and state v2
  show portfolio cap, Rule 2, GPS, symbol modes, open trades, and diagnostics
  remove active CB/Highwind control paths while they are disabled or monitor-only

bot/runtime/runner.py
  shared main loop used by both entrypoints

bot/config/
  AUDUSD.yaml
  EURJPY.yaml
  EURUSD.yaml
  USDJPY.yaml
  config.yaml
  copied deployment-local symbol configs plus separate portfolio/runtime config
```

## Config Source Rule

For the upgrade, copy the selected backtester symbol configs into `bot/config/`
and load the bot from that local folder. This makes the portfolio bot
self-contained at deployment time while preserving the backtester configs as the
research source used to create the deployment copy.

Source hierarchy:

```text
1. docs/portfolio-study/202604-portfolio_deployment.md
   portfolio deployment decision

2. docs/instruments-study/<SYMBOL>-study-conclusion.md
   concluded phase/combo/session-gate architecture per instrument

3. portfolio-analysis/candidate_registry.csv
   compact machine-readable selected candidate id, phase_config, combo, gates

4. bot/config/<SYMBOL>.yaml
   deployment-local copy of selected backtester symbol config

5. bot/config/config.yaml
   bot runtime portfolio config: symbols, cap, risk, state/log paths, switches

6. bot/sys-C-bot-architecture-v2.md
   runtime layering and execution rules

7. bot/config.yaml
   stale legacy reference only
```

Do not import live strategy settings directly from `backtester/` at runtime.
Use `backtester/` to refresh `bot/config/` intentionally when a deployment config
is promoted.

## Existing File Upgrade Audit

### `status.py`

Current status tool is V1-only and must be upgraded before the V2 bot is used
for operations.

Problems to fix:

```text
loads stale config.yaml from current working directory
expects cfg["instruments"] instead of deployment.symbols
resolves top-level state_file_paper/state_file_live instead of state.paper_file/state.live_file
creates and displays active Highwind state even though Highwind is off/monitor-only
displays CB as an active session-skip control even though CB is monitor-only
reset/highwind/manual mode commands mutate V1 keys
does not know state["portfolio"] / state["symbols"] V2 shape
does not show portfolio cap, reducer diagnostics, GPS, or internal logs
```

V2 status scope:

```text
load bot/config/config.yaml
load bot/config/<SYMBOL>.yaml for symbol metadata
read state_live_portfolio_v2.json / state_paper_portfolio_v2.json
display portfolio state: deployment id, paper/live, Rule 2, N cap, open count
display symbol state: mode, last bars, session, open trades, engine state summary
display GPS status and rolling metrics when available
display internal diagnostic log freshness: events/signals/candidates/reducer/snapshot/timing/state_audit
show CB and Highwind as monitor/off only; no active restore/reset controls
support clear-paper/clear-live by creating fresh state v2, not V1 templates
support symbol mode changes only through state["symbols"][symbol]["mode"]
```

Status commands to keep:

```text
python3 status.py
python3 status.py --paper
python3 status.py --live
python3 status.py --shadow SYMBOL
python3 status.py --live SYMBOL
python3 status.py --disable SYMBOL
python3 status.py --rescale
python3 status.py --clear-paper
python3 status.py --clear-live
```

Status commands to remove or convert to no-op/read-only:

```text
python3 status.py --reset-highwind
active CB reset messaging
Highwind force-restore from HALT
```

### `config_loader.py`

Current loader is V1-only and should be replaced or split into a V2 loader.

Problems to fix:

```text
loads bot/config.yaml, which is stale
expects instruments.<SYM> merged overrides
does not load bot/config/config.yaml
does not load copied bot/config/<SYMBOL>.yaml files
does not expose portfolio cap / GPS / diagnostic log settings
does not expose state v2 paths
validates legacy Highwind/CB/hypothesis globals
uses .ennv while start_bridge.sh verifies .env
```

V2 loader requirements:

```text
load runtime config from bot/config/config.yaml
load secrets from mt5.env_file, currently bot/.ennv
load selected symbols from deployment.symbols
load each symbol config from bot/config/<SYMBOL>.yaml
validate all selected symbols have copied YAMLs
expose get_symbol_config(symbol)
expose get_active_symbols()
expose get_state_file(mode)
expose get_log_paths(month)
expose get_portfolio_cap()
expose is_cb_enabled(), is_cb_monitor_only()
expose is_highwind_enabled(), is_highwind_monitor_only()
print V2 deployment summary
```

Secret names in `bot/.ennv` are reusable for V2:

```text
MT5_LOGIN
MT5_PASSWORD
MT5_SERVER
RPYC_HOST
RPYC_PORT
TELEGRAM_TOKEN
TELEGRAM_CHAT_ID
```

Optional secret supported by current code but not present in `.ennv`:

```text
MT5_PATH
```

For VPS native MT5, `MT5_PATH` may remain optional if MT5 initializes without an
explicit terminal path. For RPyC/Wine, `MT5_PATH` is not needed because
`start_bridge.sh` owns terminal startup.

Important:

```text
the file remains named .ennv for security/operational convention
do not rename it to .env
start_bridge.sh must verify bot/.ennv, not bot/.env
notifier.py must either receive loaded env from runtime/config.py or load .ennv itself
```

Recommended implementation:

```text
create bot/runtime/config.py for V2
keep bot/config_loader.py only as a legacy shim until old runners are replaced
after runners move to runtime/config.py, stop importing config_loader.py from V2 paths
```

### `notifier.py`

Current notifier can be reused for Telegram transport, but message formatters
need V2 updates.

Problems to fix:

```text
london_open reads ny_session_summary, instrument_highwind, rule2, cb_anchor V1 keys
trade_closed takes hw_window and reports Highwind WR
cb_triggered sends active session-skipped messaging
no portfolio cap / reducer / GPS messages
no startup-blocked-old-position notification
```

V2 notifier requirements:

```text
keep send() as the transport
trade_opened/trade_closed should accept portfolio context: open count, cap, candidate id
remove Highwind WR from trade_closed while Highwind is off
replace cb_triggered with cb_monitor_event or remove it for launch
add portfolio_status / london_open_v2 summary from state["portfolio"] and state["symbols"]
add reducer_reject summary only for important reasons, not every no-signal
add GPS status notification for Yellow/Red only; Green/Gray can stay in logs
notification failures must remain non-fatal
```

### State JSON Files

Existing state files and templates are V1 and must not be reused:

```text
bot/state_live.json
bot/state_paper.json
bot/state_template_live.json
bot/state_template_paper.json
```

V2 state files:

```text
bot/state_live_portfolio_v2.json
bot/state_paper_portfolio_v2.json
```

V2 state rule:

```text
fresh deployment only
do not migrate old V1 open_trades, pivots, cooldown, ChoCh, Highwind, CB, or session summaries
if old broker positions exist with System C magic number, block startup and require manual decision
```

V2 templates should be generated by `bot/runtime/state_store.py` instead of
committing large hand-edited template JSON files.

Initial V2 state files are prepared as fresh deployment states:

```text
bot/state_live_portfolio_v2.json
bot/state_paper_portfolio_v2.json
```

They intentionally contain no old open trades, no old pivots, no old cooldown,
no old ChoCh, no old CB state, and no old Highwind window.

### `start_bridge.sh`

Bridge shape is mostly reusable because RPyC remains the transport for the macOS
runner.

Required V2 checks:

```text
keep starting MT5 under Wine and rpyc_classic.py on port 18812
align env file with config loader: V2 config currently declares mt5.env_file = .ennv
verify the bridge with an Option A symbol, preferably EURUSD or AUDUSD, not GBPUSD-only
do not encode deployment symbols in start_bridge.sh
do not import V2 runtime logic into the bridge script
```

### `run_data.py`

Current data pull and indicator code may be partially reusable, but V2 should
prefer backtester feature/indicator parity where possible.

V2 direction:

```text
keep MT5 bar pulling helpers if they match adapter design
move rpyc obtain() behavior into RpycMt5Adapter where possible
use copied symbol YAMLs for timeframes and ST params
verify live features match backtester/features.py and backtester/indicator.py
rename df_15m/df_1h aliases to entry/context internally, keeping aliases only if needed
```

### Backtester Engine Reuse

Reuse/copy the engine code that already owns symbol-local state:

```text
backtester/engine.py
backtester/strategy.py
backtester/policy.py
backtester/features.py
backtester/indicator.py
backtester/config_loader.py
```

Recommended approach:

```text
copy or vendor the needed engine modules into bot/runtime/engine/
keep import paths bot-local for deployment stability
do not import directly from the backtester symlink at runtime
write adapter code to hydrate/dehydrate InstrumentEngine state into state["symbols"][symbol]["engine_state"]
keep open trade admission outside InstrumentEngine; portfolio reducer owns final live admission
```

Engine reuse contract:

```text
one InstrumentEngine per symbol
engine evaluates A1/A2/B together
engine must not call MT5
engine must not write files
engine returns candidates and proposed symbol-state transitions
portfolio reducer decides final admission
```

## Clock Model for V2

The V2 bot should apply the same clock separation used by the backtester.

Backtester source:

```text
backtester/cursor.py
backtester/split_cursor.py
backtester/align.py
backtester/features.py
```

Clock definitions:

```text
Indicator clock
  Builds completed-bar indicators for entry and context timeframes.
  Higher-timeframe context must be as-of merged: latest closed context bar whose
  availability time is <= the entry decision time.
  Never use unfinished 1H/4H candles for signal decisions.
  Never use current forming entry candles for signal decisions.

Entry / decision clock
  Runs once per closed entry-TF bar for each symbol/branch.
  The engine evaluates A1/A2/B together at that closed entry bar.
  Candidate production happens here.

Execution clock
  Monitors broker state, fills/orders, SL/TP repair, timeout, and open-trade
  reconciliation.
  Smallest bot-side resolution is 1m. MT5 broker SL/TP may execute intrabar, but
  bot reconciliation and monitoring should not depend on sub-minute polling.
```

No-future-leak live rule:

```text
Use only bars whose close time is <= decision_time.
For entry signals, use the last fully closed entry bar.
For context, use the latest fully closed context bar with avail_time <= entry decision time.
For execution monitoring, use broker positions/deals plus at most 1m polling cadence.
Never evaluate from an unfinished M5/M15/H1/H4 candle.
```

Backtester sequence to preserve conceptually:

```text
1. pending entry from prior signal fills at next entry bar open
2. execution clock resolves open-trade exits
3. closed entry bar is evaluated by engine.on_bar()
4. policy selects symbol-local accepted candidate
5. candidate is queued for the next executable open/tick
```

Live V2 adaptation:

```text
1. broker poll checks positions/orders/account/ticks
2. cheap latest-bar check detects closed entry bars per symbol
3. data snapshot pulls only symbols/timeframes that need evaluation
4. indicator clock rebuilds completed-bar features
5. entry clock produces candidates from frozen state snapshots
6. portfolio reducer admits final entries under N=2
7. MT5 execution sends orders sequentially
8. state/log/notifier updates happen after coherent execution result
```

Polling recommendation for first deployment:

```text
broker_poll_seconds_with_open_trades = 60
broker_poll_seconds_idle = 60
entry_bar_grace_seconds = 5
indicator_rebuild = on_entry_bar_close
entry_decision = closed_entry_bar
execution_monitor = broker_poll
minimum_resolution = 1min
context_merge = asof_closed_bar_only
mixed_symbol_timeframes = true
```

Data cache / least-poll rule:

```text
startup warm cache:
  pull 500 closed bars for every required timeframe per selected symbol/branch
  includes 1min execution, 5m/15m entry, and 1h/4h context as needed

cheap loop probe:
  pull only 2 bars for each native decision timeframe
  use the penultimate bar as latest closed bar
  never decide from the forming bar

cache update:
  if latest closed bar time is unchanged, do not pull full calculation windows
  if a new closed entry bar exists, fetch closed-bar delta with a 2-bar overlap
  append/dedupe/sort the in-memory cache
  retain 600 bars for M5/M15/H1/H4 and 720 bars for M1

feature rebuild:
  rebuild features from cached windows only when a cache updated
  use Numba kernels for EMA/ATR/RSI/SuperTrend
  compile Numba kernels during startup warmup
  fallback_to_python = false for launch parity
```

Why:

```text
The bot may need 3-4 data feeds per symbol once entry, context, execution, and
future GPS/reference timeframes are considered. Pulling and recomputing every
poll wastes time and increases MT5 load. The cheap broker poll runs often, but
full indicator rebuild only runs after a relevant entry bar closes.
```

Implementation rule:

```text
Do not tie broker polling frequency to indicator rebuild frequency.
Do not tie context timeframe updates to trade entry permission.
Do not evaluate a symbol just because a 1H/4H context bar changed unless the
symbol's entry/decision clock also has a closed entry bar ready.
Do not force all symbols onto one global entry timeframe.
Each symbol reads its selected phase/config and keeps its own entry/context clocks.
Group candidate events by actual decision timestamp before portfolio reduction.
```

For cross-phase or future mixed-entry-timeframe symbols, use the
`split_cursor.py` rule:

```text
each branch keeps its own native decision clock
1m/execution clock never creates signal events
same timestamp branch events are grouped before policy/reducer decisions
```

Copied symbol-config cross-verification result:

```text
AUDUSD
  selected candidate: AUDUSD_P3P2_XPHASE_A1A2B_V1
  bot/config: aligned
  architecture: cross_phase
  phase_config: A1=phase3 15m/1H ST12,3; A2=phase2 5m/1H ST11,2; B=phase2 5m/1H ST11,2
  selected_combo: A1+A2+B
  session_gates: A1=all sessions/no gate; A2=asian+london; B=ny

EURJPY
  selected candidate: EURJPY_P4P5_XPHASE_A1A2B_V1
  bot/config: aligned
  architecture: cross_phase
  phase_config: A1=phase4 15m/1H ST11,2; A2=phase4 15m/1H ST11,2; B=phase5 15m/1H ST11,2
  selected_combo: A1+A2+B
  session_gates: A1=asian+london+ny; A2=london+london_ny+ny; B=london+london_ny+ny

EURUSD
  selected candidate: EURUSD_P3_SEG_A1A2B_V1
  bot/config: aligned
  architecture: phase3_session_segmented
  phase_config: phase3 15m/1H ST12,3
  selected_combo: A1+A2+B
  session_gates: A1=overnight+asian; A2=london+london_ny+ny; B=london+london_ny+ny

USDJPY
  selected candidate: USDJPY_P3P7_XPHASE_A1A2B_V1
  bot/config: aligned
  architecture: cross_phase
  phase_config: A1=phase3 15m/1H ST12,3; A2=phase3 15m/1H ST12,3; B=phase7 15m/4H ST11,2
  selected_combo: A1+A2+B
  session_gates: A1=asian+london_ny; A2=asian+london+london_ny; B=asian+london_ny+ny
```

The copied `bot/config/<SYMBOL>.yaml` files are now frozen to the concluded
instrument-study and portfolio-study selections. The bot must read the selected
phase/combo and cross-phase branches from these files, not infer clocks from
legacy placeholder comments.

## Upgrade Phases

### Phase 0 - Preflight and Freeze

1. Confirm no old System C live positions should be carried into the new
   portfolio deployment.
2. Stop the existing bot.
3. Keep old state files only as manual backup if desired, but do not load them
   into the new runtime.
4. Discard old active logs for runtime purposes.
5. Use the prepared fresh V2 state files, or regenerate equivalent fresh state
   with `bot/runtime/state_store.py`.
6. Create a clean deployment marker in the new state:

```json
{
  "_version": "2.0",
  "deployment_id": "portfolio_option_a_202604",
  "fresh_deployment": true
}
```

### Phase 1 - Rebuild Deployment Config

Rebuild deployment config under `bot/config/` instead of patching stale legacy
`bot/config.yaml` in place.

Required runtime values:

```text
deployment_symbols = AUDUSD, EURJPY, EURUSD, USDJPY
portfolio.max_concurrent_live_trades = 2
base_risk_pct = 0.4 or 0.5
highwind.enabled = false for launch intervention
cb_anchor.enabled = false, monitor_only = true
```

Copy these source symbol configs into `bot/config/`:

```text
backtester/AUDUSD.yaml -> bot/config/AUDUSD.yaml
backtester/EURJPY.yaml -> bot/config/EURJPY.yaml
backtester/EURUSD.yaml -> bot/config/EURUSD.yaml
backtester/USDJPY.yaml -> bot/config/USDJPY.yaml
```

Add `bot/config/config.yaml` for runtime concerns such as deployment symbol
list, portfolio cap, risk, state file paths, logging paths, paper/live controls,
parallel evaluation switch, and kill-switch settings.

### Phase 2 - State v2

Replace the single mixed state shape with explicit portfolio and symbol state:

```text
portfolio:
  deployment_id
  base_equity
  peak_equity
  cb_anchor
  rule2
  global_paper_override
  next_paper_ticket

symbols:
  AUDUSD:
    mode
    engine_state
    highwind_monitor_state
    last_bar_times
    session_state
    session_summary
  EURJPY:
    ...

open_trades:
  shared list of live and paper trades
```

For launch, state v2 starts fresh. Do not attempt to migrate pivot arrays,
cooldown, ChoCh, Highwind windows, session summaries, or old open trade records
from the old deployment.

Startup recovery should still reconcile broker positions by magic number. If
old live positions exist, the cutover rule must decide whether to block startup
or close them explicitly. The safest default is to block startup and require a
manual decision.

### Phase 3 - MT5 Adapter Boundary

Create an adapter interface that both entrypoints use:

```text
initialize/connect
shutdown/close
account_info
positions_get
orders_get
history_deals_get
symbol_info
symbol_info_tick
copy_rates_from_pos
order_send
last_error
terminal_info
```

The RPyC adapter should normalize remote objects into local Python values before
they enter shared runtime code. This keeps `rpyc.utils.classic.obtain()` out of
the portfolio runner.

### Phase 4 - Sequential Snapshot Loop

Before any parallelism, change the loop into the architecture v2 order:

```text
1. broker poll / MT5 snapshot, sequential
2. monitor open trades and broker-side state
3. cheap new-bar gate for all symbols
4. indicator rebuild only for symbols with closed entry bars
5. evaluate all eligible symbols sequentially from frozen state snapshots
6. reduce candidates through portfolio layer
7. execute accepted orders sequentially
8. apply state/log/notifier updates
```

This phase should preserve single-threaded behavior while fixing the ownership
boundary. It is the most important safety step.

### Phase 5 - Backtester Engine Bridge

Use the backtester engine/config as the symbol authority.

Bridge tasks:

1. Load each selected symbol YAML from `backtester/`.
2. Apply its selected deployment phase/combo.
3. Build one `InstrumentEngine` per active symbol.
4. Serialize and hydrate engine state into `state["symbols"][symbol]`.
5. Feed the same confirmed-bar data used by the live bot into the engine.
6. Return zero or one candidate per symbol according to symbol-local policy.

If direct engine hydration is too large for the first patch, use an intermediate
compatibility wrapper around the current live signal functions, but keep the
public contract identical:

```text
evaluate_symbol(symbol, candle_bundle, symbol_state_snapshot, symbol_config)
  -> SymbolEvalResult(candidate, proposed_state, events)
```

### Phase 6 - Portfolio Reducer

Add final account admission after all symbol candidates are known.

Launch reducer rules:

```text
reject if Rule 2 blocks new entries
log CB would-trigger events, but do not reject because CB is monitor-only
reject if live open trade count >= 2
reject if symbol-local max concurrent rule is full
reject if latest broker-side position conflicts with state
reject if spread/stop-distance check fails
order admitted candidates deterministically
execute at most remaining portfolio slots
```

Deterministic order should be explicit. Recommended first rule:

```text
sort by candidate bar time, then symbol name, then same-bar priority B > A2 > A1
```

### Phase 7 - Logging Restart

Logs restart for the new portfolio deployment. Do not append old deployment rows
to the new active logs.

Recommended new paths:

```text
bot/logs/portfolio_option_a_202604/trades_{YYYYMM}.csv
bot/logs/portfolio_option_a_202604/events_{YYYYMM}.csv
bot/logs/portfolio_option_a_202604/signals_{YYYYMM}.csv
bot/logs/portfolio_option_a_202604/candidates_{YYYYMM}.csv
bot/logs/portfolio_option_a_202604/reducer_{YYYYMM}.csv
bot/logs/portfolio_option_a_202604/snapshot_{YYYYMM}.csv
bot/logs/portfolio_option_a_202604/timing_{YYYYMM}.csv
bot/logs/portfolio_option_a_202604/state_audit_{YYYYMM}.csv
bot/logs/portfolio_option_a_202604/gps/rolling_window_metrics.csv
bot/logs/portfolio_option_a_202604/gps/rolling_gps_report.md
bot/logs/portfolio_option_a_202604/gps/frontier_drift_report.md
```

Internal diagnostic log purpose:

```text
events_{YYYYMM}.csv
  lifecycle, startup, errors, warnings, invariant failures

signals_{YYYYMM}.csv
  every symbol evaluation result per new bar, including no-signal reasons

candidates_{YYYYMM}.csv
  every candidate generated before portfolio reduction

reducer_{YYYYMM}.csv
  every candidate accepted/rejected by the portfolio reducer with reason

snapshot_{YYYYMM}.csv
  per-loop account, open-trade, new-bar, and symbol eligibility summary

timing_{YYYYMM}.csv
  fetch/eval/reduce/execute/save timings

state_audit_{YYYYMM}.csv
  state transition summary per loop and per symbol
```

Minimum internal fields:

```text
loop_id
bar_time
symbol
entry_tf
context_tf
new_bar
mode
eval_status
signal_status
hypothesis
direction
candidate_id
portfolio_open_count_before
symbol_open_count_before
decision
reject_reason
state_version_before
state_version_after
```

Reducer reject/log reasons:

```text
portfolio_cap_full
symbol_cap_full
rule2_block
cb_monitor_only_would_trigger
spread_too_wide
stop_too_close
broker_conflict
mode_disabled
paper_only
order_send_failed
```

Loop invariant:

```text
eligible_symbols = evaluated_symbols + skipped_disabled + skipped_no_new_bar + skipped_data_error
```

If this does not balance, emit `LOOP_INVARIANT_FAIL` in the event log.

Minimum new event types:

```text
BOT_STARTED
SNAPSHOT_OK
SYMBOL_EVAL_OK
SYMBOL_EVAL_ERROR
CANDIDATE_ACCEPTED
CANDIDATE_REJECTED
PORTFOLIO_CAP_FULL
ORDER_PAPER
ORDER_PLACED
ORDER_FAIL
TRADE_CLOSED
STARTUP_BLOCKED_OLD_POSITION
LOOP_INVARIANT_FAIL
GPS_STATUS_GREEN
GPS_STATUS_YELLOW
GPS_STATUS_RED
GPS_STATUS_GRAY
```

### Phase 8 - Portfolio GPS / Rolling Review

Implement Step 8 from `docs/systemC-portfolio-frontier-bystep.md` as the live
portfolio review layer.

Purpose:

```text
Once the Option A portfolio is approved and deployed, monitor whether the
approved portfolio shape still exists.
```

Rolling windows:

```text
last 3 months
last 6 months
last 12 months
last 24 months
full available history
```

GPS status logic:

```text
Green: within expected rolling band, near approved frontier.
Yellow: degraded but not broken; review soon.
Red: hard DD / cluster / worst-month breach, or persistent negative marginal contribution.
Gray: not enough trades yet.
```

Bot implementation requirements:

```text
write enough trade log fields to rebuild rolling R by symbol/month
track accepted/skipped candidates for portfolio cap diagnostics
track live portfolio max DD, worst month, monthly std, and symbol contribution
emit GPS status events without changing trading behavior automatically
keep CB and Highwind monitor-only unless a later review explicitly promotes them
```

Review actions are manual decisions, not automatic bot behavior:

```text
keep portfolio unchanged
watchlist one symbol
reduce portfolio max N
increase portfolio max N
remove one symbol
rerun instrument study for one symbol
pause new additions until next review
```

Important operating rule:

```text
Three months is acceptable for portfolio-level monitoring.
Sparse symbols or hypotheses need a trade-count floor, or 6/12-month confirmation.
Do not change the live portfolio just because one short window moved slightly.
Change only when degradation persists or a hard risk boundary is breached.
```

Outputs:

```text
rolling_gps_report.md
rolling_window_metrics.csv
frontier_drift_report.md
```

### Phase 9 - MT5 Comment Contract

Patch both execution paths to use the architecture v2 comment format:

```text
SysC-{session}-{strategyName}
```

Keep timeout comments unchanged:

```text
SysC-timeout
```

### Phase 10 - Parallel Evaluation

Only enable parallel symbol evaluation after the sequential portfolio loop is
stable.

Worker rules:

```text
no mt5 calls
no file writes
no Telegram calls
no shared live state mutation
input is candles plus frozen state/config snapshot
output is candidate plus proposed state/events
```

Implementation:

```text
ThreadPoolExecutor(max_workers=min(4, len(symbols_to_evaluate)))
```

Parallelism remains per symbol, never per hypothesis.

Add a config switch:

```yaml
runtime:
  parallel_symbol_eval: false
  max_symbol_workers: 4
```

The first deploy should run with `parallel_symbol_eval: false`. Turn it on only
after timing logs show symbol evaluation is the bottleneck.

### Phase 11 - Tests and Verification

Add focused tests before live use:

```text
config bridge loads Option A symbols from backtester YAMLs
fresh state v2 contains all four symbols and no old log/state dependency
portfolio reducer enforces N=2
portfolio reducer deterministic ordering is stable
MT5 adapters normalize native and RPyC return values to the same shape
order comment format stays under 31 characters
workers fail if an MT5 adapter is accessed during symbol evaluation
GPS rolling metrics can be generated from fresh trade logs
GPS status stays advisory and does not auto-disable symbols
status.py loads bot/config/config.yaml instead of stale bot/config.yaml
status.py reads fresh state v2 and does not require active CB/Highwind keys
```

Manual dry runs:

```text
global paper mode with all four Option A symbols
VPS native runner smoke
RPyC runner smoke
forced two-open-trade state to verify third candidate is rejected
forced no-new-bar state to verify only monitoring runs
forced stale bar state to verify idle behavior still works
```

## Cutover Checklist

1. Stop old bot.
2. Confirm no old live positions should be carried into the new portfolio bot.
3. Rename or delete old active state files.
4. Rename or delete old active logs.
5. Start with clean state v2.
6. Run `.rpyc` in paper mode.
7. Run `.vps` in paper mode.
8. Verify both produce equivalent event/log/state behavior.
9. Enable live mode for Option A symbols only.
10. Start GPS monitoring from the fresh deployment logs.
11. Keep `parallel_symbol_eval` off for the first live deployment.
12. Enable parallel symbol evaluation only after timing logs justify it.

## Implementation Order

Recommended patch order:

1. Config alignment and fresh deployment log paths.
2. Shared adapters and thin runner wrappers.
3. State v2 store.
4. Sequential portfolio runner.
5. Portfolio reducer and N=2 cap.
6. Backtester config/engine bridge.
7. Execution/logging/notifier cleanup.
8. Portfolio GPS logging/report inputs.
9. RPyC reconnect integration.
10. Test suite.
11. Optional parallel symbol evaluation.

The key rule is to make the sequential portfolio loop correct first. Parallelism
is the final switch, not the foundation.

## Implementation Notes

### 2026-04-22 - Step 1 Runtime Config Foundation

Completed:

```text
bot/runtime/config.py
  loads bot/config/config.yaml
  loads bot/config/<SYMBOL>.yaml
  loads bot/.ennv without printing secrets
  resolves state/log/env paths from the bot repo
  validates Option A symbol setup against concluded docs/candidate registry
  validates Highwind off, CB monitor-only, Rule 2 on, parallel eval off

bot/runtime/state_store.py
  builds clean V2 live/paper state from runtime config
  writes active clean states
  writes local backup templates under bot/state/
  verifies active state and templates match deployment id, symbols, and flags

bot/state/README.md
  documents the local template recovery command
```

Verification:

```text
python3 -m runtime.config
python3 -m runtime.state_store --write-templates --reset-active --verify
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
from runtime.config import load_runtime_config
from runtime.state_store import verify_states
cfg = load_runtime_config()
verify_states(cfg)
print("import/runtime/state verify OK")
PY
```

Result:

```text
V2 runtime config verification: OK
V2 state verification: OK
```

### 2026-04-22 - Status.py V2 Observer

Completed:

```text
bot/status.py
  replaced with V2 observer
  loads runtime/config.py and state_store.py
  reads bot/config/config.yaml instead of stale bot/config.yaml
  reads state_live_portfolio_v2.json / state_paper_portfolio_v2.json
  shows portfolio cap, intervention flags, cache/clock/Numba settings
  shows per-symbol mode, phase/combo, cross-phase branches, session gates
  shows active/template state file health
  shows expected fresh log files and GPS outputs
  keeps --reset, --reset-highwind, and --rescale unavailable for V2

bot/status_v1_legacy.py
  preserves old V1 manual-control implementation separately
```

Verification:

```text
PYTHONDONTWRITEBYTECODE=1 python3 status.py --verify
PYTHONDONTWRITEBYTECODE=1 python3 status.py --paper
PYTHONDONTWRITEBYTECODE=1 python3 status.py --live
PYTHONDONTWRITEBYTECODE=1 python3 status.py --rescale
```

Result:

```text
V2 config/state verification: OK
--rescale is unavailable through V2 status while CB/Highwind/rescale controls are off
```

### 2026-04-22 - Adapters and Shared Runner Skeleton

Completed:

```text
bot/runtime/adapters.py
  NativeMt5Adapter
  RpycMt5Adapter
  normalizes account/position/order/tick/deal/terminal objects to local dicts
  keeps rpyc obtain() inside the adapter boundary

bot/runtime/runner.py
  SequentialPortfolioRunner
  loads V2 config/state
  writes BOT_STARTED, DRY_RUN_NO_MT5, SYMBOL_SKIPPED_NO_NEW_BAR, SNAPSHOT_OK
  walks all Option A symbols in deterministic config order
  keeps execution observation-only until data cache and engine bridge are wired
  supports --dry-run --once for verification without MT5

bot/runtime/logging.py
  writes fresh V2 event and snapshot CSV logs

bot/run_orders_vps.py
  thin V2 native MT5 wrapper

bot/run_orders_rpyc.py
  thin V2 RPyC MT5 wrapper

bot/run_orders_vps_v1_legacy.py
bot/run_orders_rpyc_v1_legacy.py
  old V1 runners preserved separately

bot/start_bridge.sh
  verifies bot/.ennv instead of .env
  probes EURUSD instead of GBPUSD
```

Verification:

```text
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_vps.py --dry-run --once
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_rpyc.py --dry-run --once
PYTHONDONTWRITEBYTECODE=1 python3 status.py --paper
```

Result:

```text
events_202604.csv and snapshot_202604.csv are created
status.py observes fresh V2 logs
no MT5 connection is attempted in dry-run mode
all four Option A symbols are traversed sequentially
```

### 2026-04-22 - Extra Step 5 GPS Readiness Status Check

Completed:

```text
bot/status.py --gps-check
  minimal terminal query
  reads selected V2 state
  shows last GPS status from diagnostics
  shows event/snapshot/trade/log freshness and last row marker
  shows expected GPS output files
  checks required trade log field contract
  reports missing trade log writer, runtime/gps.py, rolling metrics, GPS report, frontier drift
  prints target Step 8 design without running GPS calculations
```

Current expected result:

```text
empty or missing trade log:
  show seed baseline from docs/portfolio-study/202604-portfolio_deployment.md
  status = GRAY
  reason = Seed baseline only; no live portfolio trades logged yet.
  review action = collect live trade rows before making portfolio-level judgment

accumulated trade log:
  calculate interim closed trade count
  calculate total R
  calculate max DD R from cumulative trade R
  calculate worst month R
  list symbols seen in closed trades
  classify GRAY until gps.min_trade_count is reached
  classify GREEN/YELLOW from interim guardrails until runtime/gps.py exists
```

Target Step 8 implementation remains:

```text
runner/execution writes complete trade rows:
  close_time
  symbol
  candidate_id
  hypothesis
  session
  r_result
  risk_pct
  decision
  portfolio_open_count
  exit_reason

runtime/gps.py reads trade logs:
  computes 3m / 6m / 12m / 24m / full metrics
  computes total R, max DD, worst month, monthly std, symbol contribution
  classifies GRAY/GREEN/YELLOW/RED
  writes rolling_window_metrics.csv
  writes rolling_gps_report.md
  writes frontier_drift_report.md

status.py reads GPS outputs:
  shows current conclusion
  shows why
  shows review action suggestion
```

Verification:

```text
PYTHONDONTWRITEBYTECODE=1 python3 status.py --gps-check
PYTHONDONTWRITEBYTECODE=1 python3 status.py --live --gps-check
```

### 2026-04-22 - Step 6 Runtime Wiring

Completed:

```text
runtime/engine/* is a bot-local copy of the needed backtester engine modules
runtime/data_cache.py implements closed-bar MT5 polling with warm 500-bar cache and delta updates
runtime/engine_bridge.py rebuilds Numba-backed features from cached bars and evaluates copied engine state
runtime/portfolio.py reduces all symbol candidates against portfolio/symbol caps
runtime/execution.py writes paper trades and contains guarded MT5 live order_send support
runtime/gps.py generates rolling 3m/6m/12m/24m/full GPS reports
runtime/runner.py now runs the sequential V2 shape:
  all symbols refresh closed bars
  each symbol stages its own proposed engine state
  portfolio reducer decides accepted/rejected entries
  execution writes trades sequentially
  committed state/logs remain coherent
```

State separation rule:

```text
state.symbols.<SYMBOL>.engine_state is the only owner of per-symbol engine memory
runner stores proposed_engine_states by symbol during the loop
portfolio.open_trades remains portfolio-owned
symbol engine state is committed only after reducer/execution stage
signal/candidate/reducer/timing/state_audit logs record the sequential path
```

Launch guards:

```text
paper_mode remains true
execution.live_order_enabled remains false
CB and Highwind remain disabled + monitor_only
no direct imports from the backtester symlink are used by runtime/*
```

Notification guard:

```text
notifications.enabled=false
notifications.paper_trades=false
notifications.live_trades=true
runtime/notifications.py is the V2 Telegram boundary
execution calls notification only after trade state/log row is recorded
default launch remains silent for paper/shadow trades
```

### 2026-04-22 - Live Gate Flip

Operator decision:

```text
paper_mode=false
execution.live_order_enabled=true
notifications.enabled=true
notifications.paper_trades=false
notifications.live_trades=true
```

Implication:

```text
run_orders_vps.py without --dry-run can connect to MT5 and send live orders
run_orders_rpyc.py without --dry-run can connect through RPyC and send live orders
trade-open Telegram notification is enabled for live trades only
live state file is the active runtime state
```

Verification:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m runtime.config
PYTHONDONTWRITEBYTECODE=1 python3 -m runtime.state_store --verify
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_vps.py --dry-run --once
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_rpyc.py --dry-run --once
PYTHONDONTWRITEBYTECODE=1 python3 -m runtime.gps
PYTHONDONTWRITEBYTECODE=1 python3 status.py --gps-check
```
