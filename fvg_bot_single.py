import requests
import json
import os
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ACCOUNT_SIZE     = float(os.environ.get("ACCOUNT_SIZE", "1000"))
RISK_PCT         = float(os.environ.get("RISK_PCT", "10"))

# Strategy params
BOS_LOOKBACK   = 10
MIN_GAP        = 0.5
MAX_GAP        = 1.5
EMA_PERIOD     = 50
MAX_EMA_DIST   = 5.0
SL_BUFFER      = 0.2
SWING_LOOKBACK = 30
MIN_RR         = 1.0
MAX_LEVERAGE   = 20

STATE_FILE = "state.json"

# ── Telegram ──────────────────────────────────────────────────────
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.ok:
            print(f"Telegram error: {r.text}")
    except Exception as e:
        print(f"Telegram failed: {e}")

# ── Fetch candles ─────────────────────────────────────────────────
def fetch_candles(limit=500):
    for name, fn in [("Kraken", _kraken), ("OKX", _okx), ("Gate", _gate)]:
        try:
            print(f"  Trying {name}...")
            c = fn(limit)
            if c and len(c) > 100:
                print(f"  {name}: {len(c)} candles · BTC ${c[-1]['c']:,.1f}")
                return c, name
        except Exception as e:
            print(f"  {name} failed: {e}")
    raise Exception("All APIs failed")

