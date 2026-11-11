"""Kumpulan tool (function calling) yang dapat dipanggil oleh agen Gemini."""

from tools.news_scraper import get_macro_news
from tools.oanda_client import get_oanda_xauusd_data

__all__ = ["get_oanda_xauusd_data", "get_macro_news"]
