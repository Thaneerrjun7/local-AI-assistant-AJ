"""One non-streaming request to llama-server's slot 1, the background slot.

Why slot 1: slot 0 holds the live conversation's prompt cache. Memory work (fact extraction,
session summaries) running there would evict it and slow the next spoken reply. Temperature 0
and no thinking keep the output repeatable and fast; a JSON schema, when given, becomes a grammar
that makes invalid JSON impossible (Phase 1: Qwen3-4B was valid 10% of the time without it,
100% with it).
"""
import json
import urllib.request

SLOT = 1


def complete(base_url: str, messages: list[dict], max_tokens: int, schema: dict | None = None,
             timeout: float = 60) -> str:
    """The model's whole reply as text. Network and HTTP errors propagate to the caller."""
    body = {"messages": messages, "temperature": 0, "max_tokens": max_tokens, "id_slot": SLOT,
            "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False}}
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "result", "schema": schema}}
    req = urllib.request.Request(f"{base_url}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        reply = json.loads(r.read().decode("utf-8"))
    return (reply["choices"][0]["message"].get("content") or "").strip()
