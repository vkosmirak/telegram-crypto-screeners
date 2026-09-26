# Findings

What the backtester says, as of 2026-09-26. Raw output in `out/`.

> **Earlier numbers in this file were wrong and have been replaced.** Two
> exchange endpoints are *end-anchored* — they return the newest N rows of a
> range, not the oldest — and were being paged forward. A 30-day open-interest
> request silently returned 1.7 days. Every conclusion drawn before that fix
> was computed on ~6% of its window. The baseline was also mis-specified (see
> below). Both are fixed; everything here is from the corrected run.

Read the **edge over random entry** rows, not raw hit rates.

## The control group

For every signal at time *t* on symbol *S*, the control draws random bars at
the **same t on other symbols**. So the question is: at this moment, in this
market, did the coin the screener picked beat a coin picked at random?
Market-wide moves cancel, because both sides live through them.

The previous control sampled bars uniformly across all symbols and all time.
That made it roughly half "coins that never move" while the signal set
concentrates on whatever was volatile — so "edge" was partly measuring symbol
selection and partly that signals cluster on the month's big days. Both
flattered the rule, and that is where the since-deleted "+14pp at +60m" came
from.

---

## 1. OI growth, long — Binance, 30 days, top 200 by turnover

Trigger: open interest +5% over 15 minutes. 5m bars.
**752 firings over 30 days ≈ 25/day.** Control n≈3755.

Hit rate, percentage points above the matched control:

| horizon | trigger alone | all filters (n=304) |
|---:|---:|---:|
| +5m | +1.1 | **+6.2** |
| +15m | +3.4 | **+5.1** |
| +30m | +0.8 | +1.4 |
| +60m | −2.0 | −1.9 |
| +240m | −7.2 | −9.5 |

### What this says

**There is a real but small and very short-lived edge, and only with the
filters on.** All filters together beat the control by ~5–6pp at 5 and 15
minutes. By 30 minutes it is ~1pp; by an hour it is gone; by four hours it is
firmly negative.

**The bare trigger is close to worthless.** +1.1pp at 5m and negative from an
hour out. "Open interest jumped 5%" on its own is not a tradable fact.

**CVD is again the best single filter** at short horizons (52.7% / 54.0% hit
at +5m / +15m vs 48.7% / 47.8% control) — and it is the one unavailable
historically on Bybit (§4).

**Mean and median disagree throughout.** Mean is positive where median is
negative at most horizons, and mean MFE at +240m is +10.5% against a median
of −0.64%. A few large winners carry the average. Hit rate alone is the wrong
success metric; any real use needs asymmetric exits.

**This is a minutes-scale signal or it is nothing.** That matches the video's
own framing ("our trades always work out quickly") and argues for a hard time
stop rather than letting a position run.

### Caveat

`n=304` after filtering is workable but not large, and the +5/+15m edge is the
whole result. Worth re-running on the full 527-symbol universe before anyone
trades it.

---

## 1b. OI growth, long — Bybit, 30 days, top 200 by turnover

Same trigger and filters, minus CVD: Bybit klines carry no taker-buy volume,
so that filter cannot be computed (§4). **2913 firings over 30 days ≈ 97/day;
1084 pass the remaining filters, ≈ 36/day.** Control n≈14,500.

| horizon | trigger alone | all filters (n=1084) |
|---:|---:|---:|
| +5m | +0.8 | −0.9 |
| +15m | −0.4 | −0.0 |
| +30m | −0.7 | −1.3 |
| +60m | −0.3 | −0.5 |
| +240m | −1.1 | −2.0 |

**No edge at any horizon, with or without filters**, on a sample large enough
to trust. Where Binance's filtered set beat the control by 5–6pp in the first
15 minutes, Bybit's is flat.

The obvious difference is the missing CVD filter, which was the strongest
single filter on Binance. So this is consistent with "CVD carries most of what
edge exists" — though it does not prove it, since the two venues also differ
in liquidity and in which coins list where. Bybit also fires ~4× as often as
Binance for the same thresholds, so without CVD its alerts are both more
numerous and uninformative.

Practical consequence: Bybit OI alerts are noise at present. Either keep the
Bybit screener off, or record Bybit CVD live from `publicTrade.{symbol}` for a
few weeks and re-test with it.

---

## 2. Pump, short — Binance, 14 days, top 100 by turnover

