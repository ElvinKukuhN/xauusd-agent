"""Tool data XAU/USD OANDA (via terminal MT5) + kalkulasi indikator dengan Python murni.

Akun OANDA MT5 tidak memiliki REST API, sehingga data diambil dari terminal
MT5 yang berjalan di VPS (di bawah Wine) melalui skrip tools/mt5_bridge.py.
Modul ini sendiri tetap Python murni: ia hanya menjalankan jembatan tersebut
sebagai subprocess, lalu menghitung indikator dari candle yang dikembalikan.

Uji manual:
    python -m tools.oanda_client H1
"""

import json
import logging
import math
import os
import shlex
import subprocess
import sys

import config

logger = logging.getLogger(__name__)

CANDLE_COUNT = 220
VALID_GRANULARITIES = {"M5", "M15", "M30", "H1", "H2", "H4", "H6", "H8", "H12", "D"}
BRIDGE_SCRIPT = "tools/mt5_bridge.py"
BRIDGE_MARKER = "MT5JSON:"

EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
SR_LOOKBACK = 24

SL_ATR_MULT = 1.5
TP1_ATR_MULT = 1.5
TP2_ATR_MULT = 3.0


# ---------------------------------------------------------------------------
# Kalkulasi indikator (Python murni)
# ---------------------------------------------------------------------------
def calculate_ema(values: list, period: int):
    """EMA standar: seed = SMA(period) pertama, lalu smoothing k = 2 / (period + 1)."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = math.fsum(values[:period]) / period
    for price in values[period:]:
        ema = (price - ema) * k + ema
    return ema


def calculate_rsi(closes: list, period: int = RSI_PERIOD):
    """RSI dengan Wilder's smoothing (identik dengan RSI bawaan TradingView/MT5)."""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = math.fsum(gains[:period]) / period
    avg_loss = math.fsum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_atr(highs: list, lows: list, closes: list, period: int = ATR_PERIOD):
    """ATR: True Range + Wilder's smoothing."""
    if len(closes) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(closes)):
        prev_close = closes[i - 1]
        true_ranges.append(
            max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close))
        )

    atr = math.fsum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _round(value, digits: int = 3):
    return None if value is None else round(value, digits)


def _trend_bias(close: float, ema_fast, ema_slow) -> str:
    if ema_fast is None or ema_slow is None:
        return "UNKNOWN"
    if close > ema_fast > ema_slow:
        return "BULLISH"
    if close < ema_fast < ema_slow:
        return "BEARISH"
    return "MIXED"


def _risk_levels(live_bid: float, live_ask: float, atr) -> dict:
    """Level SL/TP referensi berbasis ATR (BUY masuk di ask, SELL masuk di bid)."""
    if atr is None:
        return {}
    sl = SL_ATR_MULT * atr
    tp1 = TP1_ATR_MULT * atr
    tp2 = TP2_ATR_MULT * atr
    return {
        "sl_distance": _round(sl, 2),
        "tp1_distance": _round(tp1, 2),
        "tp2_distance": _round(tp2, 2),
        "buy": {
            "entry": _round(live_ask, 2),
            "stop_loss": _round(live_ask - sl, 2),
            "tp1": _round(live_ask + tp1, 2),
            "tp2": _round(live_ask + tp2, 2),
        },
        "sell": {
            "entry": _round(live_bid, 2),
            "stop_loss": _round(live_bid + sl, 2),
            "tp1": _round(live_bid - tp1, 2),
            "tp2": _round(live_bid - tp2, 2),
        },
    }


def _error(message: str, granularity: str) -> dict:
    logger.error("MT5 [%s]: %s", granularity, message)
    return {"status": "error", "instrument": config.INSTRUMENT_LABEL,
            "granularity": granularity, "error": message}


# ---------------------------------------------------------------------------
# Pengambilan data dari terminal MT5 (subprocess ke jembatan Wine)
# ---------------------------------------------------------------------------
def _fetch_from_mt5(granularity: str) -> dict:
    """Jalankan tools/mt5_bridge.py dan kembalikan payload JSON-nya.

    Selalu mengembalikan dict dengan "status": "ok" atau "error".
    """
    is_windows = os.name == "nt"
    command = shlex.split(config.MT5_PYTHON_CMD, posix=not is_windows) + [
        BRIDGE_SCRIPT, "--timeframe", granularity, "--count", str(CANDLE_COUNT),
    ]
    env = os.environ.copy()  # MT5_LOGIN/MT5_PASSWORD/MT5_SERVER/MT5_SYMBOL sudah dimuat dari .env
    if not is_windows:
        env["WINEPREFIX"] = config.WINEPREFIX
        env["DISPLAY"] = config.MT5_DISPLAY
        env.setdefault("WINEDEBUG", "-all")

    try:
        proc = subprocess.run(
            command, cwd=str(config.BASE_DIR), env=env, capture_output=True,
            text=True, errors="replace", timeout=config.MT5_BRIDGE_TIMEOUT,
        )
    except FileNotFoundError:
        return {"status": "error",
                "error": f"Perintah tidak ditemukan: {command[0]}. Periksa MT5_PYTHON_CMD di .env "
                         "(Wine/Python-Windows belum terpasang?)."}
    except subprocess.TimeoutExpired:
        return {"status": "error",
                "error": f"Jembatan MT5 tidak merespons dalam {config.MT5_BRIDGE_TIMEOUT} detik. "
                         "Periksa service mt5-terminal."}

    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(BRIDGE_MARKER):
            try:
                return json.loads(line[len(BRIDGE_MARKER):])
            except ValueError:
                break

    stderr_tail = (proc.stderr or "").strip()[-300:]
    return {"status": "error",
            "error": f"Jembatan MT5 gagal (exit code {proc.returncode}). {stderr_tail}".strip()}


