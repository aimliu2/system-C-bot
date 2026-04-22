# policy.py — execution policy layer for SystemC
#
# Receives the list of fired candidates from strategy.evaluate_hypotheses()
# and decides which trade (if any) to accept and open.
#
# Three policy families:
#   Policy 1 — Free Running      each hypothesis fires independently, no routing
#   Policy 2 — Hypothesis Priority  one shared state machine, priority router
#   Policy 3 — Concurrent, Trigger Priority  all hypotheses monitored, trigger-first
#
# This module does NOT contain any indicator or trigger logic.
# All signal math lives in strategy.py.
#
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.engine.engine import InstrumentEngine

# ---------------------------------------------------------------------------
# Hypothesis classification — shared across Policy 2 and Policy 3
# ---------------------------------------------------------------------------

def classify_hypothesis(
    candidates:     list[dict],
    engine:         InstrumentEngine,
    config:         dict,
) -> tuple[Optional[str], bool]:
    """
    Apply the SystemC priority table to a list of fired candidates.

    Returns (hypothesis, allow_stack):
        hypothesis  — 'A1' | 'A2' | 'B' | None (no trade)
        allow_stack — False when conflict resolution occurred

    Priority table:
    ─────────────────────────────────────────────────────────────────
    Condition                                        Result    Stack
    ─────────────────────────────────────────────────────────────────
    B fired                                          B         B rules
    A1+A2 both fired, same direction                 A1        No
    A1+A2 both fired, A1 aligned with 1H            A1        No
    A1+A2 both fired, A2 aligned with 1H            A2        No
    A1+A2 both fired, neither aligned with 1H       NO TRADE  No
    A2 only                                          A2        Yes
    A1 only                                          A1        Yes
    None fired                                       skip      No
    ─────────────────────────────────────────────────────────────────
    """
    if not candidates:
        return (None, False)

    fired_hyps = {c['hypothesis']: c for c in candidates}

    # P1: B always wins if enabled
    if 'B' in fired_hyps and hypothesis_enabled(config, 'B'):
        return ('B', None)     # B has its own stacking rules

    a1_fired = 'A1' in fired_hyps and hypothesis_enabled(config, 'A1')
    a2_fired = 'A2' in fired_hyps and hypothesis_enabled(config, 'A2')

    # P2: conflict resolution
    if a1_fired and a2_fired:
        a1_c    = fired_hyps['A1']
        a2_c    = fired_hyps['A2']
        ctx_dir = 'long' if a1_c['context_dir'] == +1 else 'short'

        if a1_c['direction'] == a2_c['direction']:
            # Same direction — A1 wins as tiebreaker, no stack
            return ('A1', False)

        # Different directions — resolve by 1H alignment
        a1_aligned = (a1_c['direction'] == ctx_dir)
        a2_aligned = (a2_c['direction'] == ctx_dir)

        if a1_aligned and not a2_aligned:
            return ('A1', False)
        if a2_aligned and not a1_aligned:
            return ('A2', False)

        # Neither aligned — full chop, no trade
        return (None, False)

    # P3: single hypothesis
    if a2_fired:
        return ('A2', True)
    if a1_fired:
        return ('A1', True)

    return (None, False)


# ---------------------------------------------------------------------------
# Regime gate
# ---------------------------------------------------------------------------

def apply_regime_gate(
    hypothesis:      str,
    current_regime:  str,
    config:          dict,
) -> Optional[str]:
    """
    Check whether the chosen hypothesis is permitted in the current regime.

    Returns the hypothesis to run (may be a fallback), or None to skip.

    Block actions:
        'skip'               — bar skipped, no fallback
        'fallback_to_X'      — hand off to hypothesis X (X must still pass
                               its own gate and trigger)

    Config structure:
        config['regime_gate'] = {
            'A1': {'allowed_regimes': {'STEADY_TREND'}, 'block_action': 'skip'},
            'A2': {'allowed_regimes': {'ACTIVE_TREND'}, 'block_action': 'fallback_to_B'},
            'B':  {'allowed_regimes': {'ACTIVE_TREND'}, 'block_action': 'skip'},
        }
    """
    gate = config.get('regime_gate', {}).get(hypothesis)
    if not gate:
        return hypothesis  # no gate defined → always allowed

    allowed = gate.get('allowed_regimes')
    if allowed is None or current_regime in allowed:
        return hypothesis

    action = gate.get('block_action', 'skip')
    if action == 'skip':
        return None
    if action.startswith('fallback_to_'):
        return action.removeprefix('fallback_to_')

    return None


# ---------------------------------------------------------------------------
# Stacking rules
# ---------------------------------------------------------------------------

