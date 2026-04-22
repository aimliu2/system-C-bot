# execution engine : store execution state
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """A single open or closed position."""

    trade_id:        int
    symbol:          str
    hypothesis:      str           # 'A1' | 'A2' | 'B'
    direction:       str           # 'long' | 'short'
    entry_time:      pd.Timestamp
    entry_price:     float
    sl:              float
    tp:              float

    # Filled on close
    exit_time:       Optional[pd.Timestamp] = None
    exit_price:      Optional[float]        = None
    exit_reason:     Optional[str]          = None  # 'tp' | 'sl' | 'manual'
    r_result:        Optional[float]        = None  # +1.5 / -1.0 etc.

    # Flags
    is_reentry:      bool = False
    is_stacked:      bool = False
    conflict_winner: Optional[str] = None  # 'A1' | 'A2' | None

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    @property
    def sl_distance(self) -> float:
        return abs(self.entry_price - self.sl)

    def close(
        self,
        exit_time: pd.Timestamp,
        exit_price: float,
        exit_reason: str,
    ) -> None:
        self.exit_time   = exit_time
        self.exit_price  = exit_price
        self.exit_reason = exit_reason
        sl_dist          = self.sl_distance
        if sl_dist == 0:
            self.r_result = 0.0
        elif self.direction == 'long':
            self.r_result = (exit_price - self.entry_price) / sl_dist
        else:
            self.r_result = (self.entry_price - exit_price) / sl_dist


# ---------------------------------------------------------------------------
# Per-pivot state  (resets on each ST flip)
# ---------------------------------------------------------------------------

@dataclass
class PivotState:
    """
    State that belongs to a single ST episode (between two flips).
    Reset fully on every new ST flip via reset().
    """

    # Hyp A2 — new extreme tracking
    new_extreme_flag: bool = False

    # Hyp B — one structural break entry per pivot
    sb_used: bool = False

    def reset(self) -> None:
        self.new_extreme_flag = False
        self.sb_used          = False


# ---------------------------------------------------------------------------
# ChoCh state  (persists until invalidated or context flip)
# ---------------------------------------------------------------------------

@dataclass
class ChochState:
    """
    Tracks a confirmed Change of Character event.
    Persists across bars until price closes back through the ChoCh level
    or the context TF direction changes.
    """

    confirmed:  bool              = False
    direction:  Optional[int]     = None   # +1 bullish ChoCh | -1 bearish ChoCh
    level:      Optional[float]   = None   # price level that confirmed ChoCh
    confirmed_time: Optional[pd.Timestamp] = None

    def set(self, direction: int, level: float, confirmed_time: pd.Timestamp) -> None:
        self.confirmed      = True
        self.direction      = direction
        self.level          = level
        self.confirmed_time = confirmed_time

    def invalidate(self) -> None:
        self.confirmed      = False
        self.direction      = None
        self.level          = None
        self.confirmed_time = None

    def still_valid(self, close: float) -> bool:
        """
        ChoCh is invalidated when price closes back through the ChoCh level.
        """
        if not self.confirmed or self.level is None:
            return False
        if self.direction == +1:
            return close > self.level
        if self.direction == -1:
            return close < self.level
        return False


# ---------------------------------------------------------------------------
# Cooldown state  (resets after COOLDOWN_BARS following an SL hit)
# ---------------------------------------------------------------------------

@dataclass
class CooldownState:
    """
    Enforces a bar-count pause after an SL hit.
    Driven by COOLDOWN_BARS from strategy config.
    """

    active:         bool = False
    bars_elapsed:   int  = 0

    def trigger(self) -> None:
        self.active       = True
        self.bars_elapsed = 0

    def tick(self, cooldown_bars: int) -> str:
        """
        Advance one bar.

        Returns
        -------
        'active'   — engine runs normally (not in cooldown)
        'waiting'  — in cooldown, do not evaluate hypotheses
        'reassess' — cooldown just expired, re-evaluate context
        """
        if not self.active:
            return 'active'
        self.bars_elapsed += 1
        if self.bars_elapsed >= cooldown_bars:
            self.active       = False
            self.bars_elapsed = 0
            return 'reassess'
        return 'waiting'

    def reset(self) -> None:
        self.active       = False
        self.bars_elapsed = 0


