"""
Stress / robustness tests for Cortana main branch.

Covers, without any network or LLM access (everything external is stubbed):
- CommandHandler.handle with malformed and adversarial inputs
- Concurrent command handling and concurrent skill invocations
- Oversized message payloads
- Config files with corrupt / missing / wrong-shaped content
- Retry/backoff paths under simulated failure (bounded, no hangs)

Run with: pytest -q
"""
import threading
import time

import pytest

from core.handler import CommandHandler
import core.config as config
import core.refinement as refinement
import core.brain as brain
import core.skillshare as skillshare


def _noop_parse(self, text):
    return {"type": "noop"}


# ---------------------------------------------------------------------------
# Router: malformed / adversarial inputs
# ---------------------------------------------------------------------------

class TestRouterAdversarial:
    def _handler(self):
        h = CommandHandler()
        h.register("ping", lambda: "pong", help="ping")
        h.register("help", lambda: "help", help="help")
        return h

    def test_handle_none_input_returns_error_dict(self):
        h = self._handler()
        res = h.handle(None)
        assert isinstance(res, dict)
        assert res.get("ok") is False

    def test_handle_non_string_input_returns_error_dict(self):
        h = self._handler()
        for bad in (123, ["x"], {"t": 1}, b"bytes"):
            res = h.handle(bad)
            assert isinstance(res, dict) and res.get("ok") is False

    def test_handle_empty_and_whitespace(self):
        h = self._handler()
        assert h.handle("") == {"ok": False, "error": "empty"}
        assert h.handle("   \n\t ") == {"ok": False, "error": "empty"}

    def test_handle_unbalanced_quotes_does_not_crash(self):
        h = self._handler()
        h._call_llm_parse = _noop_parse.__get__(h)
        res = h.handle('/ping "unterminated')
        assert isinstance(res, dict) and "ok" in res

    def test_handle_llm_parse_missing_target(self):
        h = self._handler()
        h._call_llm_parse = lambda text: {"type": "internal"}  # no target
        res = h.handle("do the thing")
        assert isinstance(res, dict) and res.get("ok") is True

    def test_handle_llm_parse_none_target(self):
        h = self._handler()
        h._call_llm_parse = lambda text: {"type": "internal", "target": None, "args": []}
        res = h.handle("do the thing")
        assert isinstance(res, dict) and res.get("ok") is True

    def test_handle_llm_parse_none_args(self):
        h = self._handler()
        h._call_llm_parse = lambda text: {"type": "internal", "target": "/ping", "args": None}
        res = h.handle("do the thing")
        assert isinstance(res, dict) and res.get("ok") is True
        assert res["args"] == ""

    def test_handle_llm_parse_nonstring_args(self):
        h = self._handler()
        h._call_llm_parse = lambda text: {"type": "internal", "target": "/ping", "args": [1, None, "x"]}
        res = h.handle("do the thing")
        assert isinstance(res, dict) and res.get("ok") is True
        assert isinstance(res["args"], str)

    def test_handle_llm_parse_unknown_type_falls_through_to_chat(self):
        h = self._handler()
        h._call_llm_parse = lambda text: {"type": "something-new"}
        res = h.handle("hello there")
        assert res.get("ok") is True and res.get("type") == "chat"

    def test_handle_unicode_adversarial(self):
        h = self._handler()
        h._call_llm_parse = _noop_parse.__get__(h)
        nasties = [
            "hi\x00there",
            "\u202e reversed",
            "zalgo c\u0327o\u0301m\u0303b\u0325",
            "/ping\u200b",          # zero-width space
            "x" * 10 + "\U0001f600" * 50,
        ]
        for n in nasties:
            res = h.handle(n)
            assert isinstance(res, dict) and "ok" in res

    def test_suggest_empty_and_garbage(self):
        h = self._handler()
        assert isinstance(h.suggest(""), list)
        assert isinstance(h.suggest("\x00\u202e"), list)
        got = h.suggest("/hepl")
        assert "/help" in got


# ---------------------------------------------------------------------------
# Oversized payloads
# ---------------------------------------------------------------------------

class TestOversizedPayloads:
    def test_handle_2mb_payload_completes_quickly(self):
        h = CommandHandler()
        h.register("ping", lambda: "pong")
        h._call_llm_parse = _noop_parse.__get__(h)
        big = "word " * (2 * 1024 * 1024 // 5)  # ~2 MB
        start = time.monotonic()
        res = h.handle(big)
        elapsed = time.monotonic() - start
        assert isinstance(res, dict) and res.get("ok") is True
        assert elapsed < 10, f"2MB payload took {elapsed:.1f}s"

    def test_think_structured_oversized_context(self, monkeypatch):
        class StubMgr:
            def __init__(self):
                self.current_model = "stub"
            def chat(self, *a, **k):
                return "stub-answer"
        monkeypatch.setattr(brain, "ModelManager", StubMgr)
        big_ctx = "context-line\n" * 200_000  # ~2.6 MB
        res = brain.think_structured(big_ctx, "summarize")
        assert res.get("ok") is True and res.get("text") == "stub-answer"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_handle_calls(self):
        h = CommandHandler()
        h.register("ping", lambda: "pong")
        h._call_llm_parse = _noop_parse.__get__(h)
        errors = []
        results = []

        def worker(i):
            try:
                results.append(h.handle("/ping" if i % 2 == 0 else f"message number {i}"))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=30)
        assert not errors, f"concurrent handle raised: {errors[:3]}"
        assert len(results) == 40
        assert all(isinstance(r, dict) and r.get("ok") is True for r in results)

    def test_concurrent_run_skill_missing_skill(self):
        errors = []
        results = []

        def worker():
            try:
                results.append(skillshare.run_skill("definitely-not-a-skill"))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=60)
        assert not errors, f"concurrent run_skill raised: {errors[:3]}"
        assert len(results) == 16
        assert all(r.get("ok") is False for r in results)


