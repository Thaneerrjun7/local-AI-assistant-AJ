"""Web tools against saved pages. No test touches the network or opens a browser.

Run: .venv\\Scripts\\python.exe -m unittest discover -s tests -t .
"""
import io
import re
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

try:
    from ultron.tools import web
    from ultron.tools.intents import normalize
    from ultron.tools.registry import ToolError
except ModuleNotFoundError as exc:  # the tool registry isn't written yet
    if "ultron.tools.registry" not in str(exc) and "ultron.tools.intents" not in str(exc):
        raise
    raise unittest.SkipTest(f"tool registry not available yet ({exc})")

FIXTURES = Path(__file__).parent / "fixtures" / "web"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    """Just enough of http.client.HTTPResponse for web._fetch."""

    def __init__(self, body: str, status: int = 200, charset: str = "utf-8"):
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = f"text/html; charset={charset}"
        self._body = io.BytesIO(body.encode(charset))

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class WebCase(unittest.TestCase):
    """Every test gets a fake network (fails loudly unless served) and a fake os.startfile."""

    def setUp(self) -> None:
        self.urlopen = self._patch("ultron.tools.web.urllib.request.urlopen",
                                   side_effect=AssertionError("a test tried to use the real network"))
        self.startfile = self._patch("ultron.tools.web.os.startfile", create=True)

    def _patch(self, target: str, **kwargs) -> mock.MagicMock:
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def serve(self, body: str, status: int = 200) -> None:
        self.urlopen.side_effect = None
        self.urlopen.return_value = FakeResponse(body, status)

    def fail_network(self, error: Exception) -> None:
        self.urlopen.side_effect = error

    def requested_url(self) -> str:
        return self.urlopen.call_args.args[0].full_url

    def opened(self) -> list[str]:
        return [c.args[0] for c in self.startfile.call_args_list]


class ParseDuckDuckGoTests(unittest.TestCase):
    def test_organic_results_unwrapped_and_capped(self):
        results = web.parse_ddg(fixture("ddg_results.html"))
        self.assertEqual(len(results), 5)
        self.assertEqual([url for _, _, url in results], [
            "https://www.python.org/downloads/release/python-3130/",
            "https://docs.python.org/3/whatsnew/3.13.html",
            "https://endoflife.date/python",            # a direct link, not wrapped in /l/?uddg=
            "https://en.wikipedia.org/wiki/History_of_Python",
            "https://peps.python.org/pep-0719/",
        ])

    def test_ads_and_duplicates_are_dropped(self):
        text = " ".join(t + s + u for t, s, u in web.parse_ddg(fixture("ddg_results.html"), limit=20))
        self.assertNotIn("Sponsored", text)
        self.assertNotIn("ads.example", text)
        self.assertNotIn("Duplicate", text)

    def test_entities_and_bold_tags_become_plain_text(self):
        title, snippet, _ = web.parse_ddg(fixture("ddg_results.html"))[1]
        self.assertEqual(title, "What's New In Python 3.13 — Python 3 documentation")
        self.assertIn('Python 3.13 & compares it with 3.12: a new interactive interpreter, "free-threaded"', snippet)

    def test_formatted_for_the_local_model(self):
        text = web.format_results(web.parse_ddg(fixture("ddg_results.html")))
        lines = text.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("1. Python Release Python 3.13.0 | Python.org — Python 3.13.0 is"))
        self.assertTrue(lines[0].endswith("(python.org)"))
        self.assertTrue(lines[3].endswith("... (en.wikipedia.org)"))  # the long snippet is clipped
        self.assertLessEqual(len(text), 1500)

    def test_output_stays_under_the_budget_with_huge_results(self):
        huge = [("T" * 400, "word " * 400, f"https://site{i}.com/") for i in range(10)]
        text = web.format_results(huge)
        self.assertLessEqual(len(text), 1500)
        self.assertTrue(text.startswith("1. "))


