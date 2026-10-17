"""Gemini 2.5 Flash: the cloud fallback, last in line after Qwen3-4B and the local thinker.

Streams over plain HTTPS (no SDK). The key comes from Windows Credential Manager and is
sent in a header, never in the URL. Thinking is off: SivaSafe measured ~1.1 s for a chat
reply with it off and ~5.2 s with reasoning on.
"""
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

from ultron.core.secrets import GEMINI, read_secret

MODEL = "gemini-2.5-flash"
_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:streamGenerateContent?alt=sse"


class GeminiUnavailable(Exception):
    """No key, no network, quota exhausted or a server error: the caller uses its local answer."""


def has_key() -> bool:
    return bool(read_secret(GEMINI))


def _to_gemini(messages: list[dict]) -> tuple[str, list[dict]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in messages if m["role"] in ("user", "assistant")]
    return system, contents


def stream_reply(messages: list[dict], web: bool = False, max_tokens: int = 400, timeout: float = 15,
                 cancel: threading.Event | None = None) -> Iterator[str]:
    """Yield text pieces. `web=True` lets Gemini ground the answer with Google Search."""
    key = read_secret(GEMINI)
    if not key:
        raise GeminiUnavailable("no Gemini key stored (run scripts/set_gemini_key.py)")
    system, contents = _to_gemini(messages)
    body = {"contents": contents,
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": max_tokens,
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if web:
        body["tools"] = [{"google_search": {}}]
    req = urllib.request.Request(_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                if cancel is not None and cancel.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                for cand in json.loads(line[5:]).get("candidates", []):
                    for part in (cand.get("content") or {}).get("parts", []):
                        if part.get("text"):
                            yield part["text"]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise GeminiUnavailable(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise GeminiUnavailable(f"network: {exc}") from exc
