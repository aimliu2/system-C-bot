# System C Bot Architecture v2

_Canonical runtime architecture, April 2026_

This document is the single source of truth for System C bot runtime layering,
symbol state ownership, MT5 execution flow, and the planned parallel evaluation
model.

Older notes live in `bot/docs/references/`.

---

## 1. Architecture Summary

System C runs as one MT5-account trading system with three runtime layers:

```text
Portfolio / account layer
  shared capital, account drawdown protocol, final admission

Symbol / instrument layer
  one symbol engine + one config + one mutable state per active symbol

MT5 I/O layer
  one sequential bridge for all broker reads and writes
```

The bot uses one universal symbol engine implementation. Pair differences belong
in config and state, not in pair-specific engine files.

```text
EURUSD -> SymbolEngine(config=EURUSD, state=EURUSD)
GBPUSD -> SymbolEngine(config=GBPUSD, state=GBPUSD)
USDJPY -> SymbolEngine(config=USDJPY, state=USDJPY)
```

Do not create files such as `EURUSD-engine.py` or `USDJPY-engine.py` by default.
Only create an adapter if a broker/instrument has a genuinely exceptional
runtime requirement.

---

## 2. State Ownership

### Portfolio / Account Layer

Portfolio state is shared because all active symbols trade through one MT5
account and one capital pool.

```text
Owns:
  account equity
  base equity
  CB Anchor
  Rule 2 hard floor
  withdrawal / rescale state
  global paper override
  portfolio admission / account guardrails
  account-level exposure/capital controls
```

Portfolio controls answer:

```text
Can the account accept any new trade right now?
Can the account accept this symbol candidate after all symbol engines report?
Should new entries pause because account drawdown crossed a floor?
```

CB Anchor and Rule 2 are portfolio/account controls. They are not per-symbol
strategy states.

### Symbol / Instrument Layer

Each active symbol owns its own local trading state.

```text
Owns per symbol:
  selected execution policy
  active hypothesis set, e.g. A2+B
  entry/context timeframe config
  ST / indicator parameters
  pip size and stop settings
  pivot array
  A1/A2/B hypothesis state
  ChoCh state
  new_extreme / sb_used
  cooldown
  Highwind window and HALT/shadow state
  EV monitor window
  open trades for that symbol
  last entry/context bar times
  session state and session summaries
```

Symbol controls answer:

```text
Did this symbol produce a valid A1/A2/B opportunity?
Which hypothesis wins inside this symbol?
Is this symbol cooling down or halted by its own Highwind state?
Can this symbol stack another trade under local rules?
```

For multi-symbol operation:

```text
5 active symbols = 5 symbol engines + 5 symbol states + 1 portfolio layer
```

A1, A2, and B can run together inside one symbol engine. They must not share
mutable state with another symbol.

---

## 3. Execution Policy Boundary

Symbol engines enforce symbol-local trading logic:

```text
one accepted candidate per symbol per bar
selected symbol execution policy
B > A2 > A1 same-bar priority where applicable
A1/A2 conflict resolution where applicable
same-direction stacking and local max stack
symbol cooldown
symbol session window
symbol Highwind live/paper/HALT behavior
```

The portfolio reducer does not re-run A1/A2/B routing. It receives at most one
candidate per symbol and decides final account admission.

Portfolio reducer enforces:

```text
CB Anchor
Rule 2
global paper override
account-level max order attempts per loop
cross-symbol exposure/capital guardrails
spread and stop-distance checks
latest broker-side open-trade conflict checks
deterministic execution order when multiple symbol candidates arrive together
```

---

## 4. MT5 I/O Rule

MT5 is treated as a single-lane I/O bridge.

All `mt5.*` calls stay in one controlled sequential path. Parallelism is allowed
only for pure signal calculation from prefetched candles and frozen state
snapshots.

MT5 operations that remain sequential:

```text
mt5.initialize
mt5.copy_rates_*
mt5.positions_get
mt5.orders_get
mt5.history_deals_get
mt5.symbol_info
mt5.symbol_info_tick
mt5.account_info
mt5.order_send
SL/TP repair
timeout close
```

This avoids duplicate orders, unsafe state writes, and broker-side race
conditions.

### MT5 Order Comment Contract

Live order comments should stay short, readable, and within MT5's 31-character
comment limit.

Current format:

```text
SysC-{session}-{strategyName}
```

Example:

```text
SysC-London-HB
```

Do not include symbol or regime tags in live order comments by default. Symbol is
already available from the ticket/position context, and regime tags add noise
while increasing the risk of comment truncation.

Timeout order comments keep their existing fixed form:

```text
SysC-timeout
```

---

## 5. Target Loop

