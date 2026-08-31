---
name: alfred-weather
description: Get current conditions and short- or medium-range weather forecasts from MET Norway through Alfred's bounded local command. Use for questions about weather, temperature, precipitation, wind, outdoor plans, or forecasts for a city, region, named place, or supplied coordinates.
---

# Alfred weather

Use `alfred-weather` instead of general web search for forecast questions. The
command resolves named places with OpenStreetMap Nominatim and retrieves compact
forecast data from MET Norway.

## Get a forecast

For a city, region, or public named place, run:

```bash
alfred-weather --location "PLACE" --days 5 --hours 24
```

Choose `--days` from 1 through 9 and `--hours` from 0 through 48. If the user
supplies coordinates, avoid geocoding:

```bash
alfred-weather --latitude 59.9139 --longitude 10.7522 --days 3
```

If the user omits a place, run `alfred-weather` without location flags so it can
use Alfred's private configured default. Never guess or print that saved default
unless it is relevant to the answer.

Use the normal cache. Add `--no-cache` only when the user asks for a fresh check.
The cache follows MET Norway's expiry information and avoids repeat requests.

## Answer from the normalized result

- Read `time_zone` from the result. Treat hourly timestamps and daily dates as
  belonging to that IANA zone; never relabel them as another local time.
- Translate MET symbol codes into plain English, but do not invent conditions or
  precision missing from the result.
- Keep the answer proportional to the question. Mention hourly detail only when
  it helps with timing.
- Credit MET Norway for weather data and include the exact `forecast_url` when a
  source link is useful.
- When a place name was resolved, also credit OpenStreetMap contributors for the
  geocoding.
- Report backend or place-resolution failure clearly. Do not silently replace a
  failed forecast with unsourced model knowledge.

Place lookup sends the location string to OpenStreetMap. Use cities, regions, or
public landmarks by default. Do not submit a private street address or other
sensitive location unless the user explicitly requests that disclosure.
