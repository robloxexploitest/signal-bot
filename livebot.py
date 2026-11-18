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
        f"📦 Size     <code>{LOTS} lots</code>",
        f"📊 RSI      <code>{r:.1f}</code>",
        f"⚖️ Risk:Reward  <code>1 : {TP_ATR / SL_ATR:.0f}</code>",
    ]))


def send_closed(trade, pl, balance, reason=None):
    if pl is None:
        head, plt = "🔔 <b>TRADE CLOSED</b>", "unavailable, check TradeLocker"
    elif pl >= 0:
        head, plt = "✅ <b>TRADE CLOSED · WIN</b>", money(pl)
    else:
        head, plt = "❌ <b>TRADE CLOSED · LOSS</b>", money(pl)
    icon = "🟢" if trade["side"] == "buy" else "🔴"
    lines = [
        head,
        "",
        f"{icon} <b>{trade['side'].upper()}</b> · {SYMBOL}",
        LINE,
        f"💰 P/L      <code>{plt}</code>",
        f"⏱ Held     <code>{held(time.time() - trade['opened_at'])}</code>",
    ]
    if reason:
        lines.append(f"🔁 Reason   <code>{reason}</code>")
    if balance is not None:
        lines.append(f"🏦 Balance  <code>${balance:,.2f}</code>")
    send("\n".join(lines))


# ---------- main logic ----------
def run(state):
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

    # Read open positions (if we can't, do nothing)
    try:
        mine = my_positions(tl, iid)
    except Exception as e:
        send(f"⚠️ <b>Could not read positions</b>\n<code>{esc(e)}</code>\nNo trades this run.")
        return

    # Did a trade close by itself (stop or target)?
    trade = state.get("trade")
    if trade and not mine:
        time.sleep(2)
        pl, bal = realized_pl(tl, trade)
        send_closed(trade, pl, bal)
        state["trade"] = None
        trade = None
    elif trade and mine:
        try:
            v = mine[0].get("unrealizedPl")
            if v is not None:
                trade["last_pl"] = float(v)
        except Exception:
            pass

    if not side:
        print(f"No signal. Price {price}, RSI {r:.1f}")
        return

    # Existing position
    if mine:
        existing = mine[0]
        if str(existing["side"]).lower() == side:
            print("Already in a trade in this direction. Skipping.")
            return
        try:
            tl.close_position(position_id=int(existing["id"]))
        except Exception as e:
            send(f"⚠️ <b>Could not close position</b>\n<code>{esc(e)}</code>\nNo new trade.")
            return
        time.sleep(3)
        if trade:
            pl, bal = realized_pl(tl, trade)
            send_closed(trade, pl, bal, reason="Opposite signal")
            state["trade"] = None
        else:
            send("🔁 Closed an open position (opposite signal).")

    # Open the new trade
    dp = 2 if price > 100 else 5
    sl = price - SL_ATR * a if side == "buy" else price + SL_ATR * a
    tp = price + TP_ATR * a if side == "buy" else price - TP_ATR * a

    balance_before = get_balance(tl)
    send_incoming(side, price, sl, tp, r, dp)

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
        send(f"⚠️ <b>Order failed</b> · {side.upper()} {SYMBOL}\n<code>{esc(e)}</code>")
        return

    if not order_id:
        send(f"⚠️ <b>Order not accepted</b> · {side.upper()} {SYMBOL}")
        return

    time.sleep(3)
    pid, entry = None, price
    try:
        new = my_positions(tl, iid)
        if new:
            pid = str(new[0]["id"])
            entry = float(new[0].get("avgPrice", price))
    except Exception:
        pass

    state["trade"] = {
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "lots": LOTS,
        "opened_at": time.time(),
        "balance_before": balance_before,
        "position_id": pid,
        "last_pl": None,
    }


def main():
    state = load_state()
    try:
        run(state)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
