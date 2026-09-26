"""Minimal Telegram Bot API client.

Adapted from ~/src/telegram-copy-trading/src/tct/botapi.py, with two changes
this repo needs:

* `message_thread_id`, so each screener posts into its own forum topic.
* real 429 handling. The original leaned on an outer catch-and-sleep, which was
  fine at copy-trading volume. A screener watching hundreds of symbols can
  burst hard enough to hit Telegram's limits routinely, so `retry_after` is
  surfaced as a typed exception for the dispatcher to obey.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

MAX_TEXT = 4000  # Telegram caps a message at 4096; leave room for our suffixes.


class RetryAfter(RuntimeError):
    """Telegram asked us to back off, and said for how long."""

    def __init__(self, seconds: float):
        super().__init__(f"retry after {seconds:.1f}s")
        self.seconds = seconds


class BotAPIError(RuntimeError):
    pass


def call(token: str, method: str, params: dict | None = None, *, timeout: float = 15) -> Any:
    """POST a JSON body and return `result`. Raises RetryAfter on 429."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["result"]
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        if e.code == 429:
            wait = float(payload.get("parameters", {}).get("retry_after", 1))
            raise RetryAfter(wait) from e
        desc = payload.get("description") or body[:200].decode("utf-8", "replace")
        raise BotAPIError(f"{method} failed ({e.code}): {desc}") from e


def send(
    token: str,
    chat_id: str,
    text: str,
    *,
    html: bool = True,
    topic_id: str | int | None = None,
    preview_url: str | None = None,
    buttons: list[list[dict]] | None = None,
    silent: bool = False,
) -> int | None:
    """Send one message. `topic_id` targets a forum topic. Returns the message id."""
    preview = ({"url": preview_url, "prefer_large_media": True}
               if preview_url else {"is_disabled": True})
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:MAX_TEXT],
        "link_preview_options": preview,
        "disable_notification": silent,
    }
    if html:
        params["parse_mode"] = "HTML"
    if topic_id:
        params["message_thread_id"] = int(topic_id)
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    result = call(token, "sendMessage", params)
    return result.get("message_id") if isinstance(result, dict) else None


def get_me(token: str) -> dict:
    return call(token, "getMe")
