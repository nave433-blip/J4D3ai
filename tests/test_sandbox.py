"""Track C tests: Linux code-execution sandbox.

Exercises the restricted-subprocess fallback for real and mocks the bwrap
path (bwrap is not installed here). No test assumes bwrap is present.
"""
import os
import shutil
import sys

import pytest

import tools.sandbox as sandbox
from tools.sandbox import SandboxResult, run_sandboxed, sandbox_available


LINUX = sys.platform == "linux"
needs_linux = pytest.mark.skipif(not LINUX, reason="Linux-only sandbox")


@needs_linux
def test_fallback_runs_command_with_limits():
    r = run_sandboxed("echo hello", backend="subprocess")
    assert r.exit_code == 0
    assert r.stdout.strip() == "hello"
    assert r.backend == "subprocess"
    assert r.timed_out is False
    assert r.network_isolated is False  # honest: fallback cannot cut network


@needs_linux
def test_fresh_workdir_each_run_and_cleaned_up():
    r = run_sandboxed("pwd", backend="subprocess", keep_workdir=True)
    try:
        assert "jade-sandbox-" in r.stdout
        assert os.path.isdir(r.workdir)
    finally:
        shutil.rmtree(r.workdir, ignore_errors=True)
    r2 = run_sandboxed("pwd", backend="subprocess")
    assert r2.workdir != r.workdir
    assert not os.path.exists(r2.workdir)  # cleaned up by default


@needs_linux
def test_wall_clock_timeout_kills_command():
    r = run_sandboxed("sleep 30", timeout=1, backend="subprocess")
    assert r.timed_out is True
    assert r.exit_code != 0


@needs_linux
def test_memory_limit_kills_hog():
    if not shutil.which("python3"):
        pytest.skip("python3 not available for alloc test")
    r = run_sandboxed("python3 -c \"x = ' ' * 10**9; print(len(x))\"",
                      memory_mb=64, backend="subprocess")
    assert r.exit_code != 0, "1GB alloc under a 64MB address-space cap must die"


@needs_linux
def test_environment_is_scrubbed():
    os.environ["JADE_TEST_SECRET_MARKER"] = "s3cret"
    try:
        r = run_sandboxed("env", backend="subprocess")
    finally:
        del os.environ["JADE_TEST_SECRET_MARKER"]
    assert r.exit_code == 0
    assert "JADE_TEST_SECRET_MARKER" not in r.stdout
    assert "s3cret" not in r.stdout


@needs_linux
def test_exit_code_and_stderr_captured():
    r = run_sandboxed("echo oops >&2; exit 3", backend="subprocess")
    assert r.exit_code == 3
    assert "oops" in r.stderr
    assert r.ok is False


def test_non_linux_reports_unavailable(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    r = run_sandboxed("echo hi")
    assert r.backend == "unavailable"
    assert "unavailable" in r.error
    assert r.ok is False


def test_bwrap_preferred_when_present(monkeypatch):
    monkeypatch.setattr(sandbox, "have_bwrap", lambda: True)
    seen = {}

    def fake_bwrap(command, workdir, timeout, memory_mb, cpu_seconds, allow_network):
        seen["network_cut"] = not allow_network
        return SandboxResult(exit_code=0, stdout="ok", backend="bwrap",
                             network_isolated=not allow_network, workdir=workdir)

    monkeypatch.setattr(sandbox, "_run_bwrap", fake_bwrap)
    r = run_sandboxed("echo hi", backend="auto")
    assert r.backend == "bwrap"
    assert r.network_isolated is True
    assert seen["network_cut"] is True


def test_bwrap_network_opt_in(monkeypatch):
    monkeypatch.setattr(sandbox, "have_bwrap", lambda: True)
    seen = {}

    def fake_bwrap(command, workdir, timeout, memory_mb, cpu_seconds, allow_network):
        seen["allow_network"] = allow_network
        return SandboxResult(exit_code=0, backend="bwrap",
                             network_isolated=not allow_network, workdir=workdir)

    monkeypatch.setattr(sandbox, "_run_bwrap", fake_bwrap)
    run_sandboxed("echo hi", backend="auto", allow_network=True)
    assert seen["allow_network"] is True


def test_bwrap_backend_errors_when_missing(monkeypatch):
    monkeypatch.setattr(sandbox, "have_bwrap", lambda: False)
    r = run_sandboxed("echo hi", backend="bwrap")
    assert r.error and "bwrap" in r.error.lower()


def test_unknown_backend_rejected():
    r = run_sandboxed("echo hi", backend="nope")
    assert r.error and "unknown sandbox backend" in r.error


def test_sandbox_available_probe(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    p = sandbox_available()
    assert p["ok"] is False and p["backend"] == "unavailable"


@needs_linux
def test_shell_run_sandbox_opt_in_and_default_unchanged():
    from tools.shell import run
    sandboxed = run("echo hi", sandbox=True)
    assert sandboxed.get("sandboxed") is True
    assert sandboxed["stdout"].strip() == "hi"
    assert sandboxed["return_code"] == 0

    plain = run("echo hi")
    assert "sandboxed" not in plain  # default execution path untouched
    assert plain["return_code"] == 0


@needs_linux
def test_agent_shell_wiring_uses_config(monkeypatch, tmp_path):
    import core.agent as agent_mod
    import core.config as config_mod
    fake_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", fake_file)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    assert agent_mod._sandbox_enabled() is False  # default off
    fake_file.write_text('{"code_sandbox": true}')
    assert agent_mod._sandbox_enabled() is True
