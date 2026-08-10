import requests
import json
import os
import time
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
API_BASE = "https://api.coingecko.com/api/v3"
API_KEY  = os.environ.get("COINGECKO_API_KEY", "CG-G1MtEv33nwCgRyvF4YNsKyDs")

STATE_FILE = "state.json"

# Coin list + INR targets, ported from the tracker's saved portfolio.
# Edit this dict directly on GitHub to add/remove coins or change targets.
PORTFOLIO = {
    "elrond-erd-2":            {"symbol": "EGLD",  "target": 7497.09},
    "kava":                    {"symbol": "KAVA",  "target": 93.11},
    "axie-infinity":           {"symbol": "AXS",   "target": 1572.92},
    "polkadot":                {"symbol": "DOT",   "target": 1379.78},
    "theta-fuel":              {"symbol": "TFUEL", "target": 9.99},
    "polygon-ecosystem-token": {"symbol": "POL",   "target": 98.5},
    "the-graph":               {"symbol": "GRT",   "target": 15.67},
    "avalanche-2":             {"symbol": "AVAX",  "target": 6612.02},
    "1inch":                   {"symbol": "1INCH", "target": 81.37},
    "internet-computer":       {"symbol": "ICP",   "target": 1765.6},
    "nano":                    {"symbol": "XNO",   "target": 247.4},
    "ethereum":                {"symbol": "ETH",   "target": 431946},
    "bitcoin":                 {"symbol": "BTC",   "target": 11187013},
}
MACRO_ID = "bitcoin"  # used for relative-strength-vs-BTC; always fetched even if not held

# ── Signal-engine constants (ported 1:1 from Portfolio_Tracker.html) ──
DCA_EXTREME_PCT   = -85
DCA_DEEP_PCT      = -70
DCA_MODERATE_PCT  = -40
DCA_WATCH_PCT     = -20
DCA_DIP_WINDOW_DAYS = 90
DCA_DIP_NEAR_PCT    = 3
DCA_VOL_LOOKBACK_DAYS = 20
DCA_GAP_MULT      = 3.0
DCA_GAP_FLOOR_PCT = 5
DCA_GAP_CEIL_PCT  = 12
REL_STRENGTH_WINDOW_DAYS = 14
REL_STRENGTH_SCALE     = 3
REL_STRENGTH_MIN_BONUS = -10
REL_STRENGTH_MAX_BONUS = 8
EMA_TREND_FAST_SHORT = 20
EMA_TREND_FAST_LONG  = 50
EMA_TREND_LONG = 200
RSI_PERIOD    = 14
RSI_OVERSOLD  = 30
TARGET_PROXIMITY_PCT = 3
LIQUIDITY_DEAD_RATIO = 0.0005
ABSOLUTE_DEAD_VOLUME = 100000
MICROCAP_FLOOR = 2000000
RANK_CAUTION   = 500
TA_HISTORY_DAYS = 365
TA_ALIGN_BUFFER_MIN = 15  # TA is treated stale once per UTC day, 15 min after 00:00 UTC

# ── Telegram ──────────────────────────────────────────────────────
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        if not r.ok:
            print(f"Telegram error: {r.text}")
    except Exception as e:
        print(f"Telegram failed: {e}")

# ── State ─────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"signals": {}, "ta_cache": {}, "ta_timestamp": 0, "last_daily": "", "dca_log": {}}

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

# ── Indicators (ported 1:1 from calcEMASeries/calcRSISeries/etc.) ──
def calc_ema_series(values, period):
    if not values or len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [None] * len(values)
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1 - k)
        out[i] = ema
    return out

def calc_rsi_series(values, period):
    if not values or len(values) < period + 1:
        return []
    out = [None] * len(values)
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period
        out[i] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out

def calc_return_pct(closes, days):
    if not closes or len(closes) < days + 1:
        return None
    start, end = closes[-(days + 1)], closes[-1]
    if start is None or end is None or start == 0:
        return None
    return (end - start) / start * 100

