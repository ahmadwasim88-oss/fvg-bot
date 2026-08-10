# Portfolio Tracker Bot

Python port of `Portfolio_Tracker.html`'s signal engine, running on GitHub Actions cron
(reusing the fvg-bot repo's infra) instead of needing the browser tab open.

## What's ported 1:1 from the HTML tool
- `calcEMASeries` / `calcRSISeries` / `calcReturnPct` / `calcAvgDailyMovePct` / `reconstructStreakDays`
- `computeTaFromCloses` (RSI-14, EMA-200, 90-day rolling low from CoinGecko daily closes)
- `classifyHolding` — the full EXIT / AT RISK / WAIT / HOLD / TRIM / ACCUMULATE decision tree,
  including the DCA gap gate, relative-strength-vs-BTC conviction nudge, and target-proximity
  TRIM override.
- The once-daily TA refresh boundary (aligned to 00:00 UTC + 15 min, same as the HTML tool).

## What's different (automation-specific, not in the HTML tool)
- **Coin list**: hardcoded in `PORTFOLIO` at the top of `tracker_bot.py` (the HTML tool keeps
  this in browser localStorage). Edit that dict directly to add/remove coins or change targets.
- **Alerts**: sends a Telegram message the moment any coin's signal changes, plus one daily
  digest of all signals (sent once, in the 03:00–05:00 UTC / ~08:30–10:30 IST window, chosen to
  land after the daily TA refresh).
- **DCA log**: `state["dca_log"]` exists but nothing writes to it yet — there's no "Log DCA"
  button equivalent in a headless bot. Until you log a price, the gap gate has nothing to
  compare against, so it stays open (fires ACCUMULATE every day conditions hold) — same
  behavior as a fresh browser session with no DCA logged yet. If you want to log a DCA price
  from Telegram (e.g. a bot command), that needs to be added — say the word and I'll wire it up.

## Setup
1. Push this to your repo (replacing fvg-bot's contents, or in a new repo — your call).
2. In repo Settings → Secrets and variables → Actions, set:
   - `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (reuse from fvg-bot if same channel)
   - `COINGECKO_API_KEY` (optional — falls back to the demo key already in the HTML tool if unset)
3. The workflow runs every 2 hours automatically, or trigger manually via
   Actions → Portfolio Tracker Bot → Run workflow.

## Not yet ported (flag if you want these)
- Liquidity-dip-only microcap nuances that only ever showed up in the UI (colors, icons) —
  irrelevant to a text alert.
- The ⚡ "live low" / "live ATH" flags — cosmetic in the HTML tool, no Telegram equivalent needed.
