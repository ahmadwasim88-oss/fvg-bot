import requests
import json
import os
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOL           = "BTCUSDT"

# Strategy params (match backtest)
BOS_LOOKBACK     = int(os.environ.get("BOS_LOOKBACK", "10"))
MIN_GAP          = float(os.environ.get("MIN_GAP", "0.5"))
MAX_GAP          = float(os.environ.get("MAX_GAP", "1.5"))
EMA_PERIOD       = int(os.environ.get("EMA_PERIOD", "50"))
MAX_EMA_DIST     = float(os.environ.get("MAX_EMA_DIST", "5.0"))
MAX_WAIT         = int(os.environ.get("MAX_WAIT", "50"))
SL_BUFFER        = float(os.environ.get("SL_BUFFER", "0.2"))
SWING_LOOKBACK   = int(os.environ.get("SWING_LOOKBACK", "30"))
MIN_RR           = float(os.environ.get("MIN_RR", "1.0"))
RISK_PCT         = float(os.environ.get("RISK_PCT", "10.0"))
ACCOUNT_SIZE     = float(os.environ.get("ACCOUNT_SIZE", "1000"))

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

# ── Fetch candles (Kraken → OKX → Gate fallback) ─────────────────
def fetch_candles(limit=500):
    apis = [
        ("Kraken", fetch_kraken),
        ("OKX",    fetch_okx),
        ("Gate",   fetch_gate),
    ]
    for name, fn in apis:
        try:
            print(f"  Trying {name}...")
            candles = fn(limit)
            if candles and len(candles) > 100:
                print(f"  Got {len(candles)} candles from {name}. Price: ${candles[-1]['c']:,.1f}")
                return candles
        except Exception as e:
            print(f"  {name} failed: {e}")
    raise Exception("All APIs failed")

def fetch_kraken(limit):
    url = "https://api.kraken.com/0/public/OHLC?pair=XBTUSDT&interval=15"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise Exception(str(data["error"]))
    key = list(data["result"].keys())[0]
    raw = data["result"][key]
    candles = [{"t": int(c[0])*1000, "o": float(c[1]), "h": float(c[2]),
                "l": float(c[3]), "c": float(c[4])} for c in raw]
    return candles[-limit:]

def fetch_okx(limit):
    url = f"https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=15m&limit={min(limit,300)}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise Exception(data.get("msg"))
    raw = data["data"]
    raw.reverse()
    return [{"t": int(c[0]), "o": float(c[1]), "h": float(c[2]),
             "l": float(c[3]), "c": float(c[4])} for c in raw]

def fetch_gate(limit):
    url = f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=BTC_USDT&interval=15m&limit={min(limit,2000)}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    raw = r.json()
    return [{"t": int(c["t"])*1000, "o": float(c["o"]), "h": float(c["h"]),
             "l": float(c["l"]), "c": float(c["c"])} for c in raw]

# ── Indicators ────────────────────────────────────────────────────
def calc_ema(closes, period):
    k = 2 / (period + 1)
    ema = [None] * len(closes)
    for i in range(len(closes)):
        if i < period - 1:
            continue
        if ema[i-1] is None:
            ema[i] = sum(closes[i-period+1:i+1]) / period
        else:
            ema[i] = closes[i] * k + ema[i-1] * (1 - k)
    return ema

def ema_slope(ema, idx):
    if idx < 3 or ema[idx] is None or ema[idx-3] is None:
        return 0
    return ema[idx] - ema[idx-3]

def has_impulse(c):
    body = abs(c["c"] - c["o"])
    rng = c["h"] - c["l"]
    return rng > 0 and body / rng >= 0.6

def swing_high(candles, idx, lookback):
    high = 0
    for i in range(max(0, idx - lookback), idx):
        if candles[i]["h"] > high:
            high = candles[i]["h"]
    return high

def swing_low(candles, idx, lookback):
    low = float("inf")
    for i in range(max(0, idx - lookback), idx):
        if candles[i]["l"] < low:
            low = candles[i]["l"]
    return low

