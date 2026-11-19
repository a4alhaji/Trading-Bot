"""
XAUUSD MetaTrader 5 bot with a built-in web dashboard and local MQL5 bridge.

Run:  python bot.py     then open  http://localhost:8765

Modes
  recommend : the bot finds a setup and shows it on the dashboard with Accept / Decline
  auto      : the bot places the trade itself (with SL and TP)
The bot always starts switched OFF. Turn it on from the dashboard.
"""
import base64
import hmac
import ipaddress
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib import request as urlrequest
from urllib.parse import urlparse

import numpy as np

import config as C

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
HTML_FILE = os.path.join(BASE_DIR, "dashboard.html")
BARS_NEEDED = 500
CHART_BARS = 120

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8")],
)
log = logging.getLogger("bot")

# ============================== activity log ==============================
EVENTS = deque(maxlen=300)
_last_err = {"msg": None, "t": 0.0}


def event(level, msg):
    """level: info | warn | error. Shown in the dashboard's Activity panel."""
    if level == "error":
        if _last_err["msg"] == msg and time.time() - _last_err["t"] < 60:
            return
        _last_err.update(msg=msg, t=time.time())
    EVENTS.appendleft({"t": int(time.time()), "level": level, "msg": msg})
    {"info": log.info, "warn": log.warning, "error": log.error}[level](msg)


# ============================== persistence + live settings ==============================
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


EDITABLE = {  # key: (type, min, max)
    "RISK_PER_TRADE_PCT": (float, 0.05, 5.0),
    "MAX_OPEN_TRADES": (int, 1, 10),
    "MAX_DAILY_LOSS_PCT": (float, 0.5, 20.0),
    "MAX_SPREAD_POINTS": (int, 1, 1000),
    "SL_ATR_MULT": (float, 0.5, 5.0),
    "RISK_REWARD": (float, 0.5, 6.0),
    "MAX_LOT": (float, 0.01, 50.0),
}


def current_settings():
    s = {k: getattr(C, k) for k in EDITABLE}
    h = C.TRADE_HOURS_UTC or (0, 24)
    s["TRADE_START_UTC"], s["TRADE_END_UTC"] = h
    s["USE_CLAUDE_FILTER"] = bool(C.USE_CLAUDE_FILTER)
    return s


