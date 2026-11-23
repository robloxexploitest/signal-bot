import html
import json
import math
import os
import time
from datetime import datetime, timezone

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
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

RISK_PCT = float(os.getenv("RISK_PCT", "1")) / 100
DAILY_LOSS_PCT = float(os.getenv("DAILY_LOSS_PCT", "3")) / 100
GIVEBACK = float(os.getenv("GIVEBACK_PCT", "30")) / 100
MAX_STREAK = int(os.getenv("MAX_LOSS_STREAK", "3"))
POINT_VALUE = float(os.getenv("POINT_VALUE", "1.0"))  # money per price point per 1 lot
MIN_LOT, LOT_STEP = 0.01, 0.01
MAX_LOT = float(os.getenv("MAX_LOT", "0.05"))
START_MIN = int(os.getenv("START_UTC", "14:45").split(":")[0]) * 60 + int(os.getenv("START_UTC", "14:45").split(":")[1])
END_MIN = int(os.getenv("END_UTC", "20:30").split(":")[0]) * 60 + int(os.getenv("END_UTC", "20:30").split(":")[1])

FAST, SLOW, TREND, RSI_LEN, ATR_LEN = 9, 21, 200, 14, 14
SL_ATR = 1.5
TP_ATR = float(os.getenv("TP_ATR", "2.0"))
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400}
TF = TF_SECONDS.get(TIMEFRAME, 900)
STATE_FILE = "riskstate.json"
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
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("trade", None)
    s.setdefault("last_candle", 0)
    s.setdefault("streak", 0)
    s.setdefault("peak", 0.0)
    return s


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


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


def record_result(state, pl, bal):
    if pl is not None:
        state["streak"] = state.get("streak", 0) + 1 if pl < 0 else 0
    ds = state.get("day_start")
    if bal is not None and ds:
        state["peak"] = max(state.get("peak", 0.0), bal - ds)


def halt_reason(state, balance):
    if state.get("streak", 0) >= MAX_STREAK:
        return f"{MAX_STREAK} losses in a row"
    ds = state.get("day_start")
    if balance and ds:
        day_pl = balance - ds
        if day_pl <= -ds * DAILY_LOSS_PCT:
            return f"daily loss limit ({DAILY_LOSS_PCT * 100:.0f}%)"
        peak = state.get("peak", 0.0)
        if peak > 0 and day_pl <= peak * (1 - GIVEBACK):
            return f"gave back {GIVEBACK * 100:.0f}% of today's peak profit"
    return None


def size_trade(balance, stop_dist, ratio):
    factor = max(0.5, min(1.0, 1.0 / ratio)) if ratio > 0 else 1.0
    risk_money = balance * RISK_PCT * factor
    raw = risk_money / (stop_dist * POINT_VALUE)
    lots = math.floor(raw / LOT_STEP + 1e-9) * LOT_STEP
    return round(min(lots, MAX_LOT), 2), risk_money, factor


