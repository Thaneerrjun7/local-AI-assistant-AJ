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
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

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
# YouTube window already open switches to it rather than opening another tab (the user's ask,
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


# ---- Chrome's tabs (the user, 2026-11-18) -----------------------------------------------------
# "Go to the tab that has YouTube open", "go to the fifth tab", "open a new tab", "close the Gmail
# tab", "close this tab and open a new one", "what tabs do I have open". Live the same day he
# asked for the YouTube tab and AJ had no way to see Chrome's tabs at all.
#
# UI Automation reads Chrome's tab strip locally (about 0.1 s for 15 tabs, measured): each tab
# is a TabItem of class "Tab", left to right, the one in front flagged as selected. A page's own
# tab-like buttons (YouTube's chips are TabItems too) have other classes and are left out.
# Switching selects the tab through UI Automation (about 0.5 s); a new tab and closing one are
# Chrome's own Ctrl+T and Ctrl+W, sent once its window is in front. Every action is read back
# from the tab strip afterwards, so "done" is only said when it happened.

_TAB_NOISE = re.compile(
    r"\s+-\s+(?:memory usage\s+-\s+)?[\d.,]+\s*[kmg]b(?:\s+freed up)?\s*$"
    r"|\s+-\s+(?:audio playing|muted|pinned|network error|crashed|(?:camera|microphone) in use|"
    r"sharing (?:your )?screen|recording[^-]*)\s*$", re.IGNORECASE)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "last": -1}
_FILLER = {"the", "a", "my", "tab", "tabs", "page", "website", "site", "com", "www", "open", "with", "that", "has",
           "on", "it", "in", "one"}


def tab_title(name: str) -> str:
    """A tab's name as Chrome gives it, without the memory note it adds ("YouTube - Memory usage - 113 MB")."""
    previous = None
    while previous != name:
        previous, name = name, _TAB_NOISE.sub("", name)
    return name.strip()


@dataclass
class Tab:
    title: str
    window: int     # the Chrome window it's in
    position: int   # 1 is the leftmost in its window
    selected: bool  # the one showing in its window
    node: object = None


def chrome_tabs() -> list[list[Tab]]:
    """Every Chrome window's tabs, left to right; the window in front first."""
    from aj.core import uia
    from aj.tools import apps
    out = []
    for window in apps.list_windows():
        if "chrome.exe" not in {e.lower() for e in window.exes}:
            continue
        strip = [n for n in uia.find_all(window.hwnd, "TabItem") if n.class_name == "Tab"]
        if strip:
            out.append([Tab(tab_title(n.name), window.hwnd, i + 1, bool(n.legacy_state & 2), n)
                        for i, n in enumerate(strip)])
    return out


def _wanted(words: str) -> re.Pattern | None:
    """What a tab's title must show for "the YouTube tab", "the tab with my inbox"."""
    name = _site_name(words)
    if name in SHOWN_AS:
        return re.compile(SHOWN_AS[name][0], re.IGNORECASE)
    keep = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _FILLER]
    if not keep:
        return None
    return re.compile("".join(rf"(?=.*\b{re.escape(w)})" for w in keep), re.IGNORECASE)


def _position(number: int | str | None) -> int | None:
    if number is None or number == "":
        return None
    if isinstance(number, str):
        word = number.strip().lower().removesuffix(" tab")
        if word in _ORDINALS:
            return _ORDINALS[word]
        word = re.sub(r"(?<=\d)(?:st|nd|rd|th)$", "", word)
        if not word.lstrip("-").isdigit():
            return None
        number = int(word)
    return int(number)


def _find_tab(windows: list[list[Tab]], words: str = "", number: int | str | None = None) -> Tab | None:
    position = _position(number)
    if position is not None:
        tabs = windows[0]
        index = position - 1 if position > 0 else len(tabs) + position
        return tabs[index] if 0 <= index < len(tabs) else None
    pattern = _wanted(words)
    if pattern is None:
        return None
    matches = [t for tabs in windows for t in tabs if pattern.search(t.title)]
    return next((t for t in matches if t.selected), None) or (matches[0] if matches else None)


