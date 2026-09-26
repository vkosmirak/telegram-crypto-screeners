# Investigation — market screener bots

Source: [Самые Точные БОТЫ для трейдинга](https://www.youtube.com/watch?v=etxktQKHee4)
(Дмитрий Щукин | Crypto Trading, 40 min, RU). Transcript pulled 2026-09-26.

The video is a sales pitch for a closed paid product (Telegram waitlist,
per-user server capacity). No code, no API surface — but the spec is fully
described. This doc extracts that spec and checks it against real exchange APIs.

---

## 1. What the product actually is

Three Telegram alert bots ("screeners"). They do **not** trade. They watch
Binance + Bybit **USDT perpetual futures**, evaluate a rule per symbol per
tick, and push a Telegram message when it fires. The human decides entry.

Everything rests on four inputs: **price, volume, open interest (OI), CVD**
(cumulative volume delta = market-buy minus market-sell size).

### Bot 1 — OI screener → LONG signals

Fires when aggregated OI for a symbol rises by ≥ X% over the last N minutes.

| Setting | Default | Range |
|---|---|---|
| growth period | 15 min | — |
| growth % | 5% | up to 100% |
| drawdown period / % | off | optional, rarely used |
| exchanges | Binance + Bybit, both on | toggles |

Message contains: symbol, OI Δ%, price Δ%, total OI in $, **signal number for
the day**, and a deep link to Coinglass.

Read rules from the video:
- Only act on signal **#1–#3 of the day**. Higher ordinal = move already ran.
- OI↑ **and** price↑ → longs opening. OI↑ **and** price↓ → shorts stacking, skip.
- Want volume↑ and CVD↑ alongside.
- Want the prior state to be a flat range. Skip if the coin is already at
  multi-day highs / above resistance.

### Bot 2 — Pump screener → SHORT signals

Fires when price moves ≥ X% **within** N minutes (sliding window — 10% in 5 min
fires a 10%/20min rule; 10% over 30 min does not).

| Preset | Default | Range |
|---|---|---|
| long side | 2% / 2 min | 1–30 min, 0.1–100% |
| short side | 10% / 20 min | same |

Claim: 7–8 of 10 sharp pumps mean-revert. Read rules:
- **CVD falling while price pumps** = they are selling into it → short.
- OI falling on the pump = position closing → confirmation.
- Scale in with a grid rather than one full-size entry.

### Bot 3 — Liquidation screener (bundled freebie)

Streams liquidations from both exchanges, alerts on alt liquidations
≥ $20,000. Thesis: price is often pushed to the largest resting liquidation
cluster; once it is taken, the move reverses. Watch the volume on the final
push.

---

## 2. Feasibility against the real APIs

All numbers below were queried live on 2026-09-26, not taken from the video.

### Universe is bigger than the video claims

| Exchange | USDT perps, trading |
|---|---|
| Binance USDⓈ-M | **527** |
| Bybit linear | **777** |

The video says ~300. Budget for ~800 unique symbols after dedupe.

### Data availability

| Signal | Binance | Bybit |
|---|---|---|
| Price | `!ticker@arr` (1 s, all symbols, 1 stream) or `<sym>@aggTrade` | `tickers.{sym}` (100 ms) |
| CVD | `<sym>@aggTrade`, sign by `m` (isBuyerMaker) | `publicTrade.{sym}`, sign by `S` |
| Open interest | **REST poll only** — no websocket | `tickers.{sym}` carries `openInterest` + `openInterestValue`, 100 ms |
| Liquidations | `!forceOrder@arr` | `allLiquidation.{sym}` (500 ms) |

### Two real constraints

**a) Binance OI has no stream.** Only `GET /fapi/v1/openInterest` (one symbol
per call, weight 1) and `GET /futures/data/openInterestHist` (5 m granularity
minimum, 1 month retention — too coarse for a 15 min window).

Live `exchangeInfo` rate limit: **REQUEST_WEIGHT 2400 / min per IP**.
A full sweep of 527 symbols = 527 weight.

| Sweep interval | Weight/min | % of budget |
|---|---|---|
| 5 s | 6324 | over limit |
| 10 s | 3162 | over limit |
| 15 s | 2108 | 88% — no headroom |
| **20 s** | **1581** | **66% — workable** |

A 20 s sample on a 15 min window is fine (45 samples). Bybit's half of the
aggregate stays at 100 ms for free. If we ever need faster, options are a
proxy/IP pool or trimming the universe to top-N by turnover.

**b) Binance liquidation feed is lossy.** `!forceOrder@arr` pushes only the
**latest single liquidation order per symbol per 1000 ms** — it is a snapshot,
not a firehose. Bybit's `allLiquidation` batches every 500 ms but does not
drop. So Binance liquidation totals will be undercounted; the ">$20k single
trader" read from the video is only reliably reproducible on Bybit.

### Verdict

All three bots are buildable on free public endpoints. No API keys needed —
market data only, no trading. The hard part is not the signals, it is the
ingest fleet: ~800 symbols × 2–3 topics each, with reconnects, gap detection
and rolling windows.

---

## 3. Proposed architecture

