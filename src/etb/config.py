"""Config loading: rules from TOML, secrets from .env / environment.

Nothing here reaches the network or Telegram, so the backtester can import it
without any credentials present.
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES = ROOT / "config" / "rules.toml"
ENV_FILE = ROOT / ".env"


# ── rules ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class OIGrowthConfig:
    enabled: bool = True
    window_min: int = 15
    growth_pct: float = 5.0
    cooldown_min: int = 15
    max_ordinal: int = 3
    price_up: bool = True
    cvd_up: bool = True
    volume_up: bool = True
    flat_before: bool = True
    flat_lookback_h: float = 4.0
    flat_max_runup_pct: float = 15.0

    def without_filters(self) -> "OIGrowthConfig":
        """The bare trigger, for measuring what the filters actually buy us."""
        from dataclasses import replace

        return replace(
            self, max_ordinal=0, price_up=False, cvd_up=False,
            volume_up=False, flat_before=False,
        )


@dataclass(frozen=True, slots=True)
class PumpSideConfig:
    enabled: bool = True
    window_min: int = 20
    move_pct: float = 10.0


@dataclass(frozen=True, slots=True)
class PumpConfig:
    enabled: bool = True
    cooldown_min: int = 20
    long: PumpSideConfig = field(default_factory=lambda: PumpSideConfig(True, 2, 2.0))
    short: PumpSideConfig = field(default_factory=lambda: PumpSideConfig(True, 20, 10.0))
    max_ordinal: int = 0
    cvd_down: bool = True
    oi_down: bool = True

    def without_filters(self) -> "PumpConfig":
        from dataclasses import replace

        return replace(self, max_ordinal=0, cvd_down=False, oi_down=False)


@dataclass(frozen=True, slots=True)
class LiquidationConfig:
    enabled: bool = True
    min_usd: float = 20000.0
    exclude: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    cooldown_min: int = 5


@dataclass(frozen=True, slots=True)
class Rules:
    oi_growth: OIGrowthConfig
    pump: PumpConfig
    liquidation: LiquidationConfig
    horizons_min: tuple[int, ...] = (5, 15, 30, 60, 240)


def _sub(d: dict[str, Any], *path: str) -> dict[str, Any]:
    cur: Any = d
    for p in path:
        cur = cur.get(p, {}) if isinstance(cur, dict) else {}
    return cur if isinstance(cur, dict) else {}


def load_rules(path: Path | str | None = None) -> Rules:
    p = Path(path) if path else DEFAULT_RULES
    raw = tomllib.loads(p.read_text()) if p.exists() else {}

    oi, oif = _sub(raw, "oi_growth"), _sub(raw, "oi_growth", "filters")
    oi_cfg = OIGrowthConfig(
        enabled=oi.get("enabled", True),
        window_min=int(oi.get("window_min", 15)),
        growth_pct=float(oi.get("growth_pct", 5.0)),
        cooldown_min=int(oi.get("cooldown_min", 15)),
        max_ordinal=int(oif.get("max_ordinal", 3)),
        price_up=bool(oif.get("price_up", True)),
        cvd_up=bool(oif.get("cvd_up", True)),
        volume_up=bool(oif.get("volume_up", True)),
        flat_before=bool(oif.get("flat_before", True)),
        flat_lookback_h=float(oif.get("flat_lookback_h", 4.0)),
        flat_max_runup_pct=float(oif.get("flat_max_runup_pct", 15.0)),
    )

    pm, pmf = _sub(raw, "pump"), _sub(raw, "pump", "filters")

    def side(name: str, dw: int, dp: float) -> PumpSideConfig:
        s = _sub(raw, "pump", name)
        return PumpSideConfig(
            enabled=s.get("enabled", True),
            window_min=int(s.get("window_min", dw)),
            move_pct=float(s.get("move_pct", dp)),
        )

    pump_cfg = PumpConfig(
        enabled=pm.get("enabled", True),
        cooldown_min=int(pm.get("cooldown_min", 20)),
        long=side("long", 2, 2.0),
        short=side("short", 20, 10.0),
        max_ordinal=int(pmf.get("max_ordinal", 0)),
        cvd_down=bool(pmf.get("cvd_down", True)),
        oi_down=bool(pmf.get("oi_down", True)),
    )

    lq = _sub(raw, "liquidation")
    liq_cfg = LiquidationConfig(
        enabled=lq.get("enabled", True),
        min_usd=float(lq.get("min_usd", 20000.0)),
        exclude=tuple(lq.get("exclude", ["BTCUSDT", "ETHUSDT"])),
        cooldown_min=int(lq.get("cooldown_min", 5)),
    )

    bt = _sub(raw, "backtest")
    horizons = tuple(int(h) for h in bt.get("horizons_min", [5, 15, 30, 60, 240]))
    return Rules(oi_cfg, pump_cfg, liq_cfg, horizons)


# ── secrets ───────────────────────────────────────────────────────────────────

def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from .env without overriding what is already set."""
    p = path or ENV_FILE
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.split("#", 1)[0].strip().strip("'\"")
        if k and k not in os.environ:
            os.environ[k] = v


@dataclass(frozen=True, slots=True)
class NotifySettings:
    """Telegram wiring. One bot, one forum supergroup, a topic per screener."""

    bot_token: str = ""
    chat_id: str = ""
    topics: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def topic_for(self, rule: str) -> str | None:
        """Map a rule name to its forum topic.

        Rules are named per side (`pump_short`, `pump_long`) but share one
        topic (`pump`), so fall back to the prefix before the first underscore.
        Without this the pump screeners silently post to the group's General
        topic, which looks like it worked.
        """
        if rule in self.topics:
            return self.topics[rule] or None
        head = rule.split("_", 1)[0]
        return self.topics.get(head) or None

    def require(self) -> None:
        if not self.configured:
            sys.exit(
                "Telegram is not configured. Set NOTIFY_BOT_TOKEN and NOTIFY_CHAT_ID\n"
                "in .env (copy .env.example). See README -> Telegram setup."
            )


def load_notify(env: dict[str, str] | None = None) -> NotifySettings:
    load_dotenv()
    e = env if env is not None else os.environ
    topics = {
        "oi_growth": e.get("TOPIC_OI", ""),
        "pump": e.get("TOPIC_PUMP", ""),
        "liquidation": e.get("TOPIC_LIQUIDATION", ""),
        "ops": e.get("TOPIC_OPS", ""),
    }
    return NotifySettings(
        bot_token=e.get("NOTIFY_BOT_TOKEN", ""),
        chat_id=e.get("NOTIFY_CHAT_ID", ""),
        topics={k: v for k, v in topics.items() if v},
    )