def apply_settings(d):
    """Validate and apply a dict of settings. Raises ValueError with a readable message."""
    allowed = set(EDITABLE) | {"TRADE_START_UTC", "TRADE_END_UTC", "USE_CLAUDE_FILTER"}
    unknown = set(d) - allowed
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(sorted(unknown))}")
    clean = {}
    for k, (typ, lo, hi) in EDITABLE.items():
        if k in d:
            try:
                v = typ(d[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k}: not a valid number")
            if not (lo <= v <= hi):
                raise ValueError(f"{k} must be between {lo} and {hi}")
            clean[k] = v
    cur = current_settings()
    try:
        start = int(d.get("TRADE_START_UTC", cur["TRADE_START_UTC"]))
        end = int(d.get("TRADE_END_UTC", cur["TRADE_END_UTC"]))
    except (TypeError, ValueError):
        raise ValueError("Trading hours must be whole numbers")
    if not (0 <= start < end <= 24):
        raise ValueError("Trading hours: start must be before end (0-24, UTC)")
    claude = bool(d.get("USE_CLAUDE_FILTER", cur["USE_CLAUDE_FILTER"]))
    if claude and not C.ANTHROPIC_API_KEY:
        raise ValueError("Set the ANTHROPIC_API_KEY environment variable before enabling the Claude filter")
    for k, v in clean.items():
        setattr(C, k, v)
    C.TRADE_HOURS_UTC = None if (start, end) == (0, 24) else (start, end)
    C.USE_CLAUDE_FILTER = claude
    return current_settings()


# ============================== indicators ==============================
def ema(x, n):
    a = 2.0 / (n + 1)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def wilder(x, n):
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def rsi(close, n=14):
    d = np.diff(close, prepend=close[0])
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    rs = wilder(gain, n) / (wilder(loss, n) + 1e-12)
    return 100 - 100 / (1 + rs)


def atr(high, low, close, n=14):
    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    return wilder(tr, n)


# ============================== strategy ==============================
def analyze(rates):
    """rates = closed candles only (oldest -> newest). Returns a signal dict or None.

    Two setups:
      1. EMA fast/slow crossover - the cross may have happened up to SIGNAL_LOOKBACK_BARS candles ago
         (so a short disconnect or restart doesn't lose it), as long as the setup is still valid now.
      2. Trend pullback (optional) - in an established trend, price dips to the fast EMA and the last
         candle closes back in the trend direction.
    Each signal carries a 'key' so the engine never trades the same setup twice.
    """
    close = rates["close"].astype(float)
    high = rates["high"].astype(float)
    low = rates["low"].astype(float)
    opn = rates["open"].astype(float)
    times = rates["time"]
    if len(close) < max(C.EMA_TREND, C.EMA_SLOW) + 5:
        return None

    fast, slow = ema(close, C.EMA_FAST), ema(close, C.EMA_SLOW)
    trend = ema(close, C.EMA_TREND)
    r = rsi(close, C.RSI_PERIOD)
    a = atr(high, low, close, C.ATR_PERIOD)

    trend_up = not C.USE_TREND_FILTER or close[-1] > trend[-1]
    trend_dn = not C.USE_TREND_FILTER or close[-1] < trend[-1]
    side = kind = key = None

    # ---- 1. crossover within the lookback window ----
    lookback = max(1, int(getattr(C, "SIGNAL_LOOKBACK_BARS", 1)))
    for k in range(1, lookback + 1):
        i = len(close) - k
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            if fast[-1] > slow[-1] and r[-1] <= C.RSI_BUY_MAX and trend_up:
                side = "BUY"
        elif fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            if fast[-1] < slow[-1] and r[-1] >= C.RSI_SELL_MIN and trend_dn:
                side = "SELL"
        if side:
            kind = f"EMA{C.EMA_FAST}/{C.EMA_SLOW} cross {'up' if side == 'BUY' else 'down'}" + \
                   (f" ({k - 1} bars ago)" if k > 1 else "")
            key = f"cross-{side}-{int(times[i])}"
            break
        if (fast[i - 1] <= slow[i - 1]) != (fast[i] <= slow[i]):
            break    # a newer cross that failed the filters supersedes older ones

    # ---- 2. trend pullback ----
    # Previous candle closed back through the fast EMA (a real dip), the latest candle closed back in the
    # trend direction, EMAs are stacked, and no other pullback signal fired in the last PULLBACK_COOLDOWN_BARS.
    if side is None and getattr(C, "USE_PULLBACK_ENTRIES", False):
        def reclaim(i):
            if fast[i] > slow[i] > trend[i] and close[i - 1] <= fast[i - 1] and close[i] > fast[i] \
                    and close[i] > opn[i] and C.PULLBACK_RSI_MIN <= r[i] <= C.RSI_BUY_MAX:
                return "BUY"
            if fast[i] < slow[i] < trend[i] and close[i - 1] >= fast[i - 1] and close[i] < fast[i] \
                    and close[i] < opn[i] and C.RSI_SELL_MIN <= r[i] <= 100 - C.PULLBACK_RSI_MIN:
                return "SELL"
            return None
        n = len(close) - 1
        cand = reclaim(n)
        cooldown = int(getattr(C, "PULLBACK_COOLDOWN_BARS", 8))
        if cand and not any(reclaim(j) == cand for j in range(n - cooldown, n)):
            side = cand
            kind = f"Pullback to EMA{C.EMA_FAST} in {'up' if side == 'BUY' else 'down'}trend"
            key = f"pullback-{side}-{int(times[-1])}"

    if side is None:
        return None

    return {
        "side": side,
        "key": key,
        "atr": float(a[-1]),
        "rsi": float(r[-1]),
        "ema_fast": float(fast[-1]),
        "ema_slow": float(slow[-1]),
        "ema_trend": float(trend[-1]),
        "close": float(close[-1]),
        "reason": f"{kind}, RSI {r[-1]:.1f}, ATR {a[-1]:.2f}",
    }


def scan_summary(rates):
    """One-line description of the latest closed candle, for the log."""
    close = rates["close"].astype(float)
    if len(close) < C.EMA_TREND + 5:
        return f"only {len(close)} candles, need {C.EMA_TREND + 5}"
    f, s, t = ema(close, C.EMA_FAST)[-1], ema(close, C.EMA_SLOW)[-1], ema(close, C.EMA_TREND)[-1]
    r = rsi(close, C.RSI_PERIOD)[-1]
    bias = "up" if f > s else "down"
    return f"close {close[-1]:.2f}, EMA{C.EMA_FAST} {f:.2f} {'>' if f > s else '<'} EMA{C.EMA_SLOW} {s:.2f} ({bias}), " \
           f"{'above' if close[-1] > t else 'below'} EMA{C.EMA_TREND}, RSI {r:.1f}"


def ask_claude(sig, rates):
    """Optional veto layer. Returns (approved: bool, reason: str)."""
    candles = [
        [datetime.fromtimestamp(int(c["time"]), timezone.utc).strftime("%m-%d %H:%M"),
         round(float(c["open"]), 2), round(float(c["high"]), 2),
         round(float(c["low"]), 2), round(float(c["close"]), 2)]
        for c in rates[-30:]
    ]
    prompt = (
        f"You are a risk-aware trading assistant reviewing a proposed {sig['side']} on {C.SYMBOL} "
        f"({C.TIMEFRAME}). Proposed entry ~{sig['entry']}, SL {sig['sl']}, TP {sig['tp']}.\n"
        f"Indicators: {sig['reason']}. EMA{C.EMA_TREND}={sig['ema_trend']:.2f}.\n"
        f"Last 30 candles [time, o, h, l, c]: {json.dumps(candles)}\n"
        "Reject setups that look like chop, exhaustion, or a stop sitting in an obvious sweep zone. "
        'Reply ONLY with JSON: {"decision":"approve" or "reject","reason":"<one sentence>"}'
    )
    body = json.dumps({"model": C.CLAUDE_MODEL, "max_tokens": 200,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urlrequest.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": C.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            text = json.loads(resp.read())["content"][0]["text"]
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        return data.get("decision") == "approve", str(data.get("reason", ""))
    except Exception as e:
        log.warning("Claude filter failed: %s", e)
        return C.CLAUDE_FAIL_OPEN, f"Claude unavailable ({e.__class__.__name__})"


# ============================== macOS MT5 bridge ==============================
class MT5Bridge:
    """MT5-compatible facade; all terminal access is performed by the attached MQL5 EA."""

    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15 = 1, 5, 15
    TIMEFRAME_M30, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 30, 60, 240, 1440
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    SYMBOL_TRADE_MODE_FULL = 4
    DEAL_ENTRY_OUT, DEAL_TYPE_SELL = 1, 1
    DEAL_REASON_SL, DEAL_REASON_TP = 4, 5
    POSITION_TYPE_BUY = 0
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, ORDER_TIME_GTC = 1, 0
    ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN = 1, 0, 2
    TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE_PARTIAL = 10009, 10008, 10010

    def __init__(self):
        self.lock = threading.Condition()
        self.data = None
        self.updated = 0.0
        self.pending = None

    def update(self, payload):
        with self.lock:
            self.data = payload
            self.updated = time.time()
            self.lock.notify_all()

    def fresh_data(self, max_age=5):
        with self.lock:
            if self.data is None or time.time() - self.updated > max_age:
                raise RuntimeError("MT5 bridge is disconnected or its data is stale. Check the EA and WebRequest settings.")
            if not self.data.get("connected"):
                raise RuntimeError(self.data.get("error") or "MT5 terminal is disconnected.")
            return self.data

    def initialize(self, **_kwargs):
        last_notice = 0.0
        with self.lock:
            while True:
                if self.data and self.data.get("connected") and time.time() - self.updated <= 5:
                    return True
                now = time.time()
                if now - last_notice >= 15:
                    log.info("Waiting for MT5 EA bridge data on port %s. Check the EA BridgeUrl and MT5 WebRequest allow list.", C.BRIDGE_PORT)
                    last_notice = now
                self.lock.wait(1)

    def shutdown(self):
        return None

    def last_error(self):
        return "No recent response from the MT5 bridge"

    def account_info(self):
        a = self.fresh_data().get("account")
        return SimpleNamespace(
            login=a["login"], server=a["server"], currency=a["currency"],
            balance=a["balance"], equity=a["equity"], margin=a["margin"],
            margin_free=a["free_margin"], margin_level=a["margin_level"],
            leverage=a["leverage"],
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO if a["demo"] else self.ACCOUNT_TRADE_MODE_REAL,
        ) if a else None

    def terminal_info(self):
        t = self.fresh_data().get("terminal", {})
        return SimpleNamespace(connected=t.get("connected", False),
                               trade_allowed=t.get("algo_trading", False))

    def symbol_info(self, symbol):
        d = self.fresh_data()
        if symbol != d.get("symbol"):
            return None
        i = d["symbol_info"]
        return SimpleNamespace(
            digits=i["digits"], point=i["point"], trade_stops_level=i["stops_level"],
            trade_mode=self.SYMBOL_TRADE_MODE_FULL if i["trade_mode"] == "full" else 0,
            volume_min=i["volume_min"], volume_max=i["volume_max"],
            volume_step=i["volume_step"], filling_mode=i.get("filling_mode", 0),
        )

    def symbol_info_tick(self, symbol):
        d = self.fresh_data()
        if symbol != d.get("symbol"):
            return None
        t = d["tick"]
        return SimpleNamespace(bid=t["bid"], ask=t["ask"], time=t["time"])

    def symbol_select(self, symbol, _select):
        return symbol == self.fresh_data().get("symbol")

    def positions_get(self, symbol=None):
        data = self.fresh_data()
        if symbol is not None and symbol != data.get("symbol"):
            return []
        return [SimpleNamespace(
            ticket=p["ticket"], magic=p["magic"], symbol=p["symbol"],
            type=self.POSITION_TYPE_BUY if p["side"] == "BUY" else 1,
            volume=p["volume"], price_open=p["open_price"], price_current=p["price"],
            sl=p["sl"], tp=p["tp"], profit=p["profit"], swap=p["swap"], time=p["time"],
        ) for p in data.get("positions", [])]

    def copy_rates_from_pos(self, symbol, _timeframe, start, count):
        d = self.fresh_data()
        if symbol != d.get("symbol"):
            return None
        rates = d.get("rates", [])
        end = max(0, len(rates) - start)
        selected = rates[max(0, end - count):end]
        if not selected:
            return None
        dtype = [(k, "i8" if k == "time" else "f8")
                 for k in ("time", "open", "high", "low", "close")]
        return np.array([tuple(r[k] for k, _ in dtype) for r in selected], dtype=dtype)

    def history_deals_get(self, date_from, date_to):
        data = self.fresh_data()
        start, end = date_from.timestamp(), date_to.timestamp()
        return [SimpleNamespace(**d) for d in data.get("deals", [])
                if start <= d["time"] <= end]

    def _command(self, action, **args):
        with self.lock:
            if self.pending is not None:
                raise RuntimeError("An MT5 bridge command is already pending.")
            box = {"id": uuid.uuid4().hex, "action": action, "args": args,
                   "event": threading.Event()}
            self.pending = box
        if not box["event"].wait(25):
            with self.lock:
                if self.pending is box:
                    self.pending = None
            raise TimeoutError("MT5 Expert Advisor did not respond to the command.")
        if not box.get("ok"):
            raise RuntimeError(box.get("error") or "MT5 command failed")
        return box.get("result", {})

    def next_command(self):
        with self.lock:
            if self.pending is None:
                return {}
            return {"id": self.pending["id"], "action": self.pending["action"],
                    **self.pending["args"]}

    def resolve_command(self, result):
        with self.lock:
            box = self.pending
            if box is None or result.get("id") != box["id"]:
                return False
            box.update(ok=bool(result.get("ok")), error=result.get("error"),
                       result=result.get("result", {}))
            self.pending = None
            box["event"].set()
            return True

    def order_calc_profit(self, order_type, symbol, volume, price_open, price_close):
        try:
            result = self._command("calc_profit", side="BUY" if order_type == self.ORDER_TYPE_BUY else "SELL",
                                   symbol=symbol, volume=volume, entry=price_open, exit=price_close)
            return float(result["value"])
        except (RuntimeError, TimeoutError, KeyError, ValueError):
            return None

    def order_send(self, req):
        if "position" in req:
            action = "close"
            args = {"ticket": int(req["position"]), "magic": int(req["magic"])}
        else:
            action = "order"
            args = {"side": "BUY" if req["type"] == self.ORDER_TYPE_BUY else "SELL",
                    "symbol": req["symbol"], "volume": req["volume"], "sl": req["sl"],
                    "tp": req["tp"], "magic": req["magic"], "deviation": req["deviation"]}
        try:
            result = self._command(action, **args)
            retcode = result.get("retcode", self.TRADE_RETCODE_DONE)
            if retcode in (self.TRADE_RETCODE_DONE, self.TRADE_RETCODE_PLACED,
                           self.TRADE_RETCODE_DONE_PARTIAL):
                retcode = self.TRADE_RETCODE_DONE
            return SimpleNamespace(retcode=retcode,
                                   comment=result.get("comment", "Done"))
        except (RuntimeError, TimeoutError) as e:
            return SimpleNamespace(retcode=0, comment=str(e))


mt5 = MT5Bridge()
TF = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
      "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
      "D1": mt5.TIMEFRAME_D1}


def connect():
    if C.TIMEFRAME not in TF:
        raise RuntimeError(f"Unknown TIMEFRAME {C.TIMEFRAME}. Use one of: {', '.join(TF)}")
    mt5.initialize()
    acc = mt5.account_info()
    if acc is None:
        raise RuntimeError("No account logged in inside MT5.")
    if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and not C.ALLOW_LIVE:
        raise RuntimeError("This is a LIVE account. Set ALLOW_LIVE = True in config.py only after "
                           "you have tested on demo.")
    if not mt5.symbol_select(C.SYMBOL, True):
        raise RuntimeError(f"Symbol {C.SYMBOL} not available in the attached MT5 EA.")
    return acc


def my_positions():
    pos = mt5.positions_get(symbol=C.SYMBOL) or []
    return [p for p in pos if p.magic == C.MAGIC]


def build_trade(side, atr_value):
    """Entry/SL/TP/lots for a signal at the current price."""
    info = mt5.symbol_info(C.SYMBOL)
    tick = mt5.symbol_info_tick(C.SYMBOL)
    buy = side == "BUY"
    entry = tick.ask if buy else tick.bid
    min_dist = (info.trade_stops_level + 5) * info.point
    sl_dist = max(atr_value * C.SL_ATR_MULT, min_dist)
    tp_dist = max(sl_dist * C.RISK_REWARD, min_dist)
    sl = round(entry - sl_dist if buy else entry + sl_dist, info.digits)
    tp = round(entry + tp_dist if buy else entry - tp_dist, info.digits)

    order_type = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL
    loss_1lot = mt5.order_calc_profit(order_type, C.SYMBOL, 1.0, entry, sl)
    if not loss_1lot:
        raise RuntimeError("Could not calculate risk per lot.")
    risk_money = mt5.account_info().balance * C.RISK_PER_TRADE_PCT / 100
    lots = risk_money / abs(loss_1lot)
    step = info.volume_step
    lots = np.floor(lots / step) * step
    lots = round(min(max(lots, info.volume_min), min(info.volume_max, C.MAX_LOT)), 2)
    return {"side": side, "entry": float(entry), "sl": float(sl), "tp": float(tp), "lots": float(lots),
            "risk_money": float(risk_money)}


def filling_mode(_info):
    return mt5.ORDER_FILLING_RETURN


def place_order(t):
    info = mt5.symbol_info(C.SYMBOL)
    tick = mt5.symbol_info_tick(C.SYMBOL)
    buy = t["side"] == "BUY"
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": C.SYMBOL,
        "volume": t["lots"],
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if buy else tick.bid,
        "sl": t["sl"],
        "tp": t["tp"],
        "deviation": C.DEVIATION_POINTS,
        "magic": C.MAGIC,
        "comment": "xauusd-bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode(info),
    }
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"Order failed: {getattr(res, 'retcode', None)} {getattr(res, 'comment', mt5.last_error())}")
    return res