# ── BOS + FVG Signal Detection ────────────────────────────────────
def find_signal(candles):
    """
    Scans the most recent candles for a fresh BOS+FVG setup.
    Returns signal dict or None.
    Logic matches the HTML backtest exactly.
    """
    closes = [c["c"] for c in candles]
    ema = calc_ema(closes, EMA_PERIOD)
    lb = BOS_LOOKBACK
    n = len(candles)

    # Only look at the last 10 candles for fresh signals
    scan_start = max(lb + 2, n - 10)

    for i in range(scan_start, n - 1):
        prev = candles[i - lb:i]
        prev_high = max(c["h"] for c in prev)
        prev_low  = min(c["l"] for c in prev)
        c = candles[i]

        # ── Bullish BOS ──────────────────────────────────────────
        if c["c"] > prev_high:
            for fi in range(max(lb + 2, i - 10), i + 1):
                if fi < 2: continue
                c1, c2, c3 = candles[fi-2], candles[fi-1], candles[fi]
                if c3["l"] > c1["h"] and has_impulse(c2):
                    gap = (c3["l"] - c1["h"]) / c1["h"] * 100
                    if not (MIN_GAP <= gap <= MAX_GAP):
                        continue
                    fvg_top = c3["l"]
                    fvg_bot = c1["h"]
                    # Check if current price is near the FVG (about to fill)
                    cur = candles[-1]["c"]
                    dist_to_fvg = (cur - fvg_top) / fvg_top * 100
                    if -2.0 <= dist_to_fvg <= 5.0:  # price within 5% above or touching
                        ev = ema[i]
                        if ev is None: break
                        slope = ema_slope(ema, i)
                        dist_from_ema = abs((cur - ev) / ev * 100)
                        if cur > ev and slope > 0 and dist_from_ema <= MAX_EMA_DIST:
                            # Calculate SL and TP
                            entry = cur
                            sl = fvg_bot * (1 - SL_BUFFER / 100)
                            tp = swing_high(candles, i, SWING_LOOKBACK)
                            if not tp or tp <= entry:
                                break
                            sl_pct = abs((entry - sl) / entry * 100)
                            tp_pct = (tp - entry) / entry * 100
                            rr = tp_pct / sl_pct if sl_pct > 0 else 0
                            if rr < MIN_RR:
                                break
                            return {
                                "type": "bull",
                                "entry": entry,
                                "sl": sl,
                                "tp": tp,
                                "sl_pct": round(sl_pct, 2),
                                "tp_pct": round(tp_pct, 2),
                                "rr": round(rr, 2),
                                "gap": round(gap, 2),
                                "fvg_top": fvg_top,
                                "fvg_bot": fvg_bot,
                                "bos_level": prev_high,
                                "time": candles[-1]["t"],
                            }
                    break

        # ── Bearish BOS ──────────────────────────────────────────
        if c["c"] < prev_low:
            for fi in range(max(lb + 2, i - 10), i + 1):
                if fi < 2: continue
                c1, c2, c3 = candles[fi-2], candles[fi-1], candles[fi]
                if c1["l"] > c3["h"] and has_impulse(c2):
                    gap = (c1["l"] - c3["h"]) / c3["h"] * 100
                    if not (MIN_GAP <= gap <= MAX_GAP):
                        continue
                    fvg_top = c1["l"]
                    fvg_bot = c3["h"]
                    cur = candles[-1]["c"]
                    dist_to_fvg = (fvg_bot - cur) / cur * 100
                    if -2.0 <= dist_to_fvg <= 5.0:
                        ev = ema[i]
                        if ev is None: break
                        slope = ema_slope(ema, i)
                        dist_from_ema = abs((cur - ev) / ev * 100)
                        if cur < ev and slope < 0 and dist_from_ema <= MAX_EMA_DIST:
                            entry = cur
                            sl = fvg_top * (1 + SL_BUFFER / 100)
                            tp = swing_low(candles, i, SWING_LOOKBACK)
                            if not tp or tp >= entry:
                                break
                            sl_pct = abs((sl - entry) / entry * 100)
                            tp_pct = (entry - tp) / entry * 100
                            rr = tp_pct / sl_pct if sl_pct > 0 else 0
                            if rr < MIN_RR:
                                break
                            return {
                                "type": "bear",
                                "entry": entry,
                                "sl": sl,
                                "tp": tp,
                                "sl_pct": round(sl_pct, 2),
                                "tp_pct": round(tp_pct, 2),
                                "rr": round(rr, 2),
                                "gap": round(gap, 2),
                                "fvg_top": fvg_top,
                                "fvg_bot": fvg_bot,
                                "bos_level": prev_low,
                                "time": candles[-1]["t"],
                            }
                    break
    return None

# ── Position sizing ───────────────────────────────────────────────
def calc_position(entry, sl, account):
    risk_amount = account * (RISK_PCT / 100)
    sl_distance = abs(entry - sl) / entry * 100
    if sl_distance <= 0:
        return 0, 0
    notional = risk_amount / (sl_distance / 100)
    leverage = round(notional / account, 1)
    return round(notional, 2), min(leverage, 20)  # cap at 20x

# ── State ─────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {
            "open_trade": None,
            "seen_signals": [],
            "trades": [],
            "wins": 0,
            "losses": 0,
            "run_count": 0,
        }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Check open trade ──────────────────────────────────────────────
