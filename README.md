# System C Bot V2

System C Bot V2 is the portfolio runtime for the April 2026 Option A deployment.
It replaces the old single-symbol/sequential-state bot with a portfolio loop:

```text
all symbols produce candidates
portfolio reducer decides final entries
MT5 executes sequentially
state/logs update coherently
```

The current runtime is live-enabled. Treat this repository state as capable of
sending real MT5 orders when `run_orders_vps.py` or `run_orders_rpyc.py` is run
without `--dry-run`.

## Features

- V2 runtime config loader that ignores stale root `config.yaml`.
- Four-symbol portfolio deployment: `AUDUSD`, `EURJPY`, `EURUSD`, `USDJPY`.
- Per-symbol YAML configs copied into `bot/config/`.
- Bot-local copy of the needed backtester engine under `runtime/engine/`.
- Closed-bar MT5 data cache with 500-bar startup warmup and delta updates.
- Numba-backed indicator rebuild path for SuperTrend/features.
- Mixed-timeframe support by symbol and branch.
- Per-symbol staged engine state, with portfolio-owned open trades.
- Portfolio reducer with portfolio cap and symbol cap checks.
- Native MT5 and RPyC MT5 adapters.
- Live order execution guarded by config.
- Broker/state reconciliation before entries, including broker-side SL/TP/manual close detection.
- V2 trade/event/signal/candidate/reducer/snapshot/timing/state-audit logs.
- GPS reports for rolling portfolio health, written every 5 minutes or immediately after a broker close.
- Telegram notification boundary for live trade opens and a daily status update.
- Fresh V2 live and paper state files plus backup templates.
- VPS console startup banner, ALIVE heartbeat, and market-data stale warning.

## Current Deployment

Active config:

```text
bot/config/config.yaml
```

Current deployment id:

```text
portfolio_option_a_202604
```

Current symbols:

```text
AUDUSD
EURJPY
EURUSD
USDJPY
```

Current portfolio setup:

```text
portfolio cap: 2 concurrent trades
risk per trade: 0.5%
CB anchor: disabled, monitor-only
Highwind: disabled, monitor-only
Rule 2: enabled
parallel symbol evaluation: disabled
```

Current runtime gates:

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

That means live order sending is armed. Paper/shadow mode is not the current
default.

## Expected Performance

The deployment baseline is Option A clean raw deployment.

Source:

```text
docs/portfolio-study/202604-portfolio_deployment.md
bot/config/config.yaml gps.seed_baseline
```

Historical reference:

```text
total R:        534.0R
EV/trade:        0.0766R
max DD:         38.5R
R/DD:           13.87
worst month:   -22.5R
monthly std:    11.86R
```

Account-level drawdown translation from the portfolio deployment note:

```text
0.4% risk/trade -> historical DD about 15.4% from the seed study
0.5% risk/trade -> same 38.5R historical DD maps to about 19.25%
0.5% risk/trade with 2x stress maps to about 38.5%
```

GPS begins as `GRAY` because the new live deployment has no closed live trade
rows yet. Once closed trades accumulate, GPS reads the trade log and writes:

```text
logs/portfolio_option_a_202604/gps/rolling_window_metrics.csv
logs/portfolio_option_a_202604/gps/rolling_gps_report.md
logs/portfolio_option_a_202604/gps/frontier_drift_report.md
```

The GPS windows are:

```text
3m
6m
12m
24m
full
```

Classification intent:

```text
GRAY   not enough closed live trades yet
GREEN  live rolling shape remains inside guardrails
YELLOW degraded versus interim seed guardrails, review soon
RED    severe degradation, pause/reduce/stop and review
```

GPS cadence:

```text
gps.loop_interval_seconds: 300
gps.run_on_trade_close: true
```

The bot does not recompute GPS every 5-second trading loop. GPS is trade-log
based, so it runs every 5 minutes and is forced immediately after broker
reconciliation records a closed trade.

## Config Locations