def _kraken(limit):
    r = requests.get("https://api.kraken.com/0/public/OHLC?pair=XBTUSDT&interval=15", timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("error"): raise Exception(str(d["error"]))
    key = list(d["result"].keys())[0]
    return [{"t":int(x[0])*1000,"o":float(x[1]),"h":float(x[2]),"l":float(x[3]),"c":float(x[4])} for x in d["result"][key]][-limit:]

def _okx(limit):
    r = requests.get(f"https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=15m&limit={min(limit,300)}", timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != "0": raise Exception(d.get("msg"))
    return [{"t":int(x[0]),"o":float(x[1]),"h":float(x[2]),"l":float(x[3]),"c":float(x[4])} for x in reversed(d["data"])]

def _gate(limit):
    r = requests.get(f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=BTC_USDT&interval=15m&limit={min(limit,2000)}", timeout=15)
    r.raise_for_status()
    return [{"t":int(x["t"])*1000,"o":float(x["o"]),"h":float(x["h"]),"l":float(x["l"]),"c":float(x["c"])} for x in r.json()]

# ── Indicators ────────────────────────────────────────────────────
def calc_ema(closes, period):
    k = 2 / (period + 1)
    ema = [None] * len(closes)
    for i in range(len(closes)):
        if i < period - 1: continue
        if ema[i-1] is None:
            ema[i] = sum(closes[i-period+1:i+1]) / period
        else:
            ema[i] = closes[i] * k + ema[i-1] * (1 - k)
    return ema

def ema_slope(ema, idx):
    if idx < 3 or ema[idx] is None or ema[idx-3] is None: return 0
    return ema[idx] - ema[idx-3]

def has_impulse(c):
    body = abs(c["c"] - c["o"])
    rng  = c["h"] - c["l"]
    return rng > 0 and body / rng >= 0.6

def swing_high(candles, idx, lb):
    h = 0
    for i in range(max(0, idx-lb), idx):
        if candles[i]["h"] > h: h = candles[i]["h"]
    return h

def swing_low(candles, idx, lb):
    l = float("inf")
    for i in range(max(0, idx-lb), idx):
        if candles[i]["l"] < l: l = candles[i]["l"]
    return l

# ── BOS + FVG Detection ───────────────────────────────────────────
def nearest_swing_high_above(candles, entry, lookback):
    """Max high in last N candles above entry — matches backtest swingHigh logic."""
    n = len(candles)
    h = 0
    for i in range(max(0, n-lookback), n):
        if candles[i]["h"] > h:
            h = candles[i]["h"]
    return h if h > entry else None

def nearest_swing_low_below(candles, entry, lookback):
    """Min low in last N candles below entry — matches backtest swingLow logic."""
    n = len(candles)
    l = float("inf")
    for i in range(max(0, n-lookback), n):
        if candles[i]["l"] < l:
            l = candles[i]["l"]
    return l if l < entry else None

def find_signal(candles):
    closes = [c["c"] for c in candles]
    ema    = calc_ema(closes, EMA_PERIOD)
    lb     = BOS_LOOKBACK
    n      = len(candles)
    cur    = candles[-1]["c"]
    last_ev = ema[n-1]

    # Scan last 15 candles for a fresh BOS
    for i in range(max(lb+2, n-15), n-1):
        prev      = candles[i-lb:i]
        prev_high = max(c["h"] for c in prev)
        prev_low  = min(c["l"] for c in prev)
        c_bos     = candles[i]
        ev        = ema[i]
        if ev is None: continue

        # ── Bullish BOS: close breaks above N-candle high ────────
        if c_bos["c"] > prev_high:
            # Find bullish FVG within the last 10 candles before BOS
            for fi in range(max(lb+2, i-10), i+1):
                if fi < 2: continue
                c1, c2, c3 = candles[fi-2], candles[fi-1], candles[fi]
                if c3["l"] > c1["h"] and has_impulse(c2):
                    gap = (c3["l"] - c1["h"]) / c1["h"] * 100
                    if not (MIN_GAP <= gap <= MAX_GAP): continue
                    fvg_top, fvg_bot = c3["l"], c1["h"]

                    # Price must be pulling back into or near the FVG
                    if cur <= fvg_top * 1.02:
                        if last_ev is None: break
                        slope = ema_slope(ema, n-1)
                        dist  = abs((cur - last_ev) / last_ev * 100)
                        if cur > last_ev and slope > 0 and dist <= MAX_EMA_DIST:
                            entry  = cur
                            sl     = fvg_bot * (1 - SL_BUFFER/100)
                            # TP = nearest swing high ABOVE entry (not BOS level)
                            tp = nearest_swing_high_above(candles, entry, SWING_LOOKBACK)
                            if not tp or tp <= entry: break
                            # Make sure TP is meaningfully above entry
                            if (tp - entry) / entry * 100 < 0.3: break
                            sl_pct = abs((entry-sl)/entry*100)
                            tp_pct = (tp-entry)/entry*100
                            rr     = tp_pct/sl_pct if sl_pct > 0 else 0
                            if rr < MIN_RR: break
                            lev = min(round(RISK_PCT/sl_pct, 1), MAX_LEVERAGE)
                            return {
                                "type":"bull","entry":entry,
                                "sl":round(sl,2),"tp":round(tp,2),
                                "sl_pct":round(sl_pct,2),"tp_pct":round(tp_pct,2),
                                "rr":round(rr,2),"gap":round(gap,2),
                                "leverage":lev,"fvg_top":fvg_top,"fvg_bot":fvg_bot,
                                "bos_level":round(prev_high,2),"time":candles[-1]["t"],
                            }
                    break

        # ── Bearish BOS: close breaks below N-candle low ─────────
        if c_bos["c"] < prev_low:
            for fi in range(max(lb+2, i-10), i+1):
                if fi < 2: continue
                c1, c2, c3 = candles[fi-2], candles[fi-1], candles[fi]
                if c1["l"] > c3["h"] and has_impulse(c2):
                    gap = (c1["l"] - c3["h"]) / c3["h"] * 100
                    if not (MIN_GAP <= gap <= MAX_GAP): continue
                    fvg_top, fvg_bot = c1["l"], c3["h"]

                    # Price must be retracing into or near the FVG
                    if cur >= fvg_bot * 0.98:
                        if last_ev is None: break
                        slope = ema_slope(ema, n-1)
                        dist  = abs((cur - last_ev) / last_ev * 100)
                        if cur < last_ev and slope < 0 and dist <= MAX_EMA_DIST:
                            entry  = cur
                            sl     = fvg_top * (1 + SL_BUFFER/100)
                            # TP = nearest swing low BELOW entry
                            tp = nearest_swing_low_below(candles, entry, SWING_LOOKBACK)
                            if not tp or tp >= entry: break
                            if (entry - tp) / entry * 100 < 0.3: break
                            sl_pct = abs((sl-entry)/entry*100)
                            tp_pct = (entry-tp)/entry*100
                            rr     = tp_pct/sl_pct if sl_pct > 0 else 0
                            if rr < MIN_RR: break
                            lev = min(round(RISK_PCT/sl_pct, 1), MAX_LEVERAGE)
                            return {
                                "type":"bear","entry":entry,
                                "sl":round(sl,2),"tp":round(tp,2),
                                "sl_pct":round(sl_pct,2),"tp_pct":round(tp_pct,2),
                                "rr":round(rr,2),"gap":round(gap,2),
                                "leverage":lev,"fvg_top":fvg_top,"fvg_bot":fvg_bot,
                                "bos_level":round(prev_low,2),"time":candles[-1]["t"],
                            }
                    break
    return None

# ── State ─────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"open_trade":None,"seen_signals":[],"trades":[],
                "wins":0,"losses":0,"run_count":0,"last_daily":""}

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

# ── Check open trade ──────────────────────────────────────────────
def check_open_trade(candles, state):
    trade = state.get("open_trade")
    if not trade: return state
    cur  = candles[-1]["c"]
    low  = candles[-1]["l"]   # check candle low for SL
    high = candles[-1]["h"]   # check candle high for TP
    typ  = trade["type"]

    # Use candle low/high for more accurate SL/TP detection
    # Worst case: if both hit same candle, SL wins
    if typ == "bull":
        hit_sl = low  <= trade["sl"]
        hit_tp = high >= trade["tp"]
    else:
        hit_sl = high >= trade["sl"]
        hit_tp = low  <= trade["tp"]

    if not (hit_sl or hit_tp): return state

    win = hit_tp and not hit_sl  # SL wins if both hit same candle
    pnl = trade["tp_pct"] if win else -trade["sl_pct"]
    if win: state["wins"] += 1
    else:   state["losses"] += 1

    total = state["wins"] + state["losses"]
    wr    = state["wins"] / total * 100 if total else 0
    emoji = "✅" if win else "❌"

    send_telegram(
        f"{emoji} <b>TRADE CLOSED · {'📈' if typ=='bull' else '📉'} {typ.upper()}</b>\n\n"
        f"<b>BTC/USDT.P · 15m · BOS+FVG</b>\n\n"
        f"Entry:   ${trade['entry']:,.1f}\n"
        f"Exit:    ${cur:,.1f}\n"
        f"Result:  {'TP hit 🎯' if win else 'SL hit 🛑'}\n"
        f"P&amp;L:     {'+' if pnl>0 else ''}{pnl:.2f}%\n\n"
        f"📊 Record: {state['wins']}W / {state['losses']}L ({wr:.0f}% WR)\n"
        f"🎯 Backtest target: 60% WR"
    )
    state["trades"].append({"type":typ,"entry":trade["entry"],"exit":cur,
                             "result":"tp" if win else "sl","pnl":round(pnl,3)})
    state["open_trade"] = None
    print(f"Trade closed: {'WIN' if win else 'LOSS'} {pnl:+.2f}%")
    return state

# ── Daily update ──────────────────────────────────────────────────
def maybe_send_daily(candles, state, source):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hour  = datetime.now(timezone.utc).hour
    # Send once per day between 08:00–09:00 UTC
    if state.get("last_daily") == today or not (2 <= hour < 4):  # 02:30 UTC = 08:00 IST
        return state

    cur   = candles[-1]["c"]
    total = state["wins"] + state["losses"]
    wr    = state["wins"] / total * 100 if total else 0

    open_info = ""
    if state.get("open_trade"):
        t       = state["open_trade"]
        live    = (cur-t["entry"])/t["entry"]*100 if t["type"]=="bull" else (t["entry"]-cur)/t["entry"]*100
        open_info = (
            f"\n📂 <b>Open trade:</b> {'📈 LONG' if t['type']=='bull' else '📉 SHORT'}\n"
            f"Entry: ${t['entry']:,.1f} → Now: ${cur:,.1f}\n"
            f"Live P&amp;L: {'+' if live>0 else ''}{live:.2f}%\n"
            f"SL: ${t['sl']:,.1f}  ·  TP: ${t['tp']:,.1f}\n"
        )

    send_telegram(
        f"📅 <b>Daily Update · BOS+FVG Bot</b>\n\n"
        f"BTC/USDT.P: <b>${cur:,.1f}</b>\n"
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
        f"Source: {source}\n"
        f"{open_info}\n"
        f"📊 <b>Record:</b> {state['wins']}W / {state['losses']}L "
        f"({'%.0f' % wr}% WR · target 69%)\n"
        f"Total trades: {total}\n\n"
        f"⚙️ BOS({BOS_LOOKBACK}c) · FVG({MIN_GAP}–{MAX_GAP}%) · Risk {RISK_PCT}%\n"
        f"Account: ${ACCOUNT_SIZE:,.0f}"
    )
    state["last_daily"] = today
    print("Daily update sent.")
    return state

# ── Main ──────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"BOS+FVG Bot · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Account: ${ACCOUNT_SIZE:,.0f}  Risk: {RISK_PCT}%")
    print(f"{'='*50}")

    state = load_state()
    state["run_count"] = state.get("run_count", 0) + 1

    print("\nFetching candles...")
    try:
        candles, source = fetch_candles(500)
    except Exception as e:
        print(f"Fetch failed: {e}")
        send_telegram(f"⚠️ BOS+FVG Bot: candle fetch failed\n{e}")
        return

    cur_time = datetime.utcfromtimestamp(candles[-1]["t"]/1000).strftime("%H:%M UTC")

    # Price update every run
    cur  = candles[-1]["c"]
    high = max(c["h"] for c in candles[-96:])
    low  = min(c["l"] for c in candles[-96:])
    chg  = (cur - candles[-96]["c"]) / candles[-96]["c"] * 100
    chg_sign = "+" if chg > 0 else ""
    trade_line = ""
    if state.get("open_trade"):
        t    = state["open_trade"]
        live = (cur - t["entry"]) / t["entry"] * 100 if t["type"] == "bull" else (t["entry"] - cur) / t["entry"] * 100
        side = "LONG" if t["type"] == "bull" else "SHORT"
        lsign = "+" if live > 0 else ""
        trade_line = "\n📂 Open " + side + ": $" + "{:,.1f}".format(t["entry"]) + " → " + lsign + "{:.2f}%".format(live)
    price_msg = (
        "💹 <b>BTC/USDT.P</b>  $" + "{:,.1f}".format(cur) + "\n" +
        "24h: " + chg_sign + "{:.2f}%".format(chg) + "  " +
        "H: $" + "{:,.1f}".format(high) + "  L: $" + "{:,.1f}".format(low) +
        trade_line + "\n" +
        "⏰ " + cur_time
    )
    send_telegram(price_msg)


    # Daily heartbeat
    state = maybe_send_daily(candles, state, source)

    # Check open trade exit
    if state.get("open_trade"):
        t = state["open_trade"]
        print(f"\nOpen {t['type'].upper()} | Entry:${t['entry']:,.1f} SL:${t['sl']:,.1f} TP:${t['tp']:,.1f}")
        state = check_open_trade(candles, state)

    # Scan for new signal
    if not state.get("open_trade"):
        print("\nScanning for BOS+FVG signal...")
        sig = find_signal(candles)
        if sig:
            sig_id = f"{sig['type']}_{sig['time']}_{round(sig['entry'])}"
            if sig_id not in state.get("seen_signals", []):
                state.setdefault("seen_signals", []).append(sig_id)
                state["seen_signals"] = state["seen_signals"][-100:]

                risk_dollar = ACCOUNT_SIZE * RISK_PCT / 100
                notional    = round(risk_dollar / (sig["sl_pct"] / 100), 2)
                leverage    = sig["leverage"]
                de = "📈" if sig["type"]=="bull" else "📉"
                dl = "LONG" if sig["type"]=="bull" else "SHORT"

                send_telegram(
                    f"🚨 <b>BOS+FVG SIGNAL · {de} {dl}</b>\n\n"
                    f"<b>BTC/USDT.P · 15m</b>\n\n"
                    f"BOS level:  ${sig['bos_level']:,.1f}\n"
                    f"FVG zone:   ${sig['fvg_bot']:,.1f} – ${sig['fvg_top']:,.1f} ({sig['gap']}%)\n\n"
                    f"Entry:      ${sig['entry']:,.1f}\n"
                    f"SL:         ${sig['sl']:,.1f}  (-{sig['sl_pct']}%)\n"
                    f"TP:         ${sig['tp']:,.1f}  (+{sig['tp_pct']}%)\n"
                    f"R:R:        {sig['rr']}x\n\n"
                    f"💰 <b>Position @ {RISK_PCT}% risk:</b>\n"
                    f"Risk $:     ${risk_dollar:,.0f}\n"
                    f"Notional:   ${notional:,.0f}\n"
                    f"Leverage:   {leverage}x\n\n"
                    f"⏰ {cur_time}"
                )
                print(f"Signal: {dl} ${sig['entry']:,.1f} SL:${sig['sl']:,.1f} TP:${sig['tp']:,.1f} RR:{sig['rr']}x Lev:{leverage}x")
                state["open_trade"] = sig
            else:
                print(f"Already seen: {sig_id}")
        else:
            print("No signal this run.")
    else:
        print("Open trade active — skipping scan.")

    save_state(state)
    print(f"\nDone. Run #{state['run_count']}")

if __name__ == "__main__":
    main()
