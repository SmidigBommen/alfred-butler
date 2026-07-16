import tempfile
import unittest
from pathlib import Path

from alfred_tools.notes.store import NoteStore, SensitiveContentError
from alfred_tools.orchestrator.tools import build_alfred_tools


class FakeSearchClient:
    def __init__(self):
        self.requests = []

    def search(self, request):
        self.requests.append(request)
        return {"query": request.query, "results": []}


class FakeFetchClient:
    def fetch(self, request):
        return {"url": request.url, "text": "evidence"}


class FakeResearchClient:
    def __init__(self):
        self.requests = []

    def research(self, request):
        self.requests.append(request)
        return {
            "queries": list(request.queries),
            "sources": [
                {
                    "title": "Maritime Museum",
                    "url": "https://example.com/ships",
                    "cached": False,
                    "text": "private evidence payload",
                }
            ],
            "warnings": [],
        }


class FakeWeatherClient:
    def __init__(self):
        self.requests = []
        self.default_location = "Configured municipality, Norway"

    def forecast(self, request):
        self.requests.append(request)
        return {
            "location": {
                "query": request.location,
                "name": "Oslo, Norway",
                "latitude": 59.9139,
                "longitude": 10.7522,
            },
            "daily": [{"date": "2026-07-16"}],
            "source": {
                "provider": "MET Norway",
                "forecast_url": "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=59.9139&lon=10.7522",
                "attribution": "Weather data from MET Norway",
                "license": "CC BY 4.0",
            },
            "cached": False,
            "warnings": [],
        }


class AlfredToolTests(unittest.TestCase):
    def test_builds_only_bounded_registered_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = build_alfred_tools(
                notes=NoteStore(Path(directory)),
                search=FakeSearchClient(),
                fetch=FakeFetchClient(),
                research=FakeResearchClient(),
                weather=FakeWeatherClient(),
            )

        self.assertEqual(
            {tool.name for tool in tools},
            {
                "memory_search",
                "memory_capture",
                "web_search",
                "web_fetch",
                "web_research",
                "weather_forecast",
            },
        )
        self.assertNotIn("shell", {tool.name for tool in tools})

    def test_memory_capture_is_automatic_and_sensitive_content_stays_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = build_alfred_tools(
                notes=NoteStore(Path(directory)),
                search=FakeSearchClient(),
                fetch=FakeFetchClient(),
                research=FakeResearchClient(),
                weather=FakeWeatherClient(),
            )
            capture = next(tool for tool in tools if tool.name == "memory_capture")
            note = capture.handler(
                {
                    "title": "Preferred editor",
                    "body": "Use Helix for quick edits.",
                    "kind": "preference",
                    "labels": ["tools"],
                    "importance": 0.6,
                }
            )

            self.assertEqual(note["review_status"], "pending")
            with self.assertRaises(SensitiveContentError):
                capture.handler(
                    {
                        "title": "Credential",
                        "body": "api_key=do-not-store",
                        "kind": "fact",
                        "labels": [],
                        "importance": 0.5,
                    }
                )

    def test_web_search_is_forced_to_english_and_a_small_result_limit(self):
        search_client = FakeSearchClient()
        with tempfile.TemporaryDirectory() as directory:
            tools = build_alfred_tools(
                notes=NoteStore(Path(directory)),
                search=search_client,
                fetch=FakeFetchClient(),
                research=FakeResearchClient(),
                weather=FakeWeatherClient(),
            )
            search = next(tool for tool in tools if tool.name == "web_search")
            search.handler({"query": "current docs"})

        request = search_client.requests[0]
        self.assertEqual(request.language, "en")
        self.assertEqual(request.limit, 5)

    def test_web_research_trace_has_sources_but_not_fetched_page_text(self):
        research_client = FakeResearchClient()
        with tempfile.TemporaryDirectory() as directory:
            tools = build_alfred_tools(
                notes=NoteStore(Path(directory)),
                search=FakeSearchClient(),
                fetch=FakeFetchClient(),
                research=research_client,
                weather=FakeWeatherClient(),
            )
            research = next(tool for tool in tools if tool.name == "web_research")
            arguments = {"queries": ["historic ships"]}
            output = research.handler(arguments)
            summary = research.trace_builder(arguments, output, "completed")

        self.assertEqual(summary["queries"], ["historic ships"])
        self.assertEqual(summary["source_count"], 1)
        self.assertEqual(summary["sources"][0]["title"], "Maritime Museum")
        self.assertNotIn("private evidence payload", str(summary))
        self.assertEqual(research_client.requests[0].max_sources, 4)
        self.assertEqual(research_client.requests[0].max_chars_per_source, 4_000)

    def test_weather_forecast_is_bounded_and_has_attributed_trace(self):
        weather_client = FakeWeatherClient()
        with tempfile.TemporaryDirectory() as directory:
            tools = build_alfred_tools(
                notes=NoteStore(Path(directory)),
                search=FakeSearchClient(),
                fetch=FakeFetchClient(),
                research=FakeResearchClient(),
                weather=weather_client,
            )
            weather = next(tool for tool in tools if tool.name == "weather_forecast")
            arguments = {"location": "Oslo", "days": 3}
            output = weather.handler(arguments)
            summary = weather.trace_builder(arguments, output, "completed")

        request = weather_client.requests[0]
        self.assertEqual(request.location, "Oslo")
        self.assertEqual(request.days, 3)
        self.assertEqual(request.hours, 24)
        self.assertEqual(summary["location"], "Oslo, Norway")
        self.assertEqual(summary["provider"], "MET Norway")
        self.assertEqual(summary["sources"][0]["url"], output["source"]["forecast_url"])
        self.assertNotIn("daily", summary)

    def test_weather_forecast_uses_local_default_when_location_is_omitted(self):
        weather_client = FakeWeatherClient()
        with tempfile.TemporaryDirectory() as directory:
            tools = build_alfred_tools(
                notes=NoteStore(Path(directory)),
                search=FakeSearchClient(),
                fetch=FakeFetchClient(),
                research=FakeResearchClient(),
                weather=weather_client,
            )
            weather = next(tool for tool in tools if tool.name == "weather_forecast")
            output = weather.handler({"days": 2})

        self.assertNotIn("location", weather.input_schema["required"])
        self.assertEqual(weather_client.requests[0].location, "Configured municipality, Norway")
        self.assertEqual(output["location"]["name"], "Oslo, Norway")


if __name__ == "__main__":
    unittest.main()
