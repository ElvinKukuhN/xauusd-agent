"""Agen analis: Gemini 2.5 Flash + automatic function calling ke tool OANDA dan berita."""

import logging
import time
from datetime import datetime, timezone

from google import genai
from google.genai import errors, types

import config
from agent.prompts import SYSTEM_INSTRUCTION, USER_PROMPT
from tools.news_scraper import get_macro_news
from tools.oanda_client import get_oanda_xauusd_data

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 10
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_TOOL_CALLS = 8
REQUIRED_TOOLS = {"get_oanda_xauusd_data", "get_macro_news"}


class AgentError(Exception):
    """Dilempar ketika agen gagal menghasilkan laporan analisis."""


def _called_tools(response) -> set:
    """Nama tool yang benar-benar dipanggil model selama automatic function calling."""
    names = set()
    for content in getattr(response, "automatic_function_calling_history", None) or []:
        for part in getattr(content, "parts", None) or []:
            call = getattr(part, "function_call", None)
            if call is not None and call.name:
                names.add(call.name)
    return names


def run_analysis() -> str:
    """Menjalankan agen dan mengembalikan teks laporan siap kirim ke Telegram.

    Raises:
        AgentError: jika Gemini gagal, tidak memanggil tool wajib, atau respons kosong.
    """
    client = genai.Client(
        api_key=config.GEMINI_API_KEY,
        # SDK memakai milidetik. Berlaku per request HTTP (tiap putaran function calling).
        http_options=types.HttpOptions(timeout=config.GEMINI_TIMEOUT * 1000),
    )
    generation_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=config.GEMINI_TEMPERATURE,
        tools=[get_oanda_xauusd_data, get_macro_news],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=MAX_TOOL_CALLS
        ),
    )
    prompt = USER_PROMPT.format(
        now_utc=datetime.now(timezone.utc).strftime("%A, %Y-%m-%d %H:%M UTC")
    )

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            logger.info("Memanggil %s (percobaan %d/%d)", config.GEMINI_MODEL, attempt, MAX_ATTEMPTS)
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config=generation_config,
            )
        except errors.APIError as exc:
            last_error = exc
            code = getattr(exc, "code", None)
            logger.warning("Gemini API error (code=%s): %s", code, exc)
            if code not in RETRYABLE_STATUS_CODES:
                break
        except Exception as exc:  # noqa: BLE001 - jaringan/SDK; dicoba ulang
            last_error = exc
            logger.warning("Gemini gagal dipanggil: %r", exc)
        else:
            called = _called_tools(response)
            logger.info("Tool yang dipanggil model: %s", sorted(called) or "-")
            text = (response.text or "").strip()
            missing = REQUIRED_TOOLS - called
            if text and not missing:
                return text
            last_error = AgentError(
                f"Model tidak memanggil tool wajib: {sorted(missing)}" if missing
                else "Gemini mengembalikan respons kosong."
            )
            logger.warning("%s", last_error)

        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS * attempt)

    raise AgentError(f"Agen gagal menghasilkan analisis: {last_error}") from last_error
