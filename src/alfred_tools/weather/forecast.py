"""Bounded weather forecasts from MET Norway with cached place lookup."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alfred_tools.config import get_preference

DEFAULT_FORECAST_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
DEFAULT_GEOCODER_URL = "https://nominatim.openstreetmap.org/search"
DEFAULT_USER_AGENT = "AlfredWeather/0.1 github.com/SmidigBommen/alfred-butler"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_GEOCODE_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_FORECAST_TTL_SECONDS = 30 * 60
MAX_RESPONSE_BYTES = 2_000_000
MAX_LOCATION_CHARS = 200


class ForecastBackendError(RuntimeError):
    """Raised when a geocoder or forecast provider cannot return usable data."""


@dataclass(frozen=True, slots=True)
class ForecastRequest:
    location: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: int | None = None
    days: int = 5
    hours: int = 24

    def __post_init__(self) -> None:
        location = self.location.strip() if self.location is not None else None
        if location == "":
            raise ValueError("location must not be empty")
        if location is not None and len(location) > MAX_LOCATION_CHARS:
            raise ValueError(f"location must contain at most {MAX_LOCATION_CHARS} characters")

        has_coordinates = self.latitude is not None or self.longitude is not None
        complete_coordinates = self.latitude is not None and self.longitude is not None
        if location is None and not complete_coordinates:
            raise ValueError("provide either a location or both latitude and longitude")
        if location is not None and has_coordinates:
            raise ValueError("location and coordinates cannot be combined")
        if has_coordinates and not complete_coordinates:
            raise ValueError("latitude and longitude must be provided together")
        if self.latitude is not None and (
            not math.isfinite(self.latitude) or not -90 <= self.latitude <= 90
        ):
            raise ValueError("latitude must be between -90 and 90")
        if self.longitude is not None and (
            not math.isfinite(self.longitude) or not -180 <= self.longitude <= 180
        ):
            raise ValueError("longitude must be between -180 and 180")
        if self.altitude is not None and not -500 <= self.altitude <= 9_000:
            raise ValueError("altitude must be between -500 and 9000 metres")
        if not 1 <= self.days <= 9:
            raise ValueError("days must be between 1 and 9")
        if not 0 <= self.hours <= 48:
            raise ValueError("hours must be between 0 and 48")
        object.__setattr__(self, "location", location)


@dataclass(frozen=True, slots=True)
class RawForecastResponse:
    status: int
    headers: dict[str, str]
    payload: dict[str, Any] | None


class WeatherTransport(Protocol):
    def geocode(self, query: str, timeout: float) -> list[dict[str, Any]]: ...

    def forecast(
        self,
        latitude: float,
        longitude: float,
        altitude: int | None,
        timeout: float,
        if_modified_since: str | None = None,
    ) -> RawForecastResponse: ...


def _bounded_json_body(response: Any) -> Any:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ForecastBackendError("weather response exceeded 2 MB")
    encoding = str(response.headers.get("Content-Encoding", "")).lower()
    if encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as error:
            raise ForecastBackendError("weather provider returned invalid gzip data") from error
        if len(body) > MAX_RESPONSE_BYTES:
            raise ForecastBackendError("decompressed weather response exceeded 2 MB")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ForecastBackendError("weather provider returned invalid JSON") from error


class UrllibWeatherTransport:
    """Identify Alfred to both providers and serialize public geocoder calls."""

    def __init__(
        self,
        *,
        forecast_url: str = DEFAULT_FORECAST_URL,
        geocoder_url: str = DEFAULT_GEOCODER_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        opener: Any = urllib.request.urlopen,
        clock: Any = time.monotonic,
        sleeper: Any = time.sleep,
    ):
        if not user_agent.strip():
            raise ValueError("weather User-Agent must identify the application")
        self.forecast_url = _service_url(forecast_url, "forecast")
        self.geocoder_url = _service_url(geocoder_url, "geocoder")
        self.user_agent = user_agent
        self.opener = opener
        self.clock = clock
        self.sleeper = sleeper
        self._geocode_lock = threading.Lock()
        self._last_geocode_at: float | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": self.user_agent,
        }

    def geocode(self, query: str, timeout: float) -> list[dict[str, Any]]:
        parameters = urllib.parse.urlencode(
            {"q": query, "format": "jsonv2", "limit": "1", "addressdetails": "0"}
        )
        request = urllib.request.Request(
            f"{self.geocoder_url}?{parameters}", headers=self._headers(), method="GET"
        )
        with self._geocode_lock:
            if self._last_geocode_at is not None:
                remaining = 1.0 - (self.clock() - self._last_geocode_at)
                if remaining > 0:
                    self.sleeper(remaining)
            try:
                with self.opener(request, timeout=timeout) as response:
                    status = response.status
                    payload = _bounded_json_body(response)
            except urllib.error.HTTPError as error:
                raise ForecastBackendError(f"place lookup returned HTTP {error.code}") from error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise ForecastBackendError(f"could not reach place lookup: {error}") from error
            finally:
                self._last_geocode_at = self.clock()
        if status != 200 or not isinstance(payload, list):
            raise ForecastBackendError("place lookup returned an unexpected response")
        return [item for item in payload if isinstance(item, dict)]

    def forecast(
        self,
        latitude: float,
        longitude: float,
        altitude: int | None,
        timeout: float,
        if_modified_since: str | None = None,
    ) -> RawForecastResponse:
        parameters: dict[str, str] = {
            "lat": f"{latitude:.4f}",
            "lon": f"{longitude:.4f}",
        }
        if altitude is not None:
            parameters["altitude"] = str(altitude)
        headers = self._headers()
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since
        request = urllib.request.Request(
            f"{self.forecast_url}?{urllib.parse.urlencode(parameters)}",
            headers=headers,
            method="GET",
        )
        try:
            with self.opener(request, timeout=timeout) as response:
                response_headers = {key.lower(): value for key, value in response.getheaders()}
                payload = _bounded_json_body(response)
                status = response.status
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return RawForecastResponse(
                    status=304,
                    headers={key.lower(): value for key, value in error.headers.items()},
                    payload=None,
                )
            raise ForecastBackendError(f"MET Norway returned HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ForecastBackendError(f"could not reach MET Norway: {error}") from error
        if payload is not None and not isinstance(payload, dict):
            raise ForecastBackendError("MET Norway returned an unexpected JSON value")
        return RawForecastResponse(status, response_headers, payload)


def _service_url(value: str, name: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as error:
        raise ValueError(f"{name} URL is invalid") from error
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} URL must be absolute HTTP(S)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name} URL must not contain credentials, a query, or a fragment")
    return value.rstrip("/")


def default_weather_transport() -> UrllibWeatherTransport:
    return UrllibWeatherTransport(
        forecast_url=os.environ.get("ALFRED_MET_FORECAST_URL", DEFAULT_FORECAST_URL),
        geocoder_url=os.environ.get("ALFRED_NOMINATIM_URL", DEFAULT_GEOCODER_URL),
        user_agent=os.environ.get("ALFRED_WEATHER_USER_AGENT", DEFAULT_USER_AGENT),
    )


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    value: Any
    expires_at: float
    last_modified: str | None


class SQLiteWeatherCache:
    def __init__(self, path: Path, *, now: Any = time.time):
        self.path = path
        self.now = now

    def get(self, key: str) -> _CacheEntry | None:
        if not self.path.exists():
            return None
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                row = connection.execute(
                    "SELECT value, expires_at, last_modified FROM weather_cache WHERE key = ?",
                    (key,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return _CacheEntry(value, float(row[1]), row[2])

    def set(
        self,
        key: str,
        value: Any,
        *,
        expires_at: float,
        last_modified: str | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS weather_cache ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL, "
                "last_modified TEXT)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO weather_cache "
                "(key, value, expires_at, last_modified) VALUES (?, ?, ?, ?)",
                (
                    key,
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    expires_at,
                    last_modified,
                ),
            )
            connection.commit()


def _cache_key(kind: str, value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{kind}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _http_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _rounded(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def _period(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("next_1_hours", "next_6_hours", "next_12_hours"):
        candidate = data.get(key)
        if isinstance(candidate, dict):
            return candidate
    return {}


def _hour(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("time"), str):
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    instant = data.get("instant")
    details = instant.get("details") if isinstance(instant, dict) else None
    if not isinstance(details, dict):
        return None
    period = _period(data)
    summary = period.get("summary") if isinstance(period.get("summary"), dict) else {}
    next_hour = data.get("next_1_hours")
    next_hour_details = (
        next_hour.get("details")
        if isinstance(next_hour, dict) and isinstance(next_hour.get("details"), dict)
        else {}
    )
    return {
        "time": entry["time"],
        "temperature_c": _number(details.get("air_temperature")),
        "precipitation_mm": _number(next_hour_details.get("precipitation_amount")),
        "symbol_code": str(summary.get("symbol_code")) if summary.get("symbol_code") else None,
        "wind_speed_m_s": _number(details.get("wind_speed")),
        "wind_from_direction_degrees": _number(details.get("wind_from_direction")),
        "relative_humidity_percent": _number(details.get("relative_humidity")),
        "air_pressure_hpa": _number(details.get("air_pressure_at_sea_level")),
    }


def _daily(hours: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for hour in hours:
        date = hour["time"][:10]
        if date not in grouped and len(grouped) >= limit:
            continue
        grouped.setdefault(date, []).append(hour)

    result: list[dict[str, Any]] = []
    for date, values in grouped.items():
        temperatures = [
            value["temperature_c"] for value in values if value["temperature_c"] is not None
        ]
        precipitation = [
            value["precipitation_mm"] for value in values if value["precipitation_mm"] is not None
        ]
        winds = [value["wind_speed_m_s"] for value in values if value["wind_speed_m_s"] is not None]
        symbols = [value["symbol_code"] for value in values if value["symbol_code"]]
        result.append(
            {
                "date": date,
                "temperature_min_c": _rounded(min(temperatures)) if temperatures else None,
                "temperature_max_c": _rounded(max(temperatures)) if temperatures else None,
                "precipitation_mm": _rounded(sum(precipitation), 2) if precipitation else None,
                "max_wind_speed_m_s": _rounded(max(winds)) if winds else None,
                "symbol_code": Counter(symbols).most_common(1)[0][0] if symbols else None,
            }
        )
    return result


def _localize_hour(hour: dict[str, Any], zone: ZoneInfo) -> dict[str, Any] | None:
    try:
        instant = datetime.fromisoformat(hour["time"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if instant.tzinfo is None:
        return None
    localized = dict(hour)
    localized["time"] = instant.astimezone(zone).isoformat(timespec="seconds")
    return localized


def _forecast_url(base_url: str, latitude: float, longitude: float, altitude: int | None) -> str:
    parameters: dict[str, str] = {
        "lat": f"{latitude:.4f}",
        "lon": f"{longitude:.4f}",
    }
    if altitude is not None:
        parameters["altitude"] = str(altitude)
    return f"{base_url}?{urllib.parse.urlencode(parameters)}"


class ForecastClient:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: WeatherTransport | None = None,
        cache: SQLiteWeatherCache | None = None,
        time_zone: str = "UTC",
        default_location: str | None = None,
        now: Any = time.time,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self.transport = transport or default_weather_transport()
        self.cache = cache
        zone_name = time_zone.strip()
        try:
            self.zone = ZoneInfo(zone_name)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(f"unknown time zone: {zone_name or '<empty>'}") from error
        self.time_zone = zone_name
        configured_location = default_location
        self.default_location = configured_location.strip() if configured_location else None
        if self.default_location and len(self.default_location) > MAX_LOCATION_CHARS:
            raise ValueError(
                f"default location must contain at most {MAX_LOCATION_CHARS} characters"
            )
        self.now = now

    def _resolve(self, request: ForecastRequest) -> tuple[str, float, float, bool]:
        if request.location is None:
            assert request.latitude is not None and request.longitude is not None
            latitude = round(request.latitude, 4)
            longitude = round(request.longitude, 4)
            return f"{latitude:.4f}, {longitude:.4f}", latitude, longitude, False

        key = _cache_key("geocode", request.location.casefold())
        cached = self.cache.get(key) if self.cache else None
        if cached is not None and cached.expires_at > self.now():
            results = cached.value
        else:
            results = self.transport.geocode(request.location, self.timeout)
            if self.cache:
                self.cache.set(
                    key,
                    results,
                    expires_at=self.now() + DEFAULT_GEOCODE_TTL_SECONDS,
                )
        if not isinstance(results, list) or not results:
            raise ForecastBackendError(f"could not find a place matching {request.location!r}")
        first = results[0]
        try:
            latitude = round(float(first["lat"]), 4)
            longitude = round(float(first["lon"]), 4)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ForecastBackendError("place lookup returned invalid coordinates") from error
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ForecastBackendError("place lookup returned out-of-range coordinates")
        name = str(first.get("display_name") or request.location).strip()
        return name, latitude, longitude, True

    def _raw_forecast(
        self, latitude: float, longitude: float, altitude: int | None
    ) -> tuple[dict[str, Any], bool, int]:
        key = _cache_key("forecast", [latitude, longitude, altitude])
        cached = self.cache.get(key) if self.cache else None
        now = self.now()
        if cached is not None and cached.expires_at > now and isinstance(cached.value, dict):
            return cached.value, True, 200

        response = self.transport.forecast(
            latitude,
            longitude,
            altitude,
            self.timeout,
            cached.last_modified if cached else None,
        )
        expires_at = _http_timestamp(response.headers.get("expires"))
        expires_at = expires_at if expires_at is not None and expires_at > now else None
        expires_at = expires_at or (now + DEFAULT_FORECAST_TTL_SECONDS)

        if response.status == 304:
            if cached is None or not isinstance(cached.value, dict):
                raise ForecastBackendError("MET Norway returned 304 without cached forecast data")
            if self.cache:
                self.cache.set(
                    key,
                    cached.value,
                    expires_at=expires_at,
                    last_modified=cached.last_modified,
                )
            return cached.value, True, 304
        if not 200 <= response.status < 300 or not isinstance(response.payload, dict):
            raise ForecastBackendError(f"MET Norway returned HTTP {response.status}")
        if self.cache:
            self.cache.set(
                key,
                response.payload,
                expires_at=expires_at,
                last_modified=response.headers.get("last-modified"),
            )
        return response.payload, False, response.status

    def forecast(self, request: ForecastRequest) -> dict[str, Any]:
        name, latitude, longitude, geocoded = self._resolve(request)
        raw, cached, status = self._raw_forecast(latitude, longitude, request.altitude)
        properties = raw.get("properties")
        timeseries = properties.get("timeseries") if isinstance(properties, dict) else None
        if not isinstance(timeseries, list) or not timeseries:
            raise ForecastBackendError("MET Norway response contains no forecast timeseries")
        utc_hours = [normalized for entry in timeseries if (normalized := _hour(entry))]
        hours = [localized for hour in utc_hours if (localized := _localize_hour(hour, self.zone))]
        if not hours:
            raise ForecastBackendError("MET Norway response contains no usable forecast hours")
        meta = properties.get("meta") if isinstance(properties.get("meta"), dict) else {}
        source = {
            "provider": "MET Norway",
            "forecast_url": _forecast_url(
                getattr(self.transport, "forecast_url", DEFAULT_FORECAST_URL),
                latitude,
                longitude,
                request.altitude,
            ),
            "attribution": "Weather data from MET Norway",
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
        }
        if geocoded:
            source["geocoding_provider"] = "OpenStreetMap Nominatim"
            source["geocoding_attribution"] = "© OpenStreetMap contributors"
            source["geocoding_license_url"] = "https://www.openstreetmap.org/copyright"
        warnings = []
        if status == 203:
            warnings.append("MET Norway marked this forecast endpoint as deprecated or beta")
        return {
            "location": {
                "query": request.location,
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
                "altitude_m": request.altitude,
            },
            "time_zone": self.time_zone,
            "current": hours[0],
            "hourly": hours[: request.hours],
            "daily": _daily(hours, request.days),
            "updated_at": meta.get("updated_at"),
            "fetched_at": datetime.fromtimestamp(self.now(), UTC).isoformat(),
            "source": source,
            "cached": cached,
            "warnings": warnings,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Get a bounded forecast from MET Norway")
    parser.add_argument("--location", help="City, region, or named place to resolve")
    parser.add_argument("--latitude", type=float)
    parser.add_argument("--longitude", type=float)
    parser.add_argument("--altitude", type=int)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--time-zone", help="IANA time zone for forecast timestamps")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--no-cache", action="store_true")
    return parser


def run_cli(
    argv: list[str] | None = None,
    *,
    client: Any | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    try:
        location = args.location
        if location is None and args.latitude is None and args.longitude is None:
            location = get_preference("ALFRED_WEATHER_LOCATION")
            if location is None and client is not None:
                location = getattr(client, "default_location", None)
        request = ForecastRequest(
            location=location,
            latitude=args.latitude,
            longitude=args.longitude,
            altitude=args.altitude,
            days=args.days,
            hours=args.hours,
        )
        if client is None:
            cache_path = Path(
                os.environ.get(
                    "ALFRED_WEATHER_CACHE",
                    Path.home() / ".cache" / "alfred" / "weather.sqlite3",
                )
            ).expanduser()
            cache = None if args.no_cache else SQLiteWeatherCache(cache_path)
            client = ForecastClient(
                timeout=args.timeout,
                cache=cache,
                time_zone=args.time_zone or get_preference("ALFRED_TIME_ZONE", "UTC") or "UTC",
                default_location=location,
            )
        response = client.forecast(request)
    except ValueError as error:
        json.dump({"error": str(error), "type": "input"}, stderr, ensure_ascii=False)
        stderr.write("\n")
        return 1
    except ForecastBackendError as error:
        json.dump({"error": str(error), "type": "backend"}, stderr, ensure_ascii=False)
        stderr.write("\n")
        return 2

    json.dump(response, stdout, ensure_ascii=False, indent=2)
    stdout.write("\n")
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
