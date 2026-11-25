
# XAUUSD MT5 bot for macOS

The Python bot runs natively on macOS. It does not use MetaTrader's Windows-only Python package; instead, an MQL5 Expert Advisor running in your MT5 terminal sends market/account data to Python and executes trading commands from it.

The bot starts **OFF** and in **Recommend** mode. Use a demo account first.

## Requirements

- MetaTrader 5 for Mac, logged into the account you intend to use
- Python 3.10 or later
- The `MT5MacBridge.mq5` Expert Advisor in this project

## Setup

1. In Terminal, go to this project folder and install its Python dependency:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

2. Open `config.py`. Check `SYMBOL` exactly matches your broker's Market Watch symbol and choose the desired `TIMEFRAME`. Keep `ALLOW_LIVE = False` while testing.

3. Install and compile the Expert Advisor:
   - In MT5, choose **File > Open Data Folder**.
   - Copy `MT5MacBridge.mq5` into `MQL5/Experts`.
   - Open it in MetaEditor and compile it.
   - In MT5, open **Tools > Options > Expert Advisors** and tick **Allow WebRequest for listed URL** and add `http://127.0.0.1:8766`.
   - Enable Algo Trading. Attach **MT5MacBridge** to a chart and allow algorithmic trading in its EA settings after starting Python in step 5.

4. Match the EA inputs to `config.py`:
   - `BridgeUrl`: `http://127.0.0.1:8766` (the default). Only if the Experts tab says it cannot reach Python, use your Mac's network address instead (printed by `python bot.py` at startup, or `ipconfig getifaddr en0`). Note that this address can change when you switch networks, and must then be updated in both the EA and the WebRequest list.
   - `BridgeToken`: the exact value of `BRIDGE_TOKEN`
   - `TradeSymbol`: `SYMBOL`
   - `SignalTimeframe`: `TIMEFRAME` (for example `PERIOD_M15` for `M15`)
   - `TradeMagic`: `MAGIC`
   - `HistoryDays`: `HISTORY_DAYS`
   - Leave `AllowLiveTrading` off unless you have deliberately enabled live trading in both the EA and `config.py`.
   - Add the exact `BridgeUrl` host and port to MT5's WebRequest allow list.

5. Start the bot from the project directory:

   ```sh
   source .venv/bin/activate
   python bot.py
   ```

   The bot starts its dashboard at **http://localhost:8765** and waits up to one minute for the EA to connect. Now attach the EA to a chart. If it does not connect within a minute, restart the Python bot. Keep the EA attached and MT5 open while the bot runs.

## Troubleshooting the bridge

Look at MT5's **Toolbox > Experts** tab and at the Python terminal. Each side now says what is wrong:

| Message | Fix |
|---|---|
| `URL not allowed` (error 4014) | Add the exact `BridgeUrl` to Tools > Options > Expert Advisors, tick the checkbox, re-attach the EA |
| `Cannot reach Python` (5200–5203) | Start `python bot.py` first; check the port is 8766; if using a network IP, check it hasn't changed, or switch to `127.0.0.1` |
| `POST /bridge/commands -> HTTP 1003` (snapshots work, commands fail) | MT5 reused a connection Python had already closed. Make sure you run the current `bot.py` (it sends `Connection: close`), then restart the bot. |
| `wrong token` | Copy `BRIDGE_TOKEN` from config.py into the EA's `BridgeToken` input exactly |
| `TradeSymbol ... must match` / `SignalTimeframe` | Make the EA inputs match `SYMBOL` / `TIMEFRAME` in config.py |
| `Could not select symbol` | The symbol name isn't in your broker's Market Watch; fix it in both places |
| No messages at all in Experts | The EA isn't running: check the chart shows the EA's name/hat icon and the Journal tab for compile/load errors |

## How it works

- The EA sends account status, quotes, up to 500 candles, bot-owned positions and recent bot deal history to the loopback-only Python service.
- Python calculates the indicators, signal, risk sizing, and dashboard data.
- For risk sizing, Python asks the EA to calculate the potential loss using MT5's `OrderCalcProfit`.
- Orders and closes are sent back as bridge commands; the EA executes them using MT5's trade API.
- The EA only reports or closes positions with the configured symbol and magic number. Manual trades are not managed by the bot.
- The dashboard is bound to loopback. The separate bridge port accepts only private-network clients with the configured secret token and only serves bridge endpoints. Do not forward port 8766 from your router or share the bridge token.

## Safety notes

- A live account is rejected by default. To allow it, you must deliberately set `ALLOW_LIVE = True` in `config.py` **and** enable `AllowLiveTrading` in the EA inputs.
- Check that the EA and Python configuration agree before enabling the bot. Begin in Recommend mode on a demo account.
- The default strategy is generic and is not a proven trading edge. Trading can lose money; this software is not financial advice.
- Open trades retain their SL/TP in MT5 if Python or the dashboard stops.
- The Claude filter is optional and requires `ANTHROPIC_API_KEY`; it can veto but not create trades.

## Files

- `bot.py`: native Python strategy, risk checks, bridge server and dashboard backend
- `MT5MacBridge.mq5`: MT5 Expert Advisor bridge
- `dashboard.html`: local dashboard
- `config.py`: Python settings
- `requirements.txt`: Python dependencies

Runtime files such as `bot.log`, `state.json` and `settings.json` are created next to the project.
# Trading-Bot
<img width="1440" height="900" alt="Screen Shot 2026-11-25 at 1 03 58 PM" src="https://github.com/user-attachments/assets/3e22b408-60fd-4395-b8e8-68558a75dc6b" />
<img width="1440" height="900" alt="Screen Shot 2026-11-25 at 1 07 56 PM" src="https://github.com/user-attachments/assets/ca7e04e2-fa5f-45f9-af20-2a800094b240" />

