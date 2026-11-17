"""The "thinker": a second local model for hard tasks, loaded only when one arrives.

MiniCPM5-2B in thinking mode scored 12/12 on the hard-task set against Qwen3-4B's 10/12
(docs/PLAN.md, "Thinker benchmark"), using 1.66 GB of VRAM and loading in 1.6 s. VRAM is
tight with Qwen and Kokoro already loaded (~5.9 of 6 GB with all three), so llama-server
starts with --fit: as many layers on the GPU as fit, the rest on the CPU. It unloads after a
few idle minutes so it costs nothing between hard questions (the user chose on-demand).

It runs where AJ's main model runs (core/placement.py): place() is told when that changes, and a
thinker loaded on the old unit stops, to load on the new one with the next hard question. On the
Radeon 780M it scored 10/12 (median 8.2 s, 2.3 GB of system RAM) against 12/12 on the RTX 4050
(data/bench/placement, 2026-10-27).
"""
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

from aj.config import LOG_DIR, MODELS_DIR, LlmSettings
from aj.core.proc import bind_to_aj

NO_WINDOW = 0x08000000
MODEL = MODELS_DIR / "llm" / "MiniCPM5-2B-Q4_K_M.gguf"
PORT = 8078
IDLE_UNLOAD_SECS = 300
SYSTEM = ("You are a careful problem solver working for a voice assistant. Think it through, then give "
          "the final answer plainly in one to four sentences that can be read aloud. No markdown.")


class Thinker:
    def __init__(self, llm: LlmSettings, model=MODEL, port: int = PORT):
        self.llm, self.model, self.port = llm, model, port
        self.server, self._unit = self._where(llm.backend, llm.device)
        self.base = f"http://127.0.0.1:{port}"
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._reaper = threading.Thread(target=self._unload_when_idle, daemon=True, name="thinker-idle")
        self._reaper.start()

    @property
    def available(self) -> bool:
        return self.model.exists()

    def _start(self, timeout: float = 60) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = open(LOG_DIR / "thinker-server.log", "w", encoding="utf-8")
        cmd = [self.server, "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port), *self._unit,
               "-c", "8192", "-np", "1", "--jinja", "--load-mode", "none", "--cache-ram", "0"]
        self._proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        bind_to_aj(self._proc)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            if self._proc.poll() is not None:
                self._proc = None
                raise RuntimeError(f"thinker server exited; see {log.name}")
            try:
                with urllib.request.urlopen(f"{self.base}/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("thinker server did not load in time")

    def _where(self, backend: str, device: str) -> tuple[str, list[str]]:
        """(the llama-server build, its unit arguments) for a unit. On the RTX 4050, --fit: as
        many layers as fit next to Qwen; elsewhere all of them, or none on the CPU."""
        if backend == "vulkan":
            return self.llm.vulkan_server, ["-ngl", "99", *(["--device", device] if device else [])]
        if backend == "cpu":
            return self.llm.server, ["-ngl", "0"]
        return self.llm.server, ["--fit", "on", "--fit-target", "400"]

    def place(self, backend: str, device: str) -> None:
        """AJ's main model moved: load there from now on, and let go of a thinker loaded elsewhere."""
        where = self._where(backend, device)
        if where != (self.server, self._unit):
            self.stop()
            with self._lock:
                self.server, self._unit = where

    def stop(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None

    def _unload_when_idle(self) -> None:
        while True:
            time.sleep(15)
            if self._proc is not None and time.monotonic() - self._last_used > IDLE_UNLOAD_SECS:
                self.stop()

    def ensure_loaded(self) -> None:
        with self._lock:
            self._last_used = time.monotonic()
            if self._proc is None or self._proc.poll() is not None:
                self._start()

    def solve(self, question: str, context: list[dict] | None = None, timeout: float = 60) -> str:
        """Answer a hard question with thinking on. Raises on failure; the caller escalates."""
        self.ensure_loaded()
        body = {"messages": [{"role": "system", "content": SYSTEM}, *(context or []),
                             {"role": "user", "content": question}],
                "temperature": 0.2, "max_tokens": 2500, "chat_template_kwargs": {"enable_thinking": True}}
        req = urllib.request.Request(f"{self.base}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                content = json.load(r)["choices"][0]["message"].get("content") or ""
        finally:
            self._last_used = time.monotonic()
        answer = content.strip()
        if not answer:
            raise RuntimeError("thinker returned no answer")
        return answer.removeprefix("ANSWER:").strip()