def send_incoming(side, price, sl, tp, lots, risk_money, base, factor, r, dp):
    icon = "🟢" if side == "buy" else "🔴"
    send("\n".join([
        "⚡ <b>TRADE INCOMING</b>", "",
        f"{icon} <b>{side.upper()}</b> · {SYMBOL} · {TIMEFRAME}",
        LINE,
        f"💵 Entry    <code>≈ {price:,.{dp}f}</code>",
        f"🛑 Stop     <code>{sl:,.{dp}f}</code>",
        f"🎯 Target   <code>{tp:,.{dp}f}</code>",
        f"📦 Size     <code>{lots} lots</code>",
        f"🛡 Risk     <code>${risk_money:,.2f} ({risk_money / base * 100:.2f}%)</code>",
        f"🌊 Volatility  <code>{'calm' if factor >= 1 else 'high, size reduced'}</code>",
        f"📊 RSI      <code>{r:.1f}</code>",
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
        head, "",
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


def run(state):
    tl = TLAPI(environment=TL_ENV, username=EMAIL, password=PASSWORD, server=SERVER)
    iid = tl.get_instrument_id_from_symbol_name(SYMBOL)
    df = tl.get_price_history(
        instrument_id=iid, resolution=TIMEFRAME, start_timestamp=0, end_timestamp=0, lookback_period="10D"
    )
    df = df.iloc[:-1].copy()
    df["t"] = df["t"].astype(float).apply(lambda x: x / 1000 if x > 1e12 else x)
    if len(df) < TREND + 60:
        print(f"Not enough candles ({len(df)}).")
        return

    df["fast"] = df["c"].ewm(span=FAST, adjust=False).mean()
    df["slow"] = df["c"].ewm(span=SLOW, adjust=False).mean()
    df["trend"] = df["c"].ewm(span=TREND, adjust=False).mean()
    df["rsi"] = rsi(df["c"], RSI_LEN)
    df["atr"] = atr(df, ATR_LEN)
    prev, last = df.iloc[-2], df.iloc[-1]
    price, a, r = float(last["c"]), float(last["atr"]), float(last["rsi"])
    atr_avg = float(df["atr"].rolling(50).mean().iloc[-1])
    ratio = a / atr_avg if atr_avg else 1.0

    try:
        mine = my_positions(tl, iid)
    except Exception as e:
        send(f"⚠️ <b>Could not read positions</b>\n<code>{esc(e)}</code>\nNo trades this run.")
        return

    balance = get_balance(tl)
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if state.get("day") != today:
        state.update({"day": today, "day_start": balance, "peak": 0.0, "streak": 0, "halt_sent": False})

    # A trade closed by its stop or target?
    trade = state.get("trade")
    if trade and not mine:
        time.sleep(2)
        pl, bal = realized_pl(tl, trade)
        send_closed(trade, pl, bal)
        record_result(state, pl, bal)
        state["trade"] = None
        trade = None
        balance = bal if bal is not None else balance
    elif trade and mine:
        try:
            v = mine[0].get("unrealizedPl")
            if v is not None:
                trade["last_pl"] = float(v)
        except Exception:
            pass

    # Stop rules
    why = halt_reason(state, balance)
    if why:
        if not state.get("halt_sent"):
            state["halt_sent"] = True
            send(f"🚫 <b>Trading paused for today</b>\nReason: <code>{why}</code>\nResumes tomorrow (UTC).")
        return

    if balance is None:
        send("⚠️ Could not read your balance. No trades this run.")
        return

    # Trading hours
    minute = now.hour * 60 + now.minute
    if not (START_MIN <= minute <= END_MIN):
        print("Outside trading hours.")
        return

    # Signal
    fresh = (time.time() - (float(last["t"]) + TF)) <= TF
    is_new = float(last["t"]) > state.get("last_candle", 0)
    up = prev["fast"] <= prev["slow"] and last["fast"] > last["slow"] and price > last["trend"] and r < 70
    down = prev["fast"] >= prev["slow"] and last["fast"] < last["slow"] and price < last["trend"] and r > 30
    side = "buy" if up else "sell" if down else None
    if not (side and fresh and is_new):
        print(f"No signal. Price {price}, RSI {r:.1f}, volatility x{ratio:.2f}")
        return
    state["last_candle"] = float(last["t"])

    # Never two trades in the same direction
    if mine and str(mine[0]["side"]).lower() == side:
        print("Already in a trade in this direction. Skipping.")
        return

    # 1% rule sizing
    stop_dist = SL_ATR * a
    lots, risk_money, factor = size_trade(balance, stop_dist, ratio)
    if lots < MIN_LOT:
        min_risk = MIN_LOT * stop_dist * POINT_VALUE
        need = min_risk / RISK_PCT
        send(f"⏭ <b>Signal skipped</b> · {side.upper()} {SYMBOL}\n"
             f"The smallest size ({MIN_LOT} lots) would risk <code>${min_risk:,.2f} "
             f"({min_risk / balance * 100:.0f}% of your balance)</code>.\n"
             f"At 1% risk you'd need about <code>${need:,.0f}</code> in the account.")
        return

    # Close the opposite trade first
    if mine:
        try:
            tl.close_position(position_id=int(mine[0]["id"]))
        except Exception as e:
            send(f"⚠️ <b>Could not close position</b>\n<code>{esc(e)}</code>\nNo new trade.")
            return
        time.sleep(3)
        if trade:
            pl, bal = realized_pl(tl, trade)
            send_closed(trade, pl, bal, reason="Opposite signal")
            record_result(state, pl, bal)
            state["trade"] = None
            balance = bal if bal is not None else balance
            if halt_reason(state, balance):
                return
        else:
            send("🔁 Closed an open position (opposite signal).")

    dp = 2 if price > 100 else 5
    sl = price - stop_dist if side == "buy" else price + stop_dist
    tp = price + TP_ATR * a if side == "buy" else price - TP_ATR * a
    send_incoming(side, price, sl, tp, lots, risk_money, balance, factor, r, dp)

    try:
        order_id = tl.create_order(
            iid, quantity=lots, side=side, type_="market",
            stop_loss=round(sl, dp), stop_loss_type="absolute",
            take_profit=round(tp, dp), take_profit_type="absolute",
        )
    except Exception as e:
        send(f"⚠️ <b>Order failed</b> · {side.upper()} {SYMBOL}\n<code>{esc(e)}</code>")
        return
    if not order_id:
        send(f"⚠️ <b>Order not accepted</b> · {side.upper()} {SYMBOL}")
        return

    state["trade"] = {
        "side": side, "entry": price, "sl": sl, "tp": tp, "lots": lots,
        "opened_at": time.time(), "balance_before": balance, "last_pl": None,
    }


def main():
    state = load_state()
    try:
        run(state)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
