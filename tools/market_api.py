"""API HTTP kecil (stdlib) yang menyajikan data MT5 + berita untuk workflow n8n.

n8n tidak bisa membaca terminal MT5 secara langsung, jadi workflow n8n memanggil
endpoint ini lewat node HTTP Request. Logika data tetap sama persis dengan bot
Python: get_oanda_xauusd_data() dan get_macro_news().

Endpoint (semua wajib header "X-API-Key: <MARKET_API_TOKEN>"):
    GET /snapshot              -> {"generated_at_utc", "timeframes": {H1, M15, M5}, "news": [...]}
    GET /market?granularity=M5 -> hasil get_oanda_xauusd_data untuk satu timeframe
    GET /news                  -> hasil get_macro_news
    GET /health                -> {"status": "ok"} (tanpa token)

Jalankan dari root proyek:
    venv/bin/python -m tools.market_api

Server sengaja single-thread: request diproses satu per satu sehingga terminal
MT5 tidak pernah diakses paralel.
"""

import hmac
import json
import logging
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import config
from tools.news_scraper import get_macro_news
from tools.oanda_client import get_oanda_xauusd_data

logger = logging.getLogger("market_api")

SNAPSHOT_TIMEFRAMES = ("H1", "M15", "M5")


def build_snapshot() -> dict:
    """Data tiga timeframe + berita dalam satu payload."""
    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%A, %Y-%m-%d %H:%M UTC"),
        "timeframes": {tf: get_oanda_xauusd_data(tf) for tf in SNAPSHOT_TIMEFRAMES},
        "news": get_macro_news(),
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "xauusd-market-api"

    def _send(self, status: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-API-Key", "")
        return hmac.compare_digest(supplied.encode("utf-8"), config.MARKET_API_TOKEN.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 - nama method ditentukan http.server
        url = urlparse(self.path)
        if url.path == "/health":
            self._send(200, {"status": "ok"})
            return
        if not self._authorized():
            self._send(401, {"status": "error", "error": "X-API-Key tidak valid."})
            return

        try:
            if url.path == "/snapshot":
                self._send(200, build_snapshot())
            elif url.path == "/market":
                granularity = (parse_qs(url.query).get("granularity") or ["H1"])[0]
                self._send(200, get_oanda_xauusd_data(granularity))
            elif url.path == "/news":
                self._send(200, get_macro_news())
            else:
                self._send(404, {"status": "error", "error": f"Endpoint tidak dikenal: {url.path}"})
        except Exception as exc:  # noqa: BLE001 - jangan sampai server mati karena satu request
            logger.exception("Request %s gagal", url.path)
            self._send(500, {"status": "error", "error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args) -> None:
        # Jangan cetak header (berisi token); cukup method, path, dan status.
        logger.info("%s %s", self.address_string(), fmt % args)


def main() -> int:
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO),
                        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    for noisy in ("urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        config.validate_market_api()
    except config.ConfigError as exc:
        logger.error("%s", exc)
        return 2

    server = HTTPServer((config.MARKET_API_HOST, config.MARKET_API_PORT), _Handler)
    logger.info("Market API mendengarkan di http://%s:%d", config.MARKET_API_HOST, config.MARKET_API_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
