"""Track 2 tests: core/codeops.py — project-aware agentic coding loop.

No network, no real model calls: think_structured/confirm are monkeypatched.
"""
import json
import os
import shutil
import subprocess

import pytest

import core.codeops as codeops
from core.codeops import (
    CheckpointManager,
    apply_diff,
    build_context,
    handle_code_command,
    iter_project_files,
    load_project_config,
    parse_unified_diff,
    run_code,
    save_project_config,
    suggest_shell,
    explain_command,
)


def _write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(p, mode) as fh:
        fh.write(content)
    return p


# ---------------------------------------------------------------------------
# .gitignore handling
# ---------------------------------------------------------------------------

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git not available")


@needs_git
def test_gitignore_respected_via_git(tmp_path):
    root = str(tmp_path)
    subprocess.run([GIT, "init", "-q", root], check=True)
    _write(root, ".gitignore", "ignored.txt\n*.log\n")
    _write(root, "keep.py", "x = 1\n")
    _write(root, "ignored.txt", "nope\n")
    _write(root, "debug.log", "nope\n")
    _write(root, "sub/ok.py", "y = 2\n")
    files = iter_project_files(root)
    assert "keep.py" in files
    assert "sub/ok.py" in files
    assert "ignored.txt" not in files
    assert "debug.log" not in files
    # .git itself must never leak in
    assert not any(f.startswith(".git/") for f in files)


def test_gitignore_respected_via_fallback_walker(tmp_path):
    root = str(tmp_path)  # no .git -> fallback os.walk path
    _write(root, ".gitignore", "*.log\nbuild/\nsecret.txt\n")
    _write(root, "main.py", "print('hi')\n")
    _write(root, "debug.log", "nope\n")
    _write(root, "build/out.o", "nope\n")
    _write(root, "secret.txt", "nope\n")
    files = iter_project_files(root)
    # .gitignore itself is a legitimate project file and is kept
    assert files == [".gitignore", "main.py"]


def test_config_ignore_and_extra_ignore(tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "1\n")
    _write(root, "notes.tmp", "2\n")
    _write(root, "scratch.py", "3\n")
    save_project_config(root, {"model": None, "sandbox": {}, "ignore": ["*.tmp"]})
    assert iter_project_files(root) == ["a.py", "scratch.py"]
    assert iter_project_files(root, extra_ignore=["scratch.py"]) == ["a.py"]


def test_binary_and_large_files_skipped(tmp_path):
    root = str(tmp_path)
    _write(root, "ok.txt", "hello\n")
    _write(root, "blob.bin", b"\x00\x01\x02binary")
    _write(root, "big.txt", "x" * (257 * 1024))
    files = iter_project_files(root)
    assert files == ["ok.txt"]


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def test_build_context_truncation_note(tmp_path):
    root = str(tmp_path)
    for i in range(10):
        _write(root, f"f{i}.py", "x = 1\n" * 50)
    text = build_context(root, max_chars=500)
    assert "[...truncated:" in text
    assert "omitted]" in text
    assert "Project root:" in text


def test_build_context_no_truncation_when_small(tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "x = 1\n")
    text = build_context(root, max_chars=60000)
    assert "[...truncated:" not in text
    assert "=== a.py ===" in text
    assert "x = 1" in text


# ---------------------------------------------------------------------------
# Project config
# ---------------------------------------------------------------------------

def test_project_config_roundtrip(tmp_path):
    root = str(tmp_path)
    cfg = load_project_config(root)
    assert cfg["sandbox"]["timeout"] == 30
    assert cfg["model"] is None
    assert cfg["ignore"] == []
    res = save_project_config(root, {"model": "m", "sandbox": {"timeout": 5}, "ignore": ["x"]})
    assert res["ok"] is True
    cfg2 = load_project_config(root)
    assert cfg2["model"] == "m"
    assert cfg2["sandbox"]["timeout"] == 5
    assert cfg2["sandbox"]["memory_mb"] == 512  # defaults merged back in
    assert cfg2["ignore"] == ["x"]
    # corrupt JSON -> defaults, never raises
    with open(os.path.join(root, ".jade", "config.json"), "w") as fh:
        fh.write("{not json")
    assert load_project_config(root)["sandbox"]["timeout"] == 30


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