def _show(tab: Tab) -> bool:
    """Bring the tab to the front: its window, then the tab itself. True once the strip says so."""
    from aj.core import uia
    from aj.tools import apps
    apps._focus(tab.window)
    if not tab.selected and not uia.select(tab.node):
        return False
    for _ in range(10):
        now = next((t for tabs in chrome_tabs() for t in tabs
                    if t.window == tab.window and t.position == tab.position), None)
        if now is not None and now.selected:
            return True
        time.sleep(0.05)
    return False


def _keys(window: int, keys: str) -> None:
    """Chrome's own shortcut, sent to its window once it's in front."""
    from aj.core import keyboard
    if not keyboard.focus(window):
        raise ToolError("I couldn't bring Chrome to the front.")
    try:
        keyboard.press(keys)
    except OSError as exc:  # ModifiersHeld, or Windows refused the input
        raise ToolError(f"Chrome didn't take the keys ({exc}).") from exc


def _count_in(window: int) -> int:
    return next((len(tabs) for tabs in chrome_tabs() if tabs and tabs[0].window == window), 0)


def _wait_count(window: int, before: int, change: int) -> bool:
    for _ in range(20):
        if _count_in(window) == before + change:
            return True
        time.sleep(0.05)
    return False


def _describe(tab: Tab, of: int = 1) -> str:
    title = tab.title if len(tab.title) <= 60 else tab.title[:57].rstrip() + "..."
    return f"tab {tab.position}, {title}" + (f" (the first of {of} that match)" if of > 1 else "")


def _list_tabs(windows: list[list[Tab]]) -> str:
    tabs = windows[0]
    showing = next((t for t in tabs if t.selected), None)
    titles = "; ".join(f"{t.position}: {t.title[:50]}" for t in tabs[:20])
    more = f" and {len(tabs) - 20} more" if len(tabs) > 20 else ""
    others = f" Other Chrome windows have {sum(len(w) for w in windows[1:])} more." if len(windows) > 1 else ""
    now = f" Showing tab {showing.position}." if showing else ""
    return f"Chrome has {len(tabs)} tabs open: {titles}{more}.{now}{others}"


@tool(level=Level.SAFE, timeout=8.0, cloud=False)
def open_website(url_or_site: str = "", tab: Literal["open", "switch", "new", "close", "close_and_new", "list"] = "open",
                 number: str | None = None) -> str:
    """Open a website, or work with Chrome's tabs. Opening a site that already has a tab goes to that tab.

    url_or_site: a web address like example.com, a site name like youtube or gmail, or words from a tab's title
    tab: open (the site, in its tab if one is open), switch (go to the tab showing url_or_site, or tab number), new (a new empty tab), close (the tab showing url_or_site, tab number, or else the current tab), close_and_new (close the current tab and open a new one), list (which tabs are open)
    number: a tab's place in the row, counting from the left: 1, 2, "fifth", or "last"
    """
    if tab == "open" and not url_or_site.strip():
        tab = "switch" if number else "new"
    windows = chrome_tabs() if tab != "open" or _site_name(url_or_site) else []
    if tab == "list":
        if not windows:
            raise ToolError("Chrome isn't open, so there are no tabs.")
        return _list_tabs(windows)
    if tab == "new":
        if not windows:
            _open_in_browser(SITES["google"])
            return "Chrome wasn't open, so I opened it."
        window = windows[0][0].window
        before = len(windows[0])
        _keys(window, "ctrl+t")
        if not _wait_count(window, before, +1):
            raise ToolError("I pressed new tab, but Chrome didn't open one.")
        return "New tab open."
    if tab in ("close", "close_and_new"):
        if not windows:
            raise ToolError("Chrome isn't open, so there's no tab to close.")
        target = (_find_tab(windows, url_or_site, number) if (url_or_site.strip() or number) and tab == "close"
                  else next((t for t in windows[0] if t.selected), None))
        if target is None:
            raise ToolError(f"I can't find a tab {('number ' + str(number)) if number else 'with ' + url_or_site} in Chrome.")
        if not _show(target):
            raise ToolError("I couldn't get to that tab to close it.")
        before = _count_in(target.window)
        _keys(target.window, "ctrl+w")
        if not _wait_count(target.window, before, -1):
            raise ToolError("I pressed close, but the tab is still there.")
        if tab == "close":
            return f"Closed {target.title[:60]}."
        _keys(target.window, "ctrl+t")
        if not _wait_count(target.window, before - 1, +1):
            raise ToolError(f"Closed {target.title[:60]}, but Chrome didn't open a new tab.")
        return f"Closed {target.title[:60]} and opened a new tab."
    if tab == "switch" or (tab == "open" and windows):
        target = _find_tab(windows, url_or_site, number)
        if target is not None:
            pattern = _wanted(url_or_site) if number is None else None
            count = sum(bool(pattern and pattern.search(t.title)) for tabs in windows for t in tabs)
            if _show(target):
                return f"Switched to {_describe(target, count)}."
            raise ToolError("I found the tab, but Chrome wouldn't switch to it.")
        if tab == "switch":
            what = f"number {number}" if number else f"with {url_or_site}"
            raise ToolError(f"There's no tab {what} open in Chrome.")
    if not url_or_site.strip():
        raise ToolError("Which website?")
    url = resolve_url(url_or_site)
    name = _site_name(url_or_site)
    showing = window_showing(name)
    if showing is not None:
        from aj.tools import apps
        if apps._focus(showing):
            return f"Switching to {SHOWN_AS[name][1]}."
    _open_in_browser(url)
    return f"Opened {_site(url)} in a new tab."


