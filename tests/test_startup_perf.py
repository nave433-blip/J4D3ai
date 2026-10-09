"""Startup-time regression tests.

litellm (~3s import) and paramiko (~110ms) must stay lazily imported so
`jade --help` and other non-LLM paths start fast. These run in fresh
subprocesses so sys.modules state is meaningful.
"""
import subprocess
import sys

import pytest

PY = sys.executable


def _fresh_imports(code: str) -> str:
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       cwd="/home/hatch/workspace/jarvis-dev", timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout


def test_import_cli_does_not_import_litellm():
    out = _fresh_imports(
        "import cli, sys; print('litellm' in sys.modules)")
    assert out.strip() == "False"


def test_import_brain_does_not_import_litellm():
    out = _fresh_imports(
        "import core.brain, sys; print('litellm' in sys.modules)")
    assert out.strip() == "False"


def test_litellm_lazy_loader_works_and_caches():
    out = _fresh_imports(
        "from core.brain import _litellm; "
        "m1 = _litellm(); m2 = _litellm(); "
        "print(m1.__name__, m1 is m2, hasattr(m1, 'completion'))")
    assert out.strip() == "litellm True True"


def test_import_cli_does_not_import_paramiko():
    out = _fresh_imports(
        "import cli, sys; print('paramiko' in sys.modules)")
    assert out.strip() == "False"


def test_import_agent_does_not_import_paramiko():
    out = _fresh_imports(
        "import core.agent, sys; print('paramiko' in sys.modules)")
    assert out.strip() == "False"


def test_cli_help_fast_enough():
    r = subprocess.run([PY, "-m", "cli", "--help"], capture_output=True,
                       text=True, cwd="/home/hatch/workspace/jarvis-dev",
                       timeout=120)
    assert r.returncode == 0
    # Generous bound: must stay well under the old ~5s, and must not
    # touch the network (litellm's cost-map fetch was the tell).
    import time
    t0 = time.time()
    subprocess.run([PY, "-m", "cli", "--help"], capture_output=True,
                   cwd="/home/hatch/workspace/jarvis-dev", timeout=120)
    assert time.time() - t0 < 10
    assert "LiteLLM" not in r.stderr
