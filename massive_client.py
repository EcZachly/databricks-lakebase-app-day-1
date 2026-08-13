"""
Client for the Massive API.

The API key is stored in a Databricks secret scope (see setup_secrets.py) and
resolved at runtime via the Databricks SDK - it is never stored in code, env
files, or app.yaml.
"""

import base64
import os
from datetime import date, timedelta
from typing import Any

import requests
from databricks.sdk import WorkspaceClient

_w = WorkspaceClient()

_SCOPE = os.environ.get("MASSIVE_SECRET_SCOPE", "vj-massive")
_KEY = os.environ.get("MASSIVE_SECRET_KEY", "api-key")
_BASE_URL = os.environ.get("MASSIVE_API_BASE_URL", "https://api.massive.com")

_DEFAULT_TIMEOUT = 30


def _get_api_key() -> str:
    """Fetch and decode the Massive API key from the Databricks secret scope."""
    secret = _w.secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


class MassiveClient:
    """Thin wrapper around the Massive API with auth + retry-friendly session."""

    def __init__(self, base_url: str | None = None, timeout: int = _DEFAULT_TIMEOUT):
        self.base_url = (base_url or _BASE_URL).rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {_get_api_key()}",
                "Content-Type": "application/json",
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        resp = self._session.post(f"{self.base_url}{path}", json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def paginated_get(self, path: str, params: dict[str, Any] | None = None, page_size: int = 200):
        """
        Generator that yields items across all pages of a "massive" (large)
        paginated dataset. Assumes a cursor-based API shape:
        {"items": [...], "next_cursor": "..." | null}
        Adjust to match the real Massive API pagination contract.
        """
        cursor = None
        params = dict(params or {})
        params["page_size"] = page_size

        while True:
            if cursor:
                params["cursor"] = cursor
            data = self.get(path, params=params)
            items = data.get("items", [])
            for item in items:
                yield item

            cursor = data.get("next_cursor")
            if not cursor:
                break

    def get_latest_price(self, symbol: str) -> dict:
        """
        Fetch the latest traded price for a single symbol in a SINGLE API
        call (no pagination). Use this instead of paginated_get() whenever
        the caller needs to stay within tight API rate limits (e.g.
        classroom/student accounts), at the cost of only being able to
        request one symbol per request.
        """
        data = self.get(f"/v2/aggs/ticker/{symbol}/prev")
        return data

    def get_ticker_details(self, symbol: str) -> dict:
        """
        Fetch company/reference profile data (name, exchange, sector,
        market cap, etc.) for a single symbol in ONE API call. Intended to
        be called sparingly and cached by the caller - this data changes
        rarely, unlike price.
        """
        return self.get(f"/v3/reference/tickers/{symbol}")

    def get_price_history(self, symbol: str, days: int = 30) -> dict:
        """
        Fetch a short daily OHLC history for a single symbol in ONE API call,
        used to render a price trend/sparkline. Like get_ticker_details, this
        is meant to be cached by the caller rather than re-fetched on every
        page load.
        """
        end = date.today()
        start = end - timedelta(days=days)
        return self.get(
            f"/v2/aggs/ticker/{symbol}/range/1/day/{start.isoformat()}/{end.isoformat()}"
        )

    def get_ticker_news(self, symbol: str, limit: int = 5) -> dict:
        """
        Fetch recent news headlines for a single symbol in ONE API call.
        Meant to be called on-demand (e.g. when a user expands a "News"
        panel for a watchlist row) rather than pre-fetched for every symbol,
        and cached by the caller.
        """
        return self.get("/v2/reference/news", params={"ticker": symbol, "limit": limit})
