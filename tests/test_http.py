"""Rate-limit budget behaviour. A 429 must slow the whole pool, not one thread."""
from __future__ import annotations

import threading
import time
import unittest

from screeners.data.http import WeightBudget


class TestPenalty(unittest.TestCase):
    def test_penalty_holds_every_thread_sharing_the_budget(self):
        b = WeightBudget(1000)
        b.penalize(0.3)
        t0 = time.monotonic()
        waited: list[float] = []

        def spend():
            b.spend(1)
            waited.append(time.monotonic() - t0)

        threads = [threading.Thread(target=spend) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertGreaterEqual(min(waited), 0.28)

    def test_a_shorter_penalty_never_shortens_a_longer_one(self):
        b = WeightBudget(1000)
        b.penalize(0.3)
        b.penalize(0.01)
        t0 = time.monotonic()
        b.spend(1)
        self.assertGreaterEqual(time.monotonic() - t0, 0.28)

    def test_a_spend_larger_than_the_budget_fails_loudly(self):
        with self.assertRaises(ValueError):
            WeightBudget(10, headroom=0.8).spend(20)


if __name__ == "__main__":
    unittest.main()