```text
Loop tick
  1. MT5 snapshot, sequential
     - positions_get once
     - account_info once
     - symbol_info / tick cache
     - copy_rates for active symbols and configured timeframes

  2. New-bar gate
     - only run full strategy evaluation for symbols whose entry/context bar changed
     - if no new bar, only monitor open trades, SL/TP repair, timeout, heartbeat

  3. Pure symbol evaluation
     - initially sequential
     - later parallel by symbol after 5-symbol stability is proven
     - each symbol evaluates A1/A2/B together
     - returns zero or one symbol candidate plus proposed symbol-state transition

  4. Deterministic reducer, sequential
     - collect symbol candidates
     - apply portfolio/account guardrails
     - re-check open-trade conflicts against latest state snapshot

  5. MT5 execution, sequential
     - place/close/repair orders one at a time
     - before each order, refresh critical broker-side checks if needed

  6. State/log/Telegram, sequential
     - apply accepted state transitions
     - save state atomically
     - send notifications after state is coherent
```

---

## 6. Parallelism Plan

Parallelize by symbol, not by strategy.

Good:

```text
worker(EURUSD) evaluates EURUSD A1/A2/B together
worker(USDJPY) evaluates USDJPY A1/A2/B together
worker(GBPJPY) evaluates GBPJPY A1/A2/B together
```

Bad:

```text
worker(A1)
worker(A2)
worker(B)
```

A1/A2/B inside a symbol share symbol-local execution state, cooldown, stacking
rules, Highwind mode, and conflict rules. Splitting them into separate live bot
workers would create routing and state problems.

### Worker Contract

```text
evaluate_symbol(symbol, candles, symbol_state_snapshot, symbol_config)
    -> candidate | None, proposed_symbol_state_transition
```

Worker rules:

```text
No mt5.* calls.
No file writes.
No Telegram calls.
No mutation of shared live state.
Read only candle bundle, symbol config, and frozen symbol-state snapshot.
Local mutation inside a copied/throwaway symbol engine is allowed.
Return candidate plus proposed state transition metadata.
```

### Initial Parallel Implementation

Do not begin with parallel execution. First prove the 5-symbol loop is stable
single-threaded.

After stability:

```python
max_workers = min(8, len(active_symbols))

with ThreadPoolExecutor(max_workers=max_workers) as pool:
    futures = {
        pool.submit(
            evaluate_symbol,
            sym,
            candles[sym],
            state_snapshot,
            config_snapshot,
        ): sym
        for sym in symbols_to_evaluate
    }
```

For 5 pairs, use 4-5 workers. For 11-15 pairs, start with 6-8 workers. More
workers should be added only if timing logs prove evaluation is still the real
bottleneck.

---

## 7. Paper / Live / Disabled Modes

System C supports three levels of paper/live control.

### Level 1: Global Paper Override

```yaml
paper_mode: true   # all instruments shadow-trade
paper_mode: false  # each instrument follows its own mode
```

When global paper mode is active, no real orders are sent to MT5.

### Level 2: Per-Instrument Mode

Configured under `instruments.<SYM>.mode` or overridden at runtime in
`state["instrument_modes"]`.

```yaml
instruments:
  EURUSD:
    mode: live
  GBPUSD:
    mode: paper
  USDJPY:
    mode: disabled
```

Runtime commands:

```text
python3 status.py --shadow EURUSD
python3 status.py --live EURUSD
python3 status.py --disable EURUSD
```

State overrides config after startup.

### Level 3: Highwind Auto-Shadow

Highwind is symbol/instrument-level protection. It tracks rolling performance
per symbol across all active hypotheses combined.

When a symbol degrades below its HALT threshold:

```text
state["instrument_modes"][SYMBOL] = "paper"
state["instrument_highwind"][SYMBOL].halted = true
event = INSTRUMENT_HALTED
```

The symbol continues signal detection in shadow mode. No real order is sent.

Effective mode priority:

```text
global paper_mode > state instrument override > config initial mode
```

---

## 8. Highwind

Highwind is the symbol degradation protector.

It belongs to the symbol/instrument layer, not the portfolio layer.

```text
Tracks:
  rolling last N completed trades per symbol
  all active hypotheses combined
  symbol Highwind level
  symbol HALT/shadow state
```

Counting rules:

```text
TP hit              -> WIN, counted
SL hit              -> LOSS, counted
Timeout profit      -> WIN, counted
Timeout loss        -> LOSS, counted
Manual close profit -> exclude
Manual close loss   -> LOSS, counted
CB skip / cooldown  -> exclude
```

Current operational decision:

```text
Highwind auto-recovery is allowed for simplicity.
Dead-cat-bounce risk is accepted and monitored through rolling review.
```

If a stricter protocol is needed later, add explicit shadow-count and shadow-WR
recovery gates before restoring a halted symbol to live.

