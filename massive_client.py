"""
Client for the Massive API.

The API key is stored in a Databricks secret scope (see setup_secrets.py) and
resolved at runtime via the Databricks SDK - it is never stored in code, env
files, or app.yaml.
"""

import base64
import os
from typing import Any

import requests
from databricks.sdk import WorkspaceClient

_w = WorkspaceClient()

_SCOPE = os.environ.get("MASSIVE_SECRET_SCOPE", "massive")
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
        Fetch comprehensive company details and fundamentals for a ticker.
        Returns company name, description, market cap, industry classification,
        homepage, logo URL, and more.
        """
        data = self.get(f"/v3/reference/tickers/{symbol}")
        return data

    def get_historical_aggregates(
        self,
        symbol: str,
        multiplier: int = 1,
        timespan: str = "day",
        from_date: str = None,
        to_date: str = None,
        limit: int = 120,
    ) -> dict:
        """
        Fetch historical OHLC aggregate bars for a ticker.
        
        Args:
            symbol: Stock ticker symbol
            multiplier: Size of the timespan multiplier (e.g., 1 for 1 day, 5 for 5 minutes)
            timespan: Size of time window: minute, hour, day, week, month, quarter, year
            from_date: Start date (YYYY-MM-DD format)
            to_date: End date (YYYY-MM-DD format)
            limit: Max number of results (default 120, max 50000)
        
        Returns OHLC data with volume, VWAP, and transaction count.
        """
        params = {"limit": limit}
        if from_date and to_date:
            path = f"/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        else:
            # Use prev endpoint for latest bar if no date range specified
            path = f"/v2/aggs/ticker/{symbol}/prev"
            params = {}
        
        data = self.get(path, params=params)
        return data

    def get_technical_indicator(
        self,
        symbol: str,
        indicator: str = "sma",
        timespan: str = "day",
        window: int = 50,
        series_type: str = "close",
        limit: int = 120,
    ) -> dict:
        """
        Fetch technical indicator data for a ticker.
        
        Args:
            symbol: Stock ticker symbol
            indicator: Type of indicator (sma, ema, macd, rsi)
            timespan: day, hour, minute, etc.
            window: Window size for the indicator
            series_type: Price to use (close, open, high, low)
            limit: Max number of results
        
        Supported indicators:
        - sma: Simple Moving Average
        - ema: Exponential Moving Average
        - macd: Moving Average Convergence Divergence
        - rsi: Relative Strength Index
        """
        params = {
            "timespan": timespan,
            "window": window,
            "series_type": series_type,
            "limit": limit,
        }
        data = self.get(f"/v1/indicators/{indicator}/{symbol}", params=params)
        return data

    def get_market_news(
        self,
        symbol: str = None,
        limit: int = 10,
        order: str = "desc",
    ) -> dict:
        """
        Fetch market news articles, optionally filtered by ticker symbol.
        
        Args:
            symbol: Optional ticker to filter news
            limit: Max number of articles (default 10, max 1000)
            order: Sort order (desc or asc)
        
        Returns news with title, description, URL, author, published date,
        and sentiment analysis (if available).
        """
        params = {"limit": limit, "order": order}
        if symbol:
            params["ticker"] = symbol
        
        data = self.get("/v2/reference/news", params=params)
        return data

    def get_financials(
        self,
        symbol: str,
        timeframe: str = "quarterly",
        limit: int = 10,
    ) -> dict:
        """
        Fetch financial statements for a ticker.
        
        Args:
            symbol: Stock ticker symbol
            timeframe: quarterly or annual
            limit: Max number of periods to return
        
        Returns income statement, balance sheet, and cash flow data.
        """
        params = {
            "ticker": symbol,
            "timeframe": timeframe,
            "limit": limit,
        }
        data = self.get("/vX/reference/financials", params=params)
        return data
