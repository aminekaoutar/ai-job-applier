"""Tests for tools/ls_tracing.py - optional LangSmith tracing of the app's Claude calls."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import ls_tracing  # noqa: E402
import profile_store as ps  # noqa: E402


class Off(unittest.TestCase):
    def setUp(self):
        ls_tracing._client.cache_clear()
        self.addCleanup(ls_tracing._client.cache_clear)

    def test_no_key_means_plain_call(self):
        env = {k: v for k, v in os.environ.items() if k not in ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(ls_tracing, "_keychain_key", return_value=None):
            self.assertFalse(ls_tracing.enabled())
            self.assertEqual(ls_tracing.trace_llm("t", lambda a, b=0: a + b, 1, b=2), 3)

    def test_explicit_false_wins_over_key(self):
        with mock.patch.dict(os.environ, {"LANGSMITH_TRACING": "false", "LANGSMITH_API_KEY": "lsv2_pt_x"}):
            self.assertFalse(ls_tracing.enabled())

    def test_errors_propagate_untraced(self):
        with mock.patch.object(ls_tracing, "_client", return_value=None):
            with self.assertRaises(ValueError):
                ls_tracing.trace_llm("t", lambda: (_ for _ in ()).throw(ValueError("x")))


class Shaping(unittest.TestCase):
    def test_usage_metadata_counts_cache_tokens(self):
        out = {"result": "ok", "usage": {"input_tokens": 10, "output_tokens": 5,
                                         "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}
        um = ls_tracing._usage_outputs(out)["usage_metadata"]
        self.assertEqual((um["input_tokens"], um["output_tokens"], um["total_tokens"]), (130, 5, 135))
        self.assertEqual(um["input_token_details"], {"cache_read": 100, "cache_creation": 20})

    def test_model_from_model_usage(self):
        self.assertEqual(ls_tracing._model({"modelUsage": {"claude-opus-5-5": {}}}), "claude-opus-5-5")
        self.assertIsNone(ls_tracing._model({}))


class On(unittest.TestCase):
    """With a client, the call goes through langsmith's traceable and the client is flushed."""

    def test_traced_call_returns_result_and_flushes(self):
        try:
            import langsmith  # noqa: F401
        except ImportError:
            self.skipTest("langsmith not installed")
        client = mock.MagicMock()
        with mock.patch.object(ls_tracing, "_client", return_value=client):
            self.assertEqual(ls_tracing.trace_llm("cv_tailor", lambda p: {"result": p}, "hi"), {"result": "hi"})
        client.flush.assert_called()


class RunClaudeTask(unittest.TestCase):
    def test_task_name_reaches_tracer_and_data_is_parsed(self):
        seen = {}

        def fake_trace(name, fn, *args):
            seen["name"] = name
            return fn(*args)

        done = subprocess.CompletedProcess([], 0, stdout=json.dumps({"structured_output": {"a": 1}}), stderr="")
        with mock.patch.object(ps.ls_tracing, "trace_llm", side_effect=fake_trace), \
                mock.patch.object(ps, "claude_path", return_value="/bin/claude"), \
                mock.patch.object(ps.subprocess, "run", return_value=done):
            self.assertEqual(ps.run_claude("p", {"type": "object"}, task="cv_import"), {"a": 1})
        self.assertEqual(seen["name"], "cv_import")


if __name__ == "__main__":
    unittest.main()
