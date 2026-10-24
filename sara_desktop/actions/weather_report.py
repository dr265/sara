import webbrowser
from urllib.parse import quote_plus


def _fetch_wttr(city: str) -> dict | None:
    """Live weather from wttr.in (no API key). Returns a small dict or None."""
    try:
        import requests
        r = requests.get(
            f"https://wttr.in/{quote_plus(city)}?format=j1",
            timeout=12,
            headers={"User-Agent": "curl/8.0"},
        )
        r.raise_for_status()
        j = r.json()
        cur = (j.get("current_condition") or [None])[0]
        if not cur:
            return None
        days = j.get("weather") or []
        out = {
            "temp_c":    cur.get("temp_C"),
            "feels_c":   cur.get("FeelsLikeC"),
            "desc":      (cur.get("weatherDesc") or [{}])[0].get("value", ""),
            "humidity":  cur.get("humidity"),
            "wind_kmph": cur.get("windspeedKmph"),
        }
        if days:
            d0 = days[0]
            out["today"] = (f"today {d0.get('mintempC')}–{d0.get('maxtempC')}°C")
            if len(days) > 1:
                d1 = days[1]
                hourly = (d1.get("hourly") or [{}])[8 * 100 // 24:9]  # midday slot
                d1desc = (hourly[0].get("weatherDesc") or [{}])[0].get("value", "") if hourly else ""
                out["tomorrow"] = (f"tomorrow {d1.get('mintempC')}–{d1.get('maxtempC')}°C"
                                   + (f", {d1desc.lower()}" if d1desc else ""))
        return out
    except Exception as e:
        print(f"[Weather] wttr.in unavailable ({e}) — browser card only")
        return None


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    city = parameters.get("city")
    when = parameters.get("time", "today")

    if not city or not isinstance(city, str) or not city.strip():
        msg = "Sir, the city is missing for the weather report."
        _log(msg, player)
        return msg

    city = city.strip()
    when = (when or "today").strip().lower()
    if when == "tomorrow":
        city_q = f"{city}+tomorrow"
    elif when not in ("today", "now", ""):
        city_q = f"{city} {when}"
    else:
        city_q = city

    # Real data first — the spoken/returned answer should contain facts.
    data = _fetch_wttr(city_q)
    if data:
        msg = (f"In {city} now: {data.get('desc') or 'unknown'}, "
               f"{data.get('temp_c')}°C (feels like {data.get('feels_c')}°C), "
               f"humidity {data.get('humidity')}%, wind {data.get('wind_kmph')} km/h.")
        if when == "tomorrow" and data.get("tomorrow"):
            msg += f" Forecast: {data['tomorrow']}."
        else:
            if data.get("today"):
                msg += f" {data['today'].capitalize()}."
            if data.get("tomorrow"):
                msg += f" {data['tomorrow'].capitalize()}."
    else:
        msg = (f"I could not fetch live weather data for {city} right now, sir. "
               f"I'll open the weather page for you instead.")

    # Desktop behaviour preserved: open the visual weather card too.
    try:
        webbrowser.open(f"https://www.google.com/search?q={quote_plus(f'weather in {city_q}')}")
    except Exception:
        pass

    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(
                query=f"weather in {city} {when}", response=msg)
        except Exception:
            pass

    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"SARA: {message}")
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "weather_report",
    "description": ("Fetches live weather for a city (current conditions, "
                    "temperature, humidity, wind, today's and tomorrow's "
                    "forecast) and returns the real data; also opens a "
                    "weather page in the browser."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {
                "type": "STRING",
                "description": "City name"
            },
            "time": {
                "type": "STRING",
                "description": "today | tomorrow (optional, default today)"
            }
        },
        "required": [
            "city"
        ]
    },
    "handler": weather_action,
}
