import os
import unittest

from alfred_tools.weather.forecast import ForecastClient, ForecastRequest


@unittest.skipUnless(os.environ.get("ALFRED_LIVE_TESTS") == "1", "set ALFRED_LIVE_TESTS=1")
class LiveWeatherTests(unittest.TestCase):
    def test_named_place_returns_attributed_met_norway_forecast(self):
        result = ForecastClient().forecast(
            ForecastRequest(location="Oslo, Norway", days=1, hours=1)
        )

        self.assertEqual(result["source"]["provider"], "MET Norway")
        self.assertEqual(result["source"]["geocoding_provider"], "OpenStreetMap Nominatim")
        self.assertEqual(len(result["hourly"]), 1)
        self.assertGreaterEqual(len(result["daily"]), 1)
        self.assertEqual(result["time_zone"], "UTC")


if __name__ == "__main__":
    unittest.main()