def close_position(p):
    info = mt5.symbol_info(C.SYMBOL)
    tick = mt5.symbol_info_tick(C.SYMBOL)
    buy = p.type == mt5.POSITION_TYPE_BUY
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": C.SYMBOL, "volume": p.volume,
        "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
        "position": p.ticket, "price": tick.bid if buy else tick.ask,
        "deviation": C.DEVIATION_POINTS, "magic": C.MAGIC, "comment": "xauusd-bot close",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling_mode(info),
    }
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"Close failed: {getattr(res, 'retcode', None)} {getattr(res, 'comment', mt5.last_error())}")


def compute_stats(trades):
    """trades: closed trades, oldest -> newest, each with 'profit'."""
    profits = [t["profit"] for t in trades]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    cum, peak, max_dd, curve = 0.0, 0.0, 0.0, []
    for t in trades:
        cum += t["profit"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        curve.append([t["time"], round(cum, 2)])
    n = len(profits)
    return {
        "trades": n,
        "net": round(sum(profits), 2),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / n, 1) if n else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "best": round(max(profits), 2) if n else None,
        "worst": round(min(profits), 2) if n else None,
        "max_drawdown": round(max_dd, 2),
        "expectancy": round(sum(profits) / n, 2) if n else None,
    }, curve


