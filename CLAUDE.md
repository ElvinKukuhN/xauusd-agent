# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An hourly, cron-driven agent that analyses Spot Gold (XAU/USD) and sends a trade-scenario report to Telegram. Gemini (`google-genai`, automatic function calling) calls two Python tools — market data and macro news — then writes the report. It does not place trades. Code comments, prompts, log messages and the Telegram report are in Indonesian; keep that.

## Commands

Target is an Ubuntu VPS; development happens on Windows. There is no test suite, linter, or build step.

```bash
./setup_vps.sh                      # VPS: Wine + Xvfb + MT5 + Windows-Python + systemd services + venv (idempotent)
./setup_vps.sh --install-cron       # same, plus installs the cron line (minute 1, hourly, Mon-Fri)

venv/bin/python -m tools.oanda_client H1   # fetch + compute indicators for one timeframe, print JSON
venv/bin/python main.py --dry-run          # full agent run, print report, no Telegram (Telegram vars not required)
venv/bin/python main.py                    # full run, send to Telegram

# Bridge alone (inside Wine), bypassing the Linux side:
WINEPREFIX=~/.mt5wine DISPLAY=:99 wine C:/mt5py/python.exe tools/mt5_bridge.py --timeframe H1 --count 5

systemctl status mt5-terminal       # the MT5 terminal must be running and logged in
```

Run everything from the project root: modules use top-level imports (`import config`, `from tools...`) and the bridge is launched by relative path with `cwd=BASE_DIR`.

`main.py` exit codes: 0 ok, 1 analysis or Telegram failure, 2 invalid `.env`.

## Architecture

### Two Python interpreters

The OANDA account is MT5-only (no v20 REST API), so market data comes from an MT5 terminal running under Wine on the VPS.

- **Linux venv Python** runs the bot (`main.py`, `agent/`, `notifier/`, `tools/oanda_client.py`, `tools/news_scraper.py`). Dependencies are only `requests`, `python-dotenv`, `google-genai`.
- **Windows Python inside Wine** (`C:/mt5py/python.exe`, embeddable 3.10) runs only `tools/mt5_bridge.py`. It is the only place the `MetaTrader5` package (and its numpy dependency) exists.

`tools/oanda_client.py::_fetch_from_mt5` spawns the bridge as a subprocess (`MT5_PYTHON_CMD` + `tools/mt5_bridge.py`), passing `WINEPREFIX`/`DISPLAY` and the `MT5_*` credentials through the environment. The bridge prints one stdout line prefixed `MT5JSON:`; the parent takes the last such line and ignores Wine noise. Both sides share the `BRIDGE_MARKER`/`MARKER` constant and the candle JSON shape (`time,o,h,l,c,complete`) — change them together.

Consequences:
- `tools/mt5_bridge.py` must stay standalone: stdlib + `MetaTrader5` only, Python 3.10 syntax, no imports from this project (the embeddable Python has no project path and no `python-dotenv`).
- On Windows (`os.name == "nt"`) the Wine env vars are skipped and `MT5_PYTHON_CMD` defaults to `python`, so the same code runs against a native MT5 desktop terminal.
- `MT5_PYTHON_CMD` is parsed with `shlex`; use forward slashes in Windows paths.

### Request flow

`main.py` → `config.validate()` → `agent.analyst_agent.run_analysis()` → Gemini calls `get_oanda_xauusd_data("H4")`, `get_oanda_xauusd_data("H1")`, `get_macro_news()` → report text → `notifier.telegram_bot.send_telegram_message()`.

`run_analysis` rejects a response (and retries, up to 3 attempts) if the model did not actually call both required tools, checked via `automatic_function_calling_history`. On final failure `main.py` sends an error notice to Telegram instead.

### Contracts that span files

- **Tool functions are the Gemini schema.** The SDK builds function declarations from the signature and docstring of `get_oanda_xauusd_data` and `get_macro_news`. Their names are hard-coded in `agent/prompts.py` and in `REQUIRED_TOOLS` in `agent/analyst_agent.py`. Tools never raise: failures return `{"status": "error", "error": ...}` (or a one-item list with `error` for news) so the model can report them, and the prompt maps any such failure to "WAIT & SEE (NO TRADE)".
- **Result field names are referenced by the prompt.** `SYSTEM_INSTRUCTION` names `spread_dollars`, `trend_bias`, `risk_levels`, `live_bid`/`live_ask`, `support_24_bars`, etc. Renaming a key in `oanda_client.py` requires updating `agent/prompts.py`.
- **Trading rules live in two places.** The ATR multipliers (SL 1.5, TP1 1.5, TP2 3.0) are constants in `oanda_client.py` (which precomputes `risk_levels` so the model does not do arithmetic) and are also stated in the prompt. The spread threshold (0.80), RSI extremes (75/25) and the H4+H1+news confluence rule are prompt-only.
- **Report format is Telegram HTML.** The prompt restricts output to `<b>`/`<i>` and forbids Markdown and stray `<`/`>`. `telegram_bot.py` sends with `parse_mode=HTML` and, on HTTP 400, resends that chunk as plain text with tags stripped. Messages are split at 4000 chars on line boundaries.

### Indicator rules

Pure Python in `oanda_client.py` — do not introduce pandas, numpy or ta-lib on the Linux side (low-RAM constraint from the original spec).

- Indicators use only candles with `complete == True` (no repaint). The bridge marks the last returned bar (MT5 position 0) as incomplete; live bid/ask come from the tick, not from candles.
- EMA: SMA seed then `k = 2/(period+1)`. RSI and ATR: Wilder smoothing. Verified against the standard Wilder RSI reference series (70.46 → 57.92).
- `CANDLE_COUNT = 220` gives EMA 200 only ~19 bars of warm-up after its seed, so it can differ slightly from charting platforms; raising the count fixes that.
- MT5 candles are bid-based and timestamps are broker server time (output keys are suffixed `_broker`).

### News

`get_macro_news` takes items round-robin across the RSS feeds in `RSS_FEEDS` (newest first within each), then sorts the selected 8 by date. A plain "latest 8 overall" lets the busiest feed fill every slot.

### Config and secrets

`config.py` loads `.env` at import and exposes module-level constants; `validate()` is called only from `main.py`. An env var that is present but empty falls back to its default. MT5 login/password/server are optional but all-or-nothing. The Telegram bot token is part of the request URL, so `telegram_bot.py` logs only exception class names and `main.py` raises HTTP-library loggers to WARNING — keep it that way.

### VPS runtime

`setup_vps.sh` writes two systemd units: `mt5-xvfb` (virtual display `:99`) and `mt5-terminal` (Wine `terminal64.exe`, `Restart=always`). The Wine prefix defaults to `~/.mt5wine`. The first MT5 login is manual over VNC (`x11vnc -localhost` plus an SSH tunnel); the script prints the steps. The script must keep LF line endings.
