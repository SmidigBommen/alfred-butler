import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from alfred_tools.weather.forecast import (
    ForecastBackendError,
    ForecastClient,
    ForecastRequest,
    RawForecastResponse,
    SQLiteWeatherCache,
    UrllibWeatherTransport,
    default_weather_transport,
    run_cli,
)


def forecast_payload():
    def point(time, temperature, precipitation, symbol, wind=3.0):
        return {
            "time": time,
            "data": {
                "instant": {
                    "details": {
                        "air_temperature": temperature,
                        "relative_humidity": 70.0,
                        "wind_speed": wind,
                        "wind_from_direction": 180.0,
                        "air_pressure_at_sea_level": 1012.0,
                    }
                },
                "next_1_hours": {
                    "summary": {"symbol_code": symbol},
                    "details": {"precipitation_amount": precipitation},
                },
            },
        }

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [10.7522, 59.9139, 23]},
        "properties": {
            "meta": {
                "updated_at": "2026-07-15T21:30:00Z",
                "units": {
                    "air_temperature": "celsius",
                    "precipitation_amount": "mm",
                    "wind_speed": "m/s",
                },
            },
            "timeseries": [
                point("2026-07-15T22:00:00Z", 14.2, 0.2, "partlycloudy_night"),
                point("2026-07-15T23:00:00Z", 13.0, 0.4, "rain", 4.5),
                point("2026-07-16T00:00:00Z", 12.0, 0.1, "rain"),
                point("2026-07-16T12:00:00Z", 20.0, 0.0, "clearsky_day", 6.0),
                point("2026-07-17T12:00:00Z", 18.0, 1.0, "rain"),
            ],
        },
    }


class FakeWeatherTransport:
    def __init__(self):
        self.geocode_calls = []
        self.forecast_calls = []
        self.response = RawForecastResponse(
            status=200,
            headers={
                "expires": "Wed, 15 Jul 2026 22:30:00 GMT",
                "last-modified": "Wed, 15 Jul 2026 21:30:00 GMT",
            },
            payload=forecast_payload(),
        )

    def geocode(self, query, timeout):
        self.geocode_calls.append((query, timeout))
        return [
            {
                "display_name": "Oslo, Norway",
                "lat": "59.9138688",
                "lon": "10.7522454",
                "type": "city",
            }
        ]

    def forecast(self, latitude, longitude, altitude, timeout, if_modified_since=None):
        self.forecast_calls.append((latitude, longitude, altitude, timeout, if_modified_since))
        return self.response


class FakeHttpResponse:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit):
        return self.body[:limit]

    def getheaders(self):
        return list(self.headers.items())


