#!/usr/bin/env python3
"""Groq (OpenAI-compatible API) for the cheap, many-step calls - the careers-site navigator.

  python3 tools/groq_llm.py setup    (API key -> macOS Keychain; lists the models your key can use)
  python3 tools/groq_llm.py status

Key: GROQ_API_KEY env var, else the Keychain (service ai-job-search-groq). Model: GROQ_MODEL env var,
else DEFAULT_MODEL. No SDK needed: one HTTPS POST per call. Every call is traced in LangSmith
(tools/ls_tracing.py) when tracing is on.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ls_tracing  # noqa: E402

API = "https://api.groq.com/openai/v1"
SERVICE = "ai-job-search-groq"
DEFAULT_MODEL = "openai/gpt-oss-120b"


class GroqError(RuntimeError):
    pass


def api_key() -> str | None:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    if sys.platform != "darwin":
        return None
    r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", "groq", "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def available() -> bool:
    return api_key() is not None


def model() -> str:
    return os.environ.get("GROQ_MODEL", "").strip() or DEFAULT_MODEL


def _post(path: str, body: dict | None, key: str, timeout: int) -> dict:
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "ai-job-applier/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise GroqError(f"Groq HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GroqError(f"Groq unreachable: {exc}") from exc


def _call(prompt: str, schema: dict, timeout: int, model_name: str) -> dict:
    key = api_key()
    if not key:
        raise GroqError("no Groq API key - run: python3 tools/groq_llm.py setup")
    system = ("Answer with ONE JSON object only, no prose, matching this JSON schema:\n"
              + json.dumps(schema, ensure_ascii=False))
    body = {"model": model_name, "temperature": 0, "max_completion_tokens": 1024,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    out = _post("/chat/completions", body, key, timeout)
    usage = out.get("usage") or {}
    text = (((out.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    return {"result": text, "model": out.get("model", model_name),
            "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
            "modelUsage": {out.get("model", model_name): {}}}


FALLBACK_MODELS = ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"]
_cooldown: dict[str, float] = {}  # model -> time its per-minute limit frees up


def models() -> list[str]:
    """The model to use first, then others with their OWN free-tier per-minute budget (GROQ_MODELS overrides)."""
    env = [m.strip() for m in os.environ.get("GROQ_MODELS", "").split(",") if m.strip()]
    return list(dict.fromkeys(env or [model(), *FALLBACK_MODELS]))


def _wait_hint(exc: Exception) -> float:
    m = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)(m?s)", str(exc))
    if not m:
        return 15.0
    secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
    return secs + 60 * float(m.group(1) or 0)


def chat_json(prompt: str, schema: dict, timeout: int = 90, task: str = "groq") -> dict:
    """One call; returns the JSON object the model produced. A model at its per-minute limit (429) is
    skipped for the next one instead of waiting; only when all are limited does it sleep. Retries once
    on unparsable JSON."""
    for attempt in range(2):
        raw = None
        for _round in range(4):
            now = time.time()
            for name in models():
                if _cooldown.get(name, 0) > now:
                    continue
                try:
                    raw = ls_tracing.trace_llm(task, _call, prompt, schema, timeout, name)
                    break
                except GroqError as exc:
                    if "HTTP 429" in str(exc):
                        _cooldown[name] = time.time() + _wait_hint(exc)
                        continue
                    if "HTTP 413" in str(exc) or "model_not_found" in str(exc) or "HTTP 404" in str(exc):
                        continue  # too large for this model's budget / not available: try the next one
                    raise
            if raw is not None:
                break
            soonest = min((_cooldown.get(n, 0) for n in models()), default=time.time() + 5)
            time.sleep(min(max(soonest - time.time(), 1) + 0.5, 30))
        if raw is None:
            raise GroqError("all Groq models are rate-limited or refused the request")
        text = raw.get("result", "")
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass
        prompt += "\n\nYour last answer was not valid JSON. Reply with the JSON object only."
    raise GroqError("Groq returned no valid JSON")


def setup() -> int:
    print("Groq setup - the API key is stored in the macOS Keychain, nowhere else.")
    print("Create one at https://console.groq.com/keys")
    key = getpass.getpass("Groq API key: ").strip()
    if not re.fullmatch(r"gsk_[A-Za-z0-9]{20,}", key):
        print("That does not look like a Groq key (gsk_...).")
        return 1
    try:
        models = sorted(m["id"] for m in _post("/models", None, key, 30).get("data", []))
    except GroqError as exc:
        print(f"The key did not work: {exc}")
        return 1
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", "groq", "-w", key],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Could not save to the Keychain: {r.stderr.strip()}")
        return 1
    print(f"Saved. Model used: {model()} ({'available' if model() in models else 'NOT in your list - set GROQ_MODEL'})")
    print("Your key can use:", ", ".join(models))
    return 0


def status() -> int:
    key = api_key()
    print(f"Groq key: {'found' if key else 'missing (python3 tools/groq_llm.py setup)'} | model: {model()}")
    if key:
        try:
            ids = {m["id"] for m in _post("/models", None, key, 30).get("data", [])}
            print(f"API: OK | {model()} {'available' if model() in ids else 'NOT available to this key'}")
        except GroqError as exc:
            print(f"API: FAILED ({exc})")
            return 1
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit(setup() if cmd == "setup" else status() if cmd == "status" else 2)
