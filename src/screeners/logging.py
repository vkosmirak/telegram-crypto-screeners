"""One place to configure logging, so scripts do not each invent a format."""
from __future__ import annotations

import logging
import os
import sys
import time


def setup(level: str | None = None) -> None:
    """Dated UTC timestamps on every line.

    Time-only stamps made "what happened yesterday at 14:00" unanswerable
    from the logs, and local time made a log from the Mac disagree with one
    from the server by three hours for the same moment.
    """
    lvl = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)sZ %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("screeners.data.http").setLevel(logging.WARNING)