class WebSearchTests(WebCase):
    def test_returns_top_results_from_a_get_request(self):
        self.serve(fixture("ddg_results.html"))
        text = web.web_search("  python 3.13   release date ")
        self.assertTrue(text.startswith("1. Python Release"))
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://html.duckduckgo.com/html/?q=python+3.13+release+date")
        self.assertIn("Windows NT", request.get_header("User-agent"))
        self.assertLess(self.urlopen.call_args.kwargs["timeout"], 8.0)
        self.startfile.assert_not_called()

    def test_captcha_page_is_reported_not_parsed(self):
        self.serve(fixture("ddg_captcha.html"), status=202)
        with self.assertRaisesRegex(ToolError, "captcha"):
            web.web_search("anything")

    def test_captcha_page_detected_even_with_200(self):
        self.serve(fixture("ddg_captcha.html"), status=200)
        with self.assertRaisesRegex(ToolError, "captcha"):
            web.web_search("anything")

    def test_no_results(self):
        self.serve(fixture("ddg_no_results.html"))
        with self.assertRaisesRegex(ToolError, "found nothing for 'qzxwvkjh'"):
            web.web_search("qzxwvkjh")

    def test_changed_layout_is_named_as_such(self):
        self.serve("<html><body><main><article>Totally new design</article></main></body></html>")
        with self.assertRaisesRegex(ToolError, "changed"):
            web.web_search("python")

    def test_timeout_and_http_errors_become_tool_errors(self):
        for error, expected in [
            (urllib.error.URLError(TimeoutError("timed out")), "Couldn't reach html.duckduckgo.com"),
            (TimeoutError("timed out"), "Couldn't reach html.duckduckgo.com"),
            (urllib.error.URLError("getaddrinfo failed"), "getaddrinfo failed"),
            (urllib.error.HTTPError("https://html.duckduckgo.com/html/", 503, "busy", Message(), None), "503"),
        ]:
            with self.subTest(error=error):
                self.fail_network(error)
                with self.assertRaisesRegex(ToolError, expected):
                    web.web_search("python")

    def test_empty_query(self):
        with self.assertRaises(ToolError):
            web.web_search("   ")
        self.urlopen.assert_not_called()


class YouTubeTests(WebCase):
    def test_first_video_skips_shorts_and_unescapes_the_title(self):
        video_id, title = web.first_video(fixture("yt_results.html"))
        self.assertEqual(video_id, "REALvid0003")
        self.assertEqual(title, 'lofi beats & "rainy" night ✨ [study mix]')

    def test_only_shorts_means_no_video(self):
        page = ('<script>var ytInitialData = {"contents":[{"videoRenderer":{"videoId":"SHORTbbbb02",'
                '"navigationEndpoint":{"commandMetadata":{"webCommandMetadata":{"url":"/shorts/SHORTbbbb02"}}}}}]};'
                '</script>')
        self.assertIsNone(web.first_video(page))
        self.assertIsNone(web.first_video("<html>consent page</html>"))

    def test_play_opens_the_watch_page(self):
        self.serve(fixture("yt_results.html"))
        reply = web.play_youtube("lofi beats")
        self.assertEqual(self.opened(), ["https://www.youtube.com/watch?v=REALvid0003"])
        self.assertIn("rainy", reply)
        self.assertEqual(self.requested_url(),
                         "https://www.youtube.com/results?search_query=lofi+beats&sp=EgIQAQ%3D%3D")

    RESULTS_PAGE = "https://www.youtube.com/results?search_query=lofi+%26+beats&sp=EgIQAQ%3D%3D"

    def test_no_video_found_opens_the_results_page(self):
        self.serve("<html>a page with no videos</html>")
        self.assertIn("opened the YouTube results", web.play_youtube("lofi & beats"))
        self.assertEqual(self.opened(), [self.RESULTS_PAGE])

    def test_offline_still_opens_the_results_page_and_logs_why(self):
        self.fail_network(urllib.error.URLError("offline"))
        with self.assertLogs("ultron.tools.web", "WARNING") as logs:
            self.assertIn("opened the YouTube results", web.play_youtube("lofi & beats"))
        self.assertIn("offline", logs.output[0])
        self.assertEqual(self.opened(), [self.RESULTS_PAGE])

    def test_never_uses_a_shell(self):
        self.serve(fixture("yt_results.html"))
        with mock.patch("subprocess.Popen") as popen:
            web.play_youtube("lofi")
        popen.assert_not_called()

    def test_browser_failure_is_a_tool_error(self):
        self.serve(fixture("yt_results.html"))
        self.startfile.side_effect = OSError("no browser registered")
        with self.assertRaisesRegex(ToolError, "couldn't open the browser"):
            web.play_youtube("lofi")