Runtime portfolio config:

```text
bot/config/config.yaml
```

Symbol configs:

```text
bot/config/AUDUSD.yaml
bot/config/EURJPY.yaml
bot/config/EURUSD.yaml
bot/config/USDJPY.yaml
```

Secrets:

```text
bot/.ennv
```

V2 runtime code:

```text
bot/runtime/
```

Legacy V1 files:

```text
bot/lecagy/
```

The old root `bot/config.yaml`, root `config_loader.py`, root `notifier.py`,
and root `run_data.py` are retired from the V2 active path.

## Live, Shadow, Notifications

To run live:

```yaml
paper_mode: false

execution:
  live_order_enabled: true
```

To run shadow/paper with real MT5 data but no live orders:

```yaml
paper_mode: true

execution:
  live_order_enabled: false
```

To disable all Telegram notifications:

```yaml
notifications:
  enabled: false
  paper_trades: false
  live_trades: true
  daily_status: true
  daily_status_utc_hour: 7
```

To enable live trade-open notifications and the daily London-open status update:

```yaml
notifications:
  enabled: true
  paper_trades: false
  live_trades: true
  daily_status: true
  daily_status_utc_hour: 7
```

To disable the daily status update while keeping live trade-open notifications:

```yaml
notifications:
  enabled: true
  paper_trades: false
  live_trades: true
  daily_status: false
  daily_status_utc_hour: 7
```

To enable paper/shadow notifications too:

```yaml
notifications:
  enabled: true
  paper_trades: true
  live_trades: true
  daily_status: true
  daily_status_utc_hour: 7
```

## Running The Bot

Native MT5 VPS runner:

```bash
cd bot
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_vps.py
```

RPyC bridge runner:

```bash
cd bot
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_rpyc.py
```

One-loop dry run with no MT5 connection and no orders:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_vps.py --dry-run --once
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_rpyc.py --dry-run --once
```

Stop a continuous runner gracefully by creating the kill file from another
terminal:

```bash
cd bot
touch STOP
```

If running from outside the bot folder, use the full path:

```bash
touch /path/to/bot/STOP
```

Windows VPS PowerShell equivalent:

```powershell
New-Item -ItemType File -Path C:\SystemC\STOP -Force
```

If `New-Item` is unavailable or fails on Windows Server 2016, use:

```powershell
Set-Content -Path C:\SystemC\STOP -Value ""
```

or:

```powershell
"" | Out-File -FilePath C:\SystemC\STOP -Encoding ascii
```

Windows VPS `cmd.exe` equivalent:

```bat
type nul > C:\SystemC\STOP
```

or:

```bat
copy NUL C:\SystemC\STOP
```

Verify the file exists:

```bat
dir C:\SystemC\STOP
```

The `STOP` file must be created in the bot's actual working directory, the same
folder as `run_orders_vps.py`.

The runner checks this file between loops, logs `BOT_STOPPED`, exits the loop,
and closes the MT5 adapter. This is preferred over `Ctrl+C` because it avoids
interrupting an order, reconciliation, or state write mid-step.

## Console Heartbeat And Stale Markets

On startup, V2 prints a console banner with mode, deployment, symbols, risk,
live-order gate, notification gate, state file, open trade count, and STOP file.
It also detects the MT5 broker-server clock offset from UTC, for example:

```text
Broker UTC offset: UTC+3 (OK)
```

The offset can be UTC+2 or UTC+3 depending on broker DST. V2 uses this offset to
normalize MT5 bar timestamps back to UTC before session filters, feature
alignment, candidate IDs, logs, and stale-market checks.

During continuous runs, V2 prints an `ALIVE` heartbeat every
`runtime.heartbeat_minutes`:

```text
runtime.poll_interval_seconds: 5
runtime.heartbeat_minutes: 15
```

The 5-second poll controls MT5 closed-bar checks and signal detection. The
15-minute heartbeat controls only terminal output. A 15-minute heartbeat does
not make the bot miss 5-minute bars.

Market-data stale behavior:

```text
runtime.market_data_stale_minutes: 180
runtime.market_data_stale_poll_seconds: 60
```

If no deployed symbol produces a new closed entry bar for 180 minutes, V2 prints
and logs:

```text
MARKET_DATA_STALE no closed entry bars for <minutes> minutes; possible holiday/weekend/feed issue
```

While stale, the outer poll sleep backs off from 5 seconds to 60 seconds. When a
closed entry bar appears again, V2 prints/logs `MARKET_DATA_RESUMED` and returns
to 5-second polling.

V2 does not infer closed bars by comparing broker timestamps directly to local
UTC. The cache asks MT5 for `start_pos=1`, which is the latest closed bar, and
normalizes broker-server timestamps by the detected offset. This avoids false
stale states when the broker clock is UTC+2/UTC+3.

One-shot market-data probe:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_vps.py --probe-market-data --probe-bars 20
PYTHONDONTWRITEBYTECODE=1 python3 run_orders_rpyc.py --probe-market-data --probe-bars 20
```

