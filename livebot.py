import os
import time

import pandas as pd
import requests
from tradelocker import TLAPI

if os.getenv("LIVE_CONFIRM") != "I_ACCEPT_THE_RISK":
    raise SystemExit("Live trading not confirmed. Exiting.")

TL_ENV = os.getenv("TL_ENV", "https://live.tradelocker.com")
EMAIL = os.environ["TL_EMAIL"]
PASSWORD = os.environ["TL_PASSWORD"]
SERVER = os.environ["TL_SERVER"]
SYMBOL = os.getenv("SYMBOL", "BTCUSD")
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
LOTS = min(float(os.getenv("LOTS", "0.01")), 0.01)  # hard cap
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

FAST, SLOW, RSI_LEN, ATR_LEN = 9, 21, 14, 14
SL_ATR, TP_ATR = 1.5, 3.0
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400}


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
            json={"chat_id": TG_CHAT_ID, "text": msg},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"Telegram error: {e}")


def main():
    tl = TLAPI(environment=TL_ENV, username=EMAIL, password=PASSWORD, server=SERVER)
    iid = tl.get_instrument_id_from_symbol_name(SYMBOL)
    df = tl.get_price_history(
        instrument_id=iid, resolution=TIMEFRAME, start_timestamp=0, end_timestamp=0, lookback_period="5D"
    )

    df = df.iloc[:-1].copy()
    df["fast"] = df["c"].ewm(span=FAST, adjust=False).mean()
    df["slow"] = df["c"].ewm(span=SLOW, adjust=False).mean()
    df["rsi"] = rsi(df["c"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)
    prev, last = df.iloc[-2], df.iloc[-1]

    tf = TF_SECONDS.get(TIMEFRAME, 300)
    t = float(last["t"])
    t = t / 1000 if t > 1e12 else t
    fresh = (time.time() - (t + tf)) <= tf

    price, a, r = float(last["c"]), float(last["atr"]), float(last["rsi"])
    up = fresh and prev["fast"] <= prev["slow"] and last["fast"] > last["slow"] and r < 70
    down = fresh and prev["fast"] >= prev["slow"] and last["fast"] < last["slow"] and r > 30
    side = "buy" if up else "sell" if down else None

    if not side:
        print(f"No signal. Price {price}, RSI {r:.1f}")
        return

    try:
        pos = tl.get_all_positions()
        mine = []
        if pos is not None and len(pos) > 0:
            mine = pos[pos["tradableInstrumentId"] == iid].to_dict("records")
    except Exception as e:
        send(f"LIVE: signal {side.upper()} {SYMBOL} but could not read positions, NO TRADE. Error: {e}")
        return

    if mine:
        existing = mine[0]
        if str(existing["side"]).lower() == side:
            print("Already in a trade in this direction. Skipping.")
            return
        try:
            tl.close_position(position_id=int(existing["id"]))
            send(f"LIVE: closed {existing['side']} {SYMBOL} (opposite signal)")
        except Exception as e:
            send(f"LIVE: failed to close position, NO new trade. Error: {e}")
            return

    dp = 2 if price > 100 else 5
    sl = price - SL_ATR * a if side == "buy" else price + SL_ATR * a
    tp = price + TP_ATR * a if side == "buy" else price - TP_ATR * a

    try:
        order_id = tl.create_order(
            iid,
            quantity=LOTS,
            side=side,
            type_="market",
            stop_loss=round(sl, dp),
            stop_loss_type="absolute",
            take_profit=round(tp, dp),
            take_profit_type="absolute",
        )
    except Exception as e:
        send(f"LIVE: order failed for {side.upper()} {SYMBOL}. Error: {e}")
        return

    if order_id:
        send(f"LIVE TRADE OPENED: {side.upper()} {SYMBOL} ({TIMEFRAME})\nLots: {LOTS}\nPrice: ~{price:.{dp}f}\nSL: {sl:.{dp}f}\nTP: {tp:.{dp}f}\nRSI: {r:.1f}")
    else:
        send(f"LIVE: order for {side.upper()} {SYMBOL} was not accepted.")


if __name__ == "__main__":
    main()