Trigger: price +10% within 20 minutes. 1m bars.
**395 firings over 14 days ≈ 28/day.** Control n≈1975.

This tests the boldest claim in the video: *"7–8 out of 10 sharp pumps get
corrected"* — i.e. a 70–80% win rate shorting pumps.

| horizon | control hit | trigger hit | edge | all filters (n≈55) |
|---:|---:|---:|---:|---:|
| +5m | 46.9% | 48.4% | +1.4 | −5.1 |
| +15m | 47.5% | 49.1% | +1.6 | −12.9 |
| +30m | 48.2% | 48.1% | −0.2 | −14.9 |
| +60m | 46.7% | 47.7% | +1.0 | −11.5 |
| +240m | 46.5% | 47.9% | +1.5 | +4.5 |

### The claim does not survive

**Measured win rate is 48%, not 70–80%.** It is a coin flip, within ~1.5pp of
a same-time peer at every horizon. This is the cleanest result in the
exercise: n=395 is a real sample and the answer is unambiguous.

**And it loses money.** Mean signed return for the short is negative at every
horizon and worsens with time: −0.12%, −0.67%, −1.07%, −2.11%, −4.03%. Mean
adverse excursion at +240m is **−25.5%**. That is the shape you would expect
from shorting strength: right slightly more than half the time for small
amounts, and occasionally a pump keeps running and takes a quarter of the
position with it. Fading momentum without a stop is a losing trade here.

### The filters make direction worse, not better

At +5m to +60m the filtered subset is **10–15pp below** the control. Requiring
"CVD falling into the pump" does not identify pumps that revert.

What the filters do is cut the tail: at +240m mean adverse excursion falls
from −25.5% to −16.5% and mean return from −4.03% to −1.59%. So they are a
blow-up filter, not a direction predictor — a real finding, just not the
advertised one. Note n≈55 after filtering, so treat that cell as indicative.

### Verdict

Do not build the short screener as specified. If anything here is worth
pursuing it is the filtered variant as a *risk* overlay with a hard stop, and
that is a different product from the one in the video.

---

## 3. Liquidations — not measurable yet

No historical endpoint exists on either venue. `screeners record-liquidations` is
running; a 100-second sample recorded 6 Bybit liquidations and **zero** from
Binance, consistent with Binance's documented throttle (one per symbol per
second, snapshot not firehose) rather than a bug.

Bybit is the trustworthy liquidation venue. Binance will undercount precisely
during cascades. Nothing can be said about this screener until the recorder
has run for weeks.

---

## 4. The Bybit CVD problem

Bybit klines do not carry taker-buy volume, so historical CVD cannot be
reconstructed there — and CVD is the best-performing filter on Binance (§1).

Options, in order: record CVD live from `publicTrade.{symbol}` and accumulate
going forward (cannot be backfilled — start early); use Binance CVD as a proxy
for dual-listed majors; or run Bybit without it and accept a weaker screener.

The code already degrades correctly: an uncomputable filter is omitted rather
than silently passed, and the ablation table now prints `[n/total eval]` so a
partially-computed filter cannot masquerade as a clean one.

---

## 5. Next steps

1. **Full 527-symbol universe** for §1 — fixes the sample size on the only
   cell that shows an edge.
2. **Sweep thresholds.** Everything is cached, so re-scanning at 3/7/10% and
   5/15/30min is nearly free. 5%/15min is a starting point, not a result.
3. **Add a time stop to the scoring.** The +60m/+240m collapse says exits
   matter more than entries here.
4. **Keep the liquidation recorder running.**
5. Only then consider execution. Nothing here is an edge net of fees and
   slippage — at best it is evidence that a *filtered* OI signal carries
   information for a few minutes, which is necessary but nowhere near
   sufficient.

---

## 6. One line each

* **OI growth long, Binance** — real edge, but small (~5–6pp) and gone
  within 30 minutes. Filters are what carry it; the bare trigger is noise.
* **OI growth long, Bybit** — no edge at all (n=1084 filtered). Without CVD
  the filters do nothing, and it fires 4× as often.
* **Pump short** — the 7-in-10 claim is false at n=395: a 48% coin flip that
  loses money, with a −25% mean adverse excursion at four hours. The filters
  cut tail risk rather than pick direction.
* **Liquidation** — unmeasurable until the recorder has weeks of data.
