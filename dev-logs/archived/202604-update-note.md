# Update Log

---

## [PATCH] MT5 Order Comment Format — 2026-04-16

**Change:** Simplify MT5 order comment to `{SystemName}-{session}-{strategyName}`

**Before:**
```
SysC-{symbol}-{session}-{hypothesis}-{regime[:8]}
```
Example: `SysC-EURUSD-London-H1-trending`

**After:**
```
SysC-{session}-{strategyName}
```
Example: `SysC-London-HB`

**Rationale:**
- Symbol is redundant (already tracked via ticket/position)
- Regime tag adds noise and risks exceeding MT5's 31-char limit
- Cleaner comment improves readability directly on MT5 terminal

**Files to patch:**
- `run_orders_vps.py` — line ~1233–1236: update `comment` construction
- `run_orders_rpyc.py` — line ~1275–1277: same change

**Timeout order comment** (`SysC-timeout`) — no change required.

---