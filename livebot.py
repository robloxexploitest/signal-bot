import html
import json
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
SYMBOL = os.getenv("SYMBOL", "NAS100")
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
LOTS = min(float(os.getenv("LOTS", "0.01")), 0.01)  # hard cap
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

FAST, SLOW, RSI_LEN, ATR_LEN = 9, 21, 14, 14
SL_ATR, TP_ATR = 1.5, 3.0
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400}
STATE_FILE = "state.json"
LINE = "━━━━━━━━━━━━━━"


# ---------- helpers ----------
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


def esc(x):
    return html.escape(str(x))


def money(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def held(secs):
    m = int(secs // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h {m % 60}m"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"trade": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def my_positions(tl, iid):
    pos = tl.get_all_positions()
    if pos is None or len(pos) == 0:
        return []
    return pos[pos["tradableInstrumentId"] == iid].to_dict("records")


def get_balance(tl):
    try:
        s = tl.get_account_state()
        if isinstance(s, dict) and "balance" in s:
            return float(s["balance"])
    except Exception as e:
        print(f"Balance error: {e}")
    return None


def realized_pl(tl, trade):
    after = get_balance(tl)
    before = trade.get("balance_before")
    if after is not None and before is not None:
        return after - before, after
    return trade.get("last_pl"), after


# ---------- messages ----------
def send_incoming(side, price, sl, tp, r, dp):
    icon = "🟢" if side == "buy" else "🔴"
    send("\n".join([
        "⚡ <b>TRADE INCOMING</b>",
        "",
        f"{icon} <b>{side.upper()}</b> · {SYMBOL} · {TIMEFRAME}",
        LINE,
        f"💵 Entry    <code>≈ {price:,.{dp}f}</code>",
        f"🛑 Stop     <code>{sl:,.{dp}f}</code>",
        f"🎯 Target   <code>{tp:,.{dp}f}</code>",
        f"📦 Size
