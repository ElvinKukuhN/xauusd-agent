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
EMA_MICRO_FAST = 9
EMA_MICRO_SLOW = 21
RSI_PERIOD = 14
RSI_FAST_PERIOD = 9
ATR_PERIOD = 14
SR_LOOKBACK = 24

BB_PERIOD = 20
BB_STDDEV = 2.0
BB_NEAR_PCT = 10.0          # dalam 10% lebar band dari batas atas/bawah = "dekat band"

FVG_LOOKBACK = 50           # cari FVG pada 50 candle complete terakhir
FVG_MIN_ATR = 0.1           # abaikan gap lebih kecil dari 0.1 x ATR (noise)
FVG_MAX_ZONES = 3

SWEEP_LOOKBACK = 20         # swing high/low = ekstrem 20 candle sebelum candle yang diuji
SWEEP_CHECK_BARS = 3        # uji 3 candle complete terakhir
EMA_CROSS_LOOKBACK = 10
LAST_CANDLES = 5

# SL = 1.5 x ATR; TP dinyatakan sebagai kelipatan jarak SL (Risk:Reward).
SL_ATR_MULT = 1.5
TP1_RR = 1.5
TP2_RR = 3.0


# ---------------------------------------------------------------------------
# Kalkulasi indikator (Python murni)
# ---------------------------------------------------------------------------
def calculate_ema_series(values: list, period: int) -> list:
    """Deret EMA sejajar dengan `values` (None sebelum seed).

    Seed = SMA(period) pertama, lalu smoothing k = 2 / (period + 1).
    """
    series = [None] * len(values)
    if len(values) < period:
        return series
    k = 2.0 / (period + 1)
    ema = math.fsum(values[:period]) / period
    series[period - 1] = ema
    for i in range(period, len(values)):
        ema = (values[i] - ema) * k + ema
        series[i] = ema
    return series


def calculate_ema(values: list, period: int):
    """Nilai EMA terakhir (None jika data kurang dari `period`)."""
    series = calculate_ema_series(values, period)
    return series[-1] if series else None


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


