import requests
import time
import json
import os
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# FVG filter settings
MIN_GAP_PCT = float(os.environ.get("MIN_GAP", "0.5"))
MAX_GAP_PCT = float(os.environ.get("MAX_GAP", "1.0"))
EMA_PERIOD  = int(os.environ.get("EMA_PERIOD", "50"))
MAX_EMA_DIST= float(os.environ.get("MAX_EMA_DIST", "3.0"))
MAX_WAIT    = int(os.environ.get("MAX_WAIT", "20"))
EXIT_CANDLES= int(os.environ.get("EXIT_CANDLES", "8"))
SYMBOL      = os.environ.get("SYMBOL", "BTCUSDT")
INTERVAL    = "15m"
SCAN_EVERY  = 15 * 60  # seconds

# ── State (in-memory, persists while running) ─────────────────────
active_fvgs   = {}   # fvgId → fvg zone dict
open_trades   = {}   # fvgId → trade dict
closed_trades = []

# ── Telegram ──────────────────────────────────────────────────────
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"})
        if not r.ok:
            print(f"Telegram error: {r.text}")
    except Exception as e:
        print(f"Telegram send failed: {e}")

# ── Binance candles ───────────────────────────────────────────────
def fetch_candles(limit=300):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval={INTERVAL}&limit={limit}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    raw = r.json()
    return [{"time": c[0], "open": float(c[1]), "high": float(c[2]),
              "low": float(c[3]), "close": float(c[4])} for c in raw]

# ── EMA ───────────────────────────────────────────────────────────
def calc_ema(candles, period):
    k = 2 / (period + 1)
    result = [None] * len(candles)
    prev = None
    for i, c in enumerate(candles):
        if i < period - 1:
            continue
        if prev is None:
            prev = sum(x["close"] for x in candles[:period]) / period
            result[i] = prev
        else:
            prev = c["close"] * k + prev * (1 - k)
            result[i] = prev
    return result

