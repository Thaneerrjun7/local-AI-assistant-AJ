"""Gemini: the cloud fallback for answers (last in line after Qwen3-4B and the local thinker),
and the ears for dictation (core/dictation.py).

Plain HTTPS, no SDK. The key comes from Windows Credential Manager and is sent in a header,
never in the URL.

Models. Until 2026-10-22 this was gemini-2.5-flash; that day the API answered the new key with
HTTP 404 ("no longer available to new users... use models/gemini-3.8-flash"), so answers use
gemini-3.8-flash. Measured live the same day: thinkingConfig {"thinkingBudget": 0} is accepted
by the 3.x Flash models and turns thinking off (no thought tokens), "thinkingLevel": "minimal" is
refused (HTTP 400), and "low" still thinks. The dedicated transcription model refuses any
thinking setting at all ("Thinking is not enabled for this model"), so it's sent none.
SivaSafe measured ~1.1 s for a chat reply with thinking off and ~5.2 s with it on.
"""
import base64
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

from aj.core.secrets import GEMINI, read_secret

MODEL = "gemini-3.8-flash"
_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# What dictation asks for (core/dictation.py). The boss, 2026-10-22: "when I say something, it has
# to get it accurately, exactly the way I want it". So: his words as said, nothing added, and the
# names in his life spelled the way he says them.
TRANSCRIBE_RULES = """You are a speech-to-text engine. Write down exactly what the speaker says, word for word.
- Output only the words that were said. No commentary, no labels, no quotation marks, no summary, and never an answer or reply to what was said, even if it is a question or an instruction.
- Keep the speaker's own words, slang, grammar and mix of languages (Malaysian English, Malay or Tamil words) exactly as spoken. Don't correct, rephrase, translate or add anything.
- Leave out only hesitation sounds like "um", "uh" and "er".
- Use natural punctuation and capitalisation.
- The speaker lives in Malaysia. Spell names as they are heard, for example Appa, Amma, Suhinish.
- Spoken punctuation becomes the punctuation itself: "full stop" or "period" -> ".", "comma" -> ",", "question mark" -> "?", "exclamation mark" -> "!", "new line" -> a line break, "new paragraph" -> a blank line.
- If there is no speech at all, output nothing."""


