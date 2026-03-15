import requests
import json
import os
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID= os.environ.get("TELEGRAM_CHAT_ID", "")
MIN_GAP_PCT     = float(os.environ.get("MIN_GAP", "0.5"))
MAX_GAP_PCT     = float(os.environ.get("MAX_GAP", "1.0"))
EMA_PERIOD      = int(os.environ.get("EMA_PERIOD", "50"))
MAX_EMA_DIST    = float(os.environ.get("MAX_EMA_DIST", "3.0"))
MAX_WAIT        = int(os.environ.get("MAX_WAIT", "20"))
EXIT_CANDLES    = int(os.environ.get("EXIT_CANDLES", "8"))
SYMBOL          = "BTCUSDT"
STATE_FILE      = "state.json"

# Binance API endpoints — tries each until one works
BINANCE_ENDPOINTS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
]

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        if not r.ok:
            print(f"Telegram error: {r.text}")
    except Exception as e:
        print(f"Telegram failed: {e}")

def fetch_candles(limit=300):
    for base in BINANCE_ENDPOINTS:
        try:
            url = f"{base}/fapi/v1/klines?symbol={SYMBOL}&interval=15m&limit={limit}"
            r = requests.get(url, timeout=15)
            if r.status_code == 451:
                print(f"  {base} blocked (451), trying next...")
                continue
            r.raise_for_status()
            data = r.json()
            print(f"  Fetched {len(data)} candles from {base}")
            return [{"time": c[0], "open": float(c[1]), "high": float(c[2]),
                     "low": float(c[3]), "close": float(c[4])} for c in data]
        except Exception as e:
            print(f"  {base} failed: {e}, trying next...")
            continue
    raise Exception("All Binance endpoints blocked or failed. Try adding a VPN proxy.")