```
ingest/          one asyncio task per exchange per stream type
  binance_ws.py    !ticker@arr, !forceOrder@arr, <sym>@aggTrade (sharded)
  binance_oi.py    REST sweep, 20 s, weight-budgeted
  bybit_ws.py      tickers.*, publicTrade.*, allLiquidation.*
state/           in-memory ring buffers per symbol, ~30 min retention
                   price, volume, OI (aggregated across exchanges), CVD
rules/           oi_growth.py, pump.py, liquidation.py
                   pure functions over a window -> Signal | None
                   per-symbol cooldown + daily signal counter
notify/          telegram dispatcher (queue, rate limit, retry, formatting)
config/          per-rule thresholds in TOML, hot-reloadable
```

Stack: Python 3.12 + asyncio (`websockets`, `aiohttp`), `uv` for deps.
Rationale: best-in-class crypto/data ecosystem, and the rule layer stays
plain functions that are trivial to backtest.

**Backtesting matters more than the live bot.** The video's claims (7–8 of 10
pumps revert, high winrate on first-of-day OI signals) are unverified
marketing. Rules are pure functions over a window, so the same code can be fed
historical klines + `openInterestHist` to measure actual hit rate before any
money is involved. Build that first.

---

## 4. Open questions

- Aggregate OI across exchanges, or per-exchange rules? Video aggregates.
- "Signal #N of the day" — reset at UTC midnight or local?
- Ranking / filtering of "was it a flat range before" — needs an explicit
  definition (e.g. realised vol percentile over prior 4 h).
- Telegram: one bot, topic-separated channel? Or three bots?

---

## 5. Telegram — what to borrow, and from where

Scanned every repo under `~/src/`. Telegram code exists in four places; only
one is worth copying.

| Source | Verdict |
|---|---|
| **`~/src/telegram-copy-trading`** | **Copy this.** Our own Python bot, stdlib-only HTTP, no framework. |
| `quantfury-pipelines-us` | Azure DevOps YAML + `requests` one-shots. CI-shaped, not a long-lived bot. Useful idea only: message splitting. |
| `quantfury-us` | C#/.NET, built on the private NuGet `Quantfury.Telegram`. Not portable — source isn't available. |
| `~/.claude/plugins/.../telegram` | Claude Code's own channel bridge (TS/Bun/grammy). Different problem entirely. |
| `copy-trading` | Telethon user session, read-only channel scraping. No bot. |

### The base: `telegram-copy-trading/src/tct/`

Four files carry everything we need, ~zero dependencies (`urllib.request`):

- **`botapi.py`** — the whole Bot API surface we use. `send()` (HTML, link
  preview control, inline keyboards, 4000-char truncation under Telegram's
  4096 cap), `edit()` (replace a card in place and strip its buttons),
  `answer_callback()`. Inline buttons matter here: a signal card can carry
  "mute this symbol 1h" / "snooze this rule" without building a web UI.
- **`notify.py`** — `make_notify()` wrapper that logs and **silently no-ops
  when unconfigured**, so the screener runs headless in dev without a bot.
  Also `EdgeAlerts`: fires only when a condition *starts* or *clears*, not
  every poll. That is exactly the anti-spam primitive a screener needs.
- **`cards.py`** — message grammar (`<b>SUBJECT</b> emoji · state`, quoted
  detail block, footer) with a generic `card()` builder. Our signal messages
  drop straight into it.
- **`bot.py`** — `getUpdates` long-poll loop, `/command` registration,
  chat-id allowlist, catch-all + 10 s retry so a Telegram timeout never kills
  the process. Needed if we want runtime `/threshold oi 7` style control.

### Separate bot — how

Per the ask, this repo gets its **own** bot token, not `telegram-copy-trading`'s.

`telegram-copy-trading/scripts/create_notify_bot.py` automates BotFather via a
Telethon *user* session and writes `NOTIFY_BOT_TOKEN` / `NOTIFY_CHAT_ID` into
`.env` without ever printing the token. It imports `tct.config` and
`tct.telegram.client`, so reusing it here means either vendoring those two or
just doing the 30-second manual BotFather flow. Manual is fine for a one-off.

Either way the config shape is already settled and we copy it verbatim:

```
NOTIFY_BOT_TOKEN=
NOTIFY_CHAT_ID=
```

Loaded via `python-dotenv`, with a `require_notify()` guard that exits with a
useful message when unset (`tct/config.py`).

### Gaps to close vs. the borrowed code

The `tct` client has **no 429 / `retry_after` handling** — it relies on an
outer catch-and-sleep. A screener watching 800 symbols can burst far harder
than a copy-trading bot ever did, and Telegram's limit is ~30 msg/s globally
and ~20/min to a single group. So we must add, on top of the copied base:

- a send queue with a token bucket,
- real `retry_after` backoff on 429,
- per-symbol and per-rule cooldowns (partly what `EdgeAlerts` gives us),
- batching — collapse N signals in a tick into one message.

The C# service in `quantfury-us` does handle this (`TelegramTooManyRequests
Exception` + distributed lock), but the logic lives in the closed NuGet
package, so it is a reference for *what* to do, not code we can take.

### Secrets

No repo here uses 1Password for Telegram tokens — personal projects use a
`chmod 600 .env`, the Quantfury repos use Azure DevOps variable groups. `op`
is installed on this machine (used for SSH keys in `dotfiles/setup.sh`), so
`op read "op://..."` is available if we want it, but there is no existing
wiring to copy. Start with `.env`.

### Deployment precedent

`telegram-copy-trading/deploy/` — systemd unit with `Restart=always` plus an
rsync-over-SSH push script to an EC2 box. Same shape works here; the ingest
fleet wants to be a long-lived service, and the bot runs as its **own**
service separate from the screener, so a Telegram wedge can't stall ingest.
