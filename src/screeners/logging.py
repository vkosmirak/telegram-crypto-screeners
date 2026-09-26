"""One place to configure logging, so scripts do not each invent a format."""
from __future__ import annotations

import logging
import os
import sys


def setup(level: str | None = None) -> None:
    lvl = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("screeners.data.http").setLevel(logging.WARNING)