def calc_avg_daily_move_pct(closes, days):
    if not closes or len(closes) < days + 1:
        return None
    sl = closes[-(days + 1):]
    s, n = 0.0, 0
    for i in range(1, len(sl)):
        prev, cur = sl[i - 1], sl[i]
        if prev:
            s += abs((cur - prev) / prev * 100)
            n += 1
    return s / n if n else None

def reconstruct_streak_days(closes, cur):
    if not closes or cur is None:
        return 1
    series = closes + [cur]
    rsi_series = calc_rsi_series(series, RSI_PERIOD)
    streak = 0
    for i in range(len(series) - 1, -1, -1):
        rsi = rsi_series[i] if i < len(rsi_series) else None
        if rsi is None:
            break
        window_start = max(0, i - DCA_DIP_WINDOW_DAYS + 1)
        low = min(series[window_start:i + 1])
        above_low = (series[i] - low) / low * 100
        if not (above_low <= DCA_DIP_NEAR_PCT and rsi <= RSI_OVERSOLD):
            break
        streak += 1
    return streak or 1

def compute_ta_from_closes(closes):
    if not closes or len(closes) < RSI_PERIOD + 1:
        return None
    ema_long_series = calc_ema_series(closes, EMA_TREND_LONG)
    rsi_series = calc_rsi_series(closes, RSI_PERIOD)
    dip_window = closes[-DCA_DIP_WINDOW_DAYS:]
    rolling_low = min(dip_window) if dip_window else None
    rolling_low_index = dip_window.index(rolling_low) if dip_window else -1
    return {
        "rsi": rsi_series[-1] if rsi_series else None,
        "emaLong": ema_long_series[-1] if ema_long_series else None,
        "rollingLow": rolling_low,
        "rollingLowDaysAgo": (len(dip_window) - rolling_low_index) if rolling_low_index >= 0 else None,
        "rollingLowWindowDays": len(dip_window),
        "closes": closes,
        "timestamp": time.time() * 1000,
    }

def days_since(date_str):
    if not date_str:
        return None
    try:
        then = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None
    return max(0, round((datetime.now(timezone.utc) - then).total_seconds() / 86400))