def test_checkpoint_snapshot_rewind_roundtrip(tmp_path, monkeypatch):
    root = str(tmp_path)
    _write(root, "a.txt", b"original bytes")
    cm = CheckpointManager(root)
    snap = cm.snapshot(["a.txt"])
    assert snap in cm.list()
    _write(root, "a.txt", b"modified bytes")
    monkeypatch.setattr(codeops, "confirm", lambda *a, **k: True)
    res = cm.rewind(snap)
    assert res["ok"] is True
    assert res["restored"] == ["a.txt"]
    with open(os.path.join(root, "a.txt"), "rb") as fh:
        assert fh.read() == b"original bytes"


def test_checkpoint_rewind_cancelled(tmp_path, monkeypatch):
    root = str(tmp_path)
    _write(root, "a.txt", "v1\n")
    cm = CheckpointManager(root)
    snap = cm.snapshot(["a.txt"])
    _write(root, "a.txt", "v2\n")
    monkeypatch.setattr(codeops, "confirm", lambda *a, **k: False)
    res = cm.rewind(snap)
    assert res["ok"] is False
    assert open(os.path.join(root, "a.txt")).read() == "v2\n"


def test_checkpoint_rewind_unknown_id(tmp_path):
    cm = CheckpointManager(tmp_path)
    res = cm.rewind("nope-0000")
    assert res["ok"] is False
    assert "no such checkpoint" in res["error"]


def test_checkpoint_prune(tmp_path, monkeypatch):
    root = str(tmp_path)
    _write(root, "a.txt", "x\n")
    monkeypatch.setattr(codeops, "confirm", lambda *a, **k: True)
    cm = CheckpointManager(root)
    s1 = cm.snapshot(["a.txt"])
    s2 = cm.snapshot(["a.txt"])
    assert len(cm.list()) == 2
    res = cm.prune(keep=1)
    assert res["ok"] is True
    assert cm.list() == [s2]
    assert s1 in res["removed"]


def test_checkpoint_snapshot_ignores_outside_root(tmp_path):
    root = str(tmp_path)
    cm = CheckpointManager(root)
    outside = os.path.join(os.path.dirname(root), "outside-snap-test.txt")
    _write(root, "in.txt", "in\n")
    snap = cm.snapshot(["in.txt", outside])
    snap_dir = os.path.join(root, ".jade", "checkpoints", snap)
    walked = []
    for dirpath, _d, filenames in os.walk(snap_dir):
        walked.extend(os.path.relpath(os.path.join(dirpath, f), snap_dir)
                      for f in filenames)
    assert walked == ["in.txt"]


# ---------------------------------------------------------------------------
# Diff parsing / application
# ---------------------------------------------------------------------------

KNOWN_DIFF = """--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 line1
-old
+new
 line3
"""


def test_parse_unified_diff_known():
    parsed = parse_unified_diff(KNOWN_DIFF)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["path"] == "foo.py"
    hunk = parsed[0]["hunks"][0]
    assert hunk["old_start"] == 1
    assert hunk["old_lines"] == ["line1", "old", "line3"]
    assert hunk["new_lines"] == ["line1", "new", "line3"]


def test_parse_unified_diff_tolerates_fences_and_prose():
    text = "Here you go:\n```diff\n" + KNOWN_DIFF + "```\n"
    parsed = parse_unified_diff(text)
    assert isinstance(parsed, list)
    assert parsed[0]["path"] == "foo.py"


def test_parse_unified_diff_malformed():
    bad = parse_unified_diff("this is not a diff at all\njust prose\n")
    assert isinstance(bad, dict) and bad["ok"] is False
    assert "error" in bad
    empty = parse_unified_diff("   \n")
    assert empty["ok"] is False
    stray = parse_unified_diff("+++ b/x.py\n")
    assert stray["ok"] is False


def test_parse_unified_diff_rejects_new_files_and_renames():
    new_file = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n"
    res = parse_unified_diff(new_file)
    assert res["ok"] is False and "new-file" in res["error"]
    rename = "--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-x\n+x\n"
    res = parse_unified_diff(rename)
    assert res["ok"] is False and "rename" in res["error"]


