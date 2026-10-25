"""Web tools: search the web, open a site, play a YouTube video. Standard library only.

Why it is built this way:
- web_search is local-first. SivaSafe sent every web question to Gemini with Google grounding.
  Here DuckDuckGo's plain-HTML page is read directly (no API key, no account, nothing sent to
  an LLM provider) and the local model summarises the top results. When DuckDuckGo is down,
  shows its bot captcha or finds nothing, the tool raises ToolError and AJ escalates to
  Gemini with Google Search. It never pretends a search worked.
- Browsers are opened with os.startfile (ShellExecute): no console window, no shell, and no
  cmd.exe re-parsing of the '&' inside URLs, which SivaSafe's `cmd /c start` was exposed to.
- Only http(s) addresses are ever opened, so a model-written argument can't launch a local
  file, a network share or a protocol handler such as ms-settings:.
- play_youtube keeps SivaSafe's trick (take the first non-Shorts video id from the results
  page) and falls back to opening the results page when YouTube changes its markup.
"""
import http.client
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser

from aj.tools.registry import Level, ToolError, tool

log = logging.getLogger(__name__)

# A normal desktop browser identity: both sites serve stripped or empty pages to unknown clients.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
_HTTP_TIMEOUT = 5.0  # under the tools' 8 s budget, so a slow site gets our own clear error

# GET, not the form's POST: on 2026-10-04 a POST drew DuckDuckGo's captcha where a GET did not.
_DDG_URL = "https://html.duckduckgo.com/html/"
MAX_RESULTS = 5
_TITLE_CHARS = 100
_SNIPPET_CHARS = 220
_MAX_CHARS = 1500  # the local model summarises this, so keep its prompt small

_YT_RESULTS = "https://www.youtube.com/results"
_YT_VIDEOS_ONLY = "EgIQAQ=="  # YouTube's own "Type: Video" filter: drops channels and playlists

# Spoken names for sites people ask for by name. Anything else needs a real address.
SITES = {
    "youtube": "https://www.youtube.com",
    "youtube music": "https://music.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "reddit": "https://www.reddit.com",
    "wikipedia": "https://en.wikipedia.org",
    "netflix": "https://www.netflix.com",
    "chatgpt": "https://chatgpt.com",
    "twitter": "https://x.com",
    "instagram": "https://www.instagram.com",
    "linkedin": "https://www.linkedin.com",
    "stack overflow": "https://stackoverflow.com",
}

# How a browser window showing each site is titled, and the name to say. "Open YouTube" with a
# YouTube window already open switches to it rather than opening another tab (the boss's ask,
# 2026-10-18). Windows shows only each window's visible tab, so a tab behind another isn't seen.
SHOWN_AS = {
    "youtube": (r"(?:^| - )YouTube$", "YouTube"),
    "youtube music": (r"YouTube Music", "YouTube Music"),
    "gmail": (r"\bGmail\b", "Gmail"),
    "github": (r"\bGitHub\b", "GitHub"),
    "reddit": (r"\breddit\b", "Reddit"),
    "wikipedia": (r"\bWikipedia\b", "Wikipedia"),
    "netflix": (r"\bNetflix\b", "Netflix"),
    "chatgpt": (r"\bChatGPT\b", "ChatGPT"),
    "instagram": (r"\bInstagram\b", "Instagram"),
    "linkedin": (r"\bLinkedIn\b", "LinkedIn"),
    "stack overflow": (r"\bStack Overflow\b", "Stack Overflow"),
}
_BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
_BROWSER_TAIL = re.compile(r"\s+[-—]\s+(?:Google Chrome|Microsoft\u200b? Edge|Mozilla Firefox|Brave|Opera|Vivaldi)$")

