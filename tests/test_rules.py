"""Rule semantics. These pin the behaviours that are easy to get subtly wrong."""
from __future__ import annotations

import unittest

from screeners.config import LiquidationConfig, OIGrowthConfig, PumpConfig, PumpSideConfig
from screeners.data.series import Series, merge_oi
from screeners.models import Bar, Exchange, Liquidation, Side
from screeners.rules.base import scan, utc_day
from screeners.rules.liquidation import LiquidationRule, normalise_side
from screeners.rules.oi_growth import OIGrowthRule
from screeners.rules.pump import PumpRule

MIN = 60_000


def bars(prices, ois=None, *, start=0, step=MIN, vol=100.0, taker=None):
    out = []
    for n, p in enumerate(prices):
        out.append(Bar(
            ts=start + n * step, open=p, high=p, low=p, close=p, volume=vol,
            taker_buy=(taker[n] if taker else None),
            oi=(ois[n] if ois else None),
        ))
    return out


def series(bs, interval="1m", symbol="TESTUSDT", exchange=Exchange.BINANCE):
    return Series(exchange, symbol, interval, bs)


class TestMergeOI(unittest.TestCase):
    def test_forward_fill_never_looks_ahead(self):
        bs = bars([1] * 6, start=0, step=MIN)
        oi = [(2 * MIN, 10.0), (5 * MIN, 20.0)]
        got = [b.oi for b in merge_oi(bs, oi)]
        # Bars before the first sample have no OI; none inherits a future value.
        self.assertEqual(got, [None, None, 10.0, 10.0, 10.0, 20.0])

    def test_no_samples_leaves_bars_untouched(self):
        bs = bars([1, 2, 3])
        self.assertEqual([b.oi for b in merge_oi(bs, [])], [None, None, None])


class TestOIGrowth(unittest.TestCase):
    def cfg(self, **kw):
        base = dict(window_min=15, growth_pct=5.0, cooldown_min=0, max_ordinal=0,
                    price_up=False, cvd_up=False, volume_up=False, flat_before=False)
        base.update(kw)
        return OIGrowthConfig(**base)

    def test_fires_only_at_or_above_threshold(self):
        # 5m bars: window_min=15 -> 3 bars back.
        oi = [100.0, 100.0, 100.0, 104.9, 105.0]
        s = series(bars([1] * 5, oi, step=5 * MIN), interval="5m")
        sigs = scan(OIGrowthRule(self.cfg()), s)
        # bar 3 is +4.9% (no), bar 4 is +5.0% (yes)
        self.assertEqual([x.ts for x in sigs], [4 * 5 * MIN])
        self.assertAlmostEqual(sigs[0].metrics["oi_growth_pct"], 5.0, places=6)

    def test_price_up_filter_marks_oi_up_price_down(self):
        oi = [100.0, 100.0, 100.0, 110.0]
        s = series(bars([10, 10, 10, 9], oi, step=5 * MIN), interval="5m")
        sig = scan(OIGrowthRule(self.cfg(price_up=True)), s)[0]
        self.assertFalse(sig.filters["price_up"])  # shorts stacking, not longs
        self.assertLess(sig.metrics["price_change_pct"], 0)

    def test_cvd_filter_omitted_when_venue_has_no_taker_volume(self):
        oi = [100.0, 100.0, 100.0, 110.0]
        s = series(bars([10, 10, 10, 11], oi, step=5 * MIN), interval="5m",
                   exchange=Exchange.BYBIT)
        sig = scan(OIGrowthRule(self.cfg(cvd_up=True)), s)[0]
        # Absent, not silently True -- a filter we cannot compute must not vote.
        self.assertNotIn("cvd_up", sig.filters)
        self.assertEqual(sig.metrics["cvd_available"], 0.0)

    def test_needs_full_window_before_it_can_fire(self):
        oi = [100.0, 200.0]
        s = series(bars([1, 1], oi, step=5 * MIN), interval="5m")
        self.assertEqual(scan(OIGrowthRule(self.cfg()), s), [])


