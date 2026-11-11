"""Jembatan MT5: dijalankan oleh Python-Windows (di dalam Wine pada VPS Linux).

Skrip ini SATU-SATUNYA bagian proyek yang membutuhkan library `MetaTrader5`.
Ia terhubung ke terminal MT5 yang sedang berjalan, mengambil candle dan harga
bid/ask, lalu mencetak satu baris JSON berawalan "MT5JSON:" ke stdout. Bot
utama (Python Linux) memanggilnya lewat subprocess dari tools/oanda_client.py.

Jangan impor modul proyek lain di sini: Python embeddable di Wine hanya
memiliki stdlib + MetaTrader5.

Uji manual di VPS:
    WINEPREFIX=~/.mt5wine DISPLAY=:99 wine C:/mt5py/python.exe tools/mt5_bridge.py --timeframe H1 --count 5
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

MARKER = "MT5JSON:"
TIMEFRAMES = {
    "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1", "H2": "TIMEFRAME_H2", "H4": "TIMEFRAME_H4",
    "H6": "TIMEFRAME_H6", "H8": "TIMEFRAME_H8", "H12": "TIMEFRAME_H12",
    "D": "TIMEFRAME_D1",
}


def emit(payload: dict) -> None:
    print(MARKER + json.dumps(payload), flush=True)


def fail(message: str) -> int:
    emit({"status": "error", "error": message})
    return 1


def iso(epoch_seconds) -> str:
    """Waktu MT5 adalah waktu server broker yang dikodekan sebagai epoch."""
    return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def pick_symbol(mt5, wanted: str):
    """Pakai simbol dari MT5_SYMBOL, atau deteksi otomatis simbol emas broker."""
    if wanted:
        return wanted if mt5.symbol_info(wanted) is not None else None
    names = [s.name for s in (mt5.symbols_get(group="*XAU*") or []) if "XAUUSD" in s.name.upper()]
    if not names:
        return None
    if "XAUUSD" in names:
        return "XAUUSD"
    return sorted(names, key=len)[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--count", type=int, default=220)
    args = parser.parse_args()

    timeframe = args.timeframe.upper()
    if timeframe not in TIMEFRAMES:
        return fail(f"Timeframe tidak didukung: {timeframe}")

    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        return fail(f"Library MetaTrader5 belum terpasang di Python-Windows: {exc}")

    init_kwargs = {"timeout": 60000}
    terminal_path = os.environ.get("MT5_TERMINAL_PATH", "").strip()
    if terminal_path:
        init_kwargs["path"] = terminal_path
    login = os.environ.get("MT5_LOGIN", "").strip()
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "").strip()
    if login and password and server:
        try:
            init_kwargs.update(login=int(login), password=password, server=server)
        except ValueError:
            return fail("MT5_LOGIN harus berupa angka.")

    if not mt5.initialize(**init_kwargs):
        return fail(f"Gagal terhubung ke terminal MT5: {mt5.last_error()}. "
                    "Pastikan terminal MT5 berjalan dan sudah login.")
    try:
        terminal = mt5.terminal_info()
        if terminal is None or not terminal.connected:
            return fail("Terminal MT5 berjalan tetapi tidak terkoneksi ke server broker "
                        "(belum login atau koneksi putus).")

        symbol = pick_symbol(mt5, os.environ.get("MT5_SYMBOL", "").strip())
        if symbol is None:
            return fail("Simbol emas (XAUUSD) tidak ditemukan di terminal. "
                        "Isi MT5_SYMBOL di .env sesuai nama di Market Watch.")
        if not mt5.symbol_select(symbol, True):
            return fail(f"Simbol {symbol} tidak bisa ditambahkan ke Market Watch: {mt5.last_error()}")

        tick = None
        rates = None
        for _ in range(10):  # data bisa belum siap sesaat setelah simbol dipilih
            tick = mt5.symbol_info_tick(symbol)
            rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, TIMEFRAMES[timeframe]), 0, args.count)
            if tick is not None and tick.bid > 0 and tick.ask > 0 and rates is not None and len(rates) > 0:
                break
            time.sleep(1)
        else:
            return fail(f"Data {symbol} {timeframe} belum tersedia dari terminal: {mt5.last_error()}")

        candles = []
        last_index = len(rates) - 1
        for index, rate in enumerate(rates):
            candles.append({
                "time": iso(rate["time"]),
                "o": float(rate["open"]),
                "h": float(rate["high"]),
                "l": float(rate["low"]),
                "c": float(rate["close"]),
                # Posisi 0 di MT5 (elemen terakhir) adalah candle yang masih berjalan.
                "complete": index != last_index,
            })

        emit({
            "status": "ok",
            "symbol": symbol,
            "timeframe": timeframe,
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "tick_time": iso(tick.time),
            "candles": candles,
        })
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