def calculate_bollinger(closes: list, period: int = BB_PERIOD, mult: float = BB_STDDEV):
    """Bollinger Bands: SMA(period) +/- mult x standar deviasi populasi."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = math.fsum(window) / period
    std = math.sqrt(math.fsum((x - middle) ** 2 for x in window) / period)
    upper, lower = middle + mult * std, middle - mult * std
    close = closes[-1]
    width = upper - lower
    position_pct = (close - lower) / width * 100.0 if width > 0 else 50.0
    if close > upper:
        position = "ABOVE_UPPER"
    elif close < lower:
        position = "BELOW_LOWER"
    elif position_pct >= 100.0 - BB_NEAR_PCT:
        position = "NEAR_UPPER"
    elif position_pct <= BB_NEAR_PCT:
        position = "NEAR_LOWER"
    elif position_pct >= 50.0:
        position = "INSIDE_UPPER_HALF"
    else:
        position = "INSIDE_LOWER_HALF"
    return {
        "upper": _round(upper), "middle": _round(middle), "lower": _round(lower),
        "position_pct": _round(position_pct, 1), "position": position,
    }


def _ema_cross_state(closes: list) -> dict:
    """Posisi EMA 9 vs EMA 21 dan persilangan terakhir dalam EMA_CROSS_LOOKBACK candle."""
    fast = calculate_ema_series(closes, EMA_MICRO_FAST)
    slow = calculate_ema_series(closes, EMA_MICRO_SLOW)
    if fast[-1] is None or slow[-1] is None:
        return {"state": "UNKNOWN", "last_cross": None, "cross_bars_ago": None}
    state = "EMA9_ABOVE_EMA21" if fast[-1] > slow[-1] else "EMA9_BELOW_EMA21"
    n = len(closes)
    for bars_ago in range(0, min(EMA_CROSS_LOOKBACK, n - 1)):
        i = n - 1 - bars_ago
        if None in (fast[i], slow[i], fast[i - 1], slow[i - 1]):
            break
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            return {"state": state, "last_cross": "BULLISH_CROSS", "cross_bars_ago": bars_ago}
        if fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            return {"state": state, "last_cross": "BEARISH_CROSS", "cross_bars_ago": bars_ago}
    return {"state": state, "last_cross": None, "cross_bars_ago": None}


def find_active_fvg(times: list, highs: list, lows: list, closes: list, atr) -> list:
    """Fair Value Gap 3-candle yang belum tertutup penuh, terbaru lebih dulu.

    Bullish FVG: low[i] > high[i-2] -> zona [high[i-2], low[i]].
    Bearish FVG: high[i] < low[i-2] -> zona [high[i], low[i-2]].
    Tertutup penuh (dibuang) jika candle sesudahnya menembus seluruh zona;
    tersentuh sebagian -> status PARTIAL, belum tersentuh -> FRESH.
    """
    n = len(closes)
    min_size = (atr or 0.0) * FVG_MIN_ATR
    zones = []
    for i in range(max(2, n - FVG_LOOKBACK), n):
        if lows[i] > highs[i - 2]:
            kind, bottom, top = "BULLISH", highs[i - 2], lows[i]
        elif highs[i] < lows[i - 2]:
            kind, bottom, top = "BEARISH", highs[i], lows[i - 2]
        else:
            continue
        if top - bottom < min_size:
            continue
        later_lows, later_highs = lows[i + 1:], highs[i + 1:]
        if kind == "BULLISH":
            if later_lows and min(later_lows) <= bottom:
                continue
            touched = bool(later_lows) and min(later_lows) < top
        else:
            if later_highs and max(later_highs) >= top:
                continue
            touched = bool(later_highs) and max(later_highs) > bottom
        close = closes[-1]
        price_vs_zone = "ABOVE" if close > top else "BELOW" if close < bottom else "INSIDE"
        zones.append({
            "type": kind,
            "top": _round(top), "bottom": _round(bottom),
            "status": "PARTIAL" if touched else "FRESH",
            "formed_at_broker": times[i - 1],
            "bars_ago": n - 1 - i,
            "price_vs_zone": price_vs_zone,
        })
    zones.reverse()
    return zones[:FVG_MAX_ZONES]


def liquidity_sweep_check(times: list, highs: list, lows: list, closes: list) -> dict:
    """Deteksi sapuan likuiditas pada SWEEP_CHECK_BARS candle complete terakhir.

    Bullish sweep: low menembus swing low SWEEP_LOOKBACK candle sebelumnya,
    tetapi candle ditutup kembali di atas swing low itu (stop sell disapu).
    Bearish sweep: kebalikannya pada swing high.
    """
    n = len(closes)
    result = {"lookback_bars": SWEEP_LOOKBACK, "checked_last_bars": SWEEP_CHECK_BARS,
              "bullish_sweep": None, "bearish_sweep": None}
    for bars_ago in range(SWEEP_CHECK_BARS):
        i = n - 1 - bars_ago
        if i - SWEEP_LOOKBACK < 0:
            break
        swing_low = min(lows[i - SWEEP_LOOKBACK:i])
        swing_high = max(highs[i - SWEEP_LOOKBACK:i])
        if result["bullish_sweep"] is None and lows[i] < swing_low < closes[i]:
            result["bullish_sweep"] = {
                "swept_level": _round(swing_low), "wick_low": _round(lows[i]),
                "close": _round(closes[i]), "candle_time_broker": times[i], "bars_ago": bars_ago,
            }
        if result["bearish_sweep"] is None and highs[i] > swing_high > closes[i]:
            result["bearish_sweep"] = {
                "swept_level": _round(swing_high), "wick_high": _round(highs[i]),
                "close": _round(closes[i]), "candle_time_broker": times[i], "bars_ago": bars_ago,
            }
    return result


def _candle_pattern(o: float, h: float, l: float, c: float, prev) -> str:
    """Pola satu candle: ENGULFING, PIN_BAR, DOJI, atau NONE."""
    rng = h - l
    if rng <= 0:
        return "NONE"
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if prev is not None:
        po, pc = prev
        prev_body = abs(pc - po)
        if prev_body > 0 and body > prev_body:
            if pc < po and c > o and o <= pc and c >= po:
                return "BULLISH_ENGULFING"
            if pc > po and c < o and o >= pc and c <= po:
                return "BEARISH_ENGULFING"
    if lower_wick >= 2 * body and lower_wick >= 0.6 * rng and upper_wick <= 0.25 * rng:
        return "BULLISH_PIN_BAR"
    if upper_wick >= 2 * body and upper_wick >= 0.6 * rng and lower_wick <= 0.25 * rng:
        return "BEARISH_PIN_BAR"
    if body <= 0.1 * rng:
        return "DOJI"
    return "NONE"


def last_closed_candles(times: list, opens: list, highs: list, lows: list, closes: list,
                        count: int = LAST_CANDLES) -> list:
    """`count` candle complete terakhir (lama -> baru) beserta anatomi dan pola."""
    out = []
    n = len(closes)
    for i in range(max(0, n - count), n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        prev = (opens[i - 1], closes[i - 1]) if i > 0 else None
        out.append({
            "time_broker": times[i],
            "o": _round(o), "h": _round(h), "l": _round(l), "c": _round(c),
            "direction": "BULLISH" if c > o else "BEARISH" if c < o else "FLAT",
            "body": _round(abs(c - o)),
            "upper_wick": _round(h - max(o, c)),
            "lower_wick": _round(min(o, c) - l),
            "pattern": _candle_pattern(o, h, l, c, prev),
        })
    return out


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
    tp1 = TP1_RR * sl
    tp2 = TP2_RR * sl
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

    Gunakan tool ini untuk mendapatkan harga bid/ask terkini, spread, indikator
    teknikal, dan struktur harga pada satu timeframe. Semua indikator dihitung
    hanya dari candle yang sudah selesai (complete) sehingga tidak repaint.
    Panggil sekali per timeframe.

    Args:
        granularity: Timeframe candle. Gunakan "H1" untuk tren utama, "M15"
            untuk struktur setup, dan "M5" untuk trigger eksekusi. Nilai valid:
            "M5", "M15", "M30", "H1", "H2", "H4", "H6", "H8", "H12", "D".

    Returns:
        Dictionary. Jika berhasil: status="ok", live_bid, live_ask,
        spread_dollars, last_close, ema_9, ema_21, ema_50, ema_200,
        ema_9_21 (state dan persilangan terakhir), rsi_14, rsi_9, atr_14,
        bollinger_20_2 (upper/middle/lower/position), support_24_bars,
        resistance_24_bars, trend_bias (BULLISH/BEARISH/MIXED/UNKNOWN),
        active_fvg_zones (Fair Value Gap yang belum tertutup),
        liquidity_sweep_check (sapuan swing high/low pada 3 candle terakhir),
        last_5_closed_candles (OHLC, wick, dan pola Pin Bar/Engulfing/Doji),
        dan risk_levels (SL 1.5xATR, TP1 RR 1:1.5, TP2 RR 1:3 untuk buy dan
        sell). Jika gagal: status="error" dan field error berisi penjelasan.
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
        times = [c.get("time") for c in closed]
        opens = [float(c["o"]) for c in closed]
        highs = [float(c["h"]) for c in closed]
        lows = [float(c["l"]) for c in closed]
        closes = [float(c["c"]) for c in closed]
    except (KeyError, TypeError, ValueError) as exc:
        return _error(f"Format data dari jembatan MT5 tidak sesuai: {exc!r}", granularity)

    if len(closes) < ATR_PERIOD + 1:
        return _error(
            f"Candle complete tidak cukup ({len(closes)}) untuk menghitung indikator.", granularity
        )

    ema_9 = calculate_ema(closes, EMA_MICRO_FAST)
    ema_21 = calculate_ema(closes, EMA_MICRO_SLOW)
    ema_50 = calculate_ema(closes, EMA_FAST)
    ema_200 = calculate_ema(closes, EMA_SLOW)
    rsi_14 = calculate_rsi(closes, RSI_PERIOD)
    rsi_9 = calculate_rsi(closes, RSI_FAST_PERIOD)
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
        "ema_9": _round(ema_9),
        "ema_21": _round(ema_21),
        "ema_50": _round(ema_50),
        "ema_200": _round(ema_200),
        "ema_9_21": _ema_cross_state(closes),
        "rsi_14": _round(rsi_14, 2),
        "rsi_9": _round(rsi_9, 2),
        "atr_14": _round(atr_14),
        "bollinger_20_2": calculate_bollinger(closes),
        "support_24_bars": _round(min(lows[-SR_LOOKBACK:])),
        "resistance_24_bars": _round(max(highs[-SR_LOOKBACK:])),
        "trend_bias": _trend_bias(last_close, ema_50, ema_200),
        "active_fvg_zones": find_active_fvg(times, highs, lows, closes, atr_14),
        "liquidity_sweep_check": liquidity_sweep_check(times, highs, lows, closes),
        "last_5_closed_candles": last_closed_candles(times, opens, highs, lows, closes),
        "risk_levels": _risk_levels(live_bid, live_ask, atr_14),
    }
    sweep = result["liquidity_sweep_check"]
    logger.info(
        "MT5 [%s %s] bid=%s ask=%s spread=%s ema9/21=%s/%s ema50/200=%s/%s rsi14/9=%s/%s "
        "atr=%s bias=%s fvg=%d sweep=%s/%s",
        result["broker_symbol"], granularity, result["live_bid"], result["live_ask"],
        result["spread_dollars"], result["ema_9"], result["ema_21"], result["ema_50"],
        result["ema_200"], result["rsi_14"], result["rsi_9"], result["atr_14"],
        result["trend_bias"], len(result["active_fvg_zones"]),
        "BULL" if sweep["bullish_sweep"] else "-", "BEAR" if sweep["bearish_sweep"] else "-",
    )
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(get_oanda_xauusd_data(sys.argv[1] if len(sys.argv) > 1 else "H1"), indent=2))
