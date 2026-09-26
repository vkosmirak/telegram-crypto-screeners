"""The incremental live feed: fetch only what is new, and never lose bars.

These run against a fake adapter that records every call, so they can assert
on what the screener ASKED for, which is where the rate-limit cost lives.
"""
from __future__ import annotations

import unittest

from screeners.config import load_rules
from screeners.live import OI_STEP_MS, TAIL_HOURS, Screener
from screeners.models import Bar, Exchange
from screeners.notify.dispatch import Dispatcher
from screeners.config import NotifySettings

MIN = 60_000


class FakeAdapter:
    """Serves synthetic flat bars and OI for any range, and logs requests."""

    def __init__(self):
        self.kline_calls: list[tuple] = []
        self.oi_calls: list[tuple] = []

    def klines(self, symbol, interval, start, end):
        self.kline_calls.append((symbol, interval, start, end))
        step = {"1m": MIN, "5m": 5 * MIN}[interval]
        t = -(-start // step) * step
        out = []
        while t < end:
            out.append(Bar(ts=t, open=1, high=1, low=1, close=1, volume=1.0,
                           taker_buy=0.5))
            t += step
        return out

    def open_interest(self, symbol, period, start, end):
        self.oi_calls.append((symbol, start, end))
        t = -(-start // OI_STEP_MS) * OI_STEP_MS
        out = []
        while t < end:
            out.append((t, 100.0))
            t += OI_STEP_MS
        return out


def screener(symbols=("AUSDT", "BUSDT")):
    sc = Screener(Exchange.BINANCE, ["oi_growth", "pump_short"], list(symbols),
                  load_rules(), Dispatcher(NotifySettings(), dry_run=True))
    sc.adapter = FakeAdapter()
    return sc


class TestIncrementalFeed(unittest.TestCase):
    T0 = 1_790_000_000_000 // (5 * MIN) * (5 * MIN)

    def test_first_refresh_loads_the_whole_window(self):
        sc = screener()
        sc._refresh_all(self.T0)
        bars = sc._bars[("1m", "AUSDT")]
        self.assertEqual(len(bars), int(TAIL_HOURS * 60))

    def test_next_refresh_asks_only_for_bars_that_closed_since(self):
        sc = screener()
        sc._refresh_all(self.T0)
        sc.adapter.kline_calls.clear()
        sc._refresh_all(self.T0 + 2 * MIN)
        one_min = [c for c in sc.adapter.kline_calls if c[1] == "1m"]
        for _, _, start, end in one_min:
            self.assertEqual((end - start) // MIN, 2)   # two new 1m bars, not 480

    def test_window_slides_without_growing_or_losing_bars(self):
        sc = screener()
        sc._refresh_all(self.T0)
        sc._refresh_all(self.T0 + 30 * MIN)
        bars = sc._bars[("1m", "AUSDT")]
        self.assertEqual(len(bars), int(TAIL_HOURS * 60))
        ts = [b.ts for b in bars]
        self.assertEqual(ts, sorted(set(ts)))                  # no dupes, ordered
        self.assertTrue(all(b - a == MIN for a, b in zip(ts, ts[1:])))  # no gaps

    def test_oi_is_not_refetched_before_a_new_sample_can_exist(self):
        sc = screener()
        sc._refresh_all(self.T0)
        sc.adapter.oi_calls.clear()
        sc._refresh_all(self.T0 + 1 * MIN)
        sc._refresh_all(self.T0 + 2 * MIN)
        self.assertEqual(sc.adapter.oi_calls, [])
        sc._refresh_all(self.T0 + 5 * MIN)
        self.assertEqual(len(sc.adapter.oi_calls), 2)   # one per symbol

    def test_a_failing_symbol_is_counted_and_does_not_block_the_rest(self):
        sc = screener()
        real = sc.adapter.klines

        def flaky(symbol, *a):
            if symbol == "BUSDT":
                raise RuntimeError("boom")
            return real(symbol, *a)

        sc.adapter.klines = flaky
        failed = sc._refresh_all(self.T0)
        self.assertEqual(failed, {"1m": 1, "5m": 1})
        self.assertTrue(sc._bars[("1m", "AUSDT")])

    def test_first_sweep_primes_and_sends_nothing(self):
        sc = screener()
        import screeners.live as live
        real_time = live.time.time
        live.time.time = lambda: self.T0 / 1000
        try:
            sc._sweep()
        finally:
            live.time.time = real_time
        self.assertTrue(sc._warm)
        self.assertEqual(sc.emitted, 0)


class TestBudget(unittest.TestCase):
    def test_whole_binance_universe_fits_a_one_minute_sweep(self):
        sc = Screener(Exchange.BINANCE, ["oi_growth", "pump_short"],
                      [f"S{i}" for i in range(527)], load_rules(),
                      Dispatcher(NotifySettings(), dry_run=True))
        self.assertLess(sc.required_sweep_s(), 60)

    def test_whole_bybit_universe_fits_a_one_minute_sweep(self):
        sc = Screener(Exchange.BYBIT, ["oi_growth", "pump_short"],
                      [f"S{i}" for i in range(777)], load_rules(),
                      Dispatcher(NotifySettings(), dry_run=True))
        self.assertLess(sc.required_sweep_s(), 60)


if __name__ == "__main__":
    unittest.main()