---

## 9. CB Anchor and Rule 2

CB Anchor and Rule 2 are portfolio/account controls.

They answer whether the shared account should accept any new risk, regardless of
which symbol produced the signal.

### CB Anchor

Purpose:

```text
last portfolio protection layer
session/sub-session drawdown brake
```

Current design:

```text
CB is a ratchet, not a full peak reset.
When CB triggers, it protects the next lower stair-step of portfolio equity.
```

CB needs portfolio simulation:

```text
Portfolio without CB
Portfolio with CB
Portfolio with looser CB
Portfolio with tighter CB
```

The live bot should keep CB portfolio-scoped. It should not be implemented as a
per-symbol state.

### Rule 2

Rule 2 is a hard account floor.

```text
base equity set at system start or after rescale
if equity <= base * floor_threshold -> stop new entries
resume on configured next-session/day rule
```

Rule 2 is also portfolio/account-level only.

---

## 10. State Files and Atomicity

There is one owner for live state writes.

State/log/Telegram side effects happen after the reducer and execution path have
made a coherent decision.

Rules:

```text
workers never write state
workers never write files
workers never call Telegram
workers return proposed state transitions
main loop applies accepted transitions sequentially
state save is atomic
```

Paper and live modes may use separate state files where configured:

```text
state_file_paper
state_file_live
```

When multiple MT5 terminals are considered in the future, each terminal/process
must have:

```text
one owner per symbol
separate state files
separate Telegram identity or clear message prefix
cross-process account/risk guard if sharing one account
no duplicate symbol deployment across bots
```

---

## 11. Startup Recovery

On bot startup, reconcile broker positions with state before new entries.

Recovery should handle:

```text
broker position exists but state missing -> orphan handling
state trade exists but broker position missing -> close-history lookup
paper trades -> skip broker reconciliation, resolve from stored paper state
missing or invalid close price -> do not update Highwind/session/Telegram
```

Close-history safety matters because invalid close accounting can corrupt:

```text
R result
Highwind window
session summaries
Telegram reporting
portfolio analysis logs
```

---

## 12. Timing Instrumentation

Add timing logs before scaling symbol count or adding parallelism.

Suggested shape:

```text
[TIMING] symbols=5 fetch=0.42s monitor=0.08s eval=1.80s reduce=0.01s execute=0.35s save=0.02s total=2.68s
```

Also log:

```text
active symbol count
symbols evaluated after new-bar gate
candidate signals returned
orders attempted
MT5 close-history failures
max single-symbol evaluation time
```

Parallelism is justified only if timing logs show pure symbol evaluation is the
real bottleneck.

---

## 13. Scale-Up Plan

### Phase 0: Safety Patch Before More Pairs

```text
fix close-history lookup
guard against exit_price <= 0.0
do not update Highwind/session/Telegram with invalid R
add timing instrumentation
confirm new-bar gate
confirm state templates match evolved schema
confirm each configured pair has config completeness
```

Config completeness per symbol:

```text
pip_size
entry/context timeframe
mode
trading hours/windows
active hypotheses
stop settings
Highwind seed
```

### Phase 1: Run 5 Pairs Single-Threaded

Start with operational stability, not speed.

```text
keep trusted symbols live
keep newer pairs paper
run all 5 through the same single-threaded MT5 loop
enable timing logs
watch loop duration, missed bars, duplicate candidates, bad close history, state drift
```

Success criteria:

```text
no impossible R values
no UNKNOWN / 0.0 close events reaching Telegram
no duplicate same-symbol opposite-direction trades
no state corruption after restart
loop time stays below poll/new-bar budget
timing logs identify eval as bottleneck before parallelism is added
```

### Phase 2: Add Symbol-Level Parallel Evaluation

After 5-pair stability is confirmed, add `ThreadPoolExecutor` around pure
symbol evaluation only.

The reducer, MT5 execution, and state writes remain sequential.

### Phase 3: Scale Toward 11-15 Pairs

Add pairs in batches of 2-3:

```text
run new pairs in paper first
seed Highwind from study results
verify pip size and stop distance by symbol
verify entry/context timeframe behavior
verify trade close history on at least one paper SL/TP/manual close
promote to live only after close accounting is clean
```

---

## 14. Definition of Done

The bot architecture is correct when:

```text
MT5 calls are centralized and sequential.
Signal evaluation can run in parallel without touching MT5 or mutating state.
Each symbol has isolated state and one universal engine instance.
Portfolio/account controls own shared capital risk.
Reducer makes deterministic final decisions.
Order execution remains sequential.
State writes remain atomic and single-owner.
Timing logs prove the loop can handle the configured symbol count.
Invalid close history cannot produce impossible R or corrupt Highwind.
```
