"""Store your Gemini API key in Windows Credential Manager, then test it with one tiny request.

Run it yourself and paste the key at the hidden prompt (it is not echoed, not written to
any file, and not kept in your shell history):
    .venv\\Scripts\\python.exe scripts\\set_gemini_key.py
Get a key at https://aistudio.google.com/apikey. To remove it later: Control Panel >
Credential Manager > Windows Credentials > AJ/Gemini.
"""
import getpass
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aj.core import gemini  # noqa: E402
from aj.core.secrets import GEMINI, write_secret  # noqa: E402


def main() -> None:
    key = getpass.getpass("Gemini API key (hidden): ").strip()
    if not key:
        sys.exit("Nothing entered; no change.")
    write_secret(GEMINI, key)
    print("Stored in Windows Credential Manager as AJ/Gemini. Testing...")
    t0 = time.perf_counter()
    try:
        reply = "".join(gemini.stream_reply([{"role": "user", "content": "Reply with the single word: ready"}],
                                            max_tokens=5))
    except gemini.GeminiUnavailable as exc:
        sys.exit(f"Stored, but the test call failed: {exc}")
    print(f"Gemini answered {reply.strip()!r} in {time.perf_counter() - t0:.1f} s. AJ's fallback is ready.")


if __name__ == "__main__":
    main()
