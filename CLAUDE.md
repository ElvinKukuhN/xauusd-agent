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

`main.py` → `config.validate()` → `agent.analyst_agent.run_analysis()` → Gemini calls `get_oanda_xauusd_data` for `"H1"` (trend), `"M15"` (setup) and `"M5"` (trigger), plus `get_macro_news()` → report text → `notifier.telegram_bot.send_telegram_message()`. Every Gemini HTTP request is capped by `GEMINI_TIMEOUT` (seconds, min 10 — the API rejects shorter deadlines); a timeout surfaces as a retryable 504.

`run_analysis` rejects a response (and retries, up to 3 attempts) if the model did not actually call both required tools, checked via `automatic_function_calling_history`. On final failure `main.py` sends an error notice to Telegram instead.

### Contracts that span files

- **Tool functions are the Gemini schema.** The SDK builds function declarations from the signature and docstring of `get_oanda_xauusd_data` and `get_macro_news`. Their names are hard-coded in `agent/prompts.py` and in `REQUIRED_TOOLS` in `agent/analyst_agent.py`. Tools never raise: failures return `{"status": "error", "error": ...}` (or a one-item list with `error` for news) so the model can report them, and the prompt maps any such failure to "WAIT & SEE (NO TRADE)".
- **Result field names are referenced by the prompt.** `SYSTEM_INSTRUCTION` lists every result key (`ema_9_21`, `bollinger_20_2`, `active_fvg_zones`, `liquidity_sweep_check`, `last_5_closed_candles`, `risk_levels`, ...) and their enum values (`BULLISH_PIN_BAR`, `NEAR_UPPER`, `FRESH`, ...). Renaming a key or enum in `oanda_client.py` requires updating `agent/prompts.py`; a field the tool does not return will be invented by the model.
- **Trading rules live in two places.** SL = 1.5 x ATR, TP1 = RR 1:1.5, TP2 = RR 1:3 (`SL_ATR_MULT`, `TP1_RR`, `TP2_RR` in `oanda_client.py`, which precomputes `risk_levels` per timeframe so the model does not do arithmetic) and are also stated in the prompt; the prompt uses the M5 `risk_levels`. The spread threshold (0.50), the H1 → M15 → M5 hierarchy and the "no BUY/SELL without an M5 trigger" rule are prompt-only.
- **Report format is Telegram HTML.** The prompt restricts output to `<b>`/`<i>` and forbids Markdown and stray `<`/`>`. `telegram_bot.py` sends with `parse_mode=HTML` and, on HTTP 400, resends that chunk as plain text with tags stripped. Messages are split at 4000 chars on line boundaries.

### Indicator rules

Pure Python in `oanda_client.py` — do not introduce pandas, numpy or ta-lib on the Linux side (low-RAM constraint from the original spec).

- Indicators use only candles with `complete == True` (no repaint). The bridge marks the last returned bar (MT5 position 0) as incomplete; live bid/ask come from the tick, not from candles.
- EMA: SMA seed then `k = 2/(period+1)`. RSI and ATR: Wilder smoothing. Verified against the standard Wilder RSI reference series (70.46 → 57.92).
- Structure detectors (all on complete candles): Bollinger 20/2 with population std-dev; 3-candle FVG over the last 50 bars, ignoring gaps under 0.1 x ATR and dropping zones a later candle has fully traded through (touched = `PARTIAL`); liquidity sweep = one of the last 3 candles wicks past the prior 20-bar extreme and closes back inside; candle patterns are rule-based (pin bar: dominant wick ≥ 2× body and ≥ 60% of range, opposite wick ≤ 25%; engulfing: opposite-direction body fully covering the previous body; doji: body ≤ 10% of range).
- `CANDLE_COUNT = 220` gives EMA 200 only ~19 bars of warm-up after its seed, so it can differ slightly from charting platforms; raising the count fixes that.
- MT5 candles are bid-based and timestamps are broker server time (output keys are suffixed `_broker`).

### n8n variant

An n8n workflow ("XAU/USD Top-Down Report (H1-M15-M5)") can replace the cron + `main.py` path. n8n cannot reach MT5, so `tools/market_api.py` (stdlib `http.server`, systemd unit `xauusd-market-api`) serves `GET /snapshot` = H1/M15/M5 from `get_oanda_xauusd_data` plus `get_macro_news`, guarded by the `X-API-Key` header (`MARKET_API_TOKEN`, ≥24 chars; `config.validate_market_api()`). The server is single-threaded on purpose so MT5 is never hit concurrently. In n8n the data is fetched *before* the LLM and pasted into the prompt (no tool calling), so the workflow's system prompt is a copy of `SYSTEM_INSTRUCTION` with the tool-calling steps replaced — prompt edits must be made in both places. Run only one of cron or n8n, or reports are sent twice.

### Docker variant

`Dockerfile` + `docker-compose.yml` + `docker/entrypoint.sh` run MT5 and `market_api.py` in one container (`xauusd-market-api`); the bot spawns the bridge via Wine, so MT5 and the Python side cannot be split into separate containers. The entrypoint repeats `setup_vps.sh`'s install steps (Wine prefix, MT5, embeddable Python + `MetaTrader5`) on first start into the `/data` volume, so the MT5 login survives rebuilds — keep the two install paths in sync. Compose `environment` overrides `.env` for `WINEPREFIX`, `MT5_PYTHON_CMD`, `MT5_DISPLAY`, `MARKET_API_HOST/PORT` (an empty `WINEPREFIX=` from `env_file` would otherwise beat the image `ENV`). It joins the external network `stack_default` so n8n reaches `http://xauusd-market-api:8787`; VNC and the API are published on `127.0.0.1` only. Steps in `DOCKER.md`.

### News

`get_macro_news` takes items round-robin across the RSS feeds in `RSS_FEEDS` (newest first within each), then sorts the selected 8 by date. A plain "latest 8 overall" lets the busiest feed fill every slot.

### Config and secrets

`config.py` loads `.env` at import and exposes module-level constants; `validate()` is called only from `main.py`. An env var that is present but empty falls back to its default. MT5 login/password/server are optional but all-or-nothing. The Telegram bot token is part of the request URL, so `telegram_bot.py` logs only exception class names and `main.py` raises HTTP-library loggers to WARNING — keep it that way.

### VPS runtime

`setup_vps.sh` writes two systemd units: `mt5-xvfb` (virtual display `:99`) and `mt5-terminal` (Wine `terminal64.exe`, `Restart=always`). The Wine prefix defaults to `~/.mt5wine`. The first MT5 login is manual over VNC (`x11vnc -localhost` plus an SSH tunnel); the script prints the steps. The script must keep LF line endings.