# ---- play_youtube ---------------------------------------------------------------------------

_VIDEO = re.compile(r'"videoRenderer":\{"videoId":"([A-Za-z0-9_-]{11})"')
_SHORT = re.compile(r'/shorts/([A-Za-z0-9_-]{11})|"reelWatchEndpoint":\{"videoId":"([A-Za-z0-9_-]{11})"')
_TITLE = re.compile(r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"')
_ANY_VIDEO = re.compile(r'"(?:videoId|contentId)":"([A-Za-z0-9_-]{11})"')
_ANY_TITLE = re.compile(r'"title":\{(?:"runs":\[\{"text"|"content"):"((?:[^"\\]|\\.)*)"')


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
    # YouTube serves other layouts too ("lockupViewModel"): one request in a few came back without a
    # single videoRenderer (scripts/check_tools.py, 2026-11-18), so any video id that isn't a Short.
    for m in _ANY_VIDEO.finditer(html):
        if m.group(1) not in shorts:
            title = _ANY_TITLE.search(html, m.end(), m.end() + 5000)
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


# Chrome's tabs (2026-11-18): "go to the fifth tab in the row above", "go to the tab that has YouTube
# already open", "open a new tab", "close this tab and open a new one", "close the Gmail tab",
# "what tabs do I have open". Numbers before names, so "the fifth tab" isn't a site called "fifth".
_ON_CHROME = r"(?: (?:on|in) (?:google )?chrome| (?:on|in) (?:the|my) browser)?"
_NTH = (r"(?P<n>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|last|"
        r"\d{1,2}(?:st|nd|rd|th)?)")
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                 "ten": 10, "eleven": 11, "twelve": 12}
_ROW = r"(?: (?:in|on|at) (?:the )?(?:row|top|tab bar|tab row|tabs)(?: above| at the top)?| from the left| above| up top)?"
_GO = r"(?:go|switch|take me|jump|move|get|bring me|flip)(?: back)? to"
_SHUT = r"(?:close|delete|kill|remove|shut|get rid of)"
_TAB_SITE = r"(?:the |my )?(?P<site>(?!(?:this|that|current|new|a|an)\b)[a-z0-9][a-z0-9 .]*?)"
_STILL_OPEN = r"(?: (?:open|on it|in it|already open|that s open|which is open|is open|playing|up))?"


def _tab(action: str, **extra) -> Callable[[re.Match], dict]:
    def build(m: re.Match) -> dict:
        args = {"tab": action, **extra}
        groups = m.groupdict()
        if groups.get("n"):
            args["number"] = str(_NUMBER_WORDS.get(groups["n"], groups["n"]))
        if groups.get("site"):
            args["url_or_site"] = groups["site"]
        return args
    return build