def check_open_trade(candles, state):
    trade = state.get("open_trade")
    if not trade:
        return state

    cur = candles[-1]["c"]
    sl  = trade["sl"]
    tp  = trade["tp"]
    typ = trade["type"]

    hit_sl = (typ == "bull" and cur <= sl) or (typ == "bear" and cur >= sl)
    hit_tp = (typ == "bull" and cur >= tp) or (typ == "bear" and cur <= tp)

    if hit_sl or hit_tp:
        result = "tp" if hit_tp else "sl"
        win    = result == "tp"
        pnl    = trade["tp_pct"] if win else -trade["sl_pct"]

        if win:
            state["wins"] += 1
        else:
            state["losses"] += 1

        total = state["wins"] + state["losses"]
        wr    = state["wins"] / total * 100 if total else 0
        emoji = "✅" if win else "❌"
        dir_emoji = "📈" if typ == "bull" else "📉"

        msg = (
            f"{emoji} <b>TRADE CLOSED · {dir_emoji} {typ.upper()}</b>\n\n"
            f"<b>BTC/USDT · 15m · BOS+FVG</b>\n\n"
            f"Entry:  ${trade['entry']:,.1f}\n"
            f"Exit:   ${cur:,.1f}\n"
            f"Result: {'TP hit 🎯' if win else 'SL hit 🛑'}\n"
            f"P&L:    {'+' if pnl>0 else ''}{pnl:.2f}%\n\n"
            f"📊 Overall: {state['wins']}W / {state['losses']}L "
            f"({wr:.0f}% WR)"
        )
        send_telegram(msg)

        state["trades"].append({
            "type": typ,
            "entry": trade["entry"],
            "exit": cur,
            "result": result,
            "pnl": round(pnl, 3),
        })
        state["open_trade"] = None

    return state

# ── Main ──────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"BOS+FVG Bot · {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Strategy: BOS({BOS_LOOKBACK}c) + FVG({MIN_GAP}-{MAX_GAP}%) · SL gap-{SL_BUFFER}% · Min RR {MIN_RR}x")
    print(f"Account: ${ACCOUNT_SIZE:,.0f} · Risk: {RISK_PCT}%")
    print(f"{'='*50}")

    state = load_state()
    state["run_count"] = state.get("run_count", 0) + 1

    # Fetch candles
    print("\nFetching candles...")
    try:
        candles = fetch_candles(500)
    except Exception as e:
        print(f"Fetch failed: {e}")
        send_telegram(f"⚠️ BOS+FVG Bot: candle fetch failed — {e}")
        return

    cur_price = candles[-1]["c"]
    cur_time  = datetime.utcfromtimestamp(candles[-1]["t"] / 1000).strftime("%H:%M UTC")
    print(f"Current price: ${cur_price:,.1f} at {cur_time}")

    # Check open trade first
    if state.get("open_trade"):
        print(f"\nChecking open {state['open_trade']['type'].upper()} trade...")
        state = check_open_trade(candles, state)

    # Only look for new signal if no open trade
    if not state.get("open_trade"):
        print("\nScanning for BOS+FVG signal...")
        signal = find_signal(candles)

        if signal:
            sig_id = f"{signal['type']}_{signal['time']}_{round(signal['entry'])}"

            if sig_id not in state.get("seen_signals", []):
                state.setdefault("seen_signals", []).append(sig_id)
                # Keep only last 100 seen signals
                state["seen_signals"] = state["seen_signals"][-100:]

                notional, leverage = calc_position(signal["entry"], signal["sl"], ACCOUNT_SIZE)
                risk_dollar = ACCOUNT_SIZE * RISK_PCT / 100
                dir_emoji = "📈" if signal["type"] == "bull" else "📉"
                dir_label = "LONG" if signal["type"] == "bull" else "SHORT"

                msg = (
                    f"🚨 <b>BOS+FVG SIGNAL · {dir_emoji} {dir_label}</b>\n\n"
                    f"<b>BTC/USDT · 15m</b>\n\n"
                    f"BOS level: ${signal['bos_level']:,.1f}\n"
                    f"FVG zone:  ${signal['fvg_bot']:,.1f} – ${signal['fvg_top']:,.1f} ({signal['gap']}%)\n\n"
                    f"Entry:  ${signal['entry']:,.1f}\n"
                    f"SL:     ${signal['sl']:,.1f}  (-{signal['sl_pct']}%)\n"
                    f"TP:     ${signal['tp']:,.1f}  (+{signal['tp_pct']}%)\n"
                    f"R:R:    {signal['rr']}x\n\n"
                    f"💰 Sizing @ {RISK_PCT}% risk:\n"
                    f"Risk:      ${risk_dollar:,.0f}\n"
                    f"Notional:  ${notional:,.0f}\n"
                    f"Leverage:  ~{leverage}x\n\n"
                    f"⏰ {cur_time}"
                )
                send_telegram(msg)
                print(f"Signal sent: {dir_label} entry ${signal['entry']:,.1f} SL ${signal['sl']:,.1f} TP ${signal['tp']:,.1f} RR {signal['rr']}x")

                state["open_trade"] = signal
            else:
                print(f"Signal already seen: {sig_id}")
        else:
            print("No signal found this run.")
    else:
        print(f"Open trade active — skipping signal scan.")

    # Stats every 5 runs
    if state["run_count"] % 5 == 0:
        total = state["wins"] + state["losses"]
        if total > 0:
            wr = state["wins"] / total * 100
            print(f"\nStats: {state['wins']}W/{state['losses']}L ({wr:.0f}% WR)")

    save_state(state)
    print("\nDone.")

if __name__ == "__main__":
    main()
    
