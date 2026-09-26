"""Live screener.

Design choice worth stating plainly: this runs the *same* code path as the
backtester. Each sweep re-fetches a short tail of history, builds the same
`Series`, and calls the same `scan()`. Signals newer than the last one emitted
get dispatched.

The alternative -- a websocket fleet maintaining its own rolling state -- is
faster but means two implementations of every rule, and the live one is the
one you cannot test. Divergence there is how a screener ends up firing on
conditions its backtest never saw. Correctness first; latency is a known,
bounded cost (one sweep interval) and can be bought down later by feeding the
same `Series` from websockets instead of REST.

Rate limits set the ceiling on universe size: Binance bills open-interest
requests against a 1000-per-5-minute pool, i.e. ~200/min, so one symbol per
sweep-minute is the budget. `--top` exists for that reason.
"""
from __future__ import annotations

import logging
import threading
import time

from .backtest.engine import build_rule, interval_for
from .config import NotifySettings, Rules
from .data.series import load_many
from .models import Exchange, Signal
from .notify.dispatch import Dispatcher
from .rules.base import SignalState, scan

log = logging.getLogger(__name__)

# How much history each sweep pulls. Must comfortably exceed the longest
# lookback any rule uses (flat_before reaches back flat_lookback_h hours).
TAIL_HOURS = 8.0


class Screener:
    def __init__(
        self,
        exchange: Exchange,
        rule_names: list[str],
        symbols: list[str],
        rules: Rules,
        dispatcher: Dispatcher,
        *,
        sweep_s: float = 60.0,
        workers: int = 8,
        require_filters: bool = True,
    ):
        self.exchange = exchange
        self.symbols = symbols
        self.rules = rules
        self.dispatcher = dispatcher
        self.sweep_s = sweep_s
        self.workers = workers
        self.require_filters = require_filters
        self.built = [build_rule(n, rules) for n in rule_names]
        # Carried across sweeps so the daily ordinal and the cooldown are
        # continuous, rather than restarting inside each 8-hour tail.
        self._state = SignalState()
        self._warm = False
        self.emitted = 0

    def required_sweep_s(self) -> float:
        """Shortest sweep interval this universe can sustain inside the budgets.

        Each (symbol, bar-interval) costs one kline call at weight 10 against
        the /fapi pool and one OI call against the separate /futures/data
        pool. Two rules with different bar sizes therefore cost DOUBLE, which
        is how the defaults (100 symbols, 2 rules, 60s) ended up over both
        limits at once -- every sweep overran, `stop.wait` degenerated to
        wait(0), and the rate limiter became the only pacing.
        """
        intervals = {interval_for(r.name) for r in self.built}
        calls = len(self.symbols) * len(intervals)
        # WeightBudget applies 0.8 headroom to both pools.
        fapi_s = calls * 10 * 60.0 / (2400 * 0.8)
        data_s = calls * 60.0 / (1000 * 0.8 / 5)
        return max(fapi_s, data_s)

    def run(self, stop: threading.Event) -> None:
        needed = self.required_sweep_s()
        if needed > self.sweep_s:
            log.warning(
                "%d symbols x %d bar-interval(s) needs a %.0fs sweep to stay "
                "inside the rate limits; raising --sweep from %.0fs. Lower "
                "--top for faster sweeps.",
                len(self.symbols),
                len({interval_for(r.name) for r in self.built}),
                needed, self.sweep_s)
            self.sweep_s = needed

        while not stop.is_set():
            started = time.monotonic()
            try:
                self._sweep()
            except Exception as e:
                log.exception("sweep failed: %s", e)
            elapsed = time.monotonic() - started
            if elapsed > self.sweep_s:
                log.warning("sweep took %.0fs, longer than the %.0fs interval -- "
                            "reduce --top or raise --sweep", elapsed, self.sweep_s)
            stop.wait(max(0.0, self.sweep_s - elapsed))

    def _sweep(self) -> None:
        by_interval: dict[str, list] = {}
        for rule in self.built:
            by_interval.setdefault(interval_for(rule.name), []).append(rule)

        now = int(time.time() * 1000)
        fresh: list[Signal] = []

        for interval, rules in by_interval.items():
            step = 60_000 * {"1m": 1, "5m": 5}[interval]
            end = now // step * step
            start = end - int(TAIL_HOURS * 3_600_000)
            # No cache: a live tail must be re-read every sweep, and cached
            # ranges would pin us to stale bars.
            series_map = load_many(
                self.exchange, self.symbols, interval, start, end,
                with_oi=True, cache=None, workers=self.workers,
            )
            for rule in rules:
                for series in series_map.values():
                    fresh.extend(scan(rule, series, state=self._state))

        # The first sweep sees eight hours of history at once. Emitting it
        # would blast a wall of stale alerts, so the first pass only primes
        # the de-duplication state.
        if not self._warm:
            self._warm = True
            log.info("warm-up: primed %d historical signals, none sent", len(fresh))
            return

        fresh.sort(key=lambda s: s.ts)
        for sig in fresh:
            if self.require_filters and sig.filters and not sig.passed_all_filters:
                continue
            self.dispatcher.submit(sig)
            self.emitted += 1
        if fresh:
            log.info("sweep: %d new signal(s), %d dispatched", len(fresh), self.emitted)

def build_dispatcher(settings: NotifySettings, dry_run: bool = False) -> Dispatcher:
    d = Dispatcher(settings, dry_run=dry_run)
    d.start()
    return d
