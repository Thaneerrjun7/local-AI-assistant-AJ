"""Weather tool against saved Open-Meteo replies. No test touches the network or asks Windows
where the laptop is, and the cache lives in a temporary folder.

The fixtures are real replies for Toronto, captured 2026-10-04 at 08:15 local time (UTC-4),
and the clock is pinned to that moment. Tests that need rain edit a copy of the forecast.
Run: .venv\\Scripts\\python.exe -m unittest discover -s tests -t .
"""
import base64
import http.client
import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest import mock

from aj.tools import dispatch, intents, registry, system, weather
from aj.tools.registry import Level, ToolError

FIXTURES = Path(__file__).parent / "fixtures" / "weather"
CAPTURED = datetime(2026, 10, 4, 12, 15, tzinfo=timezone.utc).timestamp()  # 08:15 in Toronto
HOUR = 3600


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def place(name: str, lat: float, lon: float, code: str, country: str, admin1: str = "",
          feature: str = "PPL", population: int | None = None) -> dict:
    """One geocoder result, shaped like Open-Meteo's."""
    return {"name": name, "latitude": lat, "longitude": lon, "feature_code": feature, "country_code": code,
            "country": country, "admin1": admin1, "population": population}


class FakeOpenMeteo:
    """Stands in for weather._http_get: geocodes from a table, forecasts from the fixture."""

    def __init__(self):
        self.urls: list[str] = []
        self.forecast = fixture("forecast_toronto.json")
        self.places = {
            "toronto": fixture("geocode_toronto.json"),
            "paris": {"results": [place("Paris", 48.85341, 2.3488, "FR", "France", "Île-de-France", "PPLC", 2138551),
                                  place("Paris", 33.66094, -95.55551, "US", "United States", "Texas", population=24171)]},
            "malaysia": {"results": [place("Malaysia", 2.5, 112.5, "MY", "Malaysia", feature="PCLI",
                                           population=28274729)]},
            "singapore": {"results": [place("Singapore", 1.36667, 103.8, "SG", "Singapore", feature="PCLI"),
                                      place("Singapore", 1.28967, 103.85007, "SG", "Singapore", feature="PPLC",
                                            population=3547809)]},
            "new york": {"results": [place("New York", 40.71427, -74.00597, "US", "United States", "New York",
                                           "PPL", 8804190)]},
        }

    def __call__(self, url: str, timeout: float) -> bytes:
        self.urls.append(url)
        parts = urllib.parse.urlsplit(url)
        if parts.netloc == "geocoding-api.open-meteo.com":
            name = dict(urllib.parse.parse_qsl(parts.query))["name"].lower()
            return json.dumps(self.places.get(name, {"generationtime_ms": 0.4})).encode()  # no "results": no match
        if parts.netloc == "api.open-meteo.com":
            return json.dumps(self.forecast).encode()
        raise AssertionError(f"unexpected request {url}")

    def asked(self, host: str) -> list[dict]:
        return [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query)) for u in self.urls if host in u]

    def set_hours(self, start: str, count: int, prob: int, code: int) -> None:
        hourly = self.forecast["hourly"]
        i = hourly["time"].index(start)
        for j in range(i, i + count):
            hourly["precipitation_probability"][j], hourly["weather_code"][j] = prob, code

    def set_days(self, days: dict[int, tuple[int, int]]) -> None:
        daily = self.forecast["daily"]
        for i, (prob, code) in days.items():
            daily["precipitation_probability_max"][i], daily["weather_code"][i] = prob, code