class OpenWebsiteTests(WebCase):
    def test_names_and_addresses(self):
        for given, expected in [
            ("youtube", "https://www.youtube.com"),
            ("YouTube", "https://www.youtube.com"),
            ("the youtube website", "https://www.youtube.com"),
            ("gmail", "https://mail.google.com"),
            ("github.com", "https://github.com"),
            ("github dot com", "https://github.com"),
            ("www.example.com/some/path?q=1&x=2", "https://www.example.com/some/path?q=1&x=2"),
            ("http://example.com", "http://example.com"),
            ("HTTPS://Example.com/Path", "https://Example.com/Path"),
            ("localhost:8080", "https://localhost:8080"),
            ("example.com.", "https://example.com"),
        ]:
            with self.subTest(given=given):
                self.assertEqual(web.resolve_url(given), expected)

    def test_only_web_addresses_get_through(self):
        for bad in ["file:///C:/Windows/System32/calc.exe", "C:\\Windows\\notepad.exe", "\\\\server\\share",
                    "javascript:alert(1)", "ms-settings:display", "ftp://example.com", "mailto:a@b.com",
                    "https://user:pass@evil.com", "https://example.com\\@evil.com", "notasite",
                    "https://", "http://exa mple.com", "https://example.com:99999"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ToolError):
                    web.open_website(bad)
        self.startfile.assert_not_called()

    def test_opens_in_the_default_browser(self):
        self.assertEqual(web.open_website("youtube"), "Opened youtube.com.")
        self.assertEqual(self.opened(), ["https://www.youtube.com"])
        self.urlopen.assert_not_called()


class IntentTests(unittest.TestCase):
    """The fast-path rules, matched the way intents.match does it."""

    def route(self, said: str) -> tuple[str, dict] | None:
        text = normalize(said)
        for pattern, tool_name, build in web.INTENTS:
            found = re.fullmatch(pattern, text, re.IGNORECASE)
            if found:
                return tool_name, build(found)
        return None

    def test_commands_it_claims(self):
        for said, expected in [
            ("Search for the best pizza in Bangalore", ("web_search", {"query": "the best pizza in bangalore"})),
            ("Ultron, look up Python 3.13 release date, please.",
             ("web_search", {"query": "python 3.13 release date"})),
            ("Google how tall is Mount Everest?", ("web_search", {"query": "how tall is mount everest"})),
            ("search the web for cheap flights online", ("web_search", {"query": "cheap flights"})),
            ("Play Bohemian Rhapsody on YouTube", ("play_youtube", {"query": "bohemian rhapsody"})),
            ("hey ultron play lofi hip hop from youtube now", ("play_youtube", {"query": "lofi hip hop"})),
            ("Open YouTube.", ("open_website", {"url_or_site": "youtube"})),
            ("open youtube.com", ("open_website", {"url_or_site": "youtube"})),
            ("go to youtube", ("open_website", {"url_or_site": "youtube"})),
        ]:
            with self.subTest(said=said):
                self.assertEqual(self.route(said), expected)

    def test_leaves_the_rest_to_others(self):
        for said in ["search for my resume", "search for files named budget", "look up my downloads",
                     "google chrome", "play some music", "search for cats on youtube", "open spotify",
                     "what's the weather like", "how do I search for a word in a pdf", "youtube"]:
            with self.subTest(said=said):
                self.assertIsNone(self.route(said))

    def test_every_rule_names_a_real_tool(self):
        names = {name for _, name, _ in web.INTENTS}
        self.assertLessEqual(names, {"web_search", "open_website", "play_youtube"})


if __name__ == "__main__":
    unittest.main()
