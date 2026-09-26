"""Provision the Telegram side: discover the group, create the topics, write .env.

Why this is not fully automatic: creating a bot requires talking to @BotFather
as a *user*, and creating a group requires a user account too -- bots can do
neither. The only user session on this machine belongs to a running
copy-trading pipeline, and sharing it risks AUTH_KEY_DUPLICATED killing that
login.

So the human does two things (make the bot, make the group), and everything
after that is automated here: the bot discovers its own chat id from
getUpdates, creates its four forum topics, and writes .env.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ..config import ENV_FILE
from . import botapi

log = logging.getLogger(__name__)

# Names say the asset class and the direction, because a signal read on a
# phone has to be unambiguous without opening it. This repo only ever covers
# crypto perpetuals on Binance and Bybit -- nothing here touches equities or
# ETFs, so the naming should not imply otherwise.
GROUP_TITLE = "Crypto Screeners"

TOPICS = [
    ("TOPIC_OI", "OI growth \u2192 long", 0x6FB9F0),
    ("TOPIC_PUMP", "Pumps \u2192 short", 0xFFD67E),
    ("TOPIC_LIQUIDATION", "Liquidations", 0xFF93B2),
    ("TOPIC_OPS", "Logs", 0x8EEE98),
]


# Bots default to privacy mode on, which hides ordinary group chatter from
# them -- so waiting for a plain message is unreliable. `my_chat_member` fires
# when the bot is added or promoted regardless of privacy mode, and must be
# requested explicitly: getUpdates omits it from the default allowed set.
_ALLOWED = ["message", "channel_post", "my_chat_member", "chat_member"]


def discover_chat(token: str, timeout_s: float = 180.0) -> tuple[str, str]:
    """Wait until the bot can see a group, and return (chat_id, title).

    Resolves as soon as the bot is *added* to the group -- the user does not
    need to post anything, and privacy mode does not matter.
    """
    deadline = time.monotonic() + timeout_s
    offset = None
    seen_private: set[str] = set()
    while time.monotonic() < deadline:
        updates = botapi.call(
            token, "getUpdates",
            {"offset": offset, "timeout": 20, "allowed_updates": _ALLOWED},
            timeout=30,
        )
        for u in updates or []:
            offset = u["update_id"] + 1
            payload = (u.get("message") or u.get("channel_post")
                       or u.get("my_chat_member") or u.get("chat_member") or {})
            chat = payload.get("chat") or {}
            ctype, cid = chat.get("type"), chat.get("id")
            if not cid:
                continue
            if ctype in ("group", "supergroup"):
                return str(cid), chat.get("title") or "(untitled)"
            if ctype == "private" and str(cid) not in seen_private:
                seen_private.add(str(cid))
                log.info("saw a DM from chat %s -- add the bot to the *group*", cid)
    raise TimeoutError(
        "bot never saw a group. Add it to the group (admin, with Manage "
        "Topics), which is enough on its own."
    )


def is_forum(token: str, chat_id: str) -> bool:
    chat = botapi.call(token, "getChat", {"chat_id": chat_id})
    return bool(chat.get("is_forum"))


def create_topics(token: str, chat_id: str) -> dict[str, str]:
    """Create one forum topic per screener. Returns env-var name -> topic id."""
    out: dict[str, str] = {}
    for env_name, title, colour in TOPICS:
        try:
            res = botapi.call(token, "createForumTopic",
                              {"chat_id": chat_id, "name": title, "icon_color": colour})
            out[env_name] = str(res["message_thread_id"])
            log.info("created topic %-14s id=%s", title, out[env_name])
        except botapi.BotAPIError as e:
            log.error("could not create %r: %s", title, e)
    return out


def sync_names(token: str, chat_id: str, topic_ids: dict[str, str]) -> list[str]:
    """Rename the group and its existing topics to the canonical names above.

    Separate from create_topics so naming can be corrected on a group that is
    already set up, without tearing anything down.
    """
    changed = []
    try:
        botapi.call(token, "setChatTitle", {"chat_id": chat_id, "title": GROUP_TITLE})
        changed.append(f"group -> {GROUP_TITLE}")
    except botapi.BotAPIError as e:
        log.error("could not rename group: %s", e)

    for env_name, title, colour in TOPICS:
        tid = topic_ids.get(env_name)
        if not tid:
            continue
        try:
            botapi.call(token, "editForumTopic",
                        {"chat_id": chat_id, "message_thread_id": int(tid), "name": title})
            changed.append(f"topic {tid} -> {title}")
        except botapi.BotAPIError as e:
            # Telegram rejects a rename to the name it already has.
            if "TOPIC_NOT_MODIFIED" in str(e):
                continue
            log.error("could not rename topic %s: %s", tid, e)
    return changed


def write_env(values: dict[str, str], path: Path | None = None) -> Path:
    """Upsert keys into .env, preserving everything else. Creates from the
    example if absent. Never prints the token."""
    p = path or ENV_FILE
    if not p.exists():
        example = p.parent / ".env.example"
        p.write_text(example.read_text() if example.exists() else "")
    lines = p.read_text().splitlines()
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}={val}")
    p.write_text("\n".join(out) + "\n")
    p.chmod(0o600)
    return p


def validate_token(token: str) -> str:
    if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", token.strip()):
        raise SystemExit("that does not look like a bot token (expected 123456:ABC-...)")
    return token.strip()