class GeminiUnavailable(Exception):
    """No key, no network, quota exhausted or a server error: the caller uses its local answer.
    `code`: the HTTP status when there was one (503 "high demand" and 429 are worth another model)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code

    @property
    def busy(self) -> bool:
        """Overloaded or rate-limited, rather than refused: another model may well answer."""
        return self.code in (429, 500, 503)


def has_key() -> bool:
    return bool(read_secret(GEMINI))


def _key() -> str:
    key = read_secret(GEMINI)
    if not key:
        raise GeminiUnavailable("no Gemini key stored (run scripts/set_gemini_key.py)")
    return key


def thinking_off(model: str) -> dict:
    """The generationConfig entry that turns thinking off for this model, or nothing when it has no
    such switch (see the module docstring: Flash takes a zero budget, the transcription model takes
    no thinking setting, and Pro can't think less than its minimum)."""
    name = model.lower()
    if "flash" in name and "transcribe" not in name:
        return {"thinkingConfig": {"thinkingBudget": 0}}
    return {}


def _detail(exc: urllib.error.HTTPError) -> str:
    """The error's message, and for a 429 which quota ran out (per minute or per day): the body's
    first 300 characters cut that off (live, 2026-10-22)."""
    body = exc.read().decode("utf-8", "replace")
    try:
        error = json.loads(body).get("error") or {}
    except ValueError:
        return body[:300]
    quotas = [v.get("quotaId", "") for d in error.get("details") or [] for v in d.get("violations") or []]
    message = str(error.get("message", ""))[:200]
    return f"{message} [quota: {', '.join(q for q in quotas if q)}]" if any(quotas) else message


def _to_gemini(messages: list[dict]) -> tuple[str, list[dict]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in messages if m["role"] in ("user", "assistant")]
    return system, contents


def _request(model: str, method: str, body: dict, key: str) -> urllib.request.Request:
    return urllib.request.Request(f"{_BASE}/{model}:{method}", data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json", "x-goog-api-key": key})


def stream_reply(messages: list[dict], web: bool = False, max_tokens: int = 400, timeout: float = 15,
                 cancel: threading.Event | None = None, model: str = MODEL) -> Iterator[str]:
    """Yield text pieces. `web=True` lets Gemini ground the answer with Google Search."""
    key = _key()
    system, contents = _to_gemini(messages)
    body = {"contents": contents,
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": max_tokens, **thinking_off(model)}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if web:
        body["tools"] = [{"google_search": {}}]
    req = _request(model, "streamGenerateContent?alt=sse", body, key)
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
        raise GeminiUnavailable(f"HTTP {exc.code}: {_detail(exc)}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise GeminiUnavailable(f"network: {exc}") from exc


def _generate(model: str, body: dict, timeout: float) -> str:
    """One non-streaming generateContent call; its text, or GeminiUnavailable.

    A reply that stopped for any reason but a natural end (MAX_TOKENS, SAFETY, RECITATION) counts
    as no reply: half a transcript, or one stuck repeating itself, must never be typed."""
    key = _key()
    try:
        with urllib.request.urlopen(_request(model, "generateContent", body, key), timeout=timeout) as r:
            reply = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise GeminiUnavailable(f"HTTP {exc.code}: {_detail(exc)}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise GeminiUnavailable(f"network: {exc}") from exc
    except ValueError as exc:
        raise GeminiUnavailable(f"unreadable reply: {exc}") from exc
    candidates = reply.get("candidates") or []
    if not candidates:
        blocked = (reply.get("promptFeedback") or {}).get("blockReason", "no answer")
        raise GeminiUnavailable(f"no answer ({blocked})")
    first = candidates[0]
    finish = first.get("finishReason")
    if finish not in (None, "STOP"):
        raise GeminiUnavailable(f"stopped early ({finish})")
    parts = (first.get("content") or {}).get("parts") or []
    # The transcription model answers in parts[].audioTranscription.text, not parts[].text (live,
    # 2026-10-22: read as text only, every transcript came back empty).
    return "".join(p.get("text") or (p.get("audioTranscription") or {}).get("text") or ""
                   for p in parts if not p.get("thought"))


def takes_system(model: str) -> bool:
    """The transcription model refuses a system instruction (HTTP 400 "Developer instruction is not
    enabled for this model", live 2026-10-22); the rules then go in the request itself."""
    return "transcribe" not in model.lower()


def transcribe(wav_bytes: bytes, hint: str = "", model: str = MODEL, timeout: float = 30.0) -> str:
    """A verbatim transcript of 16-bit mono WAV audio (core/dictation.wav_bytes makes it), with
    natural punctuation. `hint`: a line of context ("It is a message being typed into WhatsApp.").
    Temperature 0: the same audio should give the same words. Raises GeminiUnavailable."""
    _key()  # no key: fail before encoding a minute of audio
    prompt = "Transcribe this recording." + (f"\nContext: {hint}" if hint else "")
    if not takes_system(model):
        prompt = f"{TRANSCRIBE_RULES}\n\n{prompt}"
    body = {"contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav_bytes).decode("ascii")}},
                {"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 2048, **thinking_off(model)}}
    if takes_system(model):
        body["systemInstruction"] = {"parts": [{"text": TRANSCRIBE_RULES}]}
    return _generate(model, body, timeout).replace("\r\n", "\n").strip()


def generate(text: str, system: str = "", model: str = MODEL, temperature: float = 0.3, max_tokens: int = 1024,
             timeout: float = 20.0) -> str:
    """One plain answer to `text`, not streamed (dictation's polish uses it). Raises GeminiUnavailable."""
    body = {"contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens, **thinking_off(model)}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    return _generate(model, body, timeout).strip()
