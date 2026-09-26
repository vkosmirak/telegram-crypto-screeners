"""Blocking JSON GET with retries and a shared weight budget.

Deliberately stdlib. The backtester's fetch phase is IO-bound but modest
(hundreds of requests, not thousands per second), and a thread pool over
urllib keeps the dependency count at zero.
"""
from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

# 408 is Binance's `-1007 Timeout waiting for response from backend server`.
# It is transient and common on long kline pulls; without it here, whole
# symbols silently drop out of a backtest.
RETRY_STATUS = {408, 418, 429, 500, 502, 503, 504}


class RateLimitError(RuntimeError):
    """Venue said slow down and told us for how long."""

    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after:.1f}s")
        self.retry_after = retry_after


class WeightBudget:
    """Sliding-window budget for Binance's REQUEST_WEIGHT cap (2400/min per IP).

    Callers `spend(n)` before a request; the call blocks until the spend fits
    inside the window. Shared across threads, so a thread pool cannot
    collectively overshoot the way independent limiters would.
    """

    def __init__(self, limit: int, window_s: float = 60.0, headroom: float = 0.8):
        self.limit = int(limit * headroom)
        self.window_s = window_s
        self._events: list[tuple[float, int]] = []
        self._lock = threading.Lock()
        self._blocked_until = 0.0

    def penalize(self, seconds: float) -> None:
        """Hold EVERY caller of this budget for `seconds`.

        When the venue answers 429, the thread that got it backing off alone
        is not enough: the rest of the pool keeps firing into the limit, and
        Binance escalates repeated 429s to a 418 IP ban. So one 429 pauses
        the whole pool.
        """
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    def spend(self, weight: int = 1) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self._blocked_until:
                    wait = self._blocked_until - now
                    blocked = True
                else:
                    blocked = False
            if blocked:
                time.sleep(wait)
                continue
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.window_s
                self._events = [(t, w) for t, w in self._events if t > cutoff]
                used = sum(w for _, w in self._events)
                if used + weight <= self.limit:
                    self._events.append((now, weight))
                    return
                if not self._events:
                    # A single spend larger than the whole budget would loop
                    # forever on an empty deque, so fail loudly instead of
                    # crashing on an index or hanging.
                    raise ValueError(
                        f"weight {weight} exceeds budget limit {self.limit}")
                oldest = self._events[0][0]
                wait = max(0.05, oldest + self.window_s - now)
            time.sleep(wait)


def get_json(
    url: str,
    params: dict[str, object] | None = None,
    *,
    timeout: float = 20.0,
    attempts: int = 5,
    budget: WeightBudget | None = None,
    weight: int = 1,
) -> object:
    """GET and decode JSON, retrying transient failures with exponential backoff."""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(clean)}"

    last: Exception | None = None
    for attempt in range(attempts):
        if budget:
            budget.spend(weight)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "screeners/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in RETRY_STATUS:
                body = e.read()[:400].decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {e.code} for {url}: {body}") from e
            retry_after = float(e.headers.get("Retry-After") or 0)
            delay = retry_after or min(30.0, 1.5 * (2**attempt))
            if e.code in (418, 429) and budget is not None:
                budget.penalize(delay)
            endpoint = urllib.parse.urlparse(url).path
            log.warning("HTTP %s on %s, retry %d/%d in %.1fs",
                        e.code, endpoint, attempt + 1, attempts, delay)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                ConnectionError, http.client.HTTPException) as e:
            # urllib only wraps failures raised during connect; a reset or a
            # short read partway through a 200KB kline body escapes as a bare
            # ConnectionError/IncompleteRead and would drop the whole symbol.
            last = e
            delay = min(15.0, 1.0 * (2**attempt))
            log.warning("%s, retry %d/%d in %.1fs", type(e).__name__, attempt + 1, attempts, delay)
            time.sleep(delay)

    raise RuntimeError(f"giving up on {url} after {attempts} attempts: {last}")