class ForecastRequestTests(unittest.TestCase):
    def test_accepts_either_a_place_or_complete_coordinates(self):
        self.assertEqual(ForecastRequest(location="  Oslo  ").location, "Oslo")
        request = ForecastRequest(latitude=59.9, longitude=10.7, days=9, hours=0)
        self.assertEqual((request.latitude, request.longitude), (59.9, 10.7))

        invalid = (
            {},
            {"location": "Oslo", "latitude": 59.9, "longitude": 10.7},
            {"latitude": 59.9},
            {"latitude": 91, "longitude": 10},
            {"latitude": 59, "longitude": 181},
            {"location": "Oslo", "days": 10},
            {"location": "Oslo", "hours": 49},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                ForecastRequest(**arguments)


class ForecastClientTests(unittest.TestCase):
    def test_geocodes_rounds_and_normalizes_a_bounded_forecast(self):
        transport = FakeWeatherTransport()
        client = ForecastClient(transport=transport, now=lambda: 1784151000.0)

        result = client.forecast(ForecastRequest(location="Oslo", days=2, hours=3))

        self.assertEqual(transport.geocode_calls, [("Oslo", 15.0)])
        self.assertEqual(transport.forecast_calls[0][:3], (59.9139, 10.7522, None))
        self.assertEqual(result["location"]["name"], "Oslo, Norway")
        self.assertEqual(result["location"]["latitude"], 59.9139)
        self.assertEqual(result["current"]["temperature_c"], 14.2)
        self.assertEqual(len(result["hourly"]), 3)
        self.assertEqual([day["date"] for day in result["daily"]], ["2026-07-15", "2026-07-16"])
        self.assertEqual(result["daily"][0]["precipitation_mm"], 0.6)
        self.assertEqual(result["daily"][1]["temperature_max_c"], 20.0)
        self.assertEqual(result["time_zone"], "UTC")
        self.assertEqual(result["source"]["provider"], "MET Norway")
        self.assertEqual(result["source"]["license"], "CC BY 4.0")
        self.assertEqual(result["source"]["geocoding_provider"], "OpenStreetMap Nominatim")
        self.assertFalse(result["cached"])

    def test_configured_timezone_converts_hours_and_groups_local_days(self):
        transport = FakeWeatherTransport()
        client = ForecastClient(
            transport=transport,
            time_zone="Europe/Oslo",
            now=lambda: 1784151000.0,
        )

        result = client.forecast(ForecastRequest(location="Oslo", days=2, hours=3))

        self.assertEqual(result["time_zone"], "Europe/Oslo")
        self.assertEqual(result["current"]["time"], "2026-07-16T00:00:00+02:00")
        self.assertEqual(result["hourly"][2]["time"], "2026-07-16T02:00:00+02:00")
        self.assertEqual([day["date"] for day in result["daily"]], ["2026-07-16", "2026-07-17"])
        self.assertEqual(result["daily"][0]["precipitation_mm"], 0.7)

    def test_rejects_an_unknown_timezone(self):
        with self.assertRaisesRegex(ValueError, "time zone"):
            ForecastClient(transport=FakeWeatherTransport(), time_zone="Mars/Olympus")

    def test_direct_coordinates_skip_geocoding(self):
        transport = FakeWeatherTransport()
        client = ForecastClient(transport=transport, now=lambda: 1784151000.0)

        result = client.forecast(ForecastRequest(latitude=59.9138688, longitude=10.7522454, days=1))

        self.assertEqual(transport.geocode_calls, [])
        self.assertEqual(result["location"]["name"], "59.9139, 10.7522")
        self.assertNotIn("geocoding_provider", result["source"])

    def test_fresh_sqlite_cache_avoids_repeating_both_network_requests(self):
        transport = FakeWeatherTransport()
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteWeatherCache(
                Path(directory) / "weather.sqlite3",
                now=lambda: 1784151000.0,
            )
            client = ForecastClient(
                transport=transport,
                cache=cache,
                now=lambda: 1784151000.0,
            )
            request = ForecastRequest(location="Oslo", days=2)

            client.forecast(request)
            cached = client.forecast(request)

        self.assertEqual(len(transport.geocode_calls), 1)
        self.assertEqual(len(transport.forecast_calls), 1)
        self.assertTrue(cached["cached"])

    def test_stale_cache_revalidates_with_last_modified_and_accepts_304(self):
        transport = FakeWeatherTransport()
        clock = [datetime(2026, 7, 15, 22, 0, tzinfo=UTC).timestamp()]
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteWeatherCache(Path(directory) / "weather.sqlite3")
            client = ForecastClient(transport=transport, cache=cache, now=lambda: clock[0])
            request = ForecastRequest(location="Oslo")
            client.forecast(request)

            clock[0] = datetime(2026, 7, 15, 23, 0, tzinfo=UTC).timestamp()
            transport.response = RawForecastResponse(
                304,
                {"expires": "Thu, 16 Jul 2026 00:00:00 GMT"},
                None,
            )
            result = client.forecast(request)

        self.assertEqual(len(transport.geocode_calls), 1)
        self.assertEqual(transport.forecast_calls[1][-1], "Wed, 15 Jul 2026 21:30:00 GMT")
        self.assertTrue(result["cached"])

    def test_missing_place_and_malformed_forecast_are_backend_errors(self):
        transport = FakeWeatherTransport()
        transport.geocode = lambda query, timeout: []
        client = ForecastClient(transport=transport)
        with self.assertRaisesRegex(ForecastBackendError, "could not find"):
            client.forecast(ForecastRequest(location="Atlantis"))

        transport = FakeWeatherTransport()
        transport.response = RawForecastResponse(200, {}, {"properties": {}})
        with self.assertRaisesRegex(ForecastBackendError, "timeseries"):
            ForecastClient(transport=transport).forecast(ForecastRequest(latitude=1, longitude=2))


class WeatherTransportTests(unittest.TestCase):
    def test_provider_endpoints_can_be_switched_with_environment_configuration(self):
        with patch.dict(
            os.environ,
            {
                "ALFRED_MET_FORECAST_URL": "https://weather.example/compact",
                "ALFRED_NOMINATIM_URL": "https://places.example/search",
                "ALFRED_WEATHER_USER_AGENT": "PrivateAlfred/1.0 https://example.test",
            },
        ):
            transport = default_weather_transport()

        self.assertEqual(transport.forecast_url, "https://weather.example/compact")
        self.assertEqual(transport.geocoder_url, "https://places.example/search")
        self.assertEqual(transport.user_agent, "PrivateAlfred/1.0 https://example.test")

        with self.assertRaises(ValueError):
            UrllibWeatherTransport(geocoder_url="https://user:secret@places.example/search")

    def test_identifies_alfred_rounds_coordinates_and_rate_limits_geocoding(self):
        requests = []
        sleeps = []

        def opener(request, timeout):
            requests.append((request, timeout))
            if urlsplit(request.full_url).hostname == "nominatim.openstreetmap.org":
                return FakeHttpResponse(
                    200,
                    [{"display_name": "Oslo", "lat": "59.9", "lon": "10.7"}],
                )
            return FakeHttpResponse(200, forecast_payload())

        transport = UrllibWeatherTransport(
            opener=opener,
            clock=lambda: 10.0,
            sleeper=sleeps.append,
        )
        transport.geocode("Oslo", 4.0)
        transport.geocode("Bergen", 4.0)
        transport.forecast(
            59.9138688,
            10.7522454,
            23,
            5.0,
            "Wed, 15 Jul 2026 21:30:00 GMT",
        )

        self.assertEqual(sleeps, [1.0])
        for request, _timeout in requests:
            headers = dict(request.header_items())
            self.assertIn("github.com/SmidigBommen/alfred-butler", headers["User-agent"])
            self.assertEqual(headers["Accept-encoding"], "gzip")
        forecast_request = requests[-1][0]
        parameters = parse_qs(urlsplit(forecast_request.full_url).query)
        self.assertEqual(parameters["lat"], ["59.9139"])
        self.assertEqual(parameters["lon"], ["10.7522"])
        self.assertEqual(parameters["altitude"], ["23"])
        self.assertEqual(
            dict(forecast_request.header_items())["If-modified-since"],
            "Wed, 15 Jul 2026 21:30:00 GMT",
        )


class ForecastCliTests(unittest.TestCase):
    def test_cli_emits_normalized_json_and_machine_readable_errors(self):
        transport = FakeWeatherTransport()
        client = ForecastClient(transport=transport, now=lambda: 1784151000.0)
        stdout = io.StringIO()
        stderr = io.StringIO()

        status = run_cli(
            ["--location", "Oslo", "--days", "2", "--hours", "3"],
            client=client,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue())["location"]["name"], "Oslo, Norway")
        self.assertEqual(stderr.getvalue(), "")

        stderr = io.StringIO()
        status = run_cli(["--latitude", "59.9"], client=client, stdout=io.StringIO(), stderr=stderr)
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stderr.getvalue())["type"], "input")

    def test_cli_uses_configured_default_location_when_omitted(self):
        transport = FakeWeatherTransport()
        client = ForecastClient(transport=transport, now=lambda: 1784151000.0)

        with patch.dict(
            os.environ,
            {"ALFRED_WEATHER_LOCATION": "Configured municipality, Norway"},
        ):
            status = run_cli(
                ["--days", "1"],
                client=client,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(status, 0)
        self.assertEqual(transport.geocode_calls[0][0], "Configured municipality, Norway")


if __name__ == "__main__":
    unittest.main()