# ============================== engine ==============================
class Engine:
    """Everything that touches MetaTrader5 runs on the main thread. The web server only reads the
    published snapshot and sends commands through a queue."""

    def __init__(self):
        self.cmds = queue.Queue()
        self.bridge = mt5
        state = load_json(STATE_FILE, {})
        self.mode = state.get("mode", "recommend")
        if self.mode not in ("auto", "recommend"):
            self.mode = "recommend"
        self.enabled = False                      # starts OFF; run() restores it if RESUME_ON_RESTART
        self.resume_enabled = bool(state.get("enabled")) and getattr(C, "RESUME_ON_RESTART", False)
        self.traded_keys = deque(state.get("traded_keys", []), maxlen=50)   # setups already acted on
        self.proposals = {}
        self.signals = deque(maxlen=60)
        self.last_bar = None
        self.last_scan = {"t": None, "result": "Bot is off"}
        self.offset = int((C.BROKER_UTC_OFFSET_HOURS or 0) * 3600)
        self.offset_known = C.BROKER_UTC_OFFSET_HOURS is not None
        self.snapshot = {"connected": False}
        self.history, self.stats, self.curve, self.daily = [], {}, [], []
        self.seen_deals = None
        self.chart, self.indicators = {}, {}
        self.t_hist = self.t_chart = self.t_snap = self.t_offset = 0.0

    # ---- thread bridge ----
    def call(self, fn, *args, timeout=20):
        box = {"done": threading.Event()}
        self.cmds.put((fn, args, box))
        if not box["done"].wait(timeout):
            raise TimeoutError("The bot is busy, try again.")
        if "err" in box:
            raise box["err"]
        return box.get("res")

    def process_commands(self):
        ran = False
        while True:
            try:
                fn, args, box = self.cmds.get_nowait()
            except queue.Empty:
                break
            try:
                box["res"] = fn(*args)
            except Exception as e:
                box["err"] = e
            box["done"].set()
            ran = True
        if ran:
            self.publish(force=True)

    # ---- time helpers (broker server time <-> UTC) ----
    def utc(self, t):
        return int(t - self.offset)

    def srv_dt(self, epoch_utc):
        return datetime.fromtimestamp(epoch_utc + self.offset, tz=timezone.utc)

    @staticmethod
    def utc_midnight():
        n = datetime.now(timezone.utc)
        return int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def detect_offset(self):
        if C.BROKER_UTC_OFFSET_HOURS is not None:
            return
        now = datetime.now(timezone.utc)
        if now.weekday() >= 5 or not (2 <= now.hour < 20):   # only when gold is certainly trading
            return
        tick = mt5.symbol_info_tick(C.SYMBOL)
        if not tick:
            return
        diff = tick.time - time.time()
        off = round(diff / 1800) * 1800
        if abs(diff - off) < 120 and abs(off) <= 14 * 3600:
            if not self.offset_known or off != self.offset:
                event("info", f"Broker time is UTC{off / 3600:+g}")
            self.offset, self.offset_known = int(off), True

    # ---- account / risk ----
    def today_pnl(self):
        deals = mt5.history_deals_get(self.srv_dt(self.utc_midnight()), self.srv_dt(time.time() + 86400)) or []
        closed = sum(d.profit + d.commission + d.swap for d in deals
                     if d.magic == C.MAGIC and d.entry == mt5.DEAL_ENTRY_OUT)
        floating = sum(p.profit + p.swap for p in my_positions())
        return closed + floating

    def block_reason(self):
        """A string if new entries are not allowed right now, else None."""
        now = datetime.now(timezone.utc)
        if now.weekday() >= 5:
            return "Weekend - market closed"
        if C.TRADE_HOURS_UTC and not (C.TRADE_HOURS_UTC[0] <= now.hour < C.TRADE_HOURS_UTC[1]):
            return f"Outside trading hours ({C.TRADE_HOURS_UTC[0]:02d}:00-{C.TRADE_HOURS_UTC[1]:02d}:00 UTC)"
        ti = mt5.terminal_info()
        if ti is not None and not ti.trade_allowed:
            return "Algo Trading is switched off in MT5"
        info = mt5.symbol_info(C.SYMBOL)
        tick = mt5.symbol_info_tick(C.SYMBOL)
        if info is None or tick is None or info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            return "Symbol not tradable right now"
        spread = (tick.ask - tick.bid) / info.point
        if spread > C.MAX_SPREAD_POINTS:
            return f"Spread too wide ({spread:.0f} pts, max {C.MAX_SPREAD_POINTS})"
        if len(my_positions()) >= C.MAX_OPEN_TRADES:
            return "Max open trades reached"
        bal = mt5.account_info().balance
        if self.today_pnl() <= -bal * C.MAX_DAILY_LOSS_PCT / 100:
            return f"Daily loss limit ({C.MAX_DAILY_LOSS_PCT}%) reached"
        return None

    # ---- commands from the dashboard (run on the main thread) ----
    def save_state(self):
        save_json(STATE_FILE, {"mode": self.mode, "enabled": self.enabled, "traded_keys": list(self.traded_keys)})

    def set_enabled(self, on):
        on = bool(on)
        if on == self.enabled:
            return "Bot is already " + ("on" if on else "off")
        self.enabled = on
        self.save_state()
        if on:
            self.sync_last_bar()
            self.last_scan = {"t": int(time.time()), "result": "Waiting for the next candle to close"}
            event("info", f"Bot turned ON ({self.mode} mode)")
            return "Bot is ON"
        for pid in list(self.proposals):
            p = self.proposals.pop(pid)
            p["rec"]["outcome"] = "Cancelled (bot turned off)"
        self.last_scan = {"t": int(time.time()), "result": "Bot is off"}
        event("info", "Bot turned OFF. Open trades keep their SL/TP.")
        return "Bot is OFF"

    def set_mode(self, mode):
        if mode not in ("auto", "recommend"):
            raise ValueError("Mode must be 'auto' or 'recommend'")
        self.mode = mode
        self.save_state()
        event("info", f"Mode set to {mode.upper()}")
        return f"Mode: {mode}"

    def update_settings(self, d):
        before = current_settings()
        after = apply_settings(d)
        save_json(SETTINGS_FILE, after)
        changed = [f"{k} {before[k]} -> {after[k]}" for k in after if before.get(k) != after[k]]
        if changed:
            event("info", "Settings changed: " + ", ".join(changed))
        return "Settings saved"

    def close_ticket(self, ticket):
        for p in my_positions():
            if p.ticket == ticket:
                close_position(p)
                event("info", f"Closed position #{ticket} manually")
                return f"Closed #{ticket}"
        raise ValueError("That position is no longer open.")

    def close_everything(self):
        n, errs = 0, []
        for p in my_positions():
            try:
                close_position(p)
                n += 1
            except Exception as e:
                errs.append(str(e))
        event("info", f"Closed {n} bot trade(s) manually")
        if errs:
            raise RuntimeError(f"Closed {n}, but {len(errs)} failed: {errs[0]}")
        return f"Closed {n} trade(s)"

    def decide(self, pid, accept):
        prop = self.proposals.pop(pid, None)
        if prop is None:
            raise ValueError("That recommendation expired or was already handled.")
        rec = prop["rec"]
        if not accept:
            rec["outcome"] = "Declined by you"
            event("info", f"Declined {rec['side']} recommendation")
            return "Declined"
        try:
            br = self.block_reason()
            if br:
                raise RuntimeError(br)
            tick = mt5.symbol_info_tick(C.SYMBOL)
            now_price = tick.ask if prop["trade"]["side"] == "BUY" else tick.bid
            if abs(now_price - prop["trade"]["entry"]) > prop["atr"] * C.MAX_DRIFT_ATR:
                raise RuntimeError("Price moved too far since the signal")
            t = dict(prop["trade"])
            fresh = build_trade(t["side"], prop["atr"])
            t.update(lots=fresh["lots"], entry=fresh["entry"], risk_money=fresh["risk_money"])
            place_order(t)
        except Exception as e:
            rec["outcome"] = f"Accept failed: {e}"
            event("warn", f"Could not place accepted {rec['side']}: {e}")
            raise ValueError(f"Not placed: {e}")
        rec["outcome"] = "Accepted - order placed"
        event("info", f"Placed {t['side']} {t['lots']} lots @ {t['entry']} (SL {t['sl']}, TP {t['tp']})")
        return "Order placed"

    # ---- scanning ----
    def closed_rates(self):
        r = mt5.copy_rates_from_pos(C.SYMBOL, TF[C.TIMEFRAME], 1, BARS_NEEDED)  # start at 1 = skip forming bar
        return r if r is not None and len(r) > 0 else None

    def sync_last_bar(self):
        r = self.closed_rates()
        if r is not None:
            self.last_bar = int(r[-1]["time"])

    def expire_proposals(self):
        now = time.time()
        for pid, p in list(self.proposals.items()):
            if now > p["expires_at"]:
                self.proposals.pop(pid)
                p["rec"]["outcome"] = "Expired (no answer)"
                event("info", f"{p['rec']['side']} recommendation expired")

    def check_market(self):
        rates = self.closed_rates()
        if rates is None:
            return
        bar_time = int(rates[-1]["time"])
        if bar_time == self.last_bar:
            return                       # only evaluate once per new closed candle
        self.last_bar = bar_time
        now = int(time.time())

        sig = analyze(rates)
        if sig is not None and sig["key"] in self.traded_keys:
            sig = None                   # this setup was already handled on an earlier candle
        if sig is None:
            self.last_scan = {"t": now, "result": "No signal on the latest candle"}
            log.info("Scan: no signal - %s", scan_summary(rates))
            return
        self.traded_keys.append(sig["key"])
        self.save_state()

        rec = {"id": uuid.uuid4().hex[:8], "t": now, "side": sig["side"], "reason": sig["reason"],
               "entry": None, "sl": None, "tp": None, "lots": None, "outcome": ""}
        self.signals.appendleft(rec)

        def done(outcome, level="info"):
            rec["outcome"] = outcome
            self.last_scan = {"t": now, "result": f"{sig['side']} signal - {outcome}"}
            event(level, f"{sig['side']} signal: {outcome}")

        br = self.block_reason()
        if br:
            return done(f"Skipped: {br}")
        try:
            trade = build_trade(sig["side"], sig["atr"])
        except Exception as e:
            return done(f"Skipped: {e}", "warn")
        rec.update(entry=trade["entry"], sl=trade["sl"], tp=trade["tp"], lots=trade["lots"])
        sig.update(entry=trade["entry"], sl=trade["sl"], tp=trade["tp"])

        if C.USE_CLAUDE_FILTER and C.ANTHROPIC_API_KEY:
            ok, why = ask_claude(sig, rates)
            rec["reason"] += f" | Claude: {'approve' if ok else 'reject'} - {why}"
            if not ok:
                return done(f"Rejected by Claude: {why}")

        if self.mode == "auto":
            try:
                place_order(trade)
                done("Placed automatically")
                event("info", f"Placed {trade['side']} {trade['lots']} lots @ {trade['entry']} "
                              f"(SL {trade['sl']}, TP {trade['tp']})")
            except Exception as e:
                done(f"Order failed: {e}", "error")
        else:
            pid = rec["id"]
            self.proposals[pid] = {"trade": trade, "atr": sig["atr"], "rec": rec,
                                   "created": now, "expires_at": time.time() + C.PROPOSAL_TTL_SEC}
            rec["outcome"] = "Awaiting your decision"
            self.last_scan = {"t": now, "result": f"{sig['side']} signal - awaiting your decision"}
            event("info", f"{sig['side']} recommendation ready: waiting for you on the dashboard")

    # ---- data for the dashboard ----
    def refresh_history(self):
        start = datetime.now(timezone.utc) - timedelta(days=C.HISTORY_DAYS)
        deals = mt5.history_deals_get(self.srv_dt(start.timestamp()), self.srv_dt(time.time() + 86400)) or []
        reasons = {mt5.DEAL_REASON_SL: "SL", mt5.DEAL_REASON_TP: "TP"}
        trades = []
        for d in deals:
            if d.magic != C.MAGIC or d.entry != mt5.DEAL_ENTRY_OUT or d.symbol != C.SYMBOL:
                continue
            trades.append({"ticket": d.ticket, "time": self.utc(d.time),
                           "side": "BUY" if d.type == mt5.DEAL_TYPE_SELL else "SELL",   # closing deal is the opposite side
                           "volume": d.volume, "price": d.price,
                           "profit": round(d.profit + d.commission + d.swap, 2),
                           "exit": reasons.get(d.reason, "Manual/other")})
        trades.sort(key=lambda t: t["time"])
        if self.seen_deals is not None:
            for t in trades:
                if t["ticket"] not in self.seen_deals:
                    event("info", f"{t['side']} closed by {t['exit']}: {t['profit']:+.2f}")
        self.seen_deals = {t["ticket"] for t in trades}
        self.history = trades
        self.stats, self.curve = compute_stats(trades)
        days = {}
        for t in trades:
            k = datetime.fromtimestamp(t["time"], timezone.utc).strftime("%Y-%m-%d")
            days[k] = round(days.get(k, 0.0) + t["profit"], 2)
        self.daily = [[k, v] for k, v in sorted(days.items())][-14:]

    def compute_chart(self):
        r = mt5.copy_rates_from_pos(C.SYMBOL, TF[C.TIMEFRAME], 0, BARS_NEEDED)
        if r is None or len(r) < 30:
            return
        d = mt5.symbol_info(C.SYMBOL).digits
        close, high, low = (r[k].astype(float) for k in ("close", "high", "low"))
        f, s, t = ema(close, C.EMA_FAST), ema(close, C.EMA_SLOW), ema(close, C.EMA_TREND)
        rs, a = rsi(close, C.RSI_PERIOD), atr(high, low, close, C.ATR_PERIOD)
        sl = slice(len(r) - min(CHART_BARS, len(r)), None)
        rd = lambda arr: [round(float(v), d) for v in arr[sl]]
        self.chart = {
            "candles": [[self.utc(int(x["time"])), round(float(x["open"]), d), round(float(x["high"]), d),
                         round(float(x["low"]), d), round(float(x["close"]), d)] for x in r[sl]],
            "ema_fast": rd(f), "ema_slow": rd(s), "ema_trend": rd(t),
        }
        i = -2   # last CLOSED candle
        self.indicators = {
            "ema_fast": round(float(f[i]), d), "ema_slow": round(float(s[i]), d),
            "ema_trend": round(float(t[i]), d), "rsi": round(float(rs[i]), 1),
            "atr": round(float(a[i]), d), "close": round(float(close[i]), d),
            "bias": "Bullish" if close[i] > t[i] else "Bearish",
        }

    def make_snapshot(self):
        acc, info, tick = mt5.account_info(), mt5.symbol_info(C.SYMBOL), mt5.symbol_info_tick(C.SYMBOL)
        ti = mt5.terminal_info()
        if not acc or not info or not tick:
            raise RuntimeError("No data from MT5 (is the terminal connected?)")
        pos = [{"ticket": p.ticket, "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": p.volume, "open_price": p.price_open, "price": p.price_current,
                "sl": p.sl, "tp": p.tp, "profit": round(p.profit + p.swap, 2), "time": self.utc(p.time)}
               for p in my_positions()]
        spread = round((tick.ask - tick.bid) / info.point)
        return {
            "connected": True,
            "server_time": int(time.time()),
            "enabled": self.enabled, "mode": self.mode,
            "symbol": C.SYMBOL, "timeframe": C.TIMEFRAME,
            "account": {"login": acc.login, "server": acc.server, "currency": acc.currency,
                        "balance": acc.balance, "equity": acc.equity, "margin": acc.margin,
                        "free_margin": acc.margin_free, "margin_level": acc.margin_level,
                        "leverage": acc.leverage, "demo": acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO},
            "terminal": {"connected": bool(ti.connected) if ti else True,
                         "algo_trading": bool(ti.trade_allowed) if ti else True},
            "market": {"bid": tick.bid, "ask": tick.ask, "spread_pts": spread, "digits": info.digits},
            "today_pnl": round(self.today_pnl(), 2),
            "open_pnl": round(sum(p["profit"] for p in pos), 2),
            "block_reason": self.block_reason(),
            "last_scan": self.last_scan,
            "offset_known": self.offset_known, "broker_utc_offset": self.offset / 3600,
            "strategy": {"ema_fast": C.EMA_FAST, "ema_slow": C.EMA_SLOW, "ema_trend": C.EMA_TREND,
                         "rsi_period": C.RSI_PERIOD, "rsi_buy_max": C.RSI_BUY_MAX,
                         "rsi_sell_min": C.RSI_SELL_MIN, "atr_period": C.ATR_PERIOD,
                         "trend_filter": C.USE_TREND_FILTER},
            "positions": pos,
            "proposals": [{"id": pid, **p["trade"], "reason": p["rec"]["reason"], "expires_at": p["expires_at"],
                           "created": p["created"]} for pid, p in self.proposals.items()],
            "signals": list(self.signals),
            "events": list(EVENTS)[:80],
            "history": list(reversed(self.history))[:100],
            "stats": self.stats, "equity_curve": self.curve, "daily": self.daily,
            "indicators": self.indicators, "chart": self.chart,
            "settings": current_settings(),
            "claude_available": bool(C.ANTHROPIC_API_KEY),
        }

    def publish(self, force=False):
        now = time.time()
        if not force and now - self.t_snap < 1.5:
            return
        self.t_snap = now
        try:
            if now - self.t_hist > 20 or not self.history and now - self.t_hist > 5:
                self.refresh_history()
                self.t_hist = now
            if now - self.t_chart > 3:
                self.compute_chart()
                self.t_chart = now
            self.snapshot = self.make_snapshot()
        except Exception as e:
            event("error", f"Data refresh failed: {e}")
            self.snapshot = {**self.snapshot, "connected": False, "error": str(e)}

    # ---- main loop ----
    def run(self):
        start_server(self)
        acc = connect()
        kind = "DEMO" if acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "LIVE"
        event("info", f"Connected to {kind} account {acc.login} ({C.SYMBOL}, {C.TIMEFRAME}). Bot is OFF.")
        self.detect_offset()
        self.t_offset = time.time()
        self.sync_last_bar()
        if self.resume_enabled:
            self.set_enabled(True)
            self.last_bar = None         # evaluate the latest closed candle right away
        self.publish(force=True)
        while True:
            try:
                self.process_commands()
                self.expire_proposals()
                if time.time() - self.t_offset > 600:
                    self.detect_offset()
                    self.t_offset = time.time()
                if self.enabled:
                    self.check_market()
                self.publish()
            except Exception as e:
                event("error", f"Loop error: {e}")
                time.sleep(5)
            time.sleep(0.3)