# ---------------------------------------------------------------------------
# Config: corrupt / missing / wrong-shaped
# ---------------------------------------------------------------------------

class TestConfigRobustness:
    def _point_config(self, monkeypatch, tmp_path, content):
        p = tmp_path / "config.json"
        if content is not None:
            p.write_text(content)
        monkeypatch.setattr(config, "CONFIG_FILE", p)
        return p

    def test_load_config_corrupt_json_falls_back_to_defaults(self, monkeypatch, tmp_path):
        self._point_config(monkeypatch, tmp_path, "{not valid json,,,")
        cfg = config.load_config()
        assert isinstance(cfg, dict)
        assert cfg.get("provider") == config.DEFAULT_CONFIG.get("provider")

    def test_load_config_nondict_json_falls_back_to_defaults(self, monkeypatch, tmp_path):
        self._point_config(monkeypatch, tmp_path, "[1, 2, 3]")
        cfg = config.load_config()
        assert isinstance(cfg, dict)
        assert cfg.get("provider") == config.DEFAULT_CONFIG.get("provider")

    def test_load_config_json_string_falls_back_to_defaults(self, monkeypatch, tmp_path):
        self._point_config(monkeypatch, tmp_path, '"just a string"')
        cfg = config.load_config()
        assert isinstance(cfg, dict)

    def test_load_config_missing_file_uses_defaults(self, monkeypatch, tmp_path):
        self._point_config(monkeypatch, tmp_path, None)
        cfg = config.load_config()
        assert isinstance(cfg, dict)
        assert cfg == config.DEFAULT_CONFIG or cfg.get("provider") == config.DEFAULT_CONFIG.get("provider")

    def test_load_config_empty_file_uses_defaults(self, monkeypatch, tmp_path):
        self._point_config(monkeypatch, tmp_path, "")
        cfg = config.load_config()
        assert isinstance(cfg, dict)

    def test_load_config_result_is_isolated_copy(self, monkeypatch, tmp_path):
        self._point_config(monkeypatch, tmp_path, '{"provider": "x"}')
        a = config.load_config()
        a["provider"] = "mutated"
        b = config.load_config()
        assert b["provider"] != "mutated"


# ---------------------------------------------------------------------------
# Retry / backoff under simulated failure
# ---------------------------------------------------------------------------

class TestRetryBackoff:
    def test_run_stress_test_success(self):
        rc, out, err = refinement.run_stress_test("true")
        assert rc == 0

    def test_run_stress_test_failure_no_exception(self):
        rc, out, err = refinement.run_stress_test("false")
        assert rc != 0

    def test_refine_loop_retries_up_to_max(self, monkeypatch, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("x = 1\n")
        calls = []
        # Repair "suggested" but never applied: the loop must keep retrying
        # the test until max_retries is exhausted, then give up.
        monkeypatch.setattr(refinement, "think_structured",
                            lambda ctx, prompt: (calls.append(1), {"ok": True, "text": "try harder"})[1])
        start = time.monotonic()
        ok = refinement.refine_loop(str(target), "false", max_retries=3)
        elapsed = time.monotonic() - start
        assert ok is False
        assert len(calls) == 3, "must attempt exactly max_retries repair rounds"
        assert elapsed < 60, "refine_loop must stay bounded"

    def test_refine_loop_aborts_when_repair_fails(self, monkeypatch, tmp_path):
        target = tmp_path / "target.py"
        target.write_text("x = 1\n")
        calls = []
        monkeypatch.setattr(refinement, "think_structured",
                            lambda ctx, prompt: (calls.append(1), {"ok": False})[1])
        ok = refinement.refine_loop(str(target), "false", max_retries=3)
        assert ok is False
        assert len(calls) == 1, "a failed repair request aborts the loop immediately"

    def test_detect_ollama_bounded_attempts_no_server(self, monkeypatch):
        import os, sys
        import requests
        monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no server")))
        systems = []
        monkeypatch.setattr(os, "system", lambda cmd: systems.append(cmd) or 0)
        sleeps = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(sys, "platform", "linux")
        host = config.detect_ollama(max_attempts=2)
        assert host is None
        assert len(sleeps) == 2, f"expected exactly 2 backoff sleeps, got {len(sleeps)}"
        assert len(systems) == 2

    def test_get_task_category_falls_back_when_provider_explodes(self, monkeypatch):
        monkeypatch.setattr(brain, "get_provider",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider down")))
        assert brain.get_task_category("anything at all") == "general"