def test_apply_diff_modifies_file(tmp_path):
    root = str(tmp_path)
    _write(root, "foo.py", "line1\nold\nline3\n")
    parsed = parse_unified_diff(KNOWN_DIFF)
    res = apply_diff(root, parsed)
    assert res["ok"] is True
    assert res["files"] == ["foo.py"]
    assert open(os.path.join(root, "foo.py")).read() == "line1\nnew\nline3\n"


def test_apply_diff_hunk_mismatch_writes_nothing(tmp_path):
    root = str(tmp_path)
    _write(root, "foo.py", "totally\ndifferent\ncontent\n")
    parsed = parse_unified_diff(KNOWN_DIFF)
    before = open(os.path.join(root, "foo.py")).read()
    res = apply_diff(root, parsed)
    assert res["ok"] is False
    assert "nothing was written" in res["error"]
    assert open(os.path.join(root, "foo.py")).read() == before


def test_apply_diff_missing_file_honest(tmp_path):
    parsed = parse_unified_diff(KNOWN_DIFF)
    res = apply_diff(str(tmp_path), parsed)
    assert res["ok"] is False
    assert "missing file" in res["error"]


def test_apply_diff_rejects_path_escape(tmp_path):
    parsed = [{"path": "../escape.py",
               "hunks": [{"old_start": 1, "old_lines": ["x"],
                          "new_lines": ["y"]}]}]
    res = apply_diff(str(tmp_path), parsed)
    assert res["ok"] is False
    assert "escapes project root" in res["error"]


def test_apply_diff_passes_parse_errors_through(tmp_path):
    err = {"ok": False, "error": "boom"}
    assert apply_diff(str(tmp_path), err) == err


# ---------------------------------------------------------------------------
# suggest / explain with stubbed model
# ---------------------------------------------------------------------------

def _stub_think(text, ok=True, error=None):
    def _stub(context, task, model=None):
        if ok:
            return {"ok": True, "text": text, "provider": "stub"}
        return {"ok": False, "error": error or "boom"}
    return _stub


def test_suggest_shell(monkeypatch):
    monkeypatch.setattr(
        codeops, "think_structured",
        _stub_think("ls -la /tmp\nList all files in long format"))
    res = suggest_shell("list files in tmp")
    assert res["ok"] is True
    assert res["command"] == "ls -la /tmp"
    assert "List all files" in res["explanation"]


def test_suggest_shell_strips_fences(monkeypatch):
    monkeypatch.setattr(
        codeops, "think_structured",
        _stub_think("```bash\ndf -h\n```"))
    res = suggest_shell("disk usage")
    assert res["command"] == "df -h"


def test_suggest_shell_model_failure(monkeypatch):
    monkeypatch.setattr(codeops, "think_structured",
                        _stub_think("", ok=False, error="no provider"))
    res = suggest_shell("anything")
    assert res["ok"] is False
    assert "no provider" in res["error"]


def test_explain_command(monkeypatch):
    monkeypatch.setattr(
        codeops, "think_structured",
        _stub_think("Lists files. The -la flags show all entries in long format."))
    res = explain_command("ls -la")
    assert res["ok"] is True
    assert "Lists files" in res["explanation"]


def test_explain_command_model_failure(monkeypatch):
    monkeypatch.setattr(codeops, "think_structured",
                        _stub_think("", ok=False, error="no provider"))
    res = explain_command("ls")
    assert res["ok"] is False


def test_propose_diff_parses_model_output(monkeypatch, tmp_path):
    root = str(tmp_path)
    _write(root, "foo.py", "line1\nold\nline3\n")
    monkeypatch.setattr(codeops, "think_structured", _stub_think(KNOWN_DIFF))
    res = codeops.propose_diff("change old to new", root)
    assert res["ok"] is True
    assert res["parsed"][0]["path"] == "foo.py"
    assert res["provider"] == "stub"


def test_propose_diff_bad_model_output(monkeypatch, tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "x = 1\n")
    monkeypatch.setattr(codeops, "think_structured",
                        _stub_think("Sure, here is my plan in prose..."))
    res = codeops.propose_diff("do stuff", root)
    assert res["ok"] is False
    assert "not a usable diff" in res["error"]
    assert "raw" in res