# ---------------------------------------------------------------------------
# Tool untuk Gemini Function Calling
# ---------------------------------------------------------------------------
def get_oanda_xauusd_data(granularity: str = "H1") -> dict:
    """Mengambil data harga live dan indikator teknikal Spot Gold (XAU/USD) dari OANDA.

    Gunakan tool ini untuk mendapatkan harga bid/ask terkini, spread, serta
    indikator EMA 50, EMA 200, RSI 14, ATR 14, dan level support/resistance
    pada satu timeframe. Semua indikator dihitung hanya dari candle yang sudah
    selesai (complete) sehingga tidak repaint. Panggil sekali per timeframe.

    Args:
        granularity: Timeframe candle. Gunakan "H4" untuk tren utama dan
            "H1" untuk setup entry. Nilai valid: "M5", "M15", "M30", "H1", "H2",
            "H4", "H6", "H8", "H12", "D".

    Returns:
        Dictionary. Jika berhasil: status="ok", live_bid, live_ask,
        spread_dollars, last_closed_candle_time, last_close, ema_50, ema_200,
        rsi_14, atr_14, support_24_bars, resistance_24_bars, trend_bias
        (BULLISH/BEARISH/MIXED/UNKNOWN), dan risk_levels (SL 1.5xATR, TP1
        1.5xATR, TP2 3.0xATR untuk skenario buy dan sell). Jika gagal:
        status="error" dan field error berisi penjelasan.
    """
    granularity = (granularity or "H1").strip().upper()
    if granularity not in VALID_GRANULARITIES:
        return _error(
            f"Granularity tidak valid: {granularity}. Pilihan: {sorted(VALID_GRANULARITIES)}",
            granularity,
        )

    payload = _fetch_from_mt5(granularity)
    if payload.get("status") != "ok":
        return _error(payload.get("error", "Kesalahan tidak dikenal dari jembatan MT5."), granularity)

    candles = payload.get("candles") or []
    if not candles:
        return _error("Terminal MT5 tidak mengembalikan candle.", granularity)

    try:
        live_bid = float(payload["bid"])
        live_ask = float(payload["ask"])

        # Indikator hanya dari candle complete agar tidak repaint.
        closed = [c for c in candles if c.get("complete") is True]
        highs = [float(c["h"]) for c in closed]
        lows = [float(c["l"]) for c in closed]
        closes = [float(c["c"]) for c in closed]
    except (KeyError, TypeError, ValueError) as exc:
        return _error(f"Format data dari jembatan MT5 tidak sesuai: {exc!r}", granularity)

    if len(closes) < ATR_PERIOD + 1:
        return _error(
            f"Candle complete tidak cukup ({len(closes)}) untuk menghitung indikator.", granularity
        )

    ema_50 = calculate_ema(closes, EMA_FAST)
    ema_200 = calculate_ema(closes, EMA_SLOW)
    rsi_14 = calculate_rsi(closes, RSI_PERIOD)
    atr_14 = calculate_atr(highs, lows, closes, ATR_PERIOD)
    last_close = closes[-1]

    result = {
        "status": "ok",
        "instrument": config.INSTRUMENT_LABEL,
        "broker_symbol": payload.get("symbol"),
        "granularity": granularity,
        "live_bid": _round(live_bid),
        "live_ask": _round(live_ask),
        "spread_dollars": _round(live_ask - live_bid),
        "live_tick_time_broker": payload.get("tick_time"),
        "last_closed_candle_time_broker": closed[-1].get("time"),
        "completed_candles_used": len(closes),
        "last_close": _round(last_close),
        "ema_50": _round(ema_50),
        "ema_200": _round(ema_200),
        "rsi_14": _round(rsi_14, 2),
        "atr_14": _round(atr_14),
        "support_24_bars": _round(min(lows[-SR_LOOKBACK:])),
        "resistance_24_bars": _round(max(highs[-SR_LOOKBACK:])),
        "trend_bias": _trend_bias(last_close, ema_50, ema_200),
        "risk_levels": _risk_levels(live_bid, live_ask, atr_14),
    }
    logger.info(
        "MT5 [%s %s] bid=%s ask=%s spread=%s ema50=%s ema200=%s rsi=%s atr=%s bias=%s",
        result["broker_symbol"], granularity, result["live_bid"], result["live_ask"],
        result["spread_dollars"], result["ema_50"], result["ema_200"], result["rsi_14"],
        result["atr_14"], result["trend_bias"],
    )
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(get_oanda_xauusd_data(sys.argv[1] if len(sys.argv) > 1 else "H1"), indent=2))
