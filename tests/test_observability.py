"""The signal journal and the status report's log parsing."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from screeners import journal
from screeners.models import Exchange, Side, Signal
from screeners.status import _parse

DAY = 86_400_000


def sig(**kw):
    base = dict(rule="oi_growth", exchange=Exchange.BYBIT, symbol="SPOTUSDT",
                side=Side.LONG, ts=1_790_000_000_000, price=1.23, ordinal=2,
                metrics={"oi_growth_pct": 7.4812345, "oi_usd": 5_110_000.0},
                filters={"price_up": True, "flat_before": False})
    base.update(kw)
    return Signal(**base)


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_round_trip_keeps_what_was_decided_and_why(self):
        now = 1_790_000_100_000
        journal.record(sig(), "skipped", now, root=self.root)
        [row] = journal.read(1, now, root=self.root)
        self.assertEqual(row["action"], "skipped")
        self.assertEqual(row["failed"], ["flat_before"])
        self.assertEqual(row["venue"], "bybit")
        self.assertEqual(row["ordinal"], 2)
        self.assertAlmostEqual(row["metrics"]["oi_growth_pct"], 7.48123, places=5)

    def test_one_file_per_utc_day(self):
        now = 1_790_000_100_000
        journal.record(sig(), "sent", now - DAY, root=self.root)
        journal.record(sig(), "sent", now, root=self.root)
        self.assertEqual(len(list(self.root.glob("*.jsonl"))), 2)
        self.assertEqual(len(journal.read(1, now, root=self.root)), 1)
        self.assertEqual(len(journal.read(2, now, root=self.root)), 2)

    def test_reading_an_empty_journal_is_not_an_error(self):
        self.assertEqual(journal.read(3, root=self.root), [])


class TestLogParsing(unittest.TestCase):
    def test_dated_utc_lines_parse(self):
        ts, level, msg = _parse(
            "2026-09-26 12:45:53Z INFO    screeners.live         sweep 12s: 527 symbols")
        self.assertEqual(level, "INFO")
        self.assertTrue(msg.startswith("sweep 12s"))
        import datetime
        want = datetime.datetime(2026, 9, 26, 12, 45, 53, tzinfo=datetime.timezone.utc)
        self.assertEqual(ts, want.timestamp())   # read as UTC, whatever the host zone

    def test_old_time_only_lines_are_ignored_not_misdated(self):
        self.assertIsNone(_parse("12:45:53 INFO    screeners.live         sweep 12s"))


if __name__ == "__main__":
    unittest.main()
