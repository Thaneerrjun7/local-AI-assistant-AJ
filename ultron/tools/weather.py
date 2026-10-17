"""Weather for home or for any named place, from Open-Meteo (free, no API key, no account).

Ported from SivaSafe's weather_report.py, keeping what it learned the hard way:
- Every request has a timeout and the whole lookup a hard budget. SivaSafe's first version set
  no timeout, so one stalled TLS handshake to the geocoder froze the assistant until the user
  gave up. That stall clears on a fresh connection, so tries are short and repeated, and each
  runs in its own thread, because a socket timeout does not cover the DNS lookup.
- Places are cached forever (they don't move) and forecasts for 20 minutes, so a repeat question
  costs no network at all.
- A country is refused, not answered: its geocoder record is the centroid ("Malaysia" was the
  Borneo jungle). SivaSafe patched that with capital and region tables; the audit dropped them,
  so Ultron asks for a city instead of guessing.
- Home is `[weather] home = "City, Country"` in settings.toml, else Windows' own location
  service through a short PowerShell/WinRT call (no extra Python packages), cached so the
  sensor is asked at most hourly. SivaSafe said "ask again in a moment" while its helper ran;
  this waits a few seconds once instead.
- The answer is one short line built only from numbers in the API's reply, for Ultron to voice.
  Rain is mentioned when it is likely enough to plan around, with its hour and chance.
"""
import base64
import http.client
import json
import logging
import os
import re
import subprocess
import threading
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from ultron.config import DATA_DIR, SETTINGS_PATH
from ultron.tools.registry import Level, ToolError, tool

log = logging.getLogger(__name__)

CACHE_PATH = DATA_DIR / "weather_cache.json"
BUDGET_SECS = 6.0         # the whole lookup; under the tool's 8 s so our own error is what's heard
_TRY_SECS = 2.5           # a healthy call takes ~0.8 s; a stalled handshake wants a fresh try, not patience
_TRIES = 3
_RETRY_PAUSE = 0.2
FORECAST_TTL = 20 * 60
_HERE_TTL = 60 * 60       # ask Windows for a fresh fix at most hourly
_HERE_RETRY = 10 * 60     # after Windows said no, don't make every question wait on it again
_LOCATE_SECS = 4.0
_MAX_PLACES = 500

_LIKELY = 50              # % chance at which rain is "likely" on its own
_POSSIBLE = 30            # % chance worth mentioning, and the floor for trusting a rain code
_WINDY = 30               # km/h

_DAYS = ("now", "today", "tomorrow", "week")
_HERE = "Here"
_NO_HOME = "I don't know where you are. Name a city, like 'weather in London'."

_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_FORECAST_QUERY = {
    "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
    "hourly": "precipitation_probability,weather_code",
    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    "timezone": "auto",
    "forecast_days": "7",
}
_HEADERS = {"User-Agent": "Ultron-voice-assistant/1.0"}

# WMO weather codes, worded to sit inside a sentence ("partly cloudy", not "Partly cloudy sky").
_CONDITIONS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "light freezing drizzle",
    57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 66: "light freezing rain",
    67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers", 85: "light snow showers",
    86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with heavy hail",
}
_WET_CODES = frozenset(c for c in _CONDITIONS if c >= 51)
_SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# What people call "where I am" when the model passes it through as a place.
_HOME_WORDS = {"here", "home", "my location", "current location", "my current location", "my area",
               "local", "outside", "near me", "where i am"}
# Spoken shorthand the geocoder can't match. Kept tiny on purpose: no country tables.
_ALIASES = {"nyc": "New York", "new york city": "New York", "la": "Los Angeles", "sf": "San Francisco"}
_COUNTRY_WORDS = {"usa": "us", "america": "us", "uk": "gb", "britain": "gb", "uae": "ae"}


class _Budget:
    """One lookup's total allowance, shared by every request and the location call it makes."""

    def __init__(self, seconds: float):
        self._deadline = time.monotonic() + seconds

    def left(self) -> float:
        return self._deadline - time.monotonic()


def _now() -> float:
    return time.time()


# ---- HTTP -----------------------------------------------------------------------------------

