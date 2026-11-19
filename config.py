"""
Settings for the XAUUSD MT5 bot. Edit this file and restart the bot.
(Risk settings can also be changed live from the dashboard's Settings panel.)
"""
import os

# ---------------- Dashboard ----------------
# The dashboard is served by the bot itself. Open http://localhost:8765 in your browser.
DASHBOARD_HOST = "127.0.0.1"   # "127.0.0.1" = only this computer. To reach it from your phone or another
                               # PC, use "0.0.0.0" AND set a password below (the bot refuses to start otherwise).
DASHBOARD_PORT = 8765
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")   # browser asks for it (username can be anything)

# ---------------- MetaTrader 5 bridge ----------------
# Run the MQL5 Expert Advisor in MT5 for Mac. Its inputs must match these values.
BRIDGE_TOKEN = "bda1e832f1874f8d90e60e148facdc9a92756d3a6eeb4f1b"
BRIDGE_PORT = 8766

SYMBOL = "XAUUSD247m"         # your broker may use "GOLD", "XAUUSD.m", "XAUUSDm", etc.
TIMEFRAME = "M15"         # M1, M5, M15, M30, H1, H4, D1
MAGIC = 560011            # tags this bot's trades so it never touches your manual ones

# Broker server time vs UTC, in hours (e.g. 2 or 3). None = auto-detect while the market is open.
BROKER_UTC_OFFSET_HOURS = None
HISTORY_DAYS = 30         # how far back the dashboard's stats and trade history go

# SAFETY: the bot refuses to trade on a REAL-money account unless this is True.
# Test on a demo account first.
ALLOW_LIVE = False



# ---------------- Strategy (EMA crossover + RSI + trend filter, ATR stops) ----------------
EMA_FAST = 20
EMA_SLOW = 50
EMA_TREND = 200           # buys only above it, sells only below it
USE_TREND_FILTER = True
RSI_PERIOD = 14
RSI_BUY_MAX = 70          # don't buy when already overbought
RSI_SELL_MIN = 30         # don't sell when already oversold
ATR_PERIOD = 14
SL_ATR_MULT = 1.5         # stop loss distance = ATR * this
RISK_REWARD = 2.0         # take profit distance = stop distance * this

# More trades:
SIGNAL_LOOKBACK_BARS = 3      # accept an EMA cross from up to this many closed candles ago if the setup still holds
                              # (1 = old behaviour: only a cross on the very last candle counts)
USE_PULLBACK_ENTRIES = True   # also enter when price pulls back to the fast EMA inside an established trend
PULLBACK_RSI_MIN = 40         # pullback buys need RSI >= this (sells need RSI <= 100 - this)
PULLBACK_COOLDOWN_BARS = 8    # at most one pullback signal per direction every 8 candles (2h on M15)

# Turn the bot back on automatically after a restart/reboot if it was on before (needed for VPS hosting).
RESUME_ON_RESTART = True

# ---------------- Risk management ----------------
RISK_PER_TRADE_PCT = 1.0      # % of balance risked per trade (lot size is calculated from SL distance)
MAX_OPEN_TRADES = 1           # bot trades open at once
MAX_DAILY_LOSS_PCT = 3.0      # stop trading for the day (UTC) after losing this % of balance
MAX_SPREAD_POINTS = 60        # skip entries when spread is wider than this (points)
MAX_LOT = 5.0                 # hard cap on lot size
DEVIATION_POINTS = 30         # allowed slippage (points)

# Only trade inside these UTC hours (London + New York overlap is the most liquid for gold).
TRADE_HOURS_UTC = (0, 24)     # start hour inclusive, end hour exclusive. None = always (when market is open)

# ---------------- Recommendation mode ----------------
PROPOSAL_TTL_SEC = 300        # a recommendation expires if you don't answer in 5 minutes
MAX_DRIFT_ATR = 0.3           # if price moved more than 0.3 ATR since the signal, accepting is refused

# ---------------- Optional: Claude as a second opinion ----------------
# When enabled, every signal from the strategy is sent to Claude along with recent candles and
# indicators. Claude replies approve/reject and a short reason. It can only veto, never invent trades.
USE_CLAUDE_FILTER = False
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-5-5"
CLAUDE_FAIL_OPEN = False      # if the Claude call fails: False = skip the trade, True = continue without it