# ============================== web server ==============================
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_bridge_rejects = {}


def bridge_reject(msg):
    """Log why the EA's request was refused (at most once every 30 s per message)."""
    now = time.time()
    if now - _bridge_rejects.get(msg, 0) >= 30:
        _bridge_rejects[msg] = now
        log.warning(msg)


def _js(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    return str(o)


class Handler(BaseHTTPRequestHandler):
    engine = None
    server_version = "XAUBot"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=_js, separators=(",", ":"))
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, post=False):
        host_header = self.headers.get("Host") or ""
        host = (urlparse("//" + host_header).hostname or "").lower()
        if C.DASHBOARD_HOST in LOOPBACK and host not in LOOPBACK:       # blocks DNS-rebinding attacks
            self._send(403, {"ok": False, "error": "Forbidden host"})
            return False
        if C.DASHBOARD_PASSWORD:
            ok = False
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    pw = base64.b64decode(auth[6:]).decode("utf-8", "replace").split(":", 1)[-1]
                    ok = hmac.compare_digest(pw.encode(), C.DASHBOARD_PASSWORD.encode())
                except Exception:
                    ok = False
            if not ok:
                self._send(401, {"ok": False, "error": "Password required"},
                           extra={"WWW-Authenticate": 'Basic realm="XAUUSD Bot"'})
                return False
        if post:
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != host_header:      # blocks cross-site form posts
                self._send(403, {"ok": False, "error": "Bad origin"})
                return False
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._send(415, {"ok": False, "error": "Expected JSON"})
                return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/bridge/commands":
            if not self._bridge_guard():
                return
            self._send(200, self.engine.bridge.next_command())
            return
        if not self._guard():
            return
        if path in ("/", "/index.html"):
            try:
                with open(HTML_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "dashboard.html is missing next to bot.py", "text/plain; charset=utf-8")
        elif path == "/api/state":
            self._send(200, self.engine.snapshot)
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/bridge/commands", "/bridge/snapshot", "/bridge/result"):
            if not self._bridge_guard():
                return
            try:
                body = self._read_json(2_000_000)
                if path == "/bridge/commands":
                    self._send(200, self.engine.bridge.next_command())
                    return
                if path == "/bridge/snapshot":
                    if body.get("symbol") != C.SYMBOL:
                        raise ValueError(f"EA TradeSymbol is '{body.get('symbol')}' but config.py "
                                         f"SYMBOL is '{C.SYMBOL}'. They must match exactly.")
                    expected_tf = f"PERIOD_{C.TIMEFRAME}"
                    if body.get("timeframe") != expected_tf:
                        raise ValueError(f"EA SignalTimeframe is {body.get('timeframe')} but config.py "
                                         f"TIMEFRAME needs {expected_tf}.")
                    if not body.get("connected"):
                        bridge_reject(f"EA is reachable but reports: {body.get('error') or 'MT5 terminal not connected to the broker'}")
                    if self.engine.bridge.data is None:
                        log.info("MT5 EA bridge connected from %s", self.client_address[0])
                    self.engine.bridge.update(body)
                    self._send(200, {"ok": True})
                    return
                if not self.engine.bridge.resolve_command(body):
                    self._send(409, {"ok": False, "error": "Unknown or expired command"})
                    return
                self._send(200, {"ok": True})
            except (ValueError, TypeError) as e:
                bridge_reject(f"EA data rejected: {e}")
                self._send(400, {"ok": False, "error": str(e)})
            return
        if not self._guard(post=True):
            return
        eng = self.engine
        try:
            body = self._read_json(20000)
            routes = {
                "/api/enable": lambda: eng.call(eng.set_enabled, body.get("enabled")),
                "/api/mode": lambda: eng.call(eng.set_mode, body.get("mode")),
                "/api/closeall": lambda: eng.call(eng.close_everything),
                "/api/close": lambda: eng.call(eng.close_ticket, int(body.get("ticket"))),
                "/api/proposal": lambda: eng.call(eng.decide, str(body.get("id")), body.get("decision") == "accept"),
                "/api/settings": lambda: eng.call(eng.update_settings, body),
            }
            if path not in routes:
                return self._send(404, {"ok": False, "error": "Not found"})
            self._send(200, {"ok": True, "message": routes[path]()})
        except (ValueError, TypeError, RuntimeError, TimeoutError) as e:
            self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:
            log.exception("API error")
            self._send(500, {"ok": False, "error": f"Internal error: {e}"})

    def _read_json(self, limit):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as e:
            raise ValueError("Invalid Content-Length") from e
        if length < 0 or length > limit:
            raise ValueError("Request too large")
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        return body

    def _bridge_guard(self, allow_private=False):
        try:
            peer = ipaddress.ip_address(self.client_address[0])
            allowed_peer = peer.is_loopback or (allow_private and peer.is_private)
        except (IndexError, TypeError, ValueError):
            allowed_peer = False
        token = self.headers.get("X-Bridge-Token", "")
        peer_ip = self.client_address[0] if self.client_address else "?"
        if not allowed_peer:
            bridge_reject(f"Bridge request from {peer_ip} refused: only this computer / private "
                          "network addresses are allowed.")
            self._send(403, {"ok": False, "error": "Bridge access denied (address not allowed)"})
            return False
        if not hmac.compare_digest(token, C.BRIDGE_TOKEN):
            bridge_reject(f"Bridge request from {peer_ip} refused: BridgeToken in the EA does not "
                          "match BRIDGE_TOKEN in config.py.")
            self._send(403, {"ok": False, "error": "Bridge access denied (wrong token)"})
            return False
        if self.command == "POST" and not (self.headers.get("Content-Type") or "").startswith("application/json"):
            bridge_reject(f"Bridge request from {peer_ip} refused: not JSON.")
            self._send(415, {"ok": False, "error": "Expected JSON"})
            return False
        return True