def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_within(url: str, seconds: float) -> bytes:
    """_http_get on a daemon thread, abandoned after `seconds` (socket timeouts don't cover DNS)."""
    box: dict = {}

    def run() -> None:
        try:
            box["body"] = _http_get(url, seconds)
        except Exception as exc:  # handed to the caller below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="weather-http")
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError(f"no answer in {seconds:.1f} s")
    if "error" in box:
        raise box["error"]
    return box["body"]


def _get_json(url: str, budget: _Budget) -> dict:
    """GET and parse, retrying fresh connections until the budget runs out."""
    last: Exception | None = None
    for attempt in range(_TRIES):
        window = min(_TRY_SECS, budget.left())
        if window < 0.3:
            break
        try:
            data = json.loads(_get_within(url, window))
        except urllib.error.HTTPError as exc:
            if exc.code < 500:  # a 4xx will say the same thing next time
                raise ToolError(f"The weather service refused the request (HTTP {exc.code}).") from exc
            last = exc
        except (OSError, TimeoutError, ValueError, http.client.HTTPException) as exc:  # URLError is an OSError
            last = exc
        else:
            if isinstance(data, dict):
                return data
            last = ValueError("reply was not a JSON object")
        if attempt + 1 < _TRIES and budget.left() > _RETRY_PAUSE + 0.5:
            time.sleep(_RETRY_PAUSE)
    log.warning("weather request failed: %s (%s)", last, url.split("?")[0])
    if last is None or isinstance(last, TimeoutError):
        raise ToolError("The weather service didn't answer in time.")
    raise ToolError(f"Couldn't reach the weather service ({getattr(last, 'reason', last)}).") from last


# ---- cache ----------------------------------------------------------------------------------

_cache_lock = threading.Lock()


def _read_cache() -> dict:
    try:
        with CACHE_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        log.warning("weather cache not saved: %s", exc)


def _cache_get(section: str, key: str) -> dict | None:
    with _cache_lock:
        bucket = _read_cache().get(section)
    entry = bucket.get(key) if isinstance(bucket, dict) else None
    return entry if isinstance(entry, dict) else None


def _cache_put(section: str, key: str, value: dict) -> None:
    with _cache_lock:
        cache = _read_cache()
        bucket = cache.get(section) if isinstance(cache.get(section), dict) else {}
        bucket.pop(key, None)
        bucket[key] = value  # newest last, so trimming drops the oldest
        if section == "forecasts":
            bucket = {k: v for k, v in bucket.items()
                      if isinstance(v, dict) and _now() - _number(v.get("at")) < FORECAST_TTL}
        while section == "places" and len(bucket) > _MAX_PLACES:
            bucket.pop(next(iter(bucket)))
        cache[section] = bucket
        _write_cache(cache)


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


# ---- places ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    country: str = ""


def _squash(text: str) -> str:
    return " ".join(text.split())


def _fold(text: object) -> str:
    """Case- and accent-blind, so "zurich" is an exact match for "Zürich"."""
    plain = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in plain if not unicodedata.combining(c)).casefold()


def _search_terms(query: str) -> list[tuple[str, list[str]]]:
    """(name to search, qualifiers its region or country must match), best guess first.

    The geocoder matches place names only: "Paris Texas" finds nothing, "Paris" + Texas does.
    """
    if "," in query:
        name, _, rest = query.partition(",")
        return [(name.strip(), [h.strip() for h in rest.split(",") if h.strip()])]
    words = query.split()
    terms = [(query, [])]
    for cut in (1, 2):
        if len(words) > cut:
            terms.append((" ".join(words[:-cut]), [" ".join(words[-cut:])]))
    return terms


def _qualifies(result: dict, hint: str) -> bool:
    hint = _COUNTRY_WORDS.get(_fold(hint), _fold(hint))
    return any(_fold(result.get(k)) == hint
               for k in ("country", "admin1", "admin2", "admin3", "country_code") if result.get(k))


def _best(results: list[dict], name: str, hints: list[str]) -> dict | None:
    """Exact name first, then the most populous: "Toronto" is the 2.8 M city, not the Ohio town."""
    fits = [(i, r) for i, r in enumerate(results) if all(_qualifies(r, h) for h in hints)]
    if not fits:
        return None
    return min(fits, key=lambda ir: (_fold(ir[1].get("name")) != _fold(name),
                                     -_number(ir[1].get("population")), ir[0]))[1]