# ── Data fetch ────────────────────────────────────────────────────
def fetch_ta_history(coin_id):
    try:
        r = requests.get(
            f"{API_BASE}/coins/{coin_id}/market_chart",
            params={"vs_currency": "inr", "days": TA_HISTORY_DAYS, "interval": "daily", "x_cg_demo_api_key": API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        points = [p for p in r.json().get("prices", []) if p and p[1] is not None]
        if not points:
            return None
        last_date = datetime.utcfromtimestamp(points[-1][0] / 1000).strftime("%Y-%m-%d")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        usable = points[:-1] if last_date == today else points
        return [p[1] for p in usable]
    except Exception as e:
        print(f"TA fetch failed for {coin_id}: {e}")
        return None

def fetch_market_data(ids):
    try:
        r = requests.get(
            f"{API_BASE}/coins/markets",
            params={
                "vs_currency": "inr", "ids": ",".join(ids), "order": "market_cap_desc",
                "per_page": 250, "page": 1, "sparkline": "false",
                "price_change_percentage": "7d,30d", "x_cg_demo_api_key": API_KEY,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        market, found = {}, set()
        for coin in data:
            found.add(coin["id"])
            market[coin["id"]] = {
                "total_volume": coin.get("total_volume"), "market_cap": coin.get("market_cap"),
                "market_cap_rank": coin.get("market_cap_rank"), "ath": coin.get("ath"),
                "ath_change_percentage": coin.get("ath_change_percentage"), "ath_date": coin.get("ath_date"),
                "atl": coin.get("atl"), "atl_date": coin.get("atl_date"),
                "current_price": coin.get("current_price"),
                "change_24h": coin.get("price_change_percentage_24h"), "notFound": False,
            }
        for cid in ids:
            if cid not in found:
                market[cid] = {"notFound": True}
        return market
    except Exception as e:
        print(f"Market data fetch failed: {e}")
        return None

# ── Signal engine (ported 1:1 from classifyHolding) ─────────────────
def classify_holding(coin_id, md, item, ta, macro_ta, dca_log):
    if md is None:
        return {"signal": "NA", "strength": 0, "reason": "No market data yet.", "cmp": None}
    if md.get("notFound"):
        return {"signal": "NA", "strength": 0, "reason": "Not found in latest CoinGecko data — may be delisted.", "cmp": None}

    volume, mcap, rank = md.get("total_volume"), md.get("market_cap"), md.get("market_cap_rank")
    cur = md.get("current_price")
    md_ath, md_atl = md.get("ath"), md.get("atl")
    ath_value = max(md_ath, cur) if (md_ath is not None and cur is not None) else md_ath
    ath_pct = (md.get("ath_change_percentage") if ath_value == md_ath else 0) if (ath_value is not None and cur is not None) else md.get("ath_change_percentage")
    atl_value = min(md_atl, cur) if (md_atl is not None and cur is not None) else md_atl
    atl_date = md.get("atl_date")

    own_ret = calc_return_pct(ta.get("closes") if ta else None, REL_STRENGTH_WINDOW_DAYS)
    btc_ret = calc_return_pct(macro_ta.get("closes") if macro_ta else None, REL_STRENGTH_WINDOW_DAYS)
    rel_strength = (own_ret - btc_ret) if (own_ret is not None and btc_ret is not None) else None

    live_closes = (ta["closes"] + [cur]) if (ta and ta.get("closes") and cur is not None) else (ta.get("closes") if ta else None)
    live_rsi_series = calc_rsi_series(live_closes, RSI_PERIOD) if live_closes else []
    live_rsi = live_rsi_series[-1] if live_rsi_series else (ta.get("rsi") if ta else None)
    ema_fs = calc_ema_series(live_closes, EMA_TREND_FAST_SHORT) if live_closes else []
    ema_fl = calc_ema_series(live_closes, EMA_TREND_FAST_LONG) if live_closes else []
    sentiment = None
    if ema_fs and ema_fl:
        a, b = ema_fs[-1], ema_fl[-1]
        sentiment = "Bullish" if a > b else ("Bearish" if a < b else "Mixed")

    no_trading_data = volume is None or mcap is None
    zero_mcap = (not no_trading_data) and mcap == 0
    vol_mcap_ratio = (volume / mcap) if (not no_trading_data and mcap > 0) else None
    negligible_volume = (not no_trading_data) and mcap < MICROCAP_FLOOR and volume < ABSOLUTE_DEAD_VOLUME
    dead_liquidity = no_trading_data or volume == 0 or zero_mcap or negligible_volume or (vol_mcap_ratio is not None and vol_mcap_ratio < LIQUIDITY_DEAD_RATIO)
    microcap = mcap is not None and mcap < MICROCAP_FLOOR
    poor_rank = rank is None or rank > RANK_CAUTION
    at_risk = (not dead_liquidity) and microcap and poor_rank

    depth = abs(ath_pct) if ath_pct is not None else None

    if dead_liquidity:
        signal, strength = "EXIT", (90 if (no_trading_data or zero_mcap) else max(60, min(95, round(100 - (vol_mcap_ratio or 0) / LIQUIDITY_DEAD_RATIO * 40))))
        reason = "No volume/market-cap data — trading may have stopped." if no_trading_data else \
                 "Real ₹0 market cap — likely defunct/delisted." if zero_mcap else \
                 f"Volume negligible against a {mcap:,.0f} market cap." if negligible_volume else \
                 f"Volume/market-cap ratio ({vol_mcap_ratio * 100:.2f}%) has dried up."
    elif at_risk:
        signal, strength = "AT RISK", 50
        reason = f"Micro-cap ({mcap:,.0f}, rank {rank}) with real survival risk."
    elif ath_pct is not None:
        if ath_pct <= DCA_EXTREME_PCT:
            signal, strength, reason = "WAIT", 20, f"{depth:.0f}% below ATH — statistically extreme."
        elif ath_pct <= DCA_DEEP_PCT:
            signal, strength, reason = "WAIT", 30, f"{depth:.0f}% below ATH — deep discount."
        elif ath_pct <= DCA_MODERATE_PCT:
            signal, strength, reason = "WAIT", 25, f"{depth:.0f}% below ATH — meaningful discount."
        elif ath_pct <= DCA_WATCH_PCT:
            signal, strength, reason = "HOLD", 35, f"Only {depth:.0f}% below ATH — not deep enough yet."
        else:
            signal, strength, reason = "TRIM", 55, f"Just {depth:.0f}% below ATH — closer to profit-taking zone."
    else:
        signal, strength, reason = "HOLD", 25, "No ATH data yet."

    atl_within_window = False
    if atl_date and ta and ta.get("rollingLowWindowDays") is not None:
        ds = days_since(atl_date)
        atl_within_window = ds is not None and ds <= ta["rollingLowWindowDays"]
    effective_low = None
    if ta and ta.get("rollingLow") is not None and cur is not None:
        candidates = [ta["rollingLow"], cur]
        if atl_within_window and atl_value is not None:
            candidates.append(atl_value)
        effective_low = min(candidates)

    dca_streak_day = None
    if signal == "WAIT":
        rel_bonus, rel_note = 0, ""
        if rel_strength is not None:
            rel_bonus = max(REL_STRENGTH_MIN_BONUS, min(REL_STRENGTH_MAX_BONUS, rel_strength / REL_STRENGTH_SCALE))
            rel_note = f" {'Outperforming' if rel_strength > 0 else 'Underperforming'} BTC ({rel_strength:+.1f}pp/{REL_STRENGTH_WINDOW_DAYS}d)."

        if ta and ta.get("rollingLow") is not None and cur is not None:
            above_low = (cur - effective_low) / effective_low * 100
            near_low = above_low <= DCA_DIP_NEAR_PCT
            rsi_oversold = live_rsi is not None and live_rsi <= RSI_OVERSOLD
            avg_daily_move = calc_avg_daily_move_pct(ta.get("closes"), DCA_VOL_LOOKBACK_DAYS)
            if near_low and rsi_oversold:
                dca_streak_day = reconstruct_streak_days(ta["closes"], cur)
                last_log = (dca_log or {}).get(coin_id)
                dyn_gap_pct = max(DCA_GAP_FLOOR_PCT, min(DCA_GAP_CEIL_PCT, (avg_daily_move or DCA_GAP_FLOOR_PCT) * DCA_GAP_MULT))
                gap_pct = ((last_log["price"] - cur) / last_log["price"] * 100) if last_log else None
                gap_cleared = last_log is None or gap_pct >= dyn_gap_pct
                if gap_cleared:
                    signal, strength = "ACCUMULATE", round(min(60, strength + 30) + rel_bonus)
                    reason = f"{depth:.0f}% below ATH, {above_low:.1f}% above its {ta['rollingLowWindowDays']}-day low, RSI {live_rsi:.0f} (oversold) — day {dca_streak_day}.{rel_note}"
                else:
                    reason = f"Meets add conditions (day {dca_streak_day}) but only {gap_pct:.1f}% below last logged DCA — needs \u2265{dyn_gap_pct:.1f}%.{rel_note}"
            else:
                if near_low and not rsi_oversold:
                    reason = f"{depth:.0f}% below ATH and near its {ta['rollingLowWindowDays']}-day low, but RSI ({'—' if live_rsi is None else f'{live_rsi:.0f}'}) isn't oversold yet."
                else:
                    reason = f"{depth:.0f}% below ATH, not near its {ta['rollingLowWindowDays']}-day low yet ({abs(above_low):.1f}% {'above' if above_low >= 0 else 'below'})."
        else:
            reason = "Not enough price history loaded yet."

    target = item.get("target")
    if target and cur is not None and cur >= target * (1 - TARGET_PROXIMITY_PCT / 100) and signal not in ("EXIT", "AT RISK"):
        signal, strength = "TRIM", 55
        reason = f"Price is at/within {TARGET_PROXIMITY_PCT}% of target (\u20b9{target:,.2f})."

    return {
        "signal": signal, "strength": strength, "reason": reason, "cmp": cur,
        "athPct": ath_pct, "rsi": live_rsi, "sentiment": sentiment,
        "relStrengthVsBtc": rel_strength, "dcaStreakDay": dca_streak_day,
    }

# ── TA refresh (once/day, boundary-aligned like taBoundaryPassedSince) ──
def ta_boundary_passed_since(ts_ms):
    if not ts_ms:
        return True
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    boundary = now.replace(hour=0, minute=TA_ALIGN_BUFFER_MIN, second=0, microsecond=0)
    if now < boundary:
        boundary -= timedelta(days=1)
    return ts_ms / 1000 < boundary.timestamp()

def refresh_ta(state, ids):
    ta_cache = state.setdefault("ta_cache", {})
    stale = [cid for cid in ids if cid not in ta_cache or ta_boundary_passed_since(ta_cache[cid].get("timestamp"))]
    updated = 0
    for cid in stale:
        closes = fetch_ta_history(cid)
        if closes:
            ta = compute_ta_from_closes(closes)
            if ta:
                ta_cache[cid] = ta
                updated += 1
        time.sleep(1.5)  # rate-limit, matches TA_FETCH_DELAY_MS
    return updated

# ── Formatting ────────────────────────────────────────────────────
SIGNAL_EMOJI = {"ACCUMULATE": "\U0001F7E2", "WAIT": "\u26AA", "HOLD": "\U0001F535", "TRIM": "\U0001F7E1", "EXIT": "\U0001F534", "AT RISK": "\U0001F7E0", "NA": "\u26AA"}

def fmt_inr(n):
    if n is None:
        return "\u2014"
    return f"\u20b9{n:,.2f}" if abs(n) < 1000 else f"\u20b9{n:,.0f}"

def digest_message(results):
    lines = ["\U0001F4CA <b>Portfolio Signal Digest</b>\n"]
    for cid, r in results.items():
        sym = PORTFOLIO[cid]["symbol"]
        emoji = SIGNAL_EMOJI.get(r["signal"], "\u26AA")
        lines.append(f"{emoji} <b>{sym}</b> {fmt_inr(r['cmp'])} — {r['signal']} ({r['strength']}%)")
    return "\n".join(lines)

def change_message(cid, prev, new):
    sym = PORTFOLIO[cid]["symbol"]
    emoji = SIGNAL_EMOJI.get(new["signal"], "\u26AA")
    return (
        f"{emoji} <b>{sym} \u2192 {new['signal']}</b> ({new['strength']}%)\n\n"
        f"Was: {prev or 'N/A'}\n"
        f"CMP: {fmt_inr(new['cmp'])}\n\n"
        f"{new['reason']}"
    )

# ── Main ──────────────────────────────────────────────────────────
def main():
    state = load_state()
    ids = list(PORTFOLIO.keys())
    all_ids = list(dict.fromkeys(ids + [MACRO_ID]))

    print("Fetching market data...")
    market = fetch_market_data(all_ids)
    if market is None:
        send_telegram("\u26A0\uFE0F Tracker bot: market data fetch failed.")
        return

    print("Refreshing TA (if stale)...")
    refresh_ta(state, all_ids)
    save_state(state)

    ta_cache = state.get("ta_cache", {})
    macro_ta = ta_cache.get(MACRO_ID)
    dca_log = state.get("dca_log", {})

    results = {}
    for cid in ids:
        results[cid] = classify_holding(cid, market.get(cid), PORTFOLIO[cid], ta_cache.get(cid), macro_ta, dca_log)

    prev_signals = state.get("signals", {})
    changes = []
    for cid, r in results.items():
        prev = prev_signals.get(cid)
        if prev != r["signal"]:
            changes.append((cid, prev, r))

    for cid, prev, r in changes:
        send_telegram(change_message(cid, prev, r))
        print(f"Signal change: {PORTFOLIO[cid]['symbol']} {prev} -> {r['signal']}")

    state["signals"] = {cid: r["signal"] for cid, r in results.items()}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hour = datetime.now(timezone.utc).hour
    if state.get("last_daily") != today and 3 <= hour < 5:
        send_telegram(digest_message(results))
        state["last_daily"] = today
        print("Daily digest sent.")

    save_state(state)
    print("Done.")

if __name__ == "__main__":
    main()