class TestPumpWindow(unittest.TestCase):
    def cfg(self, window_min, move_pct, **kw):
        base = dict(enabled=True, cooldown_min=0, max_ordinal=0,
                    cvd_down=False, oi_down=False)
        base.update(kw)
        return PumpConfig(
            long=PumpSideConfig(True, window_min, move_pct),
            short=PumpSideConfig(True, window_min, move_pct),
            **base,
        )

    def test_fires_early_when_the_move_is_faster_than_the_window(self):
        # 10% inside 5 minutes must satisfy a 10%/20min rule.
        prices = [100.0] * 5 + [110.0] + [110.0] * 5
        s = series(bars(prices))
        sigs = scan(PumpRule(self.cfg(20, 10.0), Side.SHORT), s)
        self.assertTrue(sigs)
        self.assertEqual(sigs[0].ts, 5 * MIN)

    def test_does_not_fire_when_the_same_move_takes_longer_than_the_window(self):
        # +10% spread evenly over 40 one-minute bars; never 10% inside any 20m window.
        prices = [100.0 * (1.10 ** (n / 40)) for n in range(41)]
        s = series(bars(prices))
        self.assertEqual(scan(PumpRule(self.cfg(20, 10.0), Side.SHORT), s), [])

    def test_side_determines_what_counts_as_confirmation(self):
        # Window of 5 bars ending at bar 7, so bar 2 exists to anchor the
        # span. OI falls and CVD falls across the pump.
        prices = [100.0] * 5 + [110.0] * 3
        oi = [100.0] * 5 + [90.0] * 3
        taker = [50.0] * 5 + [10.0] * 3      # delta 0, then -80/bar
        s = series(bars(prices, oi, taker=taker))
        cfg = self.cfg(5, 10.0, cvd_down=True, oi_down=True)
        short = scan(PumpRule(cfg, Side.SHORT), s)[0]
        long_ = scan(PumpRule(cfg, Side.LONG), s)[0]
        self.assertTrue(short.filters["oi_confirms"])   # falling OI confirms a short
        self.assertTrue(short.filters["cvd_confirms"])
        self.assertFalse(long_.filters["oi_confirms"])  # and refutes a long
        self.assertFalse(long_.filters["cvd_confirms"])

    def test_confirmations_span_the_whole_window_not_one_bar_short(self):
        """cvd[k] includes bar k, so the change across [j..i] is cvd[i]-cvd[j-1].

        Anchoring on cvd[j] dropped the pump's first bar -- for a 2-bar window
        that left a single bar of CVD and voted the wrong way."""
        prices = [100.0, 100.0, 100.0, 103.0]
        taker = [50.0, 50.0, 0.0, 50.0]      # bar 2 is the heavy selling
        s = series(bars(prices, taker=taker))
        cfg = self.cfg(2, 2.0, cvd_down=True)
        sig = scan(PumpRule(cfg, Side.SHORT), s)[0]
        self.assertLess(sig.metrics["cvd_change"], 0)      # sold into, as it was
        self.assertTrue(sig.filters["cvd_confirms"])

    def test_a_one_minute_window_is_allowed(self):
        """window_min=1 is documented as valid; it used to silently never fire."""
        bs = [Bar(ts=n * MIN, open=100.0, high=100.0, low=100.0, close=100.0,
                  volume=1.0) for n in range(3)]
        bs[2] = Bar(ts=2 * MIN, open=100.0, high=160.0, low=100.0, close=160.0,
                    volume=1.0)
        sigs = scan(PumpRule(self.cfg(1, 50.0), Side.SHORT), series(bs))
        self.assertEqual([x.ts for x in sigs], [2 * MIN])

    def test_identical_oi_samples_are_unknown_not_a_failed_filter(self):
        """5m OI forward-filled onto 1m bars reads the same value at both ends
        of a short window. That is no information, not 'OI did not fall'."""
        s = series(bars([100.0, 100.0, 100.0, 110.0], [7.0, 7.0, 7.0, 7.0]))
        sig = scan(PumpRule(self.cfg(2, 5.0, oi_down=True), Side.LONG), s)[0]
        self.assertNotIn("oi_confirms", sig.filters)
        self.assertEqual(sig.metrics["oi_available"], 0.0)


class TestOIGrowthTruncatedWindows(unittest.TestCase):
    """A window we cannot fully see means 'not computable', never 'passed'."""

    def cfg(self, **kw):
        base = dict(window_min=15, growth_pct=5.0, cooldown_min=0, max_ordinal=0,
                    price_up=False, cvd_up=False, volume_up=False, flat_before=False)
        base.update(kw)
        return OIGrowthConfig(**base)

    def test_volume_up_is_absent_when_the_prior_window_is_truncated(self):
        # w=3 on 5m bars; the earliest firable bar has no full prior window.
        oi = [100.0, 100.0, 100.0, 110.0]
        vols = [1000.0, 1.0, 1.0, 1.0]
        bs = [Bar(ts=n * 5 * MIN, open=1, high=1, low=1, close=1,
                  volume=vols[n], oi=oi[n]) for n in range(4)]
        sig = scan(OIGrowthRule(self.cfg(volume_up=True)),
                   series(bs, interval="5m"))[0]
        # Previously reported volume_up=True with volume_ratio=0.0, while the
        # prior window had traded 1000x more.
        self.assertNotIn("volume_up", sig.filters)
        self.assertEqual(sig.metrics["volume_available"], 0.0)

    def test_flat_before_is_absent_when_the_lookback_is_truncated(self):
        oi = [100.0, 100.0, 100.0, 110.0]
        s = series(bars([1] * 4, oi, step=5 * MIN), interval="5m")
        sig = scan(OIGrowthRule(self.cfg(flat_before=True, flat_lookback_h=4.0)), s)[0]
        self.assertNotIn("flat_before", sig.filters)
        self.assertEqual(sig.metrics["flat_available"], 0.0)