def _not_a_country(best: dict, results: list[dict]) -> dict:
    """A country's record is its centroid. City-states share their capital's name, so use that."""
    if not str(best.get("feature_code") or "").startswith("PCL"):
        return best
    for r in results:
        if (str(r.get("feature_code") or "").startswith("PPL")
                and r.get("country_code") == best.get("country_code")
                and _fold(r.get("name")) == _fold(best.get("name"))):
            return r
    raise ToolError(f"{best.get('name')} is a whole country. Name a city there.")


def _geocode(name: str, budget: _Budget) -> list[dict]:
    query = urllib.parse.urlencode({"name": name, "count": 10, "language": "en", "format": "json"})
    results = _get_json(f"{_GEO_URL}?{query}", budget).get("results")
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def find_place(text: str, budget: _Budget) -> Place:
    """Turn a spoken place name into coordinates: cache first, then the geocoder."""
    query = _squash(text).strip(" .?!")
    key = _fold(query)
    query = _ALIASES.get(key, query)
    cached = _cache_get("places", key)
    if cached:
        try:
            return Place(**cached)
        except TypeError:
            pass
    fallback = None
    for i, (name, hints) in enumerate(_search_terms(query)):
        if i and budget.left() < 2.0:  # leave time for the forecast itself
            break
        results = _geocode(name, budget)
        best = _best(results, name, hints)
        if best is None:
            continue
        best = _not_a_country(best, results)
        # The geocoder matches fuzzily, so an unqualified multi-word query only wins outright on
        # an exact name. Otherwise "Paris Texas" -> "Paris" + Texas still gets its turn.
        if hints or _fold(best.get("name")) == _fold(name) or len(query.split()) == 1:
            fallback = best
            break
        fallback = fallback or best
    if fallback is None:
        raise ToolError(f"I couldn't find a place called '{_squash(text)}'.")
    place = Place(str(fallback.get("name") or query), float(fallback["latitude"]),
                  float(fallback["longitude"]), str(fallback.get("country") or ""))
    _cache_put("places", key, asdict(place))
    return place


# ---- home -----------------------------------------------------------------------------------

def _home_setting() -> str | None:
    """`[weather] home = "City, Country"` from settings.toml, if set."""
    try:
        with Path(SETTINGS_PATH).open("rb") as fh:
            section = tomllib.load(fh).get("weather")
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("settings.toml unreadable, ignoring [weather] home: %s", exc)
        return None
    home = section.get("home") if isinstance(section, dict) else None
    return _squash(home) if isinstance(home, str) and home.strip() else None


_POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
# WinRT Geolocator from Windows PowerShell 5.1, which ships with Windows: no Python packages
# needed. A fix Windows already holds (up to 30 min old) comes back in ~0.05 s; a cold scan
# is cut off by WAIT_MS. Prints "ok <lat> <lon>", "denied <status>" or "error <message>".
_LOCATE_PS = r"""
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Devices.Geolocation.Geolocator, Windows.Devices.Geolocation, ContentType = WindowsRuntime]
  $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
  function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    if (-not $task.Wait(WAIT_MS)) { throw 'timeout' }
    $task.Result
  }
  $access = Await ([Windows.Devices.Geolocation.Geolocator]::RequestAccessAsync()) ([Windows.Devices.Geolocation.GeolocationAccessStatus])
  if ($access -ne 'Allowed') { "denied $access"; exit }
  $locator = New-Object Windows.Devices.Geolocation.Geolocator
  $fix = Await ($locator.GetGeopositionAsync([TimeSpan]::FromMinutes(30), [TimeSpan]::FromMilliseconds(WAIT_MS))) ([Windows.Devices.Geolocation.Geoposition])
  $p = $fix.Coordinate.Point.Position
  [string]::Format([Globalization.CultureInfo]::InvariantCulture, 'ok {0} {1}', $p.Latitude, $p.Longitude)
} catch { "error $($_.Exception.Message)" }
"""