# ---------------------------------------------------------------------------
# Pivot array
# ---------------------------------------------------------------------------

@dataclass
class PivotArray:
    """
    Rolling array of confirmed swing highs and lows.

    A new pivot is pushed each time the ST flips:
      - flip to bullish  → the low before the flip becomes a confirmed swing LOW
      - flip to bearish  → the high before the flip becomes a confirmed swing HIGH

    Bounded by PIVOT_MAXLEN (default 8) to limit memory.
    """

    maxlen: int = 8
    pivots: list = field(default_factory=list)  # list[dict]

    def push(self, pivot_type: str, price: float, bar_time: pd.Timestamp) -> None:
        """pivot_type: 'high' | 'low'"""
        self.pivots.append({'type': pivot_type, 'price': price, 'time': bar_time})
        if len(self.pivots) > self.maxlen:
            self.pivots.pop(0)

    def last_high(self) -> Optional[float]:
        for p in reversed(self.pivots):
            if p['type'] == 'high':
                return p['price']
        return None

    def last_low(self) -> Optional[float]:
        for p in reversed(self.pivots):
            if p['type'] == 'low':
                return p['price']
        return None

    def has_recent_flip(self) -> bool:
        """True when at least one confirmed pivot exists (a flip has occurred)."""
        return len(self.pivots) > 0

    def clear(self) -> None:
        self.pivots.clear()


# ---------------------------------------------------------------------------
# InstrumentEngine  —  owns all execution state for one symbol
# ---------------------------------------------------------------------------