def can_stack_hyp_b(engine: InstrumentEngine, direction: str) -> bool:
    """Hyp B stacks same-direction only."""
    if not engine.open_trades:
        return True
    return engine.all_open_same_direction(direction)


def can_stack(
    hypothesis:      str,
    allow_stack_flag: Optional[bool],
    engine:          InstrumentEngine,
    direction:       str,
) -> bool:
    """
    Unified stacking check.

    allow_stack_flag: from classify_hypothesis()
                      False  — conflict resolution occurred, single position only
                      True   — clean single hypothesis, stacking permitted
                      None   — Hyp B (uses its own rules)
    """
    if hypothesis == 'B':
        return can_stack_hyp_b(engine, direction)

    if not allow_stack_flag:
        return False   # conflict resolution — no stack

    if not engine.open_trades:
        return True

    return engine.all_open_same_direction(direction)


def policy3_can_stack(engine: InstrumentEngine, config: dict, direction: str) -> bool:
    """Policy 3 can be configured to allow any-direction concurrent holds."""
    policy3_cfg = config.get('execution', {}).get('policy3', config.get('policy3', {}))
    if policy3_cfg.get('allow_any_direction_stack', False):
        return True
    if not engine.open_trades:
        return True
    return engine.all_open_same_direction(direction)


# ---------------------------------------------------------------------------
# Pullback check
# ---------------------------------------------------------------------------

def is_pullback_trade(direction: str, context_dir: int) -> bool:
    """
    Pullback = trading AGAINST the context TF (1H) ST direction.
    Hyp B is never a pullback by definition.
    """
    ctx = 'long' if context_dir == +1 else 'short'
    return direction != ctx


def hypothesis_enabled(config: dict, hyp: str) -> bool:
    hyp_cfg = config.get('hypotheses', {}).get(hyp, {})
    legacy_key = f'hyp_{hyp.lower()}_enabled'
    return bool(hyp_cfg.get('enabled', config.get(legacy_key, True)))


def pullback_gate_enabled(config: dict, hyp: str) -> bool:
    hyp_cfg = config.get('hypotheses', {}).get(hyp, {})
    if 'pullback_gate' in hyp_cfg:
        return bool(hyp_cfg['pullback_gate'])
    return bool(config.get('no_pullback', True)) and hyp != 'B'


# ---------------------------------------------------------------------------
# Policy 1 — Free Running
# ---------------------------------------------------------------------------

def policy_1(
    candidates: list[dict],
    engine:     InstrumentEngine,
    config:     dict,
) -> list[dict]:
    """
    Free Running: each hypothesis fires independently.

    All fired candidates are returned as separate accepted trades.
    No routing, no conflict resolution, no stacking limit.

    Use this to measure additive standalone edge.
    NOT suitable for live deployment — ignores capital/margin constraints.

    Returns a list of accepted candidates (may be >1 per bar).
    """
    if not candidates:
        return []

    no_pullback = config.get('no_pullback', True)
    accepted    = []

    for c in candidates:
        hyp = c['hypothesis']

        if not hypothesis_enabled(config, hyp):
            continue

        if no_pullback and pullback_gate_enabled(config, hyp):
            if is_pullback_trade(c['direction'], c['context_dir']):
                continue

        accepted.append({**c, 'policy': 'P1', 'allow_stack': True})

    return accepted


# ---------------------------------------------------------------------------
# Policy 2 — Hypothesis Priority (shared state, priority router)
# ---------------------------------------------------------------------------

def policy_trigger_first_shared_priority(
    candidates:     list[dict],
    engine:         InstrumentEngine,
    config:         dict,
) -> Optional[dict]:
    """
    Trigger-first shared priority: one shared state machine routes to at most
    one already-fired hypothesis per bar.

    Steps:
    1. classify_hypothesis() — resolve which hypothesis wins
    2. apply_regime_gate()   — check if allowed in current regime
    3. can_stack()           — check stacking rules
    4. no-pullback gate      — reject if pullback and config says no
    5. Return accepted candidate or None

    If the winning hypothesis is blocked by regime gate with fallback_to_X,
    X is evaluated through its own gate (but trigger is NOT re-checked —
    X must have already fired for the fallback to be valid).
    """
    if not candidates:
        return None

    hyp, allow_stack = classify_hypothesis(candidates, engine, config)
    if hyp is None:
        return None

    fired_hyps  = {c['hypothesis']: c for c in candidates}
    regime      = _get_regime(candidates)
    no_pullback = config.get('no_pullback', True)

    # Regime gate loop — allows one fallback step
    for _ in range(2):
        routed = apply_regime_gate(hyp, regime, config)

        if routed is None:
            return None  # skip

        if routed != hyp:
            # Fallback — check that fallback hypothesis actually fired
            if routed not in fired_hyps:
                return None  # fallback hypothesis never triggered this bar
            hyp = routed
            # Fallback hypothesis uses its own allow_stack (True — no conflict)
            allow_stack = True
            continue

        break  # no fallback

    candidate = fired_hyps.get(hyp)
    if candidate is None:
        return None

    direction = candidate['direction']

    # No-pullback gate
    if no_pullback and pullback_gate_enabled(config, hyp):
        if is_pullback_trade(direction, candidate['context_dir']):
            return None

    # Stacking gate
    if not can_stack(hyp, allow_stack, engine, direction):
        return None

    return {**candidate, 'policy': 'P2', 'allow_stack': allow_stack}


