"""Entry point oanda-xauusd-agent: jalankan analisis lalu kirim laporan ke Telegram.

Pemakaian:
    python main.py            # analisis + kirim ke Telegram
    python main.py --dry-run  # analisis, cetak ke terminal, tanpa Telegram
"""

import argparse
import html
import logging
import sys
from logging.handlers import RotatingFileHandler

import config


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    file_handler = RotatingFileHandler(
        config.LOG_DIR / "agent.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [file_handler, console_handler]

    # Library HTTP mencatat URL lengkap (termasuk token bot Telegram) di level DEBUG/INFO.
    for noisy in ("urllib3", "httpx", "httpcore", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agentic AI Trading Assistant untuk XAU_USD")
    parser.add_argument("--dry-run", action="store_true",
                        help="Cetak laporan ke terminal tanpa mengirim ke Telegram")
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("main")
    logger.info("=== oanda-xauusd-agent mulai (dry_run=%s) ===", args.dry_run)

    try:
        config.validate(require_telegram=not args.dry_run)
    except config.ConfigError as exc:
        logger.error("%s", exc)
        return 2

    # Import setelah validasi agar error konfigurasi muncul lebih dulu dan jelas.
    from agent.analyst_agent import run_analysis
    from notifier.telegram_bot import send_telegram_message

    try:
        report = run_analysis()
    except Exception as exc:  # noqa: BLE001 - semua kegagalan agen dilaporkan
        logger.exception("Analisis gagal")
        if not args.dry_run:
            send_telegram_message(
                "⚠️ <b>XAU/USD Agent ERROR</b>\n"
                f"Analisis gagal dijalankan:\n{html.escape(str(exc))[:500]}"
            )
        return 1

    logger.info("Laporan dihasilkan (%d karakter).", len(report))

    if args.dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
        print("\n" + report + "\n")
        return 0

    if not send_telegram_message(report):
        logger.error("Laporan gagal dikirim ke Telegram.")
        return 1

    logger.info("=== Selesai ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
