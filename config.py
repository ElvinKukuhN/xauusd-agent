"""Memuat dan memvalidasi environment variables untuk oanda-xauusd-agent."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(Exception):
    """Dilempar ketika konfigurasi environment tidak lengkap atau tidak valid."""


def _get(name: str, default: str = "") -> str:
    # Variabel yang ada tetapi kosong (mis. "WINEPREFIX=") diperlakukan seperti tidak diisi.
    return (os.getenv(name) or "").strip() or default


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} harus berupa angka bulat, ditemukan: {raw!r}") from exc


# --- MT5 (akun OANDA) ---
INSTRUMENT_LABEL = "XAU_USD"
# Perintah untuk menjalankan Python-Windows yang memiliki library MetaTrader5.
# Linux + Wine: "wine C:/mt5py/python.exe" (pakai garis miring, bukan backslash).
# Windows asli : "python".
MT5_PYTHON_CMD = _get("MT5_PYTHON_CMD", "python" if os.name == "nt" else "wine C:/mt5py/python.exe")
WINEPREFIX = _get("WINEPREFIX", str(Path.home() / ".mt5wine"))
MT5_DISPLAY = _get("MT5_DISPLAY", ":99")
MT5_BRIDGE_TIMEOUT = _get_int("MT5_BRIDGE_TIMEOUT", 120)
# Opsional: dibaca langsung oleh tools/mt5_bridge.py dari environment.
MT5_LOGIN = _get("MT5_LOGIN")
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = _get("MT5_SERVER")
MT5_SYMBOL = _get("MT5_SYMBOL")

# --- Gemini ---
GEMINI_API_KEY = _get("GEMINI_API_KEY")
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TEMPERATURE = 0.1
# Batas waktu (detik) per request HTTP ke Gemini; tanpa ini request yang macet menggantung selamanya.
GEMINI_TIMEOUT = _get_int("GEMINI_TIMEOUT", 180)

# --- Telegram ---
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID")

# --- Market API untuk n8n (tools/market_api.py) ---
# 127.0.0.1 jika n8n terpasang langsung di VPS; alamat bridge Docker (mis. 172.17.0.1) jika n8n di Docker.
MARKET_API_HOST = _get("MARKET_API_HOST", "127.0.0.1")
MARKET_API_PORT = _get_int("MARKET_API_PORT", 8787)
MARKET_API_TOKEN = _get("MARKET_API_TOKEN")

# --- Umum ---
HTTP_TIMEOUT = _get_int("HTTP_TIMEOUT", 15)
LOG_LEVEL = _get("LOG_LEVEL", "INFO").upper()
LOG_DIR = BASE_DIR / "logs"

_REQUIRED = {
    "GEMINI_API_KEY": GEMINI_API_KEY,
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
}


def validate(require_telegram: bool = True) -> None:
    """Validasi seluruh environment variable wajib.

    Args:
        require_telegram: False untuk mode dry-run (tanpa kirim Telegram).

    Raises:
        ConfigError: jika ada variabel yang kosong, masih placeholder, atau tidak valid.
    """
    problems = []
    for name, value in _REQUIRED.items():
        if not require_telegram and name.startswith("TELEGRAM_"):
            continue
        if not value or value.lower().startswith("isi_"):
            problems.append(f"{name} belum diisi")

    if not MT5_PYTHON_CMD:
        problems.append("MT5_PYTHON_CMD kosong")
    mt5_credentials = [bool(MT5_LOGIN), bool(MT5_PASSWORD), bool(MT5_SERVER)]
    if any(mt5_credentials) and not all(mt5_credentials):
        problems.append("MT5_LOGIN, MT5_PASSWORD, dan MT5_SERVER harus diisi ketiganya "
                        "atau dikosongkan ketiganya")
    if MT5_LOGIN and not MT5_LOGIN.isdigit():
        problems.append("MT5_LOGIN harus berupa angka")
    if HTTP_TIMEOUT <= 0 or MT5_BRIDGE_TIMEOUT <= 0:
        problems.append("HTTP_TIMEOUT dan MT5_BRIDGE_TIMEOUT harus lebih besar dari 0")
    # Gemini API menolak (HTTP 400) deadline di bawah 10 detik.
    if GEMINI_TIMEOUT < 10:
        problems.append("GEMINI_TIMEOUT minimal 10 detik")
    if LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        problems.append(f"LOG_LEVEL tidak valid: {LOG_LEVEL}")

    _raise_if(problems)


def validate_market_api() -> None:
    """Validasi untuk tools/market_api.py (Gemini & Telegram tidak dibutuhkan di sini).

    Raises:
        ConfigError: jika token kosong/terlalu pendek atau setelan MT5 tidak valid.
    """
    problems = []
    if len(MARKET_API_TOKEN) < 24 or MARKET_API_TOKEN.lower().startswith("isi_"):
        problems.append("MARKET_API_TOKEN wajib diisi, minimal 24 karakter acak")
    if not 0 < MARKET_API_PORT < 65536:
        problems.append("MARKET_API_PORT harus di antara 1 dan 65535")
    if not MT5_PYTHON_CMD:
        problems.append("MT5_PYTHON_CMD kosong")
    _raise_if(problems)


def _raise_if(problems: list) -> None:
    if problems:
        raise ConfigError(
            "Konfigurasi .env bermasalah: " + "; ".join(problems)
            + f". Periksa file {BASE_DIR / '.env'} (contoh: .env.example)."
        )