# Backwards-compatible name. This is intentionally trigger-first; the legacy
# context-first Policy 2 from Phase2 scripts must be implemented as a separate
# router because it chooses the hypothesis before checking that hypothesis'
# trigger.
policy_2 = policy_trigger_first_shared_priority


# ---------------------------------------------------------------------------
# Policy 3 — Concurrent Hypothesis, Trigger Priority
# ---------------------------------------------------------------------------

def policy_3(
    candidates:     list[dict],
    engine:         InstrumentEngine,
    config:         dict,
) -> Optional[dict]:
    """
    Concurrent Hypothesis, Trigger Priority.

    All hypotheses are monitored simultaneously.
    Decision is made only at trigger time, not at routing time.

    Same-bar priority when multiple triggers fire: B > A2 > A1

    Steps:
    1. Filter by enabled + no-pullback gate + regime gate
    2. Sort by trigger priority (B > A2 > A1)
    3. Check stacking for the highest-priority candidate
    4. Return it or None

    Unlike Policy 2, if A1 is highest priority but blocked, A2 is
    still evaluated rather than skipping the bar entirely.
    """
    if not candidates:
        return None

    for c in candidates:
        c.pop('_policy_skip_reason', None)

    priority_list = config.get('execution', {}).get('same_bar_priority', ['B', 'A2', 'A1'])
    priority    = {hyp: idx for idx, hyp in enumerate(priority_list)}
    regime      = _get_regime(candidates)
    no_pullback = config.get('no_pullback', True)

    # Filter and sort by priority
    eligible = []
    for c in sorted(candidates, key=lambda x: priority.get(x['hypothesis'], 99)):
        hyp       = c['hypothesis']
        direction = c['direction']

        if not hypothesis_enabled(config, hyp):
            c['_policy_skip_reason'] = 'hypothesis_disabled'
            continue

        routed = apply_regime_gate(hyp, regime, config)
        if routed is None:
            c['_policy_skip_reason'] = 'regime_gate_blocked'
            continue  # blocked, try next priority
        if routed != hyp:
            # Fallback — only valid if fallback hypothesis also fired
            fallback_c = next((x for x in candidates if x['hypothesis'] == routed), None)
            if fallback_c is None:
                c['_policy_skip_reason'] = f'fallback_{routed}_not_triggered'
                continue
            c = fallback_c
            hyp       = routed
            direction = c['direction']

        if no_pullback and pullback_gate_enabled(config, hyp):
            if is_pullback_trade(direction, c['context_dir']):
                c['_policy_skip_reason'] = 'pullback_gate_blocked'
                continue

        eligible.append((c, hyp, direction))

    # Try each eligible candidate from highest to lowest priority
    for c, hyp, direction in eligible:
        max_positions = (
            config.get('execution', {}).get('max_concurrent_positions_per_symbol',
                                           config.get('max_concurrent_positions_per_symbol'))
        )
        # _effective_open_count is injected by SplitCursor to include pending-but-not-yet-
        # filled entries in the cross-phase runner. Falls back to open_trades for the
        # single-phase cursor where fill happens at the very next bar before any signal eval.
        exec_cfg = config.get('execution', {})
        open_count = exec_cfg.get('_effective_open_count', len(engine.open_trades))
        if max_positions is not None and open_count >= int(max_positions):
            for candidate, _, _ in eligible:
                candidate['_policy_skip_reason'] = 'symbol_cap_full'
            return None

        if policy3_can_stack(engine, config, direction):
            for other, _, _ in eligible:
                if other is not c and '_policy_skip_reason' not in other:
                    other['_policy_skip_reason'] = 'same_bar_priority_lost'
            return {**c, 'policy': 'P3', 'allow_stack': True}

        c['_policy_skip_reason'] = 'stack_blocked'

    return None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _get_regime(candidates: list[dict]) -> str:
    """Extract regime label from candidates (all share the same bar regime)."""
    for c in candidates:
        if 'regime' in c:
            return c['regime']
    return 'UNKNOWN'