_HOST = re.compile(r"^(localhost|\d{1,3}(\.\d{1,3}){3}|([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)  # "file:", "ms-settings:", "C:"
_HOST_PORT = re.compile(r"^[^/:]+:\d+(/|$)")                 # "localhost:8080" is not a scheme


# ---- fetching -------------------------------------------------------------------------------

def _read(response: http.client.HTTPResponse, deadline: float) -> bytes:
    """Read the body in pieces so a trickling server can't hold the tool past its time budget."""
    chunks: list[bytes] = []
    while chunk := response.read(65536):
        chunks.append(chunk)
        if time.monotonic() > deadline:
            raise TimeoutError("the page took too long to download")
    return b"".join(chunks)


def _fetch(url: str) -> tuple[int, str]:
    """GET a page as (status, text). Network trouble becomes a ToolError worth saying out loud."""
    host = urllib.parse.urlsplit(url).hostname
    req = urllib.request.Request(url, headers=_HEADERS)
    deadline = time.monotonic() + _HTTP_TIMEOUT
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            status, charset = r.status, r.headers.get_content_charset() or "utf-8"
            body = _read(r, deadline)
    except urllib.error.HTTPError as exc:
        raise ToolError(f"{host} answered with HTTP error {exc.code}.") from exc
    except (OSError, http.client.HTTPException) as exc:  # URLError, timeouts, resets, TLS, cut-off bodies
        raise ToolError(f"Couldn't reach {host} ({getattr(exc, 'reason', exc)}).") from exc
    try:
        return status, body.decode(charset, "replace")
    except LookupError:  # a charset Python doesn't know
        return status, body.decode("utf-8", "replace")


# ---- web_search -----------------------------------------------------------------------------

class _DdgParser(HTMLParser):
    """Collects title, link and snippet for each result block on DuckDuckGo's HTML page."""

    _FIELDS = {"result__a": "title", "result__snippet": "snippet"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._field: str | None = None  # the field being captured, until its tag closes
        self._tag = ""
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._field:
            if tag == self._tag:
                self._depth += 1
            return
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if tag == "div" and "result" in classes:
            self.results.append({"ad": "result--ad" in classes, "title": "", "href": "", "snippet": ""})
            return
        field = next((f for c, f in self._FIELDS.items() if c in classes), None)
        if field and self.results:
            self._field, self._tag, self._depth = field, tag, 1
            if field == "title":
                self.results[-1]["href"] = attr.get("href") or ""

    def handle_endtag(self, tag: str) -> None:
        if self._field and tag == self._tag:
            self._depth -= 1
            if self._depth == 0:
                self._field = None

    def handle_data(self, data: str) -> None:
        if self._field:
            self.results[-1][self._field] += data


def _real_url(href: str) -> str | None:
    """DuckDuckGo wraps links as //duckduckgo.com/l/?uddg=<target>. Unwrap; drop ads and non-web links."""
    parts = urllib.parse.urlsplit(urllib.parse.urljoin("https://duckduckgo.com/", href))
    if (parts.hostname or "").endswith("duckduckgo.com"):
        if parts.path != "/l/":
            return None  # ads go through /y.js, and internal links are no use
        parts = urllib.parse.urlsplit(urllib.parse.parse_qs(parts.query).get("uddg", [""])[0])
    return parts.geturl() if parts.scheme in ("http", "https") and parts.hostname else None


def _squash(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    text = _squash(text)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def _site(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")


def parse_ddg(html: str, limit: int = MAX_RESULTS) -> list[tuple[str, str, str]]:
    """(title, snippet, url) for the first `limit` organic results. Ads and duplicates are skipped."""
    parser = _DdgParser()
    parser.feed(html)
    parser.close()
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for r in parser.results:
        url = None if r["ad"] else _real_url(r["href"])
        title = _squash(r["title"])
        if not url or not title or url in seen:
            continue
        seen.add(url)
        out.append((title, _squash(r["snippet"]), url))
        if len(out) == limit:
            break
    return out


def format_results(results: list[tuple[str, str, str]]) -> str:
    """'1. Title — snippet (site.com)' lines, kept under _MAX_CHARS for the local model."""
    lines: list[str] = []
    used = 0
    for i, (title, snippet, url) in enumerate(results, 1):
        line = f"{i}. {_clip(title, _TITLE_CHARS)}"
        if snippet:
            line += f" — {_clip(snippet, _SNIPPET_CHARS)}"
        line += f" ({_site(url)})"
        if lines and used + len(line) + 1 > _MAX_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _is_blocked(status: int, html: str) -> bool:
    """DuckDuckGo answers suspected bots with HTTP 202 and a 'select the ducks' captcha page."""
    return status == 202 or "anomaly-modal" in html or "/anomaly.js" in html


@tool(level=Level.SAFE, timeout=8.0, cloud=False)
def web_search(query: str) -> str:
    """Search the web and return the top results.

    query: what to search for, in plain words
    """
    query = _squash(query)
    if not query:
        raise ToolError("There's nothing to search for.")
    status, html = _fetch(f"{_DDG_URL}?{urllib.parse.urlencode({'q': query[:300]})}")
    if _is_blocked(status, html):
        raise ToolError("DuckDuckGo is showing its anti-bot captcha, so web search is unavailable right now.")
    results = parse_ddg(html)
    if results:
        return format_results(results)
    if "no-results" in html or "result__" in html:
        raise ToolError(f"The web search found nothing for '{query}'.")
    raise ToolError("DuckDuckGo's results page has changed and can't be read any more.")


# ---- open_website ---------------------------------------------------------------------------

def _site_name(url_or_site: str) -> str:
    text = _squash(url_or_site).strip("\"'").rstrip(".,!?;")
    text = re.sub(r"\s+dot\s+", ".", text, flags=re.IGNORECASE)  # "github dot com", as it was spoken
    return text.lower().removeprefix("the ").removesuffix(" website").removesuffix(" site")


def window_showing(site: str) -> int | None:
    """A browser window whose visible tab is this named site, if one is open."""
    if site not in SHOWN_AS:
        return None
    from aj.tools import apps  # here: only needed for the named sites
    pattern = re.compile(SHOWN_AS[site][0], re.IGNORECASE)
    for window in apps.list_windows():
        if window.exes & _BROWSERS and pattern.search(_BROWSER_TAIL.sub("", window.title)):
            return window.hwnd
    return None


def resolve_url(url_or_site: str) -> str:
    """'youtube' -> https://www.youtube.com, 'github.com/x' -> https://github.com/x. Only http(s) passes."""
    text = _squash(url_or_site).strip("\"'").rstrip(".,!?;")
    text = re.sub(r"\s+dot\s+", ".", text, flags=re.IGNORECASE)  # "github dot com", as it was spoken
    name = _site_name(url_or_site)
    if name in SITES:
        return SITES[name]
    if "://" not in text:
        if _SCHEME.match(text) and not _HOST_PORT.match(text):
            raise ToolError(f"I only open web addresses, and '{url_or_site}' isn't one.")
        text = "https://" + text
    if "\\" in text or any(ord(c) < 32 for c in text):
        raise ToolError(f"'{url_or_site}' isn't a web address.")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise ToolError(f"I only open http and https addresses, and '{url_or_site}' isn't one.")
    if "@" in parts.netloc or " " in parts.netloc or not _HOST.match(parts.hostname or ""):
        raise ToolError(f"I don't know a site called '{url_or_site}'. Give me its address, like example.com.")
    try:
        parts.port
    except ValueError as exc:
        raise ToolError(f"'{url_or_site}' has a bad port number.") from exc
    return parts.geturl().replace(" ", "%20")


def _open_in_browser(url: str) -> None:
    """ShellExecute hands the URL to the default browser: no console, no shell, no cmd.exe parsing."""
    try:
        os.startfile(url)
    except OSError as exc:
        raise ToolError(f"Windows couldn't open the browser ({exc}).") from exc


@tool(level=Level.SAFE, timeout=8.0, cloud=False)
def open_website(url_or_site: str) -> str:
    """Open a website in the default browser.

    url_or_site: a web address like example.com, or a site name like youtube or gmail
    """
    url = resolve_url(url_or_site)
    name = _site_name(url_or_site)
    showing = window_showing(name)
    if showing is not None:
        from aj.tools import apps
        if apps._focus(showing):
            return f"Switching to {SHOWN_AS[name][1]}."
    _open_in_browser(url)
    return f"Opened {_site(url)}."


# ---- play_youtube ---------------------------------------------------------------------------

_VIDEO = re.compile(r'"videoRenderer":\{"videoId":"([A-Za-z0-9_-]{11})"')
_SHORT = re.compile(r'/shorts/([A-Za-z0-9_-]{11})|"reelWatchEndpoint":\{"videoId":"([A-Za-z0-9_-]{11})"')
_TITLE = re.compile(r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"')


def _json_text(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw


def first_video(html: str) -> tuple[str, str] | None:
    """(video id, title) of the first ordinary video on a YouTube results page. Shorts are skipped."""
    shorts = {a or b for a, b in _SHORT.findall(html)}
    matches = list(_VIDEO.finditer(html))
    for i, m in enumerate(matches):
        if m.group(1) in shorts:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        title = _TITLE.search(html, m.end(), min(end, m.end() + 5000))
        return m.group(1), _json_text(title.group(1)) if title else ""
    return None


@tool(level=Level.SAFE, timeout=8.0, cloud=False)
def play_youtube(query: str) -> str:
    """Play the top YouTube video for a search, in the default browser.

    query: what to watch or listen to, e.g. a song, artist or topic
    """
    query = _squash(query)
    if not query:
        raise ToolError("Tell me what to play.")
    results_url = f"{_YT_RESULTS}?{urllib.parse.urlencode({'search_query': query, 'sp': _YT_VIDEOS_ONLY})}"
    try:
        found = first_video(_fetch(results_url)[1])
    except ToolError as exc:
        log.warning("YouTube results unavailable, opening the search page instead: %s", exc)
        found = None
    if found is None:
        _open_in_browser(results_url)
        return f"Couldn't pick a video, so I opened the YouTube results for '{query}'."
    video_id, title = found
    _open_in_browser(f"https://www.youtube.com/watch?v={video_id}")
    return f"Playing '{title or query}' on YouTube."


# ---- fast-path intents ----------------------------------------------------------------------
# Matched in full against the normalized transcript (see intents.py): lowercase, no punctuation,
# and "hey computer", "please", "can you" ... already trimmed off both ends.

def _query(m: re.Match) -> dict:
    return {"query": m["q"]}


INTENTS: list[tuple[str, str, Callable[[re.Match], dict]]] = [
    (r"play (?P<q>.+?) (?:on|from) youtube", "play_youtube", _query),
    (r"(?:open|launch|go to|pull up|bring up) (?:the )?youtube(?: ?dot ?com| ?com)?(?: website)?",
     "open_website", lambda m: {"url_or_site": "youtube"}),
    # "Can you open google.com?" reached the model, which never opened it (live, 2026-10-19).
    # The transcript arrives without its dots ("google com"), so they're put back.
    (r"(?:open|go to|visit|load|pull up|bring up|take me to) (?:the )?(?P<site>google|gmail|github|reddit|"
     r"wikipedia|netflix|chatgpt|instagram|linkedin|twitter|stack overflow|youtube music)"
     r"(?: website| site| page)?", "open_website", lambda m: {"url_or_site": m["site"]}),
    (r"(?:open|go to|visit|load|pull up|bring up|take me to) (?:the )?(?:website )?"
     r"(?P<host>(?:[a-z0-9-]+(?: dot | ))+?(?:com|org|net|io|co|ai|dev|app|gov|edu|me|tv|uk|my|sg|in|au)"
     r"(?:(?: dot | )(?:uk|my|sg|in|au))?)(?: website| site| page)?",
     "open_website", lambda m: {"url_or_site": re.sub(r" dot | ", ".", m["host"])}),
    # Not "search for my files" (the file tool's job), "google chrome", or "... on youtube" (the model's call).
    (r"(?:search (?:the web |the internet |online |google )?for|look up|google(?! chrome\b)) "
     r"(?!my )(?!.*\b(?:youtube|files?|folders?|documents?|downloads|desktop)\b)"
     r"(?P<q>.+?)(?: online| on the (?:web|internet)| on google)?",
     "web_search", _query),
]