class InstrumentEngine:
    """
    Maintains all execution state for a single trading instrument.

    The cursor calls on_bar() once per closed entry-TF bar, passing
    precomputed feature rows. This class never looks ahead.

    State groups
    ------------
    pivot_state   — resets on every ST flip
    choch         — persists until price invalidates it or context TF flips
    cooldown      — resets after COOLDOWN_BARS post-SL hit
    pivot_array   — rolling confirmed swing highs / lows (maxlen 8)
    open_trades   — list[Trade], currently open positions
    trade_log     — list[Trade], all closed trades (append-only)
    """

    def __init__(self, symbol: str, config: dict) -> None:
        self.symbol      = symbol
        self.config      = config

        self.hypothesis_states = {
            'A1': PivotState(),
            'A2': PivotState(),
            'B': PivotState(),
        }
        # Backward-compatible alias. A2 owns the new-extreme state.
        self.pivot_state = self.hypothesis_states['A2']
        self.choch       = ChochState()
        self.cooldowns   = {
            'A1': CooldownState(),
            'A2': CooldownState(),
            'B': CooldownState(),
        }
        self.cooldown    = self.cooldowns['A1']
        self._cooldown_status = {'A1': 'active', 'A2': 'active', 'B': 'active'}
        self.pivot_array = PivotArray(maxlen=config.get('mechanics', {}).get('pivot_maxlen', 8))

        self.open_trades:  list[Trade] = []
        self.trade_log:    list[Trade] = []
        self._trade_counter: int       = 0

        # Last seen entry-TF ST direction — used to detect flips bar-by-bar
        self._last_st_dir: Optional[int] = None
        self._episode_high: Optional[float] = None
        self._episode_low: Optional[float] = None
        self._episode_high_time: Optional[pd.Timestamp] = None
        self._episode_low_time: Optional[pd.Timestamp] = None

    # ------------------------------------------------------------------
    # Public interface called by the cursor
    # ------------------------------------------------------------------

    def on_bar(
        self,
        feature_row: pd.Series,
        context_row: pd.Series,
    ) -> Optional[dict]:
        """
        Process one closed entry-TF bar.

        Parameters
        ----------
        feature_row : pd.Series
            Precomputed 15m features at this bar's close.
            Required columns: close, high, low, st_dir, st_line,
                              ema3, ema20, rsi30, atr10, atr50, regime.
        context_row : pd.Series
            Latest available 1H features (as-of merged, no future leak).
            Required columns: st_dir.

        Returns
        -------
        dict or None
            Candidate entry dict if a hypothesis trigger fires, else None.
            The policy layer decides whether to accept or skip.
        """
        # Capture prior entry direction before flip processing so _update_choch
        # can detect ChoCh on the same bar as the ST flip (flip-bar ChoCh).
        prior_entry_dir = self._last_st_dir

        # 1. Detect ST flip → push pivot, reset pivot_state
        self._handle_st_flip(feature_row)

        # 2. Advance hypothesis-scoped cooldowns
        self._tick_cooldowns()
        cooldown_status = 'active'

        # 3. Validate / detect ChoCh
        self._update_choch(feature_row, context_row, prior_entry_dir=prior_entry_dir)

        # 4. Update A2 new_extreme_flag
        self._update_new_extreme(feature_row, context_row)

        # 5. Evaluate hypothesis triggers (delegated to strategy.py)
        return self._evaluate(feature_row, context_row, cooldown_status)

    def open_trade(self, trade: Trade) -> None:
        """Accept a trade from the policy layer and register it as open."""
        self.open_trades.append(trade)

    def accept_candidate(self, candidate: dict) -> None:
        """
        Apply irreversible signal-use mutations only after policy acceptance.

        Trigger functions must stay pure so skipped/rejected candidates do not
        accidentally consume per-pivot state.
        """
        if candidate.get('hypothesis') == 'B':
            self.state_for('B').sb_used = True

    def state_for(self, hypothesis: str) -> PivotState:
        """Return mutable execution state for one hypothesis."""
        return self.hypothesis_states[hypothesis]

    def cooldown_status_for(self, hypothesis: str) -> str:
        """Return the current bar's cooldown status for one hypothesis."""
        return self._cooldown_status.get(hypothesis, 'active')

    def has_open_hypothesis(self, hypothesis: str) -> bool:
        """True when this hypothesis already has an open trade."""
        return any(t.hypothesis == hypothesis for t in self.open_trades)

    def check_exits(
        self,
        bar_time: pd.Timestamp,
        high: float,
        low: float,
    ) -> list[Trade]:
        """
        Check all open trades for TP or SL hit on this bar's high/low range.

        When both TP and SL fall within the same bar, SL is assumed first
        (conservative default). The 1m execution clock in the cursor
        overrides this when 1m data is available.

        Returns the list of trades that closed this bar.
        """
        closed = []
        for trade in list(self.open_trades):
            exit_reason, exit_price = self._check_sl_tp(trade, high, low)
            if exit_reason:
                trade.close(bar_time, exit_price, exit_reason)
                self.open_trades.remove(trade)
                self.trade_log.append(trade)
                closed.append(trade)
                hyp_state = self.state_for(trade.hypothesis)
                if exit_reason == 'sl':
                    if self._cooldown_enabled() and self._cooldown_bars() > 0:
                        self.cooldowns[trade.hypothesis].trigger()
                    hyp_state.reset()
                else:  # tp
                    hyp_state.reset()
        return closed

    def make_trade(
        self,
        hypothesis: str,
        direction: str,
        entry_time: pd.Timestamp,
        entry_price: float,
        sl: float,
        tp: float,
        is_reentry: bool = False,
        is_stacked: bool = False,
        conflict_winner: Optional[str] = None,
    ) -> Trade:
        """Construct a Trade record. Does not open it — policy decides that."""
        self._trade_counter += 1
        return Trade(
            trade_id        = self._trade_counter,
            symbol          = self.symbol,
            hypothesis      = hypothesis,
            direction       = direction,
            entry_time      = entry_time,
            entry_price     = entry_price,
            sl              = sl,
            tp              = tp,
            is_reentry      = is_reentry,
            is_stacked      = is_stacked,
            conflict_winner = conflict_winner,
        )

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    @property
    def has_open_trades(self) -> bool:
        return len(self.open_trades) > 0

    @property
    def open_directions(self) -> list[str]:
        return [t.direction for t in self.open_trades]

    def all_open_same_direction(self, direction: str) -> bool:
        return all(t.direction == direction for t in self.open_trades)

    def snapshot(self) -> dict:
        """
        Point-in-time snapshot of all execution state.
        Written to the event ledger on every candidate event.
        """
        return {
            'symbol':           self.symbol,
            'new_extreme_flag': self.state_for('A2').new_extreme_flag,
            'sb_used':          self.state_for('B').sb_used,
            'choch_confirmed':  self.choch.confirmed,
            'choch_direction':  self.choch.direction,
            'choch_level':      self.choch.level,
            'choch_confirmed_time': self.choch.confirmed_time,
            'in_cooldown':      any(c.active for c in self.cooldowns.values()),
            'cooldown_bars':    {h: c.bars_elapsed for h, c in self.cooldowns.items()},
            'cooldown_status':  dict(self._cooldown_status),
            'pivot_count':      len(self.pivot_array.pivots),
            'last_high':        self.pivot_array.last_high(),
            'last_low':         self.pivot_array.last_low(),
            'open_trade_count': len(self.open_trades),
            'open_directions':  self.open_directions,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_st_flip(self, feature_row: pd.Series) -> None:
        """
        Detect a change in entry-TF ST direction.
        On flip: push confirmed pivot to pivot_array, reset pivot_state.

        Pivot assignment on flip:
          flip to bullish  → prior episode was bearish → push swing LOW
          flip to bearish  → prior episode was bullish → push swing HIGH
        """
        st_dir = int(feature_row['st_dir'])

        high = float(feature_row['high'])
        low  = float(feature_row['low'])
        bar_time = feature_row.name

        if self._last_st_dir is None:
            self._last_st_dir = st_dir
            self._episode_high = high
            self._episode_low = low
            self._episode_high_time = bar_time
            self._episode_low_time = bar_time
            return

        if st_dir != self._last_st_dir:
            if st_dir == +1:
                pivot_price = self._episode_low if self._episode_low is not None else low
                pivot_time = self._episode_low_time or bar_time
                self.pivot_array.push('low', pivot_price, pivot_time)
            else:
                pivot_price = self._episode_high if self._episode_high is not None else high
                pivot_time = self._episode_high_time or bar_time
                self.pivot_array.push('high', pivot_price, pivot_time)

            self._reset_hypothesis_states()
            self._episode_high = high
            self._episode_low = low
            self._episode_high_time = bar_time
            self._episode_low_time = bar_time
        else:
            if self._episode_high is None or high > self._episode_high:
                self._episode_high = high
                self._episode_high_time = bar_time
            if self._episode_low is None or low < self._episode_low:
                self._episode_low = low
                self._episode_low_time = bar_time

        self._last_st_dir = st_dir

    def _update_choch(
        self,
        feature_row: pd.Series,
        context_row: pd.Series,
        prior_entry_dir: Optional[int] = None,
    ) -> None:
        """
        Validate existing ChoCh or detect a new one.

        Existing ChoCh is invalidated if:
          - price closes back through the ChoCh level, OR
          - context TF direction has changed since ChoCh was set

        New ChoCh conditions:
          Bullish: ctx bullish, entry TF was bearish (prior bar), close > last swing HIGH
          Bearish: ctx bearish, entry TF was bullish (prior bar), close < last swing LOW

        Uses prior_entry_dir (direction before this bar's ST flip) so that ChoCh is
        correctly detected on the same bar as an ST flip. Without this, flip-bar ChoCh
        events would be missed because _handle_st_flip updates entry_dir before this runs.
        """
        ctx_dir   = int(context_row['st_dir'])
        entry_dir = int(feature_row['st_dir'])
        # Use prior direction if available — catches flip-bar ChoCh correctly
        check_dir = prior_entry_dir if prior_entry_dir is not None else entry_dir
        close     = float(feature_row['close'])

        if self.choch.confirmed:
            if not self.choch.still_valid(close) or ctx_dir != self.choch.direction:
                self.choch.invalidate()
            return  # one ChoCh active at a time

        if ctx_dir == +1 and check_dir == -1:
            last_high = self.pivot_array.last_high()
            if last_high and close > last_high:
                self.choch.set(direction=+1, level=last_high, confirmed_time=feature_row.name)

        elif ctx_dir == -1 and check_dir == +1:
            last_low = self.pivot_array.last_low()
            if last_low and close < last_low:
                self.choch.set(direction=-1, level=last_low, confirmed_time=feature_row.name)

    def _update_new_extreme(self, feature_row: pd.Series, context_row: pd.Series) -> None:
        """
        Hyp A2 Step 2: arm new_extreme_flag when close breaks beyond
        the last confirmed pivot in the current OF direction.
        Stays armed until pivot_state.reset() on the next ST flip.

        OF direction source is controlled by hypotheses.A2.of_direction_mode:
          lax    — use context TF ST direction (always defined, phase-agnostic)
          strict — derive from pivot HH+HL pattern; skip if ambiguous (returns None)
        """
        a2_state = self.state_for('A2')
        if a2_state.new_extreme_flag:
            return

        close   = float(feature_row['close'])
        a2_cfg  = self.config.get('hypotheses', {}).get('A2', {})
        of_mode = a2_cfg.get('of_direction_mode', 'strict')

        if of_mode == 'lax':
            of_dir = int(context_row['st_dir'])
        else:
            of_dir = self._of_direction_from_pivot()
            if of_dir is None:
                return

        if of_dir == +1:
            last_high = self.pivot_array.last_high()
            if last_high and close > last_high:
                a2_state.new_extreme_flag = True

        elif of_dir == -1:
            last_low = self.pivot_array.last_low()
            if last_low and close < last_low:
                a2_state.new_extreme_flag = True

    def _tick_cooldowns(self) -> None:
        """Advance cooldown state independently for each hypothesis."""
        if not self._cooldown_enabled():
            self._cooldown_status = {hyp: 'active' for hyp in self.cooldowns}
            return
        bars = self._cooldown_bars()
        self._cooldown_status = {
            hyp: cooldown.tick(bars)
            for hyp, cooldown in self.cooldowns.items()
        }

    def _cooldown_enabled(self) -> bool:
        execution = self.config.get('execution', {})
        return bool(execution.get('cooldown_enabled', self.config.get('cooldown_enabled', True)))

    def _cooldown_bars(self) -> int:
        execution = self.config.get('execution', {})
        return int(execution.get('cooldown_bars', self.config.get('cooldown_bars', 6)))

    def _reset_hypothesis_states(self) -> None:
        for state in self.hypothesis_states.values():
            state.reset()

    def _of_direction_from_pivot(self) -> Optional[int]:
        """Return +1 for HH/HL, -1 for LH/LL, or None when inconclusive."""
        highs = [p['price'] for p in self.pivot_array.pivots if p['type'] == 'high']
        lows  = [p['price'] for p in self.pivot_array.pivots if p['type'] == 'low']

        if len(highs) >= 2 and len(lows) >= 2:
            if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
                return +1
            if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
                return -1

        return None

    def _evaluate(
        self,
        feature_row: pd.Series,
        context_row: pd.Series,
        cooldown_status: str,
    ) -> Optional[dict]:
        """Delegate to strategy.py to keep trigger logic separate."""
        from runtime.engine.strategy import evaluate_hypotheses
        return evaluate_hypotheses(self, feature_row, context_row, cooldown_status)

    def _check_sl_tp(
        self,
        trade: Trade,
        high: float,
        low: float,
    ) -> tuple[Optional[str], Optional[float]]:
        """
        Returns (exit_reason, exit_price) or (None, None).
        SL takes priority when both hit on the same bar (conservative default).
        """
        if trade.direction == 'long':
            sl_hit = low  <= trade.sl
            tp_hit = high >= trade.tp
        else:
            sl_hit = high >= trade.sl
            tp_hit = low  <= trade.tp

        if sl_hit:
            return ('sl', trade.sl)
        if tp_hit:
            return ('tp', trade.tp)
        return (None, None)
