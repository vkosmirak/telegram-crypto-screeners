"""Outbound queue: token bucket, 429 backoff, per-topic routing, batching.

This is the piece telegram-copy-trading did not need. Telegram allows roughly
30 messages/second overall and about 20 per minute into a single group. A
screener over hundreds of symbols routinely produces bursts well past that --
one market-wide move can fire fifty symbols within the same minute.

So: signals go on a queue, a token bucket paces the drain, 429s are obeyed
rather than retried blindly, and anything that piles up inside a batch window
is collapsed into a single digest message instead of fifty separate ones.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

from ..config import NotifySettings
from ..models import Signal
from . import botapi
from .cards import chart_button, signal_card

log = logging.getLogger(__name__)


class TokenBucket:
    """Classic bucket. `take()` blocks until a token is available."""

    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = rate_per_sec
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, n: int = 1) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                deficit = n - self._tokens
                wait = deficit / self.rate
            time.sleep(min(wait, 5.0))


@dataclass(slots=True)
class _Item:
    topic: str | None
    signal: Signal | None
    text: str | None = None


class Dispatcher:
    """Background sender. `submit()` never blocks the screener's hot path."""

    def __init__(
        self,
        settings: NotifySettings,
        *,
        rate_per_sec: float = 0.5,
        burst: int = 5,
        batch_window_s: float = 5.0,
        max_batch: int = 10,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.bucket = TokenBucket(rate_per_sec, burst)
        self.batch_window_s = batch_window_s
        self.max_batch = max_batch
        self.dry_run = dry_run or not settings.configured
        self._q: queue.Queue[_Item | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.sent = 0
        self.dropped = 0

    # ── public ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread:
            return
        if self.dry_run:
            log.warning("Telegram not configured -- signals will be logged, not sent")
        self._thread = threading.Thread(target=self._run, name="screeners-notify", daemon=True)
        self._thread.start()

    def submit(self, signal: Signal) -> None:
        self._q.put(_Item(topic=self.settings.topic_for(signal.rule), signal=signal))

    def submit_text(self, text: str, topic: str = "ops") -> None:
        self._q.put(_Item(topic=self.settings.topic_for(topic), signal=None, text=text))

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    # ── internals ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            batch = self._collect()
            if not batch:
                continue
            for topic, items in self._group(batch).items():
                self._deliver(topic, items)

    def _collect(self) -> list[_Item]:
        """Block for one item, then sweep up whatever arrives inside the window."""
        try:
            first = self._q.get(timeout=1.0)
        except queue.Empty:
            return []
        if first is None:
            self._stop.set()
            return []
        batch = [first]
        deadline = time.monotonic() + self.batch_window_s
        while len(batch) < self.max_batch:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                nxt = self._q.get(timeout=remaining)
            except queue.Empty:
                break
            if nxt is None:
                self._stop.set()
                break
            batch.append(nxt)
        return batch

    @staticmethod
    def _group(batch: list[_Item]) -> dict[str | None, list[_Item]]:
        out: dict[str | None, list[_Item]] = defaultdict(list)
        for item in batch:
            out[item.topic].append(item)
        return out

    def _deliver(self, topic: str | None, items: list[_Item]) -> None:
        if len(items) == 1:
            item = items[0]
            text = item.text if item.signal is None else signal_card(item.signal)
            buttons = chart_button(item.signal) if item.signal else None
        else:
            # Collapse a burst rather than posting N messages into one topic.
            lines = []
            for item in items:
                lines.append(item.text if item.signal is None else signal_card(item.signal))
            text = "\n\n".join(lines)
            buttons = None

        if self.dry_run:
            log.info("[dry-run topic=%s]\n%s", topic, text)
            self.sent += len(items)
            return

        self._send_with_backoff(topic, text, buttons, len(items))

    def _send_with_backoff(self, topic, text, buttons, count: int, attempts: int = 4) -> None:
        for attempt in range(attempts):
            self.bucket.take()
            try:
                botapi.send(self.settings.bot_token, self.settings.chat_id, text,
                            topic_id=topic, buttons=buttons)
                self.sent += count
                return
            except botapi.RetryAfter as e:
                wait = e.seconds + 0.5
                log.warning("telegram 429, sleeping %.1fs (attempt %d/%d)",
                            wait, attempt + 1, attempts)
                time.sleep(wait)
            except Exception as e:
                log.warning("telegram send failed: %s", e)
                time.sleep(min(10.0, 2**attempt))
        self.dropped += count
        log.error("dropped %d signal(s) after %d attempts", count, attempts)
