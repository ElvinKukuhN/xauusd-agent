"""Pengiriman pesan ke Telegram Bot API dengan pemecahan pesan, retry, dan fallback teks polos."""

import html
import logging
import re
import time

import requests

import config

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LENGTH = 4000  # batas Telegram 4096, sisakan margin
MAX_ATTEMPTS = 3
_TAG_RE = re.compile(r"</?(?:b|i|u|s|code|pre|strong|em)>", re.IGNORECASE)


def _split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list:
    """Pecah pesan panjang pada batas baris agar tidak melebihi limit Telegram."""
    chunks = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks


def _to_plain(text: str) -> str:
    """Buang tag HTML sederhana untuk fallback teks polos."""
    return html.unescape(_TAG_RE.sub("", text))


def _post(text: str, parse_mode) -> tuple:
    """Kirim satu pesan. Mengembalikan (berhasil, status_code, deskripsi_error)."""
    url = TELEGRAM_API_URL.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(url, json=payload, timeout=config.HTTP_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            # Jangan log objek exception mentah: URL-nya memuat token bot.
            logger.warning("Telegram gagal terhubung (percobaan %d/%d): %s",
                           attempt, MAX_ATTEMPTS, exc.__class__.__name__)
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 * attempt)
            continue

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 200 and body.get("ok"):
            return True, 200, ""

        description = body.get("description", response.text[:200])
        if response.status_code == 429 and attempt < MAX_ATTEMPTS:
            retry_after = int(body.get("parameters", {}).get("retry_after", 3))
            logger.warning("Telegram rate limit, menunggu %d detik", retry_after)
            time.sleep(min(retry_after, 30) + 1)
            continue
        if response.status_code >= 500 and attempt < MAX_ATTEMPTS:
            time.sleep(2 * attempt)
            continue
        return False, response.status_code, description

    return False, 0, "Tidak dapat terhubung ke Telegram"


def send_telegram_message(text: str) -> bool:
    """Kirim teks ke chat Telegram yang dikonfigurasi.

    Pesan dikirim sebagai HTML. Jika Telegram menolak format-nya (HTTP 400),
    bagian tersebut dikirim ulang sebagai teks polos.

    Returns:
        True jika seluruh bagian pesan berhasil terkirim.
    """
    if not text or not text.strip():
        logger.error("Pesan Telegram kosong, tidak dikirim.")
        return False

    all_sent = True
    chunks = _split_message(text.strip())
    for index, chunk in enumerate(chunks, start=1):
        ok, status, description = _post(chunk, "HTML")
        if not ok and status == 400:
            logger.warning("Telegram menolak format HTML (%s); kirim ulang teks polos.", description)
            ok, status, description = _post(_to_plain(chunk), None)
        if ok:
            logger.info("Telegram: bagian %d/%d terkirim.", index, len(chunks))
        else:
            all_sent = False
            logger.error("Telegram: bagian %d/%d gagal (HTTP %s): %s",
                         index, len(chunks), status, description)
    return all_sent
