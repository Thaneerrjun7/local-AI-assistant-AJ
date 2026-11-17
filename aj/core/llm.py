"""The local language model: a llama-server child process plus a streaming client."""
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

from aj.config import LOG_DIR, LlmSettings
from aj.core.proc import bind_to_aj

NO_WINDOW = 0x08000000


def server_command(s: LlmSettings) -> list[str]:
    """llama-server's command line for these settings, on the unit they name (s.backend, s.device).
    RAM on this 16 GB laptop is the tight resource (Phase 1 measurements):
      --load-mode none  ~0.75 GB instead of 2.8 GB (mmap keeps the model file resident)
      --cache-ram 0     no host-RAM prompt cache; its default lets it grow to 8 GB.
                        Each slot's KV cache on the GPU still reuses the persona prefix.
    -np 2: slot 0 for conversation, slot 1 for background memory extraction."""
    server = s.vulkan_server if s.backend == "vulkan" else s.server
    layers = 0 if s.backend == "cpu" else s.gpu_layers
    cmd = [server, "-m", s.model, "--host", "127.0.0.1", "--port", str(s.port),
           "-ngl", str(layers), "-c", str(s.context), "-np", "2", "--jinja",
           "--load-mode", "none", "--cache-ram", "0"]
    if s.device and s.backend != "cpu":
        cmd += ["--device", s.device]
    if s.kv_cache_type and s.kv_cache_type != "f16":
        cmd += ["-fa", "on", "-ctk", s.kv_cache_type, "-ctv", s.kv_cache_type]
    return cmd


class LlamaServer:
    def __init__(self, settings: LlmSettings):
        self.settings = settings
        self.proc: subprocess.Popen | None = None
        self.base = f"http://127.0.0.1:{settings.port}"

    def start(self, timeout: float = 120) -> float:
        """Start the server and wait until the model is loaded. Returns load seconds."""
        self.launch()
        return self.wait_ready(timeout)

    def launch(self) -> None:
        """Start the server process without waiting for it. The app does this before importing
        the rest of itself, so the model loads while Python does (2026-10-21: about 1.4 s
        earlier). Calling it again does nothing."""
        if self.proc is not None:
            return
        s = self.settings
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # One log per unit: while AJ moves (core/placement.py) two servers run for a moment.
        name = "llama-server.log" if s.backend == "cuda" else f"llama-server-{s.backend}.log"
        log = open(LOG_DIR / name, "w", encoding="utf-8")
        cmd = server_command(s)
        self._launched = time.perf_counter()
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        bind_to_aj(self.proc)
        self._log = log

    def wait_ready(self, timeout: float = 120) -> float:
        """Wait until the model is loaded. Returns seconds since launch."""
        t0, log = self._launched, self._log
        while time.perf_counter() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited ({self.proc.returncode}); see {log.name}")
            try:
                with urllib.request.urlopen(f"{self.base}/health", timeout=2) as r:
                    if r.status == 200:
                        return time.perf_counter() - t0
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("llama-server did not load in time")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


class Cancelled(Exception):
    pass


def wake_gpu(base: str) -> None:
    """One-token request on slot 1 to pull the GPU out of its idle power state.

    Measured on this laptop: after 5-10 s idle the RTX 4050 sits at P5/P8 (210 MHz) and a
    30-token prompt takes 250-330 ms instead of ~30 ms. Sent when you start talking, the GPU
    is back at full clock by the time you finish. Slot 1 keeps slot 0's conversation cache intact.
    """
    body = {"messages": [{"role": "user", "content": "ok"}], "max_tokens": 1, "id_slot": 1,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(f"{base}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        pass  # best effort: a failed wake-up only costs the speed-up


def stream_events(base: str, messages: list[dict], settings: LlmSettings, tools: list[dict] | None = None,
                  cancel: threading.Event | None = None, slot: int = 0, temperature: float | None = None,
                  tool_choice: str = "auto", max_tokens: int | None = None) -> Iterator[tuple[str, object]]:
    """Like stream_reply, but the model may also call tools. Yields ("text", piece) as text
    streams, then ("tools", [{"id", "name", "arguments"}, ...]) once at the end if it called any.
    Tool-call pieces arrive split across chunks and are joined by their index.

    tool_choice "none" still sends the tools, so the prompt (and its cache) is the same as for a
    request that may call them, but asks for words only. Last of all comes ("finish", reason):
    "length" means the reply was cut off at max_tokens."""
    body = {"messages": messages, "stream": True,
            "temperature": settings.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or settings.max_tokens, "cache_prompt": True, "id_slot": slot,
            "chat_template_kwargs": {"enable_thinking": False}}
    if tools:
        body.update(tools=tools, tool_choice=tool_choice)
    req = urllib.request.Request(f"{base}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    calls: dict[int, dict] = {}
    finish = None
    with urllib.request.urlopen(req, timeout=60) as r:
        for raw in r:
            if cancel is not None and cancel.is_set():
                raise Cancelled
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            for choice in json.loads(data).get("choices", []):
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    yield "text", delta["content"]
                for tc in delta.get("tool_calls") or []:
                    call = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    call["id"] = tc.get("id") or call["id"]
                    fn = tc.get("function") or {}
                    call["name"] += fn.get("name") or ""
                    args = fn.get("arguments")
                    if isinstance(args, dict):  # some builds send an object, not a string
                        call["arguments"] = json.dumps(args)
                    elif args:
                        call["arguments"] += args
    if calls:
        yield "tools", [calls[i] for i in sorted(calls)]
    if finish:
        yield "finish", finish


def stream_reply(base: str, messages: list[dict], settings: LlmSettings,
                 cancel: threading.Event | None = None, slot: int = 0,
                 temperature: float | None = None) -> Iterator[str]:
    """Yield text pieces as the model writes them. Raises Cancelled if `cancel` is set."""
    body = {"messages": messages, "stream": True,
            "temperature": settings.temperature if temperature is None else temperature,
            "max_tokens": settings.max_tokens, "cache_prompt": True, "id_slot": slot,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(f"{base}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        for raw in r:
            if cancel is not None and cancel.is_set():
                raise Cancelled
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            for choice in json.loads(data).get("choices", []):
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    yield piece
