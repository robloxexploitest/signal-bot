"""
TradeLocker Signal Bot (single-check version for GitHub Actions).
Checks once, sends a Telegram alert if there is a fresh BUY/SELL signal, then exits.
Alerts only. It does NOT place trades.
"""
import os
import time

import pandas as pd
import requests
from tradelocker import TLAPI

TL_ENV = os.getenv("TL_ENV", "https://demo.tradelocker.com")  # live: https://live.tradelocker.com
TL_EMAIL = os.environ["TL_EMAIL"]
TL_PASSWORD = os.environ["TL_PASSWORD"]
TL_SERVER = os.environ["TL_SERVER"]
SYMBOL = os.getenv("SYMBOL", "EURUSD")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
LOOKBACK = "5D"
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
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": msg},
        timeout=10,
    )


def main():
    tl = TLAPI(environment=TL_ENV, username=TL_EMAIL, password=TL_PASSWORD, server=TL_SERVER)
    iid = tl.get_instrument_id_from_symbol_name(SYMBOL)
    df = tl.get_price_history(
        instrument_id=iid, resolution=TIMEFRAME, start_timestamp=0, end_timestamp=0, lookback_period=LOOKBACK
    )

    df = df.iloc[:-1].copy()  # drop the still-forming candle
    df["fast"] = df["c"].ewm(span=FAST, adjust=False).mean()
    df["slow"] = df["c"].ewm(span=SLOW, adjust=False).mean()
    df["rsi"] = rsi(df["c"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)
    prev, last = df.iloc[-2], df.iloc[-1]

    # Only alert if the signal candle closed recently (avoids repeat alerts)
    t = float(last["t"])
    t = t / 1000 if t > 1e12 else t
    closed_at = t + TF_SECONDS.get(TIMEFRAME, 900)
    if time.time() - closed_at > 1.5 * TF_SECONDS.get(TIMEFRAME, 900):
        print("No fresh candle to check. Done.")
        return

    price, a, r = last["c"], last["atr"], last["rsi"]
    up = prev["fast"] <= prev["slow"] and last["fast"] > last["slow"] and r < 70
    down = prev["fast"] >= prev["slow"] and last["fast"] < last["slow"] and r > 30

    if up:
        send(f"BUY {SYMBOL} ({TIMEFRAME})\nPrice: {price:.5f}\nSL: {price - SL_ATR * a:.5f}\nTP: {price + TP_ATR * a:.5f}\nRSI: {r:.1f}")
    elif down:
        send(f"SELL {SYMBOL} ({TIMEFRAME})\nPrice: {price:.5f}\nSL: {price + SL_ATR * a:.5f}\nTP: {price - TP_ATR * a:.5f}\nRSI: {r:.1f}")
    else:
        print(f"No signal. Price {price:.5f}, RSI {r:.1f}")


if __name__ == "__main__":
    main()