_TAB_INTENTS: list[tuple[str, str, Callable[[re.Match], dict]]] = [
    (rf"(?:what|which) tabs (?:do i have|have i got|are|i have)(?: open)?{_ON_CHROME}"
     rf"|(?:list|show|read|tell)(?: me)?(?: all)? (?:my |the )?(?:open )?tabs{_ON_CHROME}"
     rf"|how many tabs (?:do i have|are|have i got)(?: open)?{_ON_CHROME}", "open_website", _tab("list")),
    (rf"(?:open|make|start|add|give me) (?:a |another )?new tab{_ON_CHROME}", "open_website", _tab("new")),
    (rf"{_SHUT} (?:this|the current|current|my current|the) tab and (?:open|make|start|give me) (?:a |another )?new"
     r"(?: one| tab)", "open_website", _tab("close_and_new")),
    (rf"{_SHUT}(?: this| the current| current| my current| the)? tab{_ON_CHROME}", "open_website", _tab("close")),
    (rf"{_SHUT} (?:the |my )?{_NTH} tab{_ROW}", "open_website", _tab("close")),
    (rf"{_SHUT} tab (?:number )?(?P<n>\d{{1,2}}|{'|'.join(_NUMBER_WORDS)})", "open_website", _tab("close")),
    (rf"{_SHUT} {_TAB_SITE} tab{_ON_CHROME}", "open_website", _tab("close")),
    (rf"{_GO} (?:the |my )?{_NTH} tab{_ROW}{_ON_CHROME}", "open_website", _tab("switch")),
    (rf"(?:{_GO} )?tab (?:number )?(?P<n>\d{{1,2}}|{'|'.join(_NUMBER_WORDS)})", "open_website", _tab("switch")),
    (rf"{_GO} (?:the |my )?tab (?:that has|that s got|with|where|showing|that shows|that is on|on|for) (?:the )?"
     rf"(?P<site>[a-z0-9][a-z0-9 .]*?){_STILL_OPEN}{_ON_CHROME}", "open_website", _tab("switch")),
    (rf"{_GO} {_TAB_SITE} tab{_ON_CHROME}", "open_website", _tab("switch")),
]

INTENTS: list[tuple[str, str, Callable[[re.Match], dict]]] = [
    *_TAB_INTENTS,
    (r"play (?P<q>.+?) (?:on|from) youtube", "play_youtube", _query),
    (rf"(?:open|launch|go to|pull up|bring up) (?:the )?youtube(?: ?dot ?com| ?com)?(?: website)?{_ON_CHROME}",
     "open_website", lambda m: {"url_or_site": "youtube"}),
    # "Can you open google.com?" reached the model, which never opened it (live, 2026-10-19).
    # The transcript arrives without its dots ("google com"), so they're put back.
    (r"(?:open|go to|visit|load|pull up|bring up|take me to|show me|check) (?:the |my )?(?P<site>google|gmail|github|reddit|"
     r"wikipedia|netflix|chatgpt|instagram|linkedin|twitter|stack overflow|youtube music)"
     rf"(?: website| site| page)?{_ON_CHROME}", "open_website", lambda m: {"url_or_site": m["site"]}),
    (r"(?:open|go to|visit|load|pull up|bring up|take me to) (?:the )?(?:website )?"
     r"(?P<host>(?:[a-z0-9-]+(?: dot | ))+?(?:com|org|net|io|co|ai|dev|app|gov|edu|me|tv|uk|my|sg|in|au)"
     rf"(?:(?: dot | )(?:uk|my|sg|in|au))?)(?: website| site| page)?{_ON_CHROME}",
     "open_website", lambda m: {"url_or_site": re.sub(r" dot | ", ".", m["host"])}),
    # Not "search for my files" (the file tool's job), "google chrome", or "... on youtube" (the model's call).
    (r"(?:search (?:the web |the internet |online |google )?for|look up|google(?! chrome\b)) "
     r"(?!my )(?!.*\b(?:youtube|files?|folders?|documents?|downloads|desktop)\b)"
     r"(?P<q>.+?)(?: online| on the (?:web|internet)| on google)?",
     "web_search", _query),
]