# ── Main scan ─────────────────────────────────────────────────────
def scan():
    global active_fvgs, open_trades, closed_trades

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanning {SYMBOL}...")
    candles = fetch_candles(300)
    ema = calc_ema(candles, EMA_PERIOD)
    current_price = candles[-1]["close"]
    current_time  = candles[-1]["time"]

    # ── 1. Detect new FVG zones ───────────────────────────────────
    for i in range(2, len(candles) - 1):
        c1, c3 = candles[i - 2], candles[i]

        # Bullish FVG
        if c3["low"] > c1["high"]:
            gap = (c3["low"] - c1["high"]) / c1["high"] * 100
            fid = f"bull_{c1['time']}"
            if MIN_GAP_PCT <= gap <= MAX_GAP_PCT and fid not in active_fvgs and fid not in open_trades and fid not in [t["fvgId"] for t in closed_trades]:
                active_fvgs[fid] = {
                    "type": "bull", "formed": i, "formedTime": c3["time"],
                    "gapTop": c3["low"], "gapBot": c1["high"], "gapPct": round(gap, 2)
                }

        # Bearish FVG
        if c1["low"] > c3["high"]:
            gap = (c1["low"] - c3["high"]) / c3["high"] * 100
            fid = f"bear_{c1['time']}"
            if MIN_GAP_PCT <= gap <= MAX_GAP_PCT and fid not in active_fvgs and fid not in open_trades and fid not in [t["fvgId"] for t in closed_trades]:
                active_fvgs[fid] = {
                    "type": "bear", "formed": i, "formedTime": c3["time"],
                    "gapTop": c1["low"], "gapBot": c3["high"], "gapPct": round(gap, 2)
                }

    # ── 2. Check pending FVGs for fills ───────────────────────────
    expired = []
    for fid, fvg in active_fvgs.items():
        age = len(candles) - 1 - fvg["formed"]
        if age > MAX_WAIT:
            expired.append(fid)
            continue

        for j in range(fvg["formed"] + 1, len(candles)):
            c = candles[j]
            ema_v = ema[j]
            if not ema_v:
                continue
            dist = abs((c["close"] - ema_v) / ema_v * 100)
            filled = False

            if fvg["type"] == "bull" and c["low"] <= fvg["gapTop"] and c["low"] >= fvg["gapBot"]:
                filled = True
                if c["close"] > ema_v and dist <= MAX_EMA_DIST:
                    trade = {
                        "fvgId": fid, "type": "bull",
                        "entryIdx": j, "entryTime": c["time"],
                        "entryPrice": c["close"],
                        "gapPct": fvg["gapPct"], "distPct": round(dist, 2),
                        "exitIdx": j + EXIT_CANDLES,
                        "date": datetime.fromtimestamp(c["time"]/1000).strftime("%d/%m %H:%M")
                    }
                    open_trades[fid] = trade
                    expired.append(fid)
                    direction_emoji = "📈"
                    msg = (
                        f"{direction_emoji} <b>BULL FVG SIGNAL — {SYMBOL}</b>\n\n"
                        f"Entry: <b>${c['close']:,.0f}</b>\n"
                        f"Gap size: {fvg['gapPct']}%\n"
                        f"EMA dist: {round(dist,2)}%\n"
                        f"TP target: +0.62% → ${c['close']*1.0062:,.0f}\n"
                        f"SL target: -0.57% → ${c['close']*0.9943:,.0f}\n"
                        f"Exit: 2 hours from now\n"
                        f"Time: {trade['date']}"
                    )
                    send_telegram(msg)
                    print(f"  ✓ BULL signal at ${c['close']:,.0f}")
                else:
                    expired.append(fid)

            elif fvg["type"] == "bear" and c["high"] >= fvg["gapBot"] and c["high"] <= fvg["gapTop"]:
                filled = True
                if c["close"] < ema_v and dist <= MAX_EMA_DIST:
                    trade = {
                        "fvgId": fid, "type": "bear",
                        "entryIdx": j, "entryTime": c["time"],
                        "entryPrice": c["close"],
                        "gapPct": fvg["gapPct"], "distPct": round(dist, 2),
                        "exitIdx": j + EXIT_CANDLES,
                        "date": datetime.fromtimestamp(c["time"]/1000).strftime("%d/%m %H:%M")
                    }
                    open_trades[fid] = trade
                    expired.append(fid)
                    direction_emoji = "📉"
                    msg = (
                        f"{direction_emoji} <b>BEAR FVG SIGNAL — {SYMBOL}</b>\n\n"
                        f"Entry: <b>${c['close']:,.0f}</b>\n"
                        f"Gap size: {fvg['gapPct']}%\n"
                        f"EMA dist: {round(dist,2)}%\n"
                        f"TP target: +0.62% → ${c['close']*0.9938:,.0f}\n"
                        f"SL target: -0.57% → ${c['close']*1.0057:,.0f}\n"
                        f"Exit: 2 hours from now\n"
                        f"Time: {trade['date']}"
                    )
                    send_telegram(msg)
                    print(f"  ✓ BEAR signal at ${c['close']:,.0f}")
                else:
                    expired.append(fid)

            if filled:
                break

    for fid in expired:
        active_fvgs.pop(fid, None)

    # ── 3. Check open trades for exit ─────────────────────────────
    closed_now = []
    for fid, trade in open_trades.items():
        if trade["exitIdx"] < len(candles):
            exit_c = candles[trade["exitIdx"]]
            exit_price = exit_c["close"]
            raw_pnl = (
                (exit_price - trade["entryPrice"]) / trade["entryPrice"] * 100
                if trade["type"] == "bull"
                else (trade["entryPrice"] - exit_price) / trade["entryPrice"] * 100
            )
            pnl = round(raw_pnl, 3)
            outcome = "WIN ✅" if pnl > 0 else "LOSS ❌"
            emoji = "✅" if pnl > 0 else "❌"
            msg = (
                f"{emoji} <b>TRADE CLOSED — {outcome}</b>\n\n"
                f"Direction: {'BULL' if trade['type']=='bull' else 'BEAR'}\n"
                f"Entry: ${trade['entryPrice']:,.0f}\n"
                f"Exit: ${exit_price:,.0f}\n"
                f"P&L: <b>{'+' if pnl>0 else ''}{pnl}%</b>\n"
                f"Gap: {trade['gapPct']}%"
            )
            send_telegram(msg)
            trade["exitPrice"] = exit_price
            trade["pnlPct"] = pnl
            trade["outcome"] = "win" if pnl > 0 else "loss"
            closed_trades.append(trade)
            closed_now.append(fid)
            print(f"  {'WIN' if pnl>0 else 'LOSS'}: {pnl}% at ${exit_price:,.0f}")

    for fid in closed_now:
        open_trades.pop(fid, None)

    # ── 4. Stats summary every 10 trades ─────────────────────────
    if len(closed_trades) > 0 and len(closed_trades) % 10 == 0:
        wins = [t for t in closed_trades if t["outcome"] == "win"]
        wr = len(wins) / len(closed_trades) * 100
        avg_win = sum(t["pnlPct"] for t in wins) / len(wins) if wins else 0
        losses_list = [t for t in closed_trades if t["outcome"] == "loss"]
        avg_loss = abs(sum(t["pnlPct"] for t in losses_list) / len(losses_list)) if losses_list else 0
        msg = (
            f"📊 <b>Stats Update — {len(closed_trades)} trades</b>\n\n"
            f"Win rate: <b>{wr:.0f}%</b> (target 67%)\n"
            f"Avg win: +{avg_win:.2f}%\n"
            f"Avg loss: -{avg_loss:.2f}%\n"
            f"Expectancy: {(wr/100*avg_win - (1-wr/100)*avg_loss):+.3f}% per trade\n"
            f"Status: {'✅ On track' if wr >= 60 else '⚠️ Below target'}"
        )
        send_telegram(msg)

    print(f"  Active FVGs: {len(active_fvgs)} | Open trades: {len(open_trades)} | Closed: {len(closed_trades)}")

# ── Entry point ───────────────────────────────────────────────────
if __name__ == "__main__":
    send_telegram(
        f"🤖 <b>FVG Bot Started</b>\n\n"
        f"Symbol: {SYMBOL}\n"
        f"Filters: Gap {MIN_GAP_PCT}–{MAX_GAP_PCT}% | EMA {EMA_PERIOD} | Max dist {MAX_EMA_DIST}%\n"
        f"Exit: {EXIT_CANDLES} candles ({EXIT_CANDLES*15//60}h)\n"
        f"Scanning every 15 minutes..."
    )
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Scan error: {e}")
        time.sleep(SCAN_EVERY)