class TestScanDriver(unittest.TestCase):
    def rule(self, cooldown_min=0, max_ordinal=0):
        return OIGrowthRule(OIGrowthConfig(
            window_min=1, growth_pct=5.0, cooldown_min=cooldown_min,
            max_ordinal=max_ordinal, price_up=False, cvd_up=False,
            volume_up=False, flat_before=False))

    def test_cooldown_suppresses_repeats(self):
        oi = [100.0, 110.0, 121.0, 133.0, 146.0]
        s = series(bars([1] * 5, oi))
        self.assertEqual(len(scan(self.rule(cooldown_min=0), s)), 4)
        self.assertEqual(len(scan(self.rule(cooldown_min=3), s)), 2)

    def test_ordinal_counts_triggers_not_survivors_and_resets_daily(self):
        day = 86_400_000
        oi = [100.0, 110.0, 121.0]
        a = bars([1] * 3, oi, start=day - 2 * MIN)       # straddles midnight
        s = series(a)
        sigs = scan(self.rule(), s)
        self.assertEqual([x.ordinal for x in sigs], [1, 1])
        self.assertNotEqual(utc_day(sigs[0].ts), utc_day(sigs[1].ts))

    def test_max_ordinal_is_a_filter_not_a_gate(self):
        oi = [100.0, 110.0, 121.0, 133.0]
        s = series(bars([1] * 4, oi))
        sigs = scan(self.rule(max_ordinal=2), s)
        # All three still fire and stay numbered; only the flag differs.
        self.assertEqual([x.ordinal for x in sigs], [1, 2, 3])
        self.assertEqual([x.filters["ordinal"] for x in sigs], [True, True, False])

    def test_scan_is_causal(self):
        """Truncating the future must not change past signals."""
        oi = [100.0, 100.0, 118.0, 100.0, 100.0, 100.0]
        s_full = series(bars([1] * 6, oi))
        s_trunc = series(bars([1] * 3, oi[:3]))
        full = [x.ts for x in scan(self.rule(), s_full) if x.ts <= 2 * MIN]
        trunc = [x.ts for x in scan(self.rule(), s_trunc)]
        self.assertEqual(full, trunc)


class TestLiquidation(unittest.TestCase):
    def test_venues_report_opposite_polarity(self):
        # Binance `o.S` is the closing order's side; Bybit `S` is the position's.
        self.assertIs(normalise_side(Exchange.BINANCE, "SELL"), Side.LONG)
        self.assertIs(normalise_side(Exchange.BINANCE, "BUY"), Side.SHORT)
        self.assertIs(normalise_side(Exchange.BYBIT, "Buy"), Side.LONG)
        self.assertIs(normalise_side(Exchange.BYBIT, "Sell"), Side.SHORT)

    def test_threshold_exclusions_and_fade_direction(self):
        rule = LiquidationRule(LiquidationConfig(
            min_usd=20000.0, exclude=("BTCUSDT",), cooldown_min=0))
        small = Liquidation(Exchange.BINANCE, "OGUSDT", 0, Side.LONG, 1.0, 100.0)
        self.assertIsNone(rule.evaluate_event(small))  # $100 < $20k

        big = Liquidation(Exchange.BINANCE, "OGUSDT", MIN, Side.LONG, 1000.0, 100.0)
        sig = rule.evaluate_event(big)
        self.assertIsNotNone(sig)
        self.assertIs(sig.side, Side.LONG)  # longs forced out -> fade by buying

        major = Liquidation(Exchange.BINANCE, "BTCUSDT", 2 * MIN, Side.LONG, 1000.0, 100.0)
        self.assertIsNone(rule.evaluate_event(major))

    def test_cooldown_applies_per_symbol(self):
        rule = LiquidationRule(LiquidationConfig(min_usd=1.0, exclude=(), cooldown_min=5))
        a = Liquidation(Exchange.BINANCE, "AUSDT", 0, Side.LONG, 10.0, 10.0)
        b = Liquidation(Exchange.BINANCE, "AUSDT", MIN, Side.LONG, 10.0, 10.0)
        c = Liquidation(Exchange.BINANCE, "BUSDT", MIN, Side.LONG, 10.0, 10.0)
        self.assertIsNotNone(rule.evaluate_event(a))
        self.assertIsNone(rule.evaluate_event(b))     # same symbol, inside cooldown
        self.assertIsNotNone(rule.evaluate_event(c))  # different symbol, unaffected


if __name__ == "__main__":
    unittest.main()