class WeatherCase(unittest.TestCase):
    """A fake Open-Meteo, a pinned clock, a temporary cache and settings file, and a Windows
    location service that fails the test if it is asked."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = CAPTURED
        self.net = FakeOpenMeteo()
        self._patch(weather, "CACHE_PATH", self.dir / "weather_cache.json")
        self._patch(weather, "SETTINGS_PATH", self.dir / "settings.toml")
        self._patch(weather, "_now", lambda: self.clock)
        self._patch(weather, "_http_get", self.net)
        self.locate = self._patch(weather, "_locate_windows",
                                  mock.Mock(side_effect=AssertionError("a test asked Windows for the location")))

    def _patch(self, target, attribute: str, new):
        patcher = mock.patch.object(target, attribute, new)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def cache(self) -> dict:
        return json.loads(weather.CACHE_PATH.read_text(encoding="utf-8"))


class ReportTests(WeatherCase):
    """Every number in these sentences is in the fixture; check it there, not in the code."""

    def test_now(self):
        self.assertEqual(weather.get_weather("Toronto", "now"), "Toronto now: 9°C, partly cloudy.")

    def test_today_reports_a_clear_sky(self):
        # 7 of the 16 hours left are code 0. Zero is falsy, which once made this "overcast".
        self.assertEqual(weather.get_weather("Toronto"), "Toronto today: 9°C now, high 20°C, low 8°C, clear, no rain expected.")

    def test_tomorrow(self):
        self.assertEqual(weather.get_weather("Toronto", "tomorrow"), "Toronto tomorrow: high 13°C, low 6°C, clear, no rain expected.")

    def test_week_ignores_a_drizzle_code_at_eleven_percent(self):
        self.assertEqual(weather.get_weather("Toronto", "week"),
                         "Toronto over the next 7 days: highs 13 to 23°C, lows 6 to 11°C, little chance of rain.")

    def test_rain_later_today_gives_its_hour_and_chance(self):
        self.net.set_hours("2026-10-04T15:00", 3, prob=70, code=61)
        self.assertEqual(weather.get_weather("Toronto", "today"),
                         "Toronto today: 9°C now, high 20°C, low 8°C, light rain from 3 pm, 70% chance.")

    def test_raining_now(self):
        self.net.forecast["current"]["weather_code"] = 63
        self.net.set_hours("2026-10-04T08:00", 2, prob=80, code=63)
        self.assertEqual(weather.get_weather("Toronto", "now"), "Toronto now: 9°C, rain.")
        self.assertEqual(weather.get_weather("Toronto", "today"),
                         "Toronto today: 9°C now, high 20°C, low 8°C, rain now, 80% chance.")

    def test_storms_outrank_commoner_rain(self):
        self.net.set_hours("2026-10-04T15:00", 3, prob=60, code=61)
        self.net.set_hours("2026-10-04T16:00", 1, prob=60, code=95)
        self.assertTrue(weather.get_weather("Toronto").endswith("thunderstorms from 3 pm, 60% chance."))

    def test_now_warns_about_the_next_three_hours(self):
        self.net.set_hours("2026-10-04T10:00", 1, prob=60, code=61)
        self.assertEqual(weather.get_weather("Toronto", "now"),
                         "Toronto now: 9°C, partly cloudy, light rain from 10 am, 60% chance.")

    def test_now_mentions_a_possible_shower(self):
        self.net.set_hours("2026-10-04T11:00", 1, prob=40, code=3)
        self.assertEqual(weather.get_weather("Toronto", "now"),
                         "Toronto now: 9°C, partly cloudy, 40% chance of rain in the next 3 hours.")

    def test_rain_three_hours_out_is_not_now(self):
        self.net.set_hours("2026-10-04T12:00", 3, prob=90, code=63)  # starts at hour +4
        self.assertEqual(weather.get_weather("Toronto", "now"), "Toronto now: 9°C, partly cloudy.")

    def test_feels_like_and_wind_only_when_they_matter(self):
        self.net.forecast["current"].update(apparent_temperature=4.2, wind_speed_10m=35.4)
        self.assertEqual(weather.get_weather("Toronto", "now"),
                         "Toronto now: 9°C, partly cloudy, feels like 4°C, wind 35 km/h.")

    def test_today_never_says_a_high_below_the_current_temperature(self):
        self.net.forecast["current"]["temperature_2m"] = 21.0
        self.assertEqual(weather.get_weather("Toronto"), "Toronto today: 21°C now, high 21°C, low 8°C, clear, no rain expected.")

    def test_week_names_the_wet_days(self):
        self.net.set_days({0: (70, 61), 1: (60, 61), 4: (75, 63)})
        self.assertEqual(weather.get_weather("Toronto", "week"),
                         "Toronto over the next 7 days: highs 13 to 23°C, lows 6 to 11°C, "
                         "rain likely today, tomorrow and Thursday.")

    def test_week_counts_many_wet_days_and_says_snow(self):
        self.net.set_days({2: (80, 73), 5: (60, 71)})
        self.assertTrue(weather.get_weather("Toronto", "week").endswith("snow likely Tuesday and Friday."))
        self.net.set_days({0: (70, 61), 1: (70, 61), 3: (70, 61), 6: (70, 71)})
        self.clock += weather.FORECAST_TTL  # let the edited forecast in
        self.assertTrue(weather.get_weather("Toronto", "week").endswith("rain or snow likely on 6 of 7 days."))

    def test_missing_numbers_are_left_out_not_invented(self):
        del self.net.forecast["daily"]["temperature_2m_max"]
        self.assertEqual(weather.get_weather("Toronto", "tomorrow"), "Toronto tomorrow: low 6°C, clear, no rain expected.")

    def test_days_are_found_by_date_after_midnight(self):
        self.clock = CAPTURED + 15 * HOUR + 50 * 60  # 00:05 on Monday in Toronto
        self.assertEqual(weather.get_weather("Toronto"), "Toronto today: 9°C now, high 13°C, low 6°C, clear, no rain expected.")

    def test_a_day_past_the_forecast_is_refused(self):
        local = weather._local_now(self.net.forecast) + timedelta(days=8)
        with self.assertRaisesRegex(ToolError, "doesn't cover that day"):
            weather._report_today("Toronto", self.net.forecast, local)

    def test_incomplete_forecast_is_an_error(self):
        del self.net.forecast["current"]
        with self.assertRaisesRegex(ToolError, "incomplete forecast"):
            weather.get_weather("Toronto")

    def test_rain_rules(self):
        self.assertFalse(weather._wet(7, 51))     # a drizzle code at 7% isn't rain worth planning around
        self.assertTrue(weather._wet(30, 61))
        self.assertTrue(weather._wet(None, 61))
        self.assertTrue(weather._wet(55, 3))
        self.assertEqual([weather._clock(datetime(2026, 1, 1, h)) for h in (0, 9, 12, 15)],
                         ["midnight", "9 am", "noon", "3 pm"])

    def test_request_asks_for_what_the_reports_read(self):
        weather.get_weather("Toronto")
        (geo,), (forecast,) = self.net.asked("geocoding-api"), self.net.asked("api.open-meteo.com/v1/forecast")
        self.assertEqual((geo["name"], geo["count"]), ("Toronto", "10"))
        self.assertEqual((forecast["latitude"], forecast["longitude"]), ("43.706", "-79.399"))
        self.assertEqual(forecast["current"], "temperature_2m,apparent_temperature,weather_code,wind_speed_10m")
        self.assertEqual((forecast["timezone"], forecast["forecast_days"]), ("auto", "7"))

    def test_bad_day_is_refused(self):
        with self.assertRaisesRegex(ToolError, "Day must be one of"):
            weather.get_weather("Toronto", "yesterday")


class PlaceTests(WeatherCase):
    def find(self, text: str) -> weather.Place:
        return weather.find_place(text, weather._Budget(weather.BUDGET_SECS))

    def test_the_big_city_wins_over_the_small_town(self):
        self.assertEqual(self.find("Toronto"), weather.Place("Toronto", 43.70643, -79.39864, "Canada"))

    def test_region_or_country_picks_the_right_one(self):
        self.assertEqual(self.find("Toronto, Ohio").lat, 40.46423)
        self.assertEqual(self.find("toronto ohio").lat, 40.46423)  # no comma: tries "toronto" + Ohio next
        self.assertEqual(self.find("Paris Texas").lat, 33.66094)
        self.assertEqual(self.find("Paris, France").lat, 48.85341)
        self.assertEqual(self.find("Paris").lat, 48.85341)

    def test_a_country_is_refused_but_a_city_state_is_not(self):
        with self.assertRaisesRegex(ToolError, "Malaysia is a whole country. Name a city there."):
            self.find("Malaysia")
        self.assertEqual(self.find("Singapore").lat, 1.28967)

    def test_unknown_place(self):
        with self.assertRaisesRegex(ToolError, "I couldn't find a place called 'Xyzzyville'."):
            self.find("Xyzzyville")

    def test_spoken_shorthand(self):
        self.assertEqual(self.find("NYC").name, "New York")


class CacheTests(WeatherCase):
    def test_a_repeat_question_costs_no_network(self):
        first = weather.get_weather("Toronto")
        self.assertEqual(len(self.net.urls), 2)
        self.clock += 60
        self.assertEqual(weather.get_weather("toronto"), first)
        self.assertEqual(weather.get_weather("Toronto", "week")[:7], "Toronto")
        self.assertEqual(len(self.net.urls), 2)

    def test_forecasts_expire_after_twenty_minutes_places_never(self):
        weather.get_weather("Toronto")
        self.clock += weather.FORECAST_TTL - 1
        weather.get_weather("Toronto")
        self.assertEqual(len(self.net.urls), 2)
        self.clock += 2
        weather.get_weather("Toronto")
        self.assertEqual(len(self.net.asked("geocoding-api")), 1)
        self.assertEqual(len(self.net.asked("/v1/forecast")), 2)

    def test_cache_file(self):
        weather.get_weather("Toronto")
        cache = self.cache()
        self.assertEqual(cache["places"]["toronto"],
                         {"name": "Toronto", "lat": 43.70643, "lon": -79.39864, "country": "Canada"})
        self.assertEqual(cache["forecasts"]["43.706,-79.399"]["at"], CAPTURED)

    def test_stale_forecasts_are_dropped_when_saving(self):
        weather.CACHE_PATH.write_text(json.dumps({"forecasts": {
            "1.000,1.000": {"at": CAPTURED - weather.FORECAST_TTL - 1, "data": {}}, "2.000,2.000": "junk"}}),
            encoding="utf-8")
        weather.get_weather("Toronto")
        self.assertEqual(list(self.cache()["forecasts"]), ["43.706,-79.399"])

    def test_a_broken_cache_file_is_ignored(self):
        weather.CACHE_PATH.write_text("{not json", encoding="utf-8")
        self.assertTrue(weather.get_weather("Toronto").startswith("Toronto today:"))
        self.assertIn("toronto", self.cache()["places"])


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.open-meteo.com/", code, "error", Message(), None)


class NetworkTests(WeatherCase):
    def setUp(self) -> None:
        super().setUp()
        self._patch(weather, "_RETRY_PAUSE", 0.0)

    def test_a_stalled_service_gives_up_inside_the_budget(self):
        release = threading.Event()
        self.addCleanup(release.set)  # lets the abandoned request threads finish

        def stall(url: str, timeout: float) -> bytes:
            release.wait(10)
            raise OSError("released")

        self._patch(weather, "_http_get", stall)
        self._patch(weather, "BUDGET_SECS", 1.0)
        self._patch(weather, "_TRY_SECS", 0.4)
        started = time.monotonic()
        with self.assertRaisesRegex(ToolError, "^The weather service didn't answer in time.$"), \
             self.assertLogs(weather.log, "WARNING"):
            weather.get_weather("Toronto")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_the_budget_ends_in_our_error_not_the_dispatchers_timeout(self):
        self.assertLess(weather.BUDGET_SECS, registry.get("get_weather").timeout)
        release = threading.Event()
        self.addCleanup(release.set)
        self._patch(weather, "_http_get", lambda url, timeout: release.wait(10) and b"")
        self._patch(weather, "BUDGET_SECS", 0.8)
        with self.assertLogs(weather.log, "WARNING"):
            result = dispatch.execute(registry.get("get_weather"), {"location": "Toronto", "day": "now"})
        self.assertEqual((result.outcome, str(result)), ("error", "The weather service didn't answer in time."))

    def test_unreachable_service_is_tried_three_times(self):
        fail = mock.Mock(side_effect=urllib.error.URLError("getaddrinfo failed"))
        self._patch(weather, "_http_get", fail)
        with self.assertRaisesRegex(ToolError, r"^Couldn't reach the weather service \(getaddrinfo failed\).$"), \
             self.assertLogs(weather.log, "WARNING"):
            weather.get_weather("Toronto")
        self.assertEqual(fail.call_count, weather._TRIES)

    def test_a_refusal_is_not_retried(self):
        fail = mock.Mock(side_effect=http_error(400))
        self._patch(weather, "_http_get", fail)
        with self.assertRaisesRegex(ToolError, r"refused the request \(HTTP 400\)"):
            weather.get_weather("Toronto")
        self.assertEqual(fail.call_count, 1)

    def test_a_server_error_or_a_cut_reply_is_retried(self):
        for error in (http_error(503), http.client.IncompleteRead(b"{")):
            with self.subTest(error=type(error).__name__):
                weather.CACHE_PATH.unlink(missing_ok=True)
                calls = []

                def flaky(url: str, timeout: float) -> bytes:
                    calls.append(url)
                    if len(calls) == 1:
                        raise error
                    return self.net(url, timeout)

                with mock.patch.object(weather, "_http_get", flaky):
                    self.assertTrue(weather.get_weather("Toronto").startswith("Toronto today:"))
                self.assertEqual(len(calls), 3)  # geocode twice, forecast once


class HomeTests(WeatherCase):
    def test_home_from_settings(self):
        (self.dir / "settings.toml").write_text('[weather]\nhome = "Toronto, Canada"\n', encoding="utf-8")
        self.assertTrue(weather.get_weather().startswith("Toronto today: "))
        self.assertTrue(weather.get_weather("here", "now").startswith("Toronto now: "))
        self.locate.assert_not_called()

    def test_windows_location_is_asked_once_an_hour(self):
        self.locate.side_effect, self.locate.return_value = None, (43.7, -79.4)
        self.assertEqual(weather.get_weather(), "Here today: 9°C now, high 20°C, low 8°C, clear, no rain expected.")
        self.clock += weather.FORECAST_TTL + 1
        weather.get_weather("my location")
        self.assertEqual(self.locate.call_count, 1)
        self.assertEqual(self.net.asked("/v1/forecast")[0]["latitude"], "43.700")
        self.clock += weather._HERE_TTL
        weather.get_weather()
        self.assertEqual(self.locate.call_count, 2)

    def test_no_location_asks_for_a_city_without_waiting_every_time(self):
        self.locate.side_effect, self.locate.return_value = None, None
        for _ in range(2):
            with self.assertRaisesRegex(ToolError, r"^I don't know where you are. Name a city"):
                weather.get_weather()
        self.assertEqual(self.locate.call_count, 1)
        self.assertEqual(self.net.urls, [])
        self.clock += weather._HERE_RETRY
        with self.assertRaises(ToolError):
            weather.get_weather()
        self.assertEqual(self.locate.call_count, 2)

    def test_an_old_fix_beats_none(self):
        self.locate.side_effect, self.locate.return_value = None, (43.7, -79.4)
        weather.get_weather()
        self.clock += weather._HERE_TTL + 1
        self.locate.return_value = None
        self.assertTrue(weather.get_weather().startswith("Here today: "))

    def test_unreadable_settings_fall_back_to_windows(self):
        (self.dir / "settings.toml").write_text("[weather\nhome = ", encoding="utf-8")
        self.locate.side_effect, self.locate.return_value = None, (43.7, -79.4)
        with self.assertLogs(weather.log, "WARNING"):
            self.assertTrue(weather.get_weather().startswith("Here today: "))


class WindowsLocationTests(unittest.TestCase):
    def run_with(self, stdout: str = "", error: Exception | None = None):
        done = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
        with mock.patch.object(weather.subprocess, "run", return_value=done, side_effect=error) as run:
            return weather._locate_windows(3.0), run.call_args

    def test_one_safe_powershell_call(self):
        found, (args, kwargs) = self.run_with("ok 43.6532 -79.3832\n")
        self.assertEqual(found, (43.6532, -79.3832))
        self.assertTrue(args[0][0].lower().endswith("powershell.exe"))
        self.assertNotIn("shell", kwargs)
        self.assertEqual((kwargs["creationflags"], kwargs["timeout"]), (subprocess.CREATE_NO_WINDOW, 3.0))
        script = base64.b64decode(args[0][args[0].index("-EncodedCommand") + 1]).decode("utf-16-le")
        self.assertIn("Windows.Devices.Geolocation.Geolocator", script)
        self.assertIn("Wait(2000)", script)

    def test_refusals_and_nonsense_are_none(self):
        for out in ("denied Denied\n", "error The operation timed out\n", "ok 999 0\n", "", "ok north west\n"):
            with self.subTest(out=out):
                self.assertIsNone(self.run_with(out)[0])
        self.assertIsNone(self.run_with(error=subprocess.TimeoutExpired("powershell", 3.0))[0])
        self.assertIsNone(self.run_with(error=FileNotFoundError("powershell"))[0])


class ToolSpecTests(unittest.TestCase):
    def test_registered_safe_local_with_an_eight_second_timeout(self):
        spec = registry.get("get_weather")
        self.assertEqual((spec.level, spec.timeout, spec.cloud), (Level.SAFE, 8.0, False))
        location, day = spec.params
        self.assertEqual((location.name, location.kind, location.required), ("location", str, False))
        self.assertEqual((day.choices, day.default), (("now", "today", "tomorrow", "week"), "today"))


SAID = {
    "what's the weather": {"day": "today"},
    "What's the weather like?": {"day": "today"},
    "Hey Computer, how's the weather": {"day": "today"},
    "weather in toronto tomorrow": {"day": "tomorrow", "location": "toronto"},
    "Weather in Toronto tomorrow?": {"day": "tomorrow", "location": "toronto"},
    "what's the weather in paris texas": {"day": "today", "location": "paris texas"},
    "what's the weather in new york right now": {"day": "now", "location": "new york"},
    "what's the weather going to be like tomorrow": {"day": "tomorrow"},
    "how's the weather looking this weekend": {"day": "week"},
    "weather in the next few days": {"day": "week"},
    "what's the temperature outside": {"day": "now"},
    "what's the temperature in dubai": {"day": "now", "location": "dubai"},
    "will it rain today": {"day": "today"},
    "Will it rain tomorrow in London?": {"day": "tomorrow", "location": "london"},
    "is it going to rain": {"day": "today"},
    "is it raining": {"day": "now"},
    "is it raining outside": {"day": "now"},
    "is it snowing in denver": {"day": "now", "location": "denver"},
    "will it snow this week": {"day": "week"},
    "do I need an umbrella tomorrow": {"day": "tomorrow"},
    "is it cold outside": {"day": "now"},
    "is it cold in toronto": {"day": "now", "location": "toronto"},
    "how cold is it outside": {"day": "now"},
    "is it going to be hot tomorrow": {"day": "tomorrow"},
}

NOT_MINE = ["is it hot in here", "what's the weather app called", "is it raining men", "weather toronto banana",
            "what's the weather like in the morning", "it's cold", "what time is it", "rain sounds",
            "play rain sounds", "how cold is the moon"]


class IntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = intents._rules
        cls.accepted = intents.collect([weather])

    @classmethod
    def tearDownClass(cls):
        intents._rules = cls.saved

    def test_every_rule_compiles_and_names_a_real_tool(self):
        self.assertEqual(self.accepted, len(weather.INTENTS))

    def test_phrasings_reach_get_weather(self):
        for said, args in SAID.items():
            with self.subTest(said=said):
                self.assertEqual(intents.match(said), ("get_weather", args))

    def test_other_requests_are_left_alone(self):
        for said in NOT_MINE:
            with self.subTest(said=said):
                self.assertIsNone(intents.match(said))

    def test_no_clash_with_the_system_tools(self):
        intents.collect([system, weather])  # tools.MODULES order
        try:
            self.assertEqual(intents.match("what's the date"), ("current_time", {}))
            self.assertEqual(intents.match("how much battery"), ("battery_status", {}))
            self.assertEqual(intents.match("what's the weather"), ("get_weather", {"day": "today"}))
            self.assertEqual(intents.match("what's the temperature"), ("get_weather", {"day": "now"}))
        finally:
            intents.collect([weather])


if __name__ == "__main__":
    unittest.main()