# ---------------------------------------------------------------------------
# run_code / sandbox
# ---------------------------------------------------------------------------

def test_run_code_surfaces_bad_backend_honestly(tmp_path):
    root = str(tmp_path)
    save_project_config(root, {"model": None,
                               "sandbox": {"timeout": 5, "memory_mb": 64,
                                           "allow_network": False,
                                           "backend": "bogus"},
                               "ignore": []})
    res = run_code("echo hi", root)
    assert res["ok"] is False
    assert "error" in res
    assert "bogus" in res["error"]


def test_run_code_policy_from_project_config(tmp_path, monkeypatch):
    root = str(tmp_path)
    _write(root, "x.txt", "x\n")
    save_project_config(root, {"model": None,
                               "sandbox": {"timeout": 7, "memory_mb": 64,
                                           "allow_network": False,
                                           "backend": "subprocess"},
                               "ignore": []})
    seen = {}

    def fake_run(command, timeout=30, memory_mb=512, cpu_seconds=30,
                 allow_network=False, workdir="", backend="auto"):
        seen.update(timeout=timeout, memory_mb=memory_mb,
                    allow_network=allow_network, workdir=workdir,
                    backend=backend, command=command)
        from tools.sandbox import SandboxResult
        return SandboxResult(stdout="ok\n", exit_code=0, backend="subprocess")

    monkeypatch.setattr(codeops, "run_sandboxed", fake_run)
    res = run_code("echo ok", root)
    assert res["ok"] is True
    assert seen["timeout"] == 7
    assert seen["memory_mb"] == 64
    assert seen["backend"] == "subprocess"
    assert seen["workdir"] == root


# ---------------------------------------------------------------------------
# handle_code_command confirm gating
# ---------------------------------------------------------------------------

def _code_diff_for(path_rel, old_line, new_line):
    return (f"--- a/{path_rel}\n+++ b/{path_rel}\n"
            f"@@ -1 +1 @@\n-{old_line}\n+{new_line}\n")


def test_handle_code_command_decline_leaves_files(tmp_path, monkeypatch, capsys):
    root = str(tmp_path)
    _write(root, "a.txt", "hello\n")
    monkeypatch.setattr(
        codeops, "think_structured",
        _stub_think(_code_diff_for("a.txt", "hello", "goodbye")))
    calls = []
    monkeypatch.setattr(
        codeops, "confirm",
        lambda prompt, default=False, **kw: calls.append(prompt) or False)
    res = handle_code_command(f"change greeting in {root}")
    assert res["ok"] is False
    assert any("Apply this diff" in c for c in calls)
    assert open(os.path.join(root, "a.txt")).read() == "hello\n"
    # no checkpoint written on decline
    assert not os.path.isdir(os.path.join(root, ".jade", "checkpoints"))


def test_handle_code_command_apply_checkpoints_and_rewinds(
        tmp_path, monkeypatch, capsys):
    root = str(tmp_path)
    _write(root, "a.txt", "hello\n")
    monkeypatch.setattr(
        codeops, "think_structured",
        _stub_think(_code_diff_for("a.txt", "hello", "goodbye")))
    monkeypatch.setattr(codeops, "confirm", lambda *a, **k: True)
    res = handle_code_command(f"change greeting in {root}")
    assert res["ok"] is True
    assert open(os.path.join(root, "a.txt")).read() == "goodbye\n"
    snap_id = res["snap_id"]
    assert snap_id in CheckpointManager(root).list()
    out = capsys.readouterr().out
    assert snap_id in out  # checkpoint id printed with rewind instructions
    # rewind restores the pre-apply bytes
    rw = CheckpointManager(root).rewind(snap_id)
    assert rw["ok"] is True
    assert open(os.path.join(root, "a.txt")).read() == "hello\n"


def test_handle_code_command_bad_dir():
    res = handle_code_command("fix it in /nonexistent-dir-xyz-123")
    assert res["ok"] is False
    assert "not a directory" in res["error"]


def test_handle_code_command_empty():
    assert handle_code_command("")["ok"] is False
    assert handle_code_command("   ")["ok"] is False