class BridgeHandler(Handler):
    def _bridge_guard(self):
        return Handler._bridge_guard(self, allow_private=True)

    def do_GET(self):
        if urlparse(self.path).path != "/bridge/commands":
            self._send(404, {"ok": False, "error": "Not found"})
            return
        super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path not in ("/bridge/commands", "/bridge/snapshot", "/bridge/result"):
            self._send(404, {"ok": False, "error": "Not found"})
            return
        super().do_POST()


def _lan_ip():
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # no packet is sent; just picks the outgoing interface
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def start_server(engine):
    Handler.engine = engine
    BridgeHandler.engine = engine
    dashboard_srv = ThreadingHTTPServer((C.DASHBOARD_HOST, C.DASHBOARD_PORT), Handler)
    bridge_srv = ThreadingHTTPServer(("", C.BRIDGE_PORT), BridgeHandler)
    threading.Thread(target=dashboard_srv.serve_forever, daemon=True).start()
    threading.Thread(target=bridge_srv.serve_forever, daemon=True).start()
    shown = "localhost" if C.DASHBOARD_HOST in ("127.0.0.1", "0.0.0.0") else C.DASHBOARD_HOST
    print(f"\n  Dashboard running:  http://{shown}:{C.DASHBOARD_PORT}")
    print(f"  MT5 bridge listening on port {C.BRIDGE_PORT} (token required; private network only)")
    print(f"  EA BridgeUrl: http://127.0.0.1:{C.BRIDGE_PORT}"
          + (f"   (or http://{_lan_ip()}:{C.BRIDGE_PORT} if 127.0.0.1 can't connect)" if _lan_ip() else ""))
    print("  (Ctrl+C to stop)\n")


def main():
    if C.DASHBOARD_HOST not in LOOPBACK and not C.DASHBOARD_PASSWORD:
        raise SystemExit("DASHBOARD_HOST is open to the network, so set DASHBOARD_PASSWORD "
                         "(environment variable or config.py) before starting.")
    saved = load_json(SETTINGS_FILE, None)
    if saved:
        try:
            apply_settings(saved)
        except ValueError as e:
            log.warning("Ignoring saved settings: %s", e)
    eng = Engine()
    try:
        eng.run()
    except KeyboardInterrupt:
        print("\nStopped. Open trades keep their SL/TP in MT5.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