The probe writes a separate file:

```text
logs/portfolio_option_a_202604/market_probe_202604.csv
```

Expected healthy result is mostly or entirely `OK`. `EMPTY_RATES` points to
symbol/history/feed availability. `FUTURE_AFTER_OFFSET` means the detected
broker offset did not fully explain the MT5 timestamps.

## Status And Performance Review

Verify config and both V2 states:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 status.py --verify
```

View active state. This follows `paper_mode`:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 status.py
```

View live state explicitly:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 status.py --live
```

View paper state explicitly:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 status.py --paper
```

Minimal GPS/log/performance query:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 status.py --gps-check
```

Live GPS query:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 status.py --live --gps-check
```

`--gps-check` answers:

```text
which state is active
last GPS status from state diagnostics
seed baseline when live trade rows are empty
trade log freshness and schema readiness
event/signal/candidate/reducer/snapshot/timing/state-audit freshness
GPS report file paths and freshness
current interim conclusion
review action suggestion
GPS run/skip diagnostics
```

To decide if the portfolio has degraded, check:

```text
current_interim_status.status
current_interim_status.reason
current_interim_status.review_action
closed_trades
total_r
max_dd_r
worst_month_r
symbols
```

With no closed live trades, the expected answer is:

```text
status: GRAY
reason: Seed baseline only; no live portfolio trades logged yet.
review_action: Collect live trade rows before making portfolio-level judgment.
```

After trades accumulate, status compares the live trade log against the seed
guardrails and the GPS reports.

## State Recovery

V2 active states:

```text
bot/state_live_portfolio_v2.json
bot/state_paper_portfolio_v2.json
```

Templates:

```text
bot/state/state_live_portfolio_v2.template.json
bot/state/state_paper_portfolio_v2.template.json
```

Restore paper state:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 status.py --restore-paper
```

Restore live state:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 status.py --restore-live
```

Live restore asks for confirmation.

## Important Notes

- V2 does not migrate old state.
- V2 logs are a fresh deployment series under `logs/portfolio_option_a_202604/`.
- CB and Highwind are not active controls in this launch.
- Current live risk is `portfolio.base_risk_pct: 0.5`; the `gps.seed_baseline`
  risk value remains historical metadata from the seed study.
- `status.py` is observation-first. Legacy reset/highwind/rescale controls are
  intentionally unavailable in V2.
- Live Telegram trade-open notification fires only after the trade state and
  trade log row are recorded.
- Daily Telegram status fires once per UTC day at or after
  `notifications.daily_status_utc_hour`. It reports mode, deployment, symbols,
  equity/balance, open trades, broker positions, market-data status, latest
  entry bar, and GPS status. It intentionally excludes Highwind, Rule2, and CB.
- Live state is persisted immediately after successful `order_send` and ticket
  capture, before notification and nonessential logs.
