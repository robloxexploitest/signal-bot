import html
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests
from tradelocker import TLAPI

TL_ENV = os.getenv("TL_ENV", "https://live.tradelocker.com")
EMAIL = os.environ["TL_EMAIL"]
PASSWORD = os.environ["TL_PASSWORD"]
SERVER = os.environ["TL_SERVER"]
SYMBOL = os.getenv("SYMBOL", "NAS100")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

FAST, SLOW, TREND, RSI_LEN, ATR_LEN = 9, 21, 200, 14, 14
SL_ATR, TP_ATR = 1.5, 3.0
MAX_CANDLES = 96  # a signal expires after this many candles
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400}
TF = TF_SECONDS.get(TIMEFRAME, 900)
STATE_FILE = "signals.json"
LINE = "━━━━━━━━━━━━━━"


def rsi(close, n):
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def atr(df, n):
    pc = df["c"].shift()
    tr = pd.concat([df["h"] - df["l"], (df["h"] - pc).abs(), (df["l"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def send(msg):
    print(msg, flush=True)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"Telegram error: {e}")


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("last_candle", 0)
    s.setdefault("open", [])
    s.setdefault("closed", [])
    s.setdefault("last_status", "")
    return s


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def stats(s):
    wins = sum(1 for c in s["closed"] if c["result"] == "win")
    losses = sum(1 for c in s["closed"] if c["result"] == "loss")
    total_r = sum(c["r"] for c in s["closed"])
    rate = (wins / (wins + losses) * 100) if (wins + losses) else None
    return wins, losses, total_r, rate


def stats_line(s):
    w, l, r, rate = stats(s)
    rate_txt = f"{rate:.0f}%" if rate is not None else "n/a"
    return f"📈 Record  <code>{w}W / {l}L · {rate_txt} · {r:+.1f}R</code>"


def check_outcomes(s, df):
    still_open = []
    for sig in s["open"]:
        after = df[df["t"] > sig["t"]]
        result, exit_price = None, None
        for _, c in after.iterrows():
            if sig["side"] == "buy":
                hit_sl, hit_tp = c["l"] <= sig["sl"], c["h"] >= sig["tp"]
            else:
                hit_sl, hit_tp = c["h"] >= sig["sl"], c["l"] <= sig["tp"]
            if hit_sl:  # if both hit in one candle, count the loss (conservative)
                result, exit_price = "loss", sig["sl"]
                break
            if hit_tp:
                result, exit_price = "win", sig["tp"]
                break
        if not result and len(after) >= MAX_CANDLES:
            result, exit_price = "expired", float(after.iloc[-1]["c"])
        if not result:
            still_open.append(sig)
            continue

        risk = abs(sig["entry"] - sig["sl"])
        move = (exit_price - sig["entry"]) if sig["side"] == "buy" else (sig["entry"] - exit_price)
        r = round(move / risk, 2) if risk else 0.0
        s["closed"].append({"t": sig["t"], "side": sig["side"], "result": result, "r": r})
        icon = "🟢" if sig["side"] == "buy" else "🔴"
        head = {"win": "✅ <b>SIGNAL HIT TARGET</b>", "loss": "❌ <b>SIGNAL HIT STOP</b>", "expired": "⏳ <b>SIGNAL EXPIRED</b>"}[result]
        send("\n".join([
            head, "",
            f"{icon} <b>{sig['side'].upper()}</b> · {SYMBOL} · {TIMEFRAME}",
            LINE,
            f"📍 Entry   <code>{sig['entry']:,.2f}</code>",
            f"🏁 Exit    <code>{exit_price:,.2f}</code>",
            f"⚖️ Result  <code>{r:+.2f}R</code>",
            stats_line(s),
        ]))
    s["open"] = still_open


def main():
    s = load_state()
    try:
        run(s)
    finally:
        save_state(s)


def run(s):
    tl = TLAPI(environment=TL_ENV, username=EMAIL, password=PASSWORD, server=SERVER)
    iid = tl.get_instrument_id_from_symbol_name(SYMBOL)
    df = tl.get_price_history(
        instrument_id=iid, resolution=TIMEFRAME, start_timestamp=0, end_timestamp=0, lookback_period="10D"
    )
    df = df.iloc[:-1].copy()  # closed candles only
    df["t"] = df["t"].astype(float).apply(lambda x: x / 1000 if x > 1e12 else x)

    if len(df) < TREND + 30:
        print(f"Not enough candles yet ({len(df)}).")
        return

    df["fast"] = df["c"].ewm(span=FAST, adjust=False).mean()
    df["slow"] = df["c"].ewm(span=SLOW, adjust=False).mean()
    df["trend"] = df["c"].ewm(span=TREND, adjust=False).mean()
    df["rsi"] = rsi(df["c"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)
    df["atr_avg"] = df["atr"].rolling(50).mean()
    prev, last = df.iloc[-2], df.iloc[-1]

    # 1) Resolve earlier signals
    check_outcomes(s, df)

    # 2) Look for a new signal on the latest closed candle
    price, a, r = float(last["c"]), float(last["atr"]), float(last["rsi"])
    is_new = float(last["t"]) > s["last_candle"]
    is_fresh = (time.time() - (float(last["t"]) + TF)) <= 2 * TF
    vol_ok = last["atr"] >= 0.7 * last["atr_avg"]

    cross_up = prev["fast"] <= prev["slow"] and last["fast"] > last["slow"]
    cross_dn = prev["fast"] >= prev["slow"] and last["fast"] < last["slow"]
    up = cross_up and price > last["trend"] and r < 70
    down = cross_dn and price < last["trend"] and r > 30
    side = "buy" if up else "sell" if down else None

    if is_new:
        if side and is_fresh and vol_ok:
            sl = price - SL_ATR * a if side == "buy" else price + SL_ATR * a
            tp = price + TP_ATR * a if side == "buy" else price - TP_ATR * a
            s["open"].append({"t": float(last["t"]), "side": side, "entry": price, "sl": sl, "tp": tp})
            icon = "🟢" if side == "buy" else "🔴"
            trend_txt = "above 200 EMA" if side == "buy" else "below 200 EMA"
            send("\n".join([
                f"🔔 <b>{side.upper()} SIGNAL</b>", "",
                f"{icon} <b>{SYMBOL}</b> · {TIMEFRAME}",
                LINE,
                f"💵 Entry   <code>≈ {price:,.2f}</code>",
                f"🛑 Stop    <code>{sl:,.2f}</code>",
                f"🎯 Target  <code>{tp:,.2f}</code>",
                f"📊 RSI     <code>{r:.1f}</code>",
                f"🧭 Trend   <code>{trend_txt}</code>",
                f"⚖️ Risk:Reward  <code>1 : {TP_ATR / SL_ATR:.0f}</code>",
                "",
                "<i>Alert only, no trade placed.</i>",
            ]))
        elif cross_up or cross_dn:
            print("Crossover skipped by filters (trend, RSI, volatility or too old).")
        s["last_candle"] = float(last["t"])
    else:
        print("No new candle yet.")

    # 3) Status message every 2 hours
    now = datetime.now(timezone.utc)
    key = f"{now:%Y-%m-%d}-{now.hour}"
    if now.hour % 2 == 0 and s["last_status"] != key:
        s["last_status"] = key
        market = "open" if (time.time() - (float(last["t"]) + TF)) <= 3 * TF else "closed or no new candles"
        send("\n".join([
            "🤖 <b>Bot status: running</b>", "",
            f"{SYMBOL} · {TIMEFRAME}",
            LINE,
            f"💵 Price   <code>{price:,.2f}</code>",
            f"📊 RSI     <code>{r:.1f}</code>",
            f"🕒 Market  <code>{market}</code>",
            f"📂 Open signals  <code>{len(s['open'])}</code>",
            stats_line(s),
        ]))


if __name__ == "__main__":
    main()
