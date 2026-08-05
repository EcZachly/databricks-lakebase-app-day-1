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
        Fetch detailed company information for a ticker symbol.
        Includes company name, description, market cap, sector, industry, etc.
        
        Endpoint: /v3/reference/tickers/{symbol}
        """
        try:
            data = self.get(f"/v3/reference/tickers/{symbol}")
            return data
        except requests.HTTPError as e:
            # If v3 endpoint not available, try v2
            if e.response.status_code == 404:
                data = self.get(f"/v2/reference/tickers/{symbol}")
                return data
            raise

    def get_snapshot(self, symbol: str) -> dict:
        """
        Fetch real-time snapshot data for a ticker.
        Includes current day's OHLC, volume, previous close, and more.
        
        Endpoint: /v2/snapshot/locale/us/markets/stocks/tickers/{symbol}
        """
        data = self.get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}")
        return data

    def get_rich_stock_data(self, symbol: str) -> dict:
        """
        Fetch comprehensive stock data by combining multiple API calls.
        Returns a unified dictionary with:
        - Company details (name, description, market cap, sector)
        - Current trading data (price, volume, day range)
        - Previous close data
        - Calculated metrics (change %, day high/low)
        
        This makes 2-3 API calls but provides a complete picture.
        """
        result = {"symbol": symbol}
        
        # Get ticker details (company info)
        try:
            details = self.get_ticker_details(symbol)
            if "results" in details:
                ticker_data = details["results"]
                result["company_name"] = ticker_data.get("name")
                result["description"] = ticker_data.get("description")
                result["market_cap"] = ticker_data.get("market_cap")
                result["sector"] = ticker_data.get("sic_description") or ticker_data.get("sector")
                result["industry"] = ticker_data.get("industry")
                result["primary_exchange"] = ticker_data.get("primary_exchange")
                result["locale"] = ticker_data.get("locale")
                result["currency"] = ticker_data.get("currency_name")
                result["homepage_url"] = ticker_data.get("homepage_url")
                result["total_employees"] = ticker_data.get("total_employees")
        except Exception:
            # If details API fails, continue without company info
            pass
        
        # Get snapshot (real-time trading data)
        try:
            snapshot = self.get_snapshot(symbol)
            if "ticker" in snapshot:
                ticker_snap = snapshot["ticker"]
                
                # Current day data
                day = ticker_snap.get("day", {})
                result["current_price"] = day.get("c")  # Close/current
                result["open_price"] = day.get("o")
                result["high_price"] = day.get("h")
                result["low_price"] = day.get("l")
                result["volume"] = day.get("v")
                result["volume_weighted_price"] = day.get("vw")
                
                # Previous close data
                prev = ticker_snap.get("prevDay", {})
                result["prev_close"] = prev.get("c")
                result["prev_open"] = prev.get("o")
                result["prev_high"] = prev.get("h")
                result["prev_low"] = prev.get("l")
                result["prev_volume"] = prev.get("v")
                
                # Calculate change
                if result.get("current_price") and result.get("prev_close"):
                    change = result["current_price"] - result["prev_close"]
                    change_pct = (change / result["prev_close"]) * 100
                    result["change"] = change
                    result["change_percent"] = change_pct
                
                # Additional metrics
                result["updated_timestamp"] = ticker_snap.get("updated")
        except Exception:
            # If snapshot fails, fall back to previous close
            prev_data = self.get_latest_price(symbol)
            if "results" in prev_data and prev_data["results"]:
                prev_bar = prev_data["results"][0]
                result["current_price"] = prev_bar.get("c")
                result["open_price"] = prev_bar.get("o")
                result["high_price"] = prev_bar.get("h")
                result["low_price"] = prev_bar.get("l")
                result["volume"] = prev_bar.get("v")
                result["volume_weighted_price"] = prev_bar.get("vw")
        
        return result