def _locate_windows(seconds: float) -> tuple[float, float] | None:
    """(lat, lon) from Windows' location service, or None if it's off, denied or too slow."""
    script = _LOCATE_PS.replace("WAIT_MS", str(max(500, int((seconds - 1.0) * 1000))))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        out = subprocess.run([str(_POWERSHELL), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                             capture_output=True, text=True, timeout=seconds,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info("Windows location unavailable: %s", exc)
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "ok":
            try:
                lat, lon = float(parts[1]), float(parts[2])
            except ValueError:
                break
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    log.info("Windows location unavailable: %s", (out.stdout.strip() or out.stderr.strip())[:200])
    return None


def _here(budget: _Budget) -> Place:
    """Where Windows says this laptop is: cached, asked at most hourly, never worse than the last fix."""
    now = _now()
    seen = _cache_get("here", "windows") or {}
    has_fix = isinstance(seen.get("lat"), (int, float)) and isinstance(seen.get("lon"), (int, float))
    if has_fix and now - _number(seen.get("checked")) < _HERE_TTL:
        return Place(_HERE, seen["lat"], seen["lon"])
    if not has_fix and now - _number(seen.get("failed")) < _HERE_RETRY:
        raise ToolError(_NO_HOME)
    seconds = min(_LOCATE_SECS, budget.left() - 2.0)  # keep time for the forecast
    found = _locate_windows(seconds) if seconds >= 1.0 else None
    if found:
        _cache_put("here", "windows", {"lat": found[0], "lon": found[1], "checked": now})
        return Place(_HERE, *found)
    if has_fix:  # a stale fix beats none; the sensor gets asked again in an hour
        _cache_put("here", "windows", {**seen, "checked": now})
        return Place(_HERE, seen["lat"], seen["lon"])
    _cache_put("here", "windows", {"failed": now})
    raise ToolError(_NO_HOME)


def _home(budget: _Budget) -> Place:
    setting = _home_setting()
    return find_place(setting, budget) if setting else _here(budget)


# ---- forecast -------------------------------------------------------------------------------

def _forecast(place: Place, budget: _Budget) -> dict:
    key = f"{place.lat:.3f},{place.lon:.3f}"
    hit = _cache_get("forecasts", key)
    if hit and isinstance(hit.get("data"), dict) and 0 <= _now() - _number(hit.get("at")) < FORECAST_TTL:
        return hit["data"]
    query = urllib.parse.urlencode({"latitude": f"{place.lat:.3f}", "longitude": f"{place.lon:.3f}",
                                    **_FORECAST_QUERY}, safe=",")
    data = _get_json(f"{_FORECAST_URL}?{query}", budget)
    if not isinstance(data.get("current"), dict) or not isinstance(data.get("daily"), dict):
        raise ToolError("The weather service sent an incomplete forecast.")
    _cache_put("forecasts", key, {"at": _now(), "data": data})
    return data


def _num(value: object) -> int | None:
    """An API number rounded for speech, or None when it's missing. Never a made-up default."""
    try:
        return round(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _at(seq: object, i: int) -> object:
    return seq[i] if isinstance(seq, list) and 0 <= i < len(seq) else None


def _local_now(data: dict) -> datetime:
    """Wall-clock time at the place (naive, like the forecast's own timestamps)."""
    utc = datetime.fromtimestamp(_now(), timezone.utc).replace(tzinfo=None)
    return utc + timedelta(seconds=_number(data.get("utc_offset_seconds")))


def _day_index(data: dict, date: datetime) -> int:
    """Found by date, not position: a forecast cached at 23:55 still reads right at 00:05."""
    times = data["daily"].get("time") or []
    try:
        return times.index(date.date().isoformat())
    except ValueError:
        raise ToolError("The forecast doesn't cover that day.") from None


def _daily(data: dict, field: str, i: int) -> int | None:
    return _num(_at(data["daily"].get(field), i))


Hour = tuple[datetime, int | None, int | None]  # (local time, % chance of rain, WMO code)


def _hours(data: dict, start: datetime, end: datetime) -> list[Hour]:
    hourly = data.get("hourly") or {}
    probs, codes = hourly.get("precipitation_probability"), hourly.get("weather_code")
    out = []
    for i, stamp in enumerate(hourly.get("time") or []):
        try:
            t = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if start <= t < end:
            out.append((t, _num(_at(probs, i)), _num(_at(codes, i))))
    return out


def _wet(prob: int | None, code: int | None) -> bool:
    """Rain worth planning around. The ensemble's % and the main model's code often disagree
    (the live feed showed "light drizzle" days at 7%), so a code alone needs the odds behind it."""
    return (prob is not None and prob >= _LIKELY) or (code in _WET_CODES and (prob is None or prob >= _POSSIBLE))


def _usual(codes: list[int | None]) -> int | None:
    """The most common code; ties go to the more severe one."""
    counts = Counter(c for c in codes if c is not None)
    return max(counts, key=lambda c: (counts[c], c)) if counts else None


def _clock(t: datetime) -> str:
    if t.hour in (0, 12):
        return "midnight" if t.hour == 0 else "noon"
    return f"{t.hour % 12} {'am' if t.hour < 12 else 'pm'}"


def _rain(hours: list[Hour], now: datetime | None = None, span: str = "") -> str | None:
    """'light rain from 3 pm, 70% chance', '40% chance of rain', or None when it's dry."""
    peak = max((p for _, p, _ in hours if p is not None), default=None)
    wet = [(t, c) for t, p, c in hours if _wet(p, c)]
    if wet:
        codes = [c for _, c in wet if c in _WET_CODES]
        storms = [c for c in codes if c >= 95]
        what = _CONDITIONS[_usual(storms or codes)] if codes else "rain"
        start = wet[0][0]
        when = "now" if now is not None and start <= now else f"from {_clock(start)}"
        return f"{what} {when}" + (f", {peak}% chance" if peak is not None else "")
    if peak is not None and peak >= _POSSIBLE:
        return f"{peak}% chance of rain{span}"
    return None


def _outlook(hours: list[Hour], fallback_code: int | None, now: datetime | None = None) -> str | None:
    """Rain if it's likely enough to mention, otherwise the usual sky, saying outright when no
    rain is expected: Ultron answers "does it rain today?" from this, and "clear" alone left
    the model guessing."""
    rain = _rain(hours, now)
    if rain:
        return rain
    usual = _usual([c for _, _, c in hours])  # 0 is "clear", so test for None, not truth
    code = fallback_code if usual is None else usual
    dry = "no rain expected" if hours and code not in _WET_CODES else None
    return ", ".join(p for p in (_CONDITIONS.get(code), dry) if p) or None


def _deg(value: int) -> str:
    return f"{value}°C"


def _report_now(label: str, data: dict, local: datetime) -> str:
    current = data["current"]
    temp = _num(current.get("temperature_2m"))
    if temp is None:
        raise ToolError(f"The forecast for {label} came back without a temperature.")
    code = _num(current.get("weather_code"))
    parts = [_deg(temp)] + ([_CONDITIONS[code]] if code in _CONDITIONS else [])
    feels = _num(current.get("apparent_temperature"))
    if feels is not None and abs(feels - temp) >= 3:
        parts.append(f"feels like {_deg(feels)}")
    wind = _num(current.get("wind_speed_10m"))
    if wind is not None and wind >= _WINDY:
        parts.append(f"wind {wind} km/h")
    if code not in _WET_CODES:  # if it's raining now, the condition already says so
        hour = local.replace(minute=0, second=0, microsecond=0)
        soon = _hours(data, hour + timedelta(hours=1), hour + timedelta(hours=4))
        rain = _rain(soon, span=" in the next 3 hours")
        if rain:
            parts.append(rain)
    return f"{label} now: {', '.join(parts)}."


def _report_today(label: str, data: dict, local: datetime) -> str:
    i = _day_index(data, local)
    hour = local.replace(minute=0, second=0, microsecond=0)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    temp = _num(data["current"].get("temperature_2m"))
    high, low = _daily(data, "temperature_2m_max", i), _daily(data, "temperature_2m_min", i)
    if temp is not None:  # the daily range is hourly; never say "29 now, high 28"
        high = max(high, temp) if high is not None else None
        low = min(low, temp) if low is not None else None
    parts = [f"{_deg(temp)} now"] if temp is not None else []
    parts += [f"high {_deg(high)}"] if high is not None else []
    parts += [f"low {_deg(low)}"] if low is not None else []
    outlook = _outlook(_hours(data, hour, midnight), _daily(data, "weather_code", i), now=hour)
    return _sentence(label, "today", parts + ([outlook] if outlook else []))


def _report_tomorrow(label: str, data: dict, local: datetime) -> str:
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    i = _day_index(data, start)
    high, low = _daily(data, "temperature_2m_max", i), _daily(data, "temperature_2m_min", i)
    parts = [f"high {_deg(high)}"] if high is not None else []
    parts += [f"low {_deg(low)}"] if low is not None else []
    outlook = _outlook(_hours(data, start, start + timedelta(days=1)), _daily(data, "weather_code", i))
    return _sentence(label, "tomorrow", parts + ([outlook] if outlook else []))


def _report_week(label: str, data: dict, local: datetime) -> str:
    first = _day_index(data, local)
    days = range(first, min(first + 7, len(data["daily"].get("time") or [])))
    highs = [h for h in (_daily(data, "temperature_2m_max", i) for i in days) if h is not None]
    lows = [lo for lo in (_daily(data, "temperature_2m_min", i) for i in days) if lo is not None]
    parts = []
    if highs:
        parts.append(f"highs {_range(highs)}")
    if lows:
        parts.append(f"lows {_range(lows)}")
    probs = {i: _daily(data, "precipitation_probability_max", i) for i in days}
    codes = {i: _daily(data, "weather_code", i) for i in days}
    wet = [i for i in days if _wet(probs[i], codes[i])]
    if wet:
        snow = [i for i in wet if codes[i] in _SNOW_CODES]
        kind = "snow" if len(snow) == len(wet) else "rain or snow" if snow else "rain"
        if len(wet) > 3:
            parts.append(f"{kind} likely on {len(wet)} of {len(days)} days")
        else:
            names = [_day_name(data, i, i - first) for i in wet]
            parts.append(f"{kind} likely {_join(names)}")
    else:
        peak = max((p for p in probs.values() if p is not None), default=None)
        if peak is not None:
            parts.append(f"up to {peak}% chance of rain" if peak >= _POSSIBLE else "little chance of rain")
    return _sentence(label, "over the next 7 days", parts)


def _range(values: list[int]) -> str:
    low, high = min(values), max(values)
    return _deg(low) if low == high else f"{low} to {_deg(high)}"


def _day_name(data: dict, i: int, ahead: int) -> str:
    if ahead < 2:
        return ("today", "tomorrow")[ahead]
    return _WEEKDAYS[datetime.fromisoformat(data["daily"]["time"][i]).weekday()]


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def _sentence(label: str, when: str, parts: list[str]) -> str:
    if not parts:
        raise ToolError(f"The forecast for {label} came back empty.")
    return f"{label} {when}: {', '.join(parts)}."


_REPORTS: dict[str, Callable[[str, dict, datetime], str]] = {
    "now": _report_now, "today": _report_today, "tomorrow": _report_tomorrow, "week": _report_week,
}


@tool(level=Level.SAFE, timeout=8.0, cloud=False)
def get_weather(location: str | None = None,
                day: Literal["now", "today", "tomorrow", "week"] = "today") -> str:
    """Get the weather forecast.

    location: a city, e.g. "Toronto" or "Paris, Texas"; leave it out for the user's home
    day: "now" for current conditions, "week" for the next 7 days
    """
    if day not in _DAYS:
        raise ToolError(f"Day must be one of: {', '.join(_DAYS)}.")
    budget = _Budget(BUDGET_SECS)
    wanted = _squash(location or "").strip(" .?!")
    place = find_place(wanted, budget) if wanted and _fold(wanted) not in _HOME_WORDS else _home(budget)
    data = _forecast(place, budget)
    return _REPORTS[day](place.name, data, _local_now(data))


# ---- fast-path intents ----------------------------------------------------------------------
# Matched against a lowercased, punctuation-stripped transcript ("what's" -> "whats" or "what s").

_LEAD = (r"^(?:(?:hey\s+|ok(?:ay)?\s+)?ultron\s+)?(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
         r"(?:(?:tell\s+me|check|give\s+me|get\s+me|show\s+me)\s+)?")
_TAIL = r"(?:\s+(?:please|ultron|for\s+me))*$"
_NOT_A_PLACE = (r"(?!(?:here|there|me|outside|right\s+now|now|today|tonight|tomorrow|later|"
                r"this\s+\w+|next\s+\w+|the\s+(?:week|weekend|next|moment|day|morning|afternoon|evening|night))\b)")
_AROUND = r"(?:\s+(?:near\s+me|around\s+(?:here|me)|here|where\s+i\s+am|outside|out\s+there))?"
# A lone "right" at the end is "right now": the normalizer strips a trailing "now".
_WHEN_WORDS = (r"right\s+now|right$|now|currently|at\s+the\s+moment|today|tonight|later(?:\s+today)?|"
               r"this\s+(?:morning|afternoon|evening)|tomorrow(?:\s+(?:morning|afternoon|evening|night))?|"
               r"(?:for\s+)?(?:this|the)\s+week(?:end)?|(?:for\s+|over\s+|in\s+)?the\s+next\s+(?:few|couple\s+of|7|seven)\s+days")


def _where(group: str) -> str:
    return rf"(?:\s+(?:in|for|at)\s+{_NOT_A_PLACE}(?P<{group}>[a-z][a-z\s'-]*?)(?:\s+like)?)?"


def _when(group: str) -> str:
    return rf"(?:\s+(?P<{group}>{_WHEN_WORDS}))?"


_PLACE_AND_DAY = _AROUND + _where("loc") + _when("day") + _where("loc2") + _TAIL

_FORECAST_ASK = (_LEAD + r"(?:(?:what|how)(?:\s*s|\s+is|\s+will|\s+does)\s+)?(?:the\s+)?"
                 r"(?P<what>weather(?:\s+forecast|\s+report)?|forecast|temperature)"
                 r"(?:\s+(?:like|look(?:ing)?(?:\s+like)?|(?:going\s+to|gonna)\s+be(?:\s+like)?|be(?:\s+like)?))?"
                 r"(?P<out>\s+(?:outside|out\s+there))?" + _PLACE_AND_DAY)
_RAIN_ASK = (_LEAD + r"(?P<lead>is\s+it|will\s+it|does\s+it|is\s+there|do\s+you\s+think\s+it(?:\s*ll|\s+will)?)\s+"
             r"(?P<future>(?:going\s+to|gonna|supposed\s+to|likely\s+to|expected\s+to)\s+)?(?:be\s+)?"
             r"(?:(?:a|any)\s+(?:chance\s+of\s+)?)?"
             r"(?P<what>rain(?:ing|y)?|snow(?:ing|y)?|pouring|storm(?:ing|y)?|drizzl(?:e|ing))" + _PLACE_AND_DAY)
# Needs "outside", a day or a place, so "is it hot in here" stays with the model.
_FEEL_ASK = (_LEAD + r"(?=.*\b(?:outside|out|today|tonight|tomorrow|week|in\s+(?!here\b)[a-z]))"
             r"(?:(?P<future>will\s+it\s+be|is\s+it\s+(?:going\s+to|gonna)\s+be)|is\s+it|how)\s+"
             r"(?:cold|hot|warm|chilly|freezing|humid)"
             r"(?:\s+is\s+it|\s+(?P<future2>will\s+it\s+be|is\s+it\s+(?:going\s+to|gonna)\s+be))?"
             r"(?:\s+(?:outside|out\s+there|out))?" + _PLACE_AND_DAY)
_UMBRELLA_ASK = _LEAD + r"(?:do|will|should)\s+i\s+need\s+(?:an?\s+)?(?:umbrella|raincoat)" + _PLACE_AND_DAY


def _day_from(words: str | None, default: str) -> str:
    if not words:
        return default
    words = _squash(words)
    if words.startswith("tomorrow"):
        return "tomorrow"
    if words in ("now", "right now", "right", "currently", "at the moment"):
        return "now"
    return "week" if "week" in words or "days" in words else "today"


def _args(m: re.Match, default_day: str) -> dict:
    groups = m.groupdict()
    args = {"day": _day_from(groups.get("day"), default_day)}
    place = _squash(groups.get("loc") or groups.get("loc2") or "")
    if place:
        args["location"] = place
    return args


def _forecast_args(m: re.Match) -> dict:
    return _args(m, "now" if m.group("what") == "temperature" or m.group("out") else "today")


def _rain_args(m: re.Match) -> dict:
    raining_now = m.group("lead") == "is it" and not m.group("future") and m.group("what").endswith("ing")
    return _args(m, "now" if raining_now else "today")


def _feel_args(m: re.Match) -> dict:
    return _args(m, "today" if m.group("future") or m.group("future2") else "now")


INTENTS: list[tuple[str, str, Callable[[re.Match], dict]]] = [
    (_FORECAST_ASK, "get_weather", _forecast_args),
    (_RAIN_ASK, "get_weather", _rain_args),
    (_FEEL_ASK, "get_weather", _feel_args),
    (_UMBRELLA_ASK, "get_weather", lambda m: _args(m, "today")),
]
