"""Optional LangSmith tracing for the app's Claude calls.

Every AI step in the app (CV import, CV tailoring, GitHub project bullets,
recruiter email drafts) goes through ``profile_store.run_claude``. When
tracing is on, each call becomes one LangSmith "llm" run named after its
task, with the prompt, the JSON schema, Claude's result event, token usage
and cost.

Tracing is on when the ``langsmith`` package is installed and an API key is
found, unless ``LANGSMITH_TRACING=false``. Without either, calls run
untraced and nothing leaves the machine.

Key lookup: ``LANGSMITH_API_KEY`` env var, then the macOS Keychain
(service ``ai-job-search-langsmith``). Store it once with:

    python3 tools/ls_tracing.py setup

Project: ``LANGSMITH_PROJECT`` (default ``ai-job-applier``).
Endpoint: ``LANGSMITH_ENDPOINT`` (default LangSmith cloud, US).
"""

from __future__ import annotations

import functools
import getpass
import os
import re
import subprocess
import sys
from typing import Any, Callable

SERVICE = "ai-job-search-langsmith"
ACCOUNT = "langsmith"
DEFAULT_PROJECT = "ai-job-applier"
KEY_RE = re.compile(r"lsv2_(pt|sk)_[A-Za-z0-9_]{20,}")


def _keychain_key() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def api_key() -> str | None:
    for var in ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        key = os.environ.get(var, "").strip()
        if key:
            return key
    return _keychain_key()


def project() -> str:
    return os.environ.get("LANGSMITH_PROJECT", "").strip() or DEFAULT_PROJECT


@functools.lru_cache(maxsize=1)
def _client() -> Any | None:
    """The LangSmith client, or None when tracing is off. Resolved once per process."""
    if os.environ.get("LANGSMITH_TRACING", "").strip().lower() in ("false", "0", "no", "off"):
        return None
    key = api_key()
    if not key:
        return None
    try:
        from langsmith import Client
    except ImportError:
        return None
    endpoint = os.environ.get("LANGSMITH_ENDPOINT", "").strip() or None
    return Client(api_key=key, api_url=endpoint)


def enabled() -> bool:
    return _client() is not None


def _usage_outputs(out: Any) -> dict:
    """Shape Claude's result event as LangSmith llm-run outputs (adds usage_metadata for token/cost views)."""
    if not isinstance(out, dict):
        return {"output": out}
    shaped = dict(out)
    usage = out.get("usage") or {}
    if isinstance(usage, dict):
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        inp = int(usage.get("input_tokens") or 0) + cache_read + cache_write
        outp = int(usage.get("output_tokens") or 0)
        shaped["usage_metadata"] = {
            "input_tokens": inp, "output_tokens": outp, "total_tokens": inp + outp,
            "input_token_details": {"cache_read": cache_read, "cache_creation": cache_write},
        }
    return shaped


def _model(out: Any) -> str | None:
    usage = out.get("modelUsage") if isinstance(out, dict) else None
    return next(iter(usage), None) if isinstance(usage, dict) and usage else None


def trace_llm(name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run fn(*args, **kwargs) as a LangSmith llm run called `name`; plain call when tracing is off."""
    client = _client()
    if client is None:
        return fn(*args, **kwargs)
    from langsmith import traceable, tracing_context

    def _add_model(out: Any) -> dict:
        shaped = _usage_outputs(out)
        model = _model(out)
        if model:
            shaped["model"] = model
        return shaped

    traced = traceable(name=name, run_type="llm", process_outputs=_add_model,
                       metadata={"ls_provider": "anthropic", "via": "claude-cli"},
                       tags=["ai-job-applier", name])(fn)
    try:
        with tracing_context(enabled=True, client=client, project_name=project()):
            return traced(*args, **kwargs)
    finally:
        try:
            client.flush()  # short-lived CLI runs (daily.py, batch.py) exit right after
        except Exception:  # tracing must never break the app
            pass


def setup() -> int:
    print("LangSmith setup - your API key is stored in the macOS Keychain, nowhere else.")
    print("Create one at https://smith.langchain.com > Settings > API Keys.")
    key = getpass.getpass("LangSmith API key: ").strip()
    if not KEY_RE.fullmatch(key):
        print("That does not look like a LangSmith API key (lsv2_pt_... or lsv2_sk_...).")
        return 1
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", ACCOUNT, "-w", key],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Could not save to the Keychain: {r.stderr.strip()}")
        return 1
    print(f"Saved. Claude calls will be traced to the LangSmith project '{project()}'.")
    return 0


def status() -> int:
    key = api_key()
    try:
        import langsmith  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    print(f"langsmith package: {'installed' if installed else 'missing (pip install langsmith)'}")
    print(f"API key: {'found' if key else 'missing (python3 tools/ls_tracing.py setup)'}")
    print(f"project: {project()}")
    print(f"tracing: {'ON' if enabled() else 'off'}")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup":
        sys.exit(setup())
    if cmd == "status":
        sys.exit(status())
    print("usage: python3 tools/ls_tracing.py [setup|status]")
    sys.exit(2)
