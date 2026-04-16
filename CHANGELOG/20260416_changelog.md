# 2026-04-16 Changelog

## GBPJPY Phase 5 Deployment Preparation

Prepared the System C bot codebase for GBPJPY Phase 5 `A1+A2+B` shadow deployment.

### Configuration

- Added `GBPJPY` to `deployment_symbols`.
- Added granular indicator config:
  - Global default entry SuperTrend: `ST(12,3)`
  - Global default context SuperTrend: `ST(12,3)`
  - GBPJPY entry override: 15m `ST(11,2)`
  - GBPJPY context override: 1H `ST(12,3)`
- Added global no-countertrend contract:
  - `countertrend_trades.enabled: false`
- Added GBPJPY trading windows:
  - `00:00-21:00 UTC`
- Updated GBPJPY to paper/shadow mode for pilot validation:
  - `mode: paper`
- Enabled all three GBPJPY hypotheses:
  - `A1`
  - `A2`
  - `B`
- Updated GBPJPY stop parameters:
  - A1 `sl_min: 0.15`, `sl_max: 0.25`
  - A2 `sl_min: 0.15`, `sl_max: 0.25`, `sl_epsilon: 0.03`
  - B `sl_fixed: 0.25`
- Recalibrated GBPJPY Highwind for Phase 5 combined-stack performance:
  - `halt_threshold: 0.34`
  - `l2_threshold: 0.37`
  - `l1_threshold: 0.40`
  - seed window `12W / 18L`

### Code

- Updated `config_loader.py`:
  - Added per-timeframe SuperTrend accessor support.
  - Added `trading_windows` support.
  - Preserved legacy `trading_hours` compatibility.
  - Added single-hour shorthand support, e.g. `[11]` becomes `[11,12]`.
  - Added `is_in_trading_window()`.
  - Added `countertrend_enabled()`.
  - Added validation for trading windows and per-timeframe ST configs.
  - Updated config summary output to show windows and entry/context ST separately.
- Updated `run_data.py`:
  - 15m indicators now use entry ST config.
  - 1H indicators now use context ST config.
- Updated `run_orders_vps.py`:
  - Uses `is_in_trading_window()` instead of a single `trading_hours` interval.
  - Enforces no-countertrend alignment before A1/A2/B eligibility.
  - Updated A1 EMA3 trajectory check to match Phase 2/5 study behavior.
- Updated `run_orders_rpyc.py`:
  - Uses `is_in_trading_window()` instead of a single `trading_hours` interval.
  - Enforces no-countertrend alignment before A1/A2/B eligibility.
  - Synced A1 context/trigger logic with the study/VPS behavior.

### Validation

- Syntax check passed for:
  - `config_loader.py`
  - `run_data.py`
  - `run_orders_vps.py`
  - `run_orders_rpyc.py`
  - `status.py`
- Config validation passed with dummy MT5 environment variables.
- Effective GBPJPY config resolves to:
  - `mode: PAPER`
  - `A1 ON / A2 ON / B ON`
  - `windows=00-21UTC`
  - `ST(entry=11,2.0 context=12,3.0)`
  - `countertrend_trades.enabled=false`
- Verified fractional window shorthand:
  - `[[0,7], [11], [13,21]]` resolves to `[(0,7), (11,12), (13,21)]`.

## Post-Audit Execution Fixes

Applied technical adversary-audit fixes for GBPJPY Phase 5 and EURUSD live execution.

### Configuration

- Added explicit A2 same-direction stacking cap:
  - `hyp_a2.max_stack: 99`
  - Preserves study-supported pyramiding behavior while making the cap configurable.

### Code

- Updated `config_loader.py`:
  - Added validation for optional hypothesis `max_stack`.
  - Validation now rejects non-integer or `< 1` stack caps cleanly.
- Updated `run_orders_vps.py`:
  - Moved B/ChoCh dispatch ahead of the no-countertrend alignment gate.
  - B now keeps its intended pullback/ChoCh exception while A1/A2 remain gated by 15m/1H alignment.
  - Added configurable same-hypothesis stack-cap enforcement via effective hypothesis config.
- Updated `run_orders_rpyc.py`:
  - Mirrored the VPS B/ChoCh dispatch ordering fix.
  - Mirrored configurable same-hypothesis stack-cap enforcement.

### Validation

- Config validation passed:
  - `python3 config_loader.py`
- Syntax check passed for:
  - `config_loader.py`
  - `run_orders_vps.py`
  - `run_orders_rpyc.py`
- Diff whitespace check passed:
  - `git diff --check`

## AUDUSD Phase 6 Timeframe Parity Preparation

Prepared the codebase for AUDUSD London+NY Phase 6 `A2-only` paper deployment.

### Configuration

- Added `AUDUSD` to `deployment_symbols`.
- Added AUDUSD paper/shadow configuration:
  - `mode: paper`
  - `timeframes.entry: M15`
  - `timeframes.context: H4`
  - `A1 OFF / A2 ON / B OFF`
  - London+NY window `07:00-21:00 UTC`
- Added AUDUSD A2 stop parameters:
  - `sl_min: 0.0015`
  - `sl_max: 0.0025`
  - `sl_epsilon: 0.0003`
- Seeded AUDUSD Highwind from Phase 6 A2-only study:
  - Study WR `50.63%`
  - Payoff `+1.5R / -1R`
  - Break-even WR `40.0%`
  - `halt_threshold: 0.40`
  - `l2_threshold: 0.45`
  - `l1_threshold: 0.50`
  - seed window `16W / 14L`

### Code

- Updated `config_loader.py`:
  - Added effective timeframe resolver for `entry` and `context`.
  - Added timeframe validation.
  - Updated config summary to print entry/context TFs.
- Updated `run_data.py`:
  - Entry/context bar pulls now use configured timeframes instead of hardcoded `M15/H1`.
  - Added generic context indicator path while preserving `df_1h` compatibility aliases.
  - Regime classifier now forward-fills configured context bars onto entry bars.
- Updated `run_orders_vps.py` and `run_orders_rpyc.py`:
  - New-bar detection now uses configured entry timeframe.
  - Context stability now uses configured context timeframe.
  - Startup prints and signal replay headers now use generic entry/context wording.
  - State keeps new `last_bar_times.entry/context` keys while preserving legacy `m15/h1`.
- Updated `status.py`:
  - Last-processed display now shows each symbol's configured entry/context TFs.

### Validation

- Config validation passed and resolved:
  - `EURUSD M15/H1 A2+B live`
  - `GBPJPY M15/H1 A1+A2+B paper`
  - `AUDUSD M15/H4 A2-only paper`
- Syntax check passed for:
  - `config_loader.py`
  - `run_data.py`
  - `run_orders_vps.py`
  - `run_orders_rpyc.py`
  - `status.py`
- Diff whitespace check passed:
  - `git diff --check`

### Remaining Work

- Add a bot-side historical replay harness that executes the same live strategy logic against historical GBPJPY data.
- Use replay to confirm close parity with Phase 5 study metrics before promoting GBPJPY from paper to live.