def calc_ema(candles, period):
    k = 2 / (period + 1)
    result = [None] * len(candles)
    prev = None
    for i, c in enumerate(candles):
        if i < period - 1: continue
        if prev is None:
            prev = sum(x["close"] for x in candles[:period]) / period
            result[i] = prev
        else:
            prev = c["close"] * k + prev * (1 - k)
            result[i] = prev
    return result

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"open_trades": {}, "seen_fvgs": [], "closed_count": 0, "win_count": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def run():
    state       = load_state()
    open_trades = state.get("open_trades", {})
    seen_fvgs   = set(state.get("seen_fvgs", []))
    closed_count= state.get("closed_count", 0)
    win_count   = state.get("win_count", 0)

    candles = fetch_candles(300)
    ema     = calc_ema(candles, EMA_PERIOD)
    now_ts  = candles[-1]["time"]

    # ── 1. Detect FVG zones ───────────────────────────────────────
    for i in range(2, len(candles) - 1):
        c1, c3 = candles[i-2], candles[i]

        # Bullish FVG
        if c3["low"] > c1["high"]:
            gap = (c3["low"] - c1["high"]) / c1["high"] * 100
            fid = f"bull_{c1['time']}"
            if MIN_GAP_PCT <= gap <= MAX_GAP_PCT and fid not in seen_fvgs and fid not in open_trades:
                end = min(i + MAX_WAIT + 1, len(candles))
                for j in range(i+1, end):
                    c = candles[j]
                    ev = ema[j]
                    if not ev: continue
                    if c["low"] <= c3["low"] and c["low"] >= c1["high"]:
                        dist = abs((c["close"] - ev) / ev * 100)
                        if c["close"] > ev and dist <= MAX_EMA_DIST:
                            open_trades[fid] = {
                                "type": "bull", "entryPrice": c["close"],
                                "entryTime": c["time"],
                                "exitTime": c["time"] + EXIT_CANDLES * 15 * 60 * 1000,
                                "gapPct": round(gap, 2), "distPct": round(dist, 2)
                            }
                            send_telegram(
                                f"📈 <b>BULL FVG — {SYMBOL}</b>\n\n"
                                f"Entry: <b>${c['close']:,.0f}</b>\n"
                                f"Gap: {round(gap,2)}%  |  EMA dist: {round(dist,2)}%\n"
                                f"TP: ${c['close']*1.0062:,.0f}  (+0.62%)\n"
                                f"SL: ${c['close']*0.9943:,.0f}  (-0.57%)\n"
                                f"Exit: 2h from now ⏱"
                            )
                        seen_fvgs.add(fid)
                        break

        # Bearish FVG
        if c1["low"] > c3["high"]:
            gap = (c1["low"] - c3["high"]) / c3["high"] * 100
            fid = f"bear_{c1['time']}"
            if MIN_GAP_PCT <= gap <= MAX_GAP_PCT and fid not in seen_fvgs and fid not in open_trades:
                end = min(i + MAX_WAIT + 1, len(candles))
                for j in range(i+1, end):
                    c = candles[j]
                    ev = ema[j]
                    if not ev: continue
                    if c["high"] >= c3["high"] and c["high"] <= c1["low"]:
                        dist = abs((c["close"] - ev) / ev * 100)
                        if c["close"] < ev and dist <= MAX_EMA_DIST:
                            open_trades[fid] = {
                                "type": "bear", "entryPrice": c["close"],
                                "entryTime": c["time"],
                                "exitTime": c["time"] + EXIT_CANDLES * 15 * 60 * 1000,
                                "gapPct": round(gap, 2), "distPct": round(dist, 2)
                            }
                            send_telegram(
                                f"📉 <b>BEAR FVG — {SYMBOL}</b>\n\n"
                                f"Entry: <b>${c['close']:,.0f}</b>\n"
                                f"Gap: {round(gap,2)}%  |  EMA dist: {round(dist,2)}%\n"
                                f"TP: ${c['close']*0.9938:,.0f}  (+0.62%)\n"
                                f"SL: ${c['close']*1.0057:,.0f}  (-0.57%)\n"
                                f"Exit: 2h from now ⏱"
                            )
                        seen_fvgs.add(fid)
                        break

    # ── 2. Check exits ────────────────────────────────────────────
    closed_now = []
    for fid, trade in open_trades.items():
        if now_ts >= trade["exitTime"]:
            exit_c = next((c for c in reversed(candles) if c["time"] <= trade["exitTime"]), None)
            if not exit_c: continue
            ep  = exit_c["close"]
            raw = (ep - trade["entryPrice"]) / trade["entryPrice"] * 100 if trade["type"] == "bull" \
                  else (trade["entryPrice"] - ep) / trade["entryPrice"] * 100
            pnl = round(raw, 3)
            win = pnl > 0
            closed_count += 1
            if win: win_count += 1
            wr = win_count / closed_count * 100
            send_telegram(
                f"{'✅' if win else '❌'} <b>TRADE CLOSED — {'WIN' if win else 'LOSS'}</b>\n\n"
                f"{'BULL' if trade['type']=='bull' else 'BEAR'} | "
                f"Entry: ${trade['entryPrice']:,.0f} → Exit: ${ep:,.0f}\n"
                f"P&L: <b>{'+' if pnl>0 else ''}{pnl}%</b>\n\n"
                f"📊 Running: {win_count}W / {closed_count-win_count}L | WR: {wr:.0f}%"
            )
            closed_now.append(fid)

    for fid in closed_now:
        open_trades.pop(fid, None)

    # ── 3. Save state ─────────────────────────────────────────────
    save_state({
        "open_trades": open_trades,
        "seen_fvgs": list(seen_fvgs)[-500:],
        "closed_count": closed_count,
        "win_count": win_count
    })
    print(f"Done. Open: {len(open_trades)} | Closed: {closed_count} | WR: {win_count}/{closed_count}")

if __name__ == "__main__":
    run()
                                     
