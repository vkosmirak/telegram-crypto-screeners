"""Ordinal/cooldown continuity across sweeps, and the paired baseline.

Both were untested, and both were wrong. The live screener re-scans a rolling
tail every sweep, so anything stateful inside `scan` silently restarts.
"""
from __future__ import annotations

import unittest

from etb.backtest.engine import _paired_baseline
from etb.config import OIGrowthConfig
from etb.data.series import Series
from etb.models import Bar, Exchange, Side, Signal
from etb.rules.base import SignalState, scan
from etb.rules.oi_growth import OIGrowthRule

MIN = 60_000
HOUR = 3_600_000


def rule(cooldown_min=0, max_ordinal=3):
    return OIGrowthRule(OIGrowthConfig(
        window_min=1, growth_pct=5.0, cooldown_min=cooldown_min,
        max_ordinal=max_ordinal, price_up=False, cvd_up=False,
        volume_up=False, flat_before=False))


def rising_oi_series(n, start=0, step=MIN, symbol="AUSDT"):
    oi = [100.0 * (1.10 ** k) for k in range(n)]
    bars = [Bar(ts=start + k * step, open=1, high=1, low=1, close=1,
                volume=1.0, oi=oi[k]) for k in range(n)]
    return Series(Exchange.BINANCE, symbol, "1m", bars)


class TestSignalStateAcrossSweeps(unittest.TestCase):
    def test_ordinal_keeps_counting_when_the_window_slides(self):
        """The live failure: each sweep sees only a tail, so a shared state is
        the only thing that makes '#1-3 of the day' mean the day."""
        full = rising_oi_series(9)
        state = SignalState()
        first = scan(rule(), Series(full.exchange, full.symbol, "1m", full.bars[:5]), state)
        second = scan(rule(), Series(full.exchange, full.symbol, "1m", full.bars[3:]), state)
        ordinals = [s.ordinal for s in first] + [s.ordinal for s in second]
        self.assertEqual(ordinals, list(range(1, len(ordinals) + 1)))

    def test_without_shared_state_the_count_restarts(self):
        """Pins the bug: a fresh state per sweep re-numbers from 1."""
        full = rising_oi_series(9)
        scan(rule(), Series(full.exchange, full.symbol, "1m", full.bars[:5]))
        again = scan(rule(), Series(full.exchange, full.symbol, "1m", full.bars[3:]))
        self.assertEqual(again[0].ordinal, 1)

    def test_a_rescanned_bar_is_not_emitted_twice(self):
        full = rising_oi_series(6)
        state = SignalState()
        a = scan(rule(), full, state)
        b = scan(rule(), full, state)   # identical sweep, nothing new
        self.assertTrue(a)
        self.assertEqual(b, [])

    def test_max_ordinal_still_bites_after_the_window_slides(self):
        """The dispatched-a-#9-as-#1 failure."""
        full = rising_oi_series(9)
        state = SignalState()
        scan(rule(max_ordinal=3), Series(full.exchange, full.symbol, "1m", full.bars[:5]), state)
        later = scan(rule(max_ordinal=3), Series(full.exchange, full.symbol, "1m", full.bars[5:]), state)
        self.assertTrue(later)
        self.assertFalse(any(s.filters["ordinal"] for s in later))

    def test_cooldown_is_continuous_across_sweeps(self):
        full = rising_oi_series(9)
        state = SignalState()
        scan(rule(cooldown_min=5), Series(full.exchange, full.symbol, "1m", full.bars[:5]), state)
        # bars 3..5 are 3-5 min after the sweep-1 fire at t=1min, so all
        # still inside a 5-minute cooldown. (bar 6 sits exactly on the
        # boundary and is allowed to fire -- see the boundary test below.)
        later = scan(rule(cooldown_min=5), Series(full.exchange, full.symbol, "1m", full.bars[3:6]), state)
        self.assertEqual(later, [])

    def test_cooldown_boundary_fires_at_exactly_the_cooldown(self):
        full = rising_oi_series(9)
        state = SignalState()
        scan(rule(cooldown_min=5), Series(full.exchange, full.symbol, "1m", full.bars[:5]), state)
        # Slice must carry the preceding bar too: oi_growth is a delta, so a
        # single-bar window has nothing to measure against.
        at_boundary = scan(rule(cooldown_min=5),
                           Series(full.exchange, full.symbol, "1m", full.bars[5:7]), state)
        self.assertEqual([s.ordinal for s in at_boundary], [2])

    def test_state_is_per_symbol(self):
        state = SignalState()
        a = scan(rule(), rising_oi_series(4, symbol="AUSDT"), state)
        b = scan(rule(), rising_oi_series(4, symbol="BUSDT"), state)
        self.assertEqual([s.ordinal for s in a], [s.ordinal for s in b])

    def test_ordinal_resets_at_utc_midnight(self):
        state = SignalState()
        before = scan(rule(), rising_oi_series(3, start=86_400_000 - 2 * MIN), state)
        self.assertEqual([s.ordinal for s in before], [1, 1])


class TestPairedBaseline(unittest.TestCase):
    def series_map(self, n=40):
        out = {}
        for sym in ("AUSDT", "BUSDT", "CUSDT"):
            bars = [Bar(ts=k * MIN, open=10, high=10, low=10, close=10, volume=1.0)
                    for k in range(n)]
            out[sym] = Series(Exchange.BINANCE, sym, "1m", bars)
        return out

    def signal(self, symbol, ts):
        return Signal(rule="oi_growth", exchange=Exchange.BINANCE, symbol=symbol,
                      side=Side.LONG, ts=ts, price=10.0, ordinal=1)

    def test_control_bars_are_drawn_at_the_signal_s_own_timestamp(self):
        sm = self.series_map()
        sigs = [self.signal("AUSDT", 5 * MIN)]
        out = _paired_baseline(sm, sigs, rule(), (1,), per_signal=5)
        self.assertTrue(out)
        self.assertTrue(all(o.signal.ts == 5 * MIN for o in out))

    def test_control_never_reuses_the_signal_s_own_symbol(self):
        sm = self.series_map()
        sigs = [self.signal("AUSDT", 5 * MIN)]
        out = _paired_baseline(sm, sigs, rule(), (1,), per_signal=5)
        self.assertNotIn("AUSDT", {o.signal.symbol for o in out})

    def test_control_inherits_the_rule_s_side(self):
        from etb.config import PumpConfig
        from etb.rules.pump import PumpRule
        sm = self.series_map()
        out = _paired_baseline(sm, [self.signal("AUSDT", 5 * MIN)],
                               PumpRule(PumpConfig(), Side.SHORT), (1,))
        self.assertTrue(all(o.signal.side is Side.SHORT for o in out))

    def test_no_signals_means_no_control(self):
        self.assertEqual(_paired_baseline(self.series_map(), [], rule(), (1,)), [])

    def test_is_deterministic_for_a_given_seed(self):
        sm = self.series_map()
        sigs = [self.signal("AUSDT", 5 * MIN)]
        a = _paired_baseline(sm, sigs, rule(), (1,), seed=7)
        b = _paired_baseline(sm, sigs, rule(), (1,), seed=7)
        self.assertEqual([o.signal.symbol for o in a], [o.signal.symbol for o in b])


if __name__ == "__main__":
    unittest.main()
