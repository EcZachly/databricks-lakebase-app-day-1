"""Utilities to fetch and normalize weather data from api.weather.gov."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests

NWS_BASE_URL = "https://api.weather.gov"
_GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
_DEFAULT_TIMEOUT = 30


class WeatherClientError(Exception):
    """Raised when weather data cannot be fetched or normalized."""


@dataclass
class ResolvedLocation:
    display_name: str
    latitude: float
    longitude: float


class WeatherClient:
    def __init__(self, timeout: int = _DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/geo+json, application/json",
                "User-Agent": "databricks-lakebase-weather-app/1.0",
            }
        )

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise WeatherClientError(f"Request failed for {url}: {exc}") from exc

    def resolve_location(self, location: str) -> ResolvedLocation:
        text = (location or "").strip()
        if not text:
            raise WeatherClientError("Location cannot be empty")

        latlon = self._parse_latlon(text)
        if latlon:
            lat, lon = latlon
            return ResolvedLocation(display_name=f"{lat:.4f},{lon:.4f}", latitude=lat, longitude=lon)

        data = self._get_json(
            _GEOCODE_URL,
            params={
                "q": text,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "us",
            },
        )
        if not isinstance(data, list) or not data:
            raise WeatherClientError(f"Could not resolve location: {text}")

        best = data[0]
        try:
            lat = float(best["lat"])
            lon = float(best["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherClientError(f"Invalid geocoding response for location: {text}") from exc

        display_name = best.get("display_name") or text
        return ResolvedLocation(display_name=display_name, latitude=lat, longitude=lon)

    @staticmethod
    def _parse_latlon(text: str) -> tuple[float, float] | None:
        match = re.fullmatch(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*", text)
        if not match:
            return None

        lat = float(match.group(1))
        lon = float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise WeatherClientError(f"Invalid latitude/longitude: {text}")
        return lat, lon

    def fetch_documents_for_location(self, location: str, limit: int = 50) -> list[dict[str, Any]]:
        resolved = self.resolve_location(location)
        points = self._get_json(f"{NWS_BASE_URL}/points/{resolved.latitude},{resolved.longitude}")
        properties = points.get("properties") or {}

        resolved_name = self._resolved_name(location, properties, resolved)

        forecast_url = properties.get("forecast")
        if not forecast_url:
            raise WeatherClientError(f"Forecast URL was not returned for location: {location}")

        forecast = self._get_json(forecast_url)
        alerts = self._get_json(
            f"{NWS_BASE_URL}/alerts/active",
            params={"point": f"{resolved.latitude},{resolved.longitude}"},
        )

        docs: list[dict[str, Any]] = []
        docs.extend(self._normalize_alerts(alerts, resolved_name))
        docs.extend(self._normalize_forecast(forecast, resolved_name))

        return docs[:limit]

    @staticmethod
    def _resolved_name(original: str, point_props: dict[str, Any], resolved: ResolvedLocation) -> str:
        relative = (point_props.get("relativeLocation") or {}).get("properties") or {}
        city = relative.get("city")
        state = relative.get("state")
        if city and state:
            return f"{city}, {state}"
        return (original or resolved.display_name).strip() or resolved.display_name

    def _normalize_alerts(self, alerts: dict[str, Any], location: str) -> list[dict[str, Any]]:
        features = alerts.get("features") or []
        synced_at = datetime.now(UTC)
        documents: list[dict[str, Any]] = []

        for feature in features:
            props = feature.get("properties") or {}
            headline = props.get("headline") or props.get("event") or "Weather Alert"
            description = props.get("description") or ""
            instruction = props.get("instruction") or ""
            narrative_text = "\n\n".join(part for part in [description, instruction] if part).strip()
            if not narrative_text:
                narrative_text = headline

            source_key = feature.get("id") or f"{props.get('event')}|{props.get('effective')}|{props.get('sent')}"
            doc_id = self._stable_id(location, "alert", source_key)

            documents.append(
                {
                    "id": doc_id,
                    "location": location,
                    "source_type": "alert",
                    "headline": headline,
                    "narrative_text": narrative_text,
                    "issued_at": self._parse_time(props.get("sent")),
                    "effective_at": self._parse_time(props.get("effective") or props.get("onset")),
                    "payload": json.dumps(feature),
                    "synced_at": synced_at,
                }
            )

        return documents

    def _normalize_forecast(self, forecast: dict[str, Any], location: str) -> list[dict[str, Any]]:
        periods = ((forecast.get("properties") or {}).get("periods")) or []
        synced_at = datetime.now(UTC)
        documents: list[dict[str, Any]] = []

        for period in periods:
            detailed = period.get("detailedForecast") or ""
            headline = period.get("name") or period.get("shortForecast") or "Forecast"
            narrative_text = detailed.strip() or headline

            source_key = f"{period.get('startTime')}|{period.get('name')}|{period.get('number')}"
            doc_id = self._stable_id(location, "forecast", source_key)

            documents.append(
                {
                    "id": doc_id,
                    "location": location,
                    "source_type": "forecast",
                    "headline": headline,
                    "narrative_text": narrative_text,
                    "issued_at": self._parse_time(period.get("startTime")),
                    "effective_at": self._parse_time(period.get("endTime")),
                    "payload": json.dumps(period),
                    "synced_at": synced_at,
                }
            )

        return documents

    @staticmethod
    def _stable_id(location: str, source_type: str, source_key: str) -> str:
        digest = hashlib.sha256(f"{location}|{source_type}|{source_key}".encode("utf-8")).hexdigest()
        return digest

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
