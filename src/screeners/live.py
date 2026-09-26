"""Live screener.

Design choice worth stating plainly: this runs the *same* rule code as the
backtester. Bars are held in memory as the same `Series` the backtest uses,
and every sweep calls the same `scan()`. Only how the bars arrive differs.

Each symbol keeps a rolling window of TAIL_HOURS. The first sweep fetches the
whole window; after that a sweep fetches only the bars that closed since the
last one, and open interest only when a new 5-minute sample can exist. The
first version re-downloaded the entire 8-hour tail -- bars and OI -- for every
symbol every minute, and the rate limits capped it at 60 symbols. Incremental
fetching is what lets it watch the whole universe.

The alternative -- websockets feeding the same `Series` -- would cut latency
further, but REST keeps one code path and is plenty for minute bars.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .backtest.engine import build_rule, interval_for
from .config import NotifySettings, Rules
from .data import binance, bybit
from .data.binance import INTERVAL_MS
from . import journal
from .data.series import Series, merge_oi
from .models import Bar, Exchange, Signal
from .notify.dispatch import Dispatcher
from .rules.base import SignalState, scan

log = logging.getLogger(__name__)

ADAPTERS = {Exchange.BINANCE: binance, Exchange.BYBIT: bybit}

# How much history each symbol keeps. Must comfortably exceed the longest
# lookback any rule uses (flat_before reaches back flat_lookback_h hours).
TAIL_HOURS = 8.0
OI_PERIOD = "5m"
OI_STEP_MS = INTERVAL_MS[OI_PERIOD]
# Binance publishes each 5m open-interest sample ~30-35s after its timestamp
# (measured 2026-09-26). Asking the instant the 5 minutes are up returns
# nothing new, and when every symbol then retried every sweep the OI pool
# (800 requests per 5 minutes) starved and one sweep stalled for 259s.
OI_LAG_MS = 45_000
# After a fetch that brought nothing new, wait this long before asking again.
OI_RETRY_MS = 60_000
# Bars this recent are re-judged every sweep. An OI sample can land a little
# after the bar it belongs to was first judged; re-checking a short trailing
# window lets that bar fire once its data is complete. SignalState stops any
# bar from being sent twice.
RECHECK_MS = 15 * 60_000


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
        workers: int = 16,
        require_filters: bool = True,
    ):
        self.exchange = exchange
        self.adapter = ADAPTERS[exchange]
        self.symbols = symbols
        self.rules = rules
        self.dispatcher = dispatcher
        self.sweep_s = sweep_s
        self.workers = workers
        self.require_filters = require_filters
        self.built = [build_rule(n, rules) for n in rule_names]
        self.intervals = sorted({interval_for(r.name) for r in self.built})
        # Carried across sweeps so the daily ordinal and the cooldown are
        # continuous, rather than restarting inside each window.
        self._state = SignalState()
        self._bars: dict[tuple[str, str], list[Bar]] = {}
        self._oi: dict[str, list[tuple[int, float]]] = {}
        self._oi_next: dict[str, int] = {}  # earliest time worth asking again
        self._warm = False
        self.emitted = 0

    # ── budget ────────────────────────────────────────────────────────────────

    def required_sweep_s(self) -> float:
        """Shortest sweep interval the steady state can sustain, per venue.

        After warm-up a sweep costs one small kline call per (symbol,
        interval), plus one OI call per symbol every 5 minutes. Each venue has
        its own limits -- the first version applied Binance's to Bybit too,
        which understated Bybit's headroom several times over.
        """
        kline_calls = len(self.symbols) * len(self.intervals)
        oi_calls_per_min = len(self.symbols) / 5.0
        if self.exchange is Exchange.BINANCE:
            # /fapi pool: 2400/min, 0.8 headroom; incremental fetches are weight 1.
            fapi_s = kline_calls * 1 * 60.0 / (2400 * 0.8)
            # /futures/data pool: 1000 per 5 min, 0.8 headroom.
            data_ok = oi_calls_per_min <= (1000 * 0.8) / 5.0
            return fapi_s if data_ok else float("inf")
        # Bybit: 600 requests per 5s across public endpoints; we use half.
        per_s = 600 / 5.0 * 0.5
        return (kline_calls + oi_calls_per_min) / per_s

    # ── loop ──────────────────────────────────────────────────────────────────

    def run(self, stop: threading.Event) -> None:
        needed = self.required_sweep_s()
        if needed > self.sweep_s:
            log.warning(
                "%d symbols x %d bar-interval(s) needs a %.0fs sweep to stay "
                "inside the rate limits; raising --sweep from %.0fs.",
                len(self.symbols), len(self.intervals), needed, self.sweep_s)
            self.sweep_s = needed

        while not stop.is_set():
            started = time.monotonic()
            try:
                self._sweep()
            except Exception as e:
                log.exception("sweep failed: %s", e)
            elapsed = time.monotonic() - started
            if self._warm and elapsed > self.sweep_s:
                log.warning("sweep took %.0fs, longer than the %.0fs interval",
                            elapsed, self.sweep_s)
            stop.wait(max(0.0, self.sweep_s - elapsed))

    # ── fetching ──────────────────────────────────────────────────────────────

    def _refresh_bars(self, symbol: str, interval: str, start: int, end: int) -> None:
        key = (interval, symbol)
        have = self._bars.get(key)
        step = INTERVAL_MS[interval]
        frm = start if not have else have[-1].ts + step
        if frm < end:
            new = self.adapter.klines(symbol, interval, frm, end)
            if have:
                last = have[-1].ts
                have = have + [b for b in new if b.ts > last]
            else:
                have = new
        # Drop what has aged out of the window.
        self._bars[key] = [b for b in (have or []) if b.ts >= start]

    def _refresh_oi(self, symbol: str, start: int, now: int) -> None:
        have = self._oi.get(symbol)
        if have and now < self._oi_next.get(symbol, 0):
            return  # the next sample is not published yet
        frm = start - OI_STEP_MS if not have else have[-1][0] + 1
        new = self.adapter.open_interest(symbol, OI_PERIOD, frm, now + 1)
        before = have[-1][0] if have else None
        if have:
            have = have + [(t, v) for t, v in new if t > before]
        else:
            have = new
        self._oi[symbol] = [(t, v) for t, v in have if t >= start - OI_STEP_MS]
        newest = self._oi[symbol][-1][0] if self._oi[symbol] else None
        if newest is not None and newest != before:
            self._oi_next[symbol] = newest + OI_STEP_MS + OI_LAG_MS
        else:
            self._oi_next[symbol] = now + OI_RETRY_MS

    def _refresh_all(self, now: int) -> dict[str, int]:
        """Update every symbol in parallel. Returns failure counts per stream."""
        windows = {}
        for iv in self.intervals:
            step = INTERVAL_MS[iv]
            end = now // step * step  # excludes the bar still forming
            windows[iv] = (end - int(TAIL_HOURS * 3_600_000), end)
        oi_start = min(s for s, _ in windows.values())

        jobs = []
        for sym in self.symbols:
            for iv, (s, e) in windows.items():
                jobs.append((iv, lambda sym=sym, iv=iv, s=s, e=e:
                             self._refresh_bars(sym, iv, s, e)))
            jobs.append(("oi", lambda sym=sym: self._refresh_oi(sym, oi_start, now)))

        failed: dict[str, int] = {}
        reasons: Counter[str] = Counter()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [(name, pool.submit(fn)) for name, fn in jobs]
            for name, fut in futures:
                try:
                    fut.result()
                except Exception as e:
                    failed[name] = failed.get(name, 0) + 1
                    # Group by message with symbols and numbers blanked, so
                    # 65 failures of one kind read as one line, not 65.
                    msg = re.sub(r"symbol=[A-Z0-9]+", "symbol=*", str(e))
                    reasons[re.sub(r"\d{5,}", "N", msg)[:160]] += 1
        if reasons:
            log.warning("refresh failures: %s", "; ".join(
                f"{n}x {r}" for r, n in reasons.most_common(3)))
        return failed

    # ── one sweep ─────────────────────────────────────────────────────────────

    def _sweep(self) -> None:
        now = int(time.time() * 1000)
        started = time.monotonic()
        failed = self._refresh_all(now)

        since = None if not self._warm else now - RECHECK_MS
        fresh: list[Signal] = []
        for rule in self.built:
            iv = interval_for(rule.name)
            step = INTERVAL_MS[iv]
            for sym in self.symbols:
                bars = self._bars.get((iv, sym))
                if not bars:
                    continue
                series = Series(self.exchange, sym, iv,
                                merge_oi(bars, self._oi.get(sym, []), step))
                fresh.extend(scan(rule, series, state=self._state, since_ts=since))

        # The first sweep sees the whole window at once. Emitting it would
        # blast a wall of stale alerts, so the first pass only primes state.
        if not self._warm:
            self._warm = True
            log.info("warm-up %.0fs: %d symbols, primed %d historical signals, "
                     "none sent%s", time.monotonic() - started, len(self.symbols),
                     len(fresh), f"; FAILED {failed}" if failed else "")
            return

        fresh.sort(key=lambda s: s.ts)
        sent = 0
        for sig in fresh:
            # Measured from the bar's CLOSE: from its open, a 5m signal always
            # looks at least 5 minutes stale, which is just the bar's length.
            closed = sig.ts + INTERVAL_MS[interval_for(sig.rule)]
            tag = (f"{sig.rule} {sig.symbol} #{sig.ordinal} "
                   f"({(now - closed) / 1000:.0f}s after bar close)")
            if self.require_filters and sig.filters and not sig.passed_all_filters:
                bad = ",".join(k for k, ok in sig.filters.items() if not ok)
                log.info("  skip %s (fails %s)", tag, bad)
                self._journal(sig, "skipped", now)
                continue
            log.info("  SEND %s", tag)
            self.dispatcher.submit(sig)
            self._journal(sig, "sent", now)
            sent += 1
        self.emitted += sent

        # One line per sweep, always. Without it a quiet market and a wedged
        # sweep are indistinguishable, and a stream silently failing for some
        # symbols (rate limit, delisting) goes unnoticed.
        log.info("sweep %.0fs: %d symbols, %d triggered, %d sent (total %d)%s",
                 time.monotonic() - started, len(self.symbols), len(fresh), sent,
                 self.emitted, f"; FAILED {failed}" if failed else "")


    @staticmethod
    def _journal(sig: Signal, action: str, now: int) -> None:
        try:
            journal.record(sig, action, now)
        except Exception as e:  # a full disk must not stop alerts
            log.warning("journal write failed: %s", e)


def build_dispatcher(settings: NotifySettings, dry_run: bool = False) -> Dispatcher:
    d = Dispatcher(settings, dry_run=dry_run)
    d.start()
    return d
