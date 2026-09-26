"""Scoring and reporting. The sign convention is the easy thing to get wrong."""
from __future__ import annotations

import unittest

from etb.backtest.stats import score, summarise
from etb.data.series import Series
from etb.models import Bar, Exchange, Side, Signal

MIN = 60_000


def series(prices, interval="1m"):
    bars = [Bar(ts=n * MIN, open=p, high=p, low=p, close=p, volume=1.0)
            for n, p in enumerate(prices)]
    return Series(Exchange.BINANCE, "TESTUSDT", interval, bars)


def signal(side, price, ts=0):
    return Signal(rule="r", exchange=Exchange.BINANCE, symbol="TESTUSDT",
                  side=side, ts=ts, price=price, ordinal=1)


class TestScoring(unittest.TestCase):
    def test_a_fall_is_a_win_for_a_short_and_a_loss_for_a_long(self):
        s = series([100.0] + [90.0] * 5)
        long_ = score(signal(Side.LONG, 100.0), s, 0, (5,))
        short = score(signal(Side.SHORT, 100.0), s, 0, (5,))
        self.assertAlmostEqual(long_.returns[5], -10.0, places=6)
        self.assertAlmostEqual(short.returns[5], +10.0, places=6)

    def test_excursions_are_signed_to_the_side_too(self):
        # Price dips to 90 then ends at 105.
        prices = [100.0, 95.0, 90.0, 100.0, 105.0, 105.0]
        s = series(prices)
        long_ = score(signal(Side.LONG, 100.0), s, 0, (5,))
        self.assertAlmostEqual(long_.mfe[5], 5.0, places=6)    # best was +5%
        self.assertAlmostEqual(long_.mae[5], -10.0, places=6)  # worst was -10%
        short = score(signal(Side.SHORT, 100.0), s, 0, (5,))
        self.assertAlmostEqual(short.mfe[5], 10.0, places=6)   # mirrored
        self.assertAlmostEqual(short.mae[5], -5.0, places=6)

    def test_horizons_past_the_end_of_data_are_dropped_not_guessed(self):
        s = series([100.0, 101.0, 102.0])
        out = score(signal(Side.LONG, 100.0), s, 0, (1, 2, 60))
        self.assertIn(1, out.returns)
        self.assertIn(2, out.returns)
        self.assertNotIn(60, out.returns)  # would need bar 60; silently 0 would lie

    def test_horizon_is_in_minutes_not_bars(self):
        # On 5m bars, +15m must look 3 bars ahead, not 15.
        bars = [Bar(ts=n * 5 * MIN, open=100.0 + n, high=100.0 + n,
                    low=100.0 + n, close=100.0 + n, volume=1.0) for n in range(6)]
        s = Series(Exchange.BINANCE, "T", "5m", bars)
        out = score(signal(Side.LONG, 100.0), s, 0, (15,))
        self.assertAlmostEqual(out.returns[15], 3.0, places=6)  # bar 3 -> 103


class TestSummary(unittest.TestCase):
    def test_hit_rate_counts_strictly_positive_returns(self):
        s = series([100.0] + [110.0] * 5)
        win = score(signal(Side.LONG, 100.0), s, 0, (5,))
        flat_s = series([100.0] * 6)
        flat = score(signal(Side.LONG, 100.0), flat_s, 0, (5,))
        out = summarise("x", [win, flat], (5,))
        self.assertEqual(out.n, 2)
        self.assertAlmostEqual(out.per_horizon[5]["hit"], 50.0)  # flat is not a win

    def test_empty_input_does_not_explode(self):
        out = summarise("x", [], (5, 15))
        self.assertEqual(out.n, 0)
        self.assertIn("no data", out.line(5))


class TestResultSubset(unittest.TestCase):
    def test_uncomputed_filters_do_not_exclude_a_signal(self):
        from etb.backtest.engine import BacktestResult

        s = series([100.0] * 6)
        a = score(Signal(rule="r", exchange=Exchange.BINANCE, symbol="A",
                         side=Side.LONG, ts=0, price=100.0, ordinal=1,
                         filters={"price_up": True}), s, 0, (5,))
        b = score(Signal(rule="r", exchange=Exchange.BINANCE, symbol="B",
                         side=Side.LONG, ts=0, price=100.0, ordinal=1,
                         filters={"price_up": True, "cvd_up": False}), s, 0, (5,))
        res = BacktestResult(Exchange.BINANCE, "r", "1m", 0, 1, 2, [a, b], (5,))
        # A never computed cvd_up (Bybit-style); it must survive the cvd subset,
        # while B, which computed it and failed, must not.
        self.assertEqual([o.signal.symbol for o in res.subset(["cvd_up"])], ["A"])
        self.assertEqual(len(res.subset(["price_up"])), 2)
        self.assertEqual(len(res.subset(None)), 2)




class TestTopicRouting(unittest.TestCase):
    """Rules are named per side but share a topic; regression for signals
    silently landing in the group's General topic."""

    def settings(self):
        from etb.config import load_notify
        return load_notify(env={
            "NOTIFY_BOT_TOKEN": "t", "NOTIFY_CHAT_ID": "-100",
            "TOPIC_OI": "3", "TOPIC_PUMP": "4",
            "TOPIC_LIQUIDATION": "5", "TOPIC_OPS": "6",
        })

    def test_both_pump_sides_share_the_pump_topic(self):
        s = self.settings()
        self.assertEqual(s.topic_for("pump_short"), "4")
        self.assertEqual(s.topic_for("pump_long"), "4")

    def test_exact_names_still_win(self):
        s = self.settings()
        self.assertEqual(s.topic_for("oi_growth"), "3")
        self.assertEqual(s.topic_for("liquidation"), "5")
        self.assertEqual(s.topic_for("ops"), "6")

    def test_unknown_rule_has_no_topic(self):
        self.assertIsNone(self.settings().topic_for("nonsense_rule"))


if __name__ == "__main__":
    unittest.main()
