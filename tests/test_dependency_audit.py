"""Dependency audit — the httpx-class bug must not exist here.

Every third-party import in shipped code must be either
(a) declared in ``pyproject.toml`` [project.dependencies], or
(b) an acknowledged *optional* import: probed inside
    ``try/except ImportError`` with an honest "not installed" fallback
    (never a silent fake, never an unguarded crash).

``server/`` has its own ``requirements.txt`` (audited separately);
``tests/`` may use the ``dev`` extras; ``mobile/`` and ``docs/`` are out
of scope for the Python package audit.
"""
import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = (".git", "__pycache__", "node_modules", "venv", ".venv",
             "mobile", "docs", "server", "tests")

# pip name -> import name for everything in [project.dependencies]
DECLARED = {
    "typer", "rich", "requests", "watchdog", "sounddevice", "scipy",
    "numpy", "faiss", "speech_recognition", "prompt_toolkit", "dropbox",
    "keyring", "paramiko", "psutil", "ddgs", "litellm",
    "typing_extensions", "packaging", "urllib3",
}

# Optional imports: allowed undeclared ONLY with a guarded probe site.
OPTIONAL = {
    "PIL",            # tray icon art (Pillow)
    "keyboard",       # global hotkeys
    "webview",        # pywebview desktop window
    "openwakeword",   # wake-word engine
    "faster_whisper",  # offline STT
    "pyttsx3",        # fallback TTS
    "miniupnpc",      # P2P NAT traversal
    "zeroconf",       # P2P LAN discovery
    "winrt",          # Windows integrations (pywinrt)
    "playwright",     # browser tool
    "qrcode",         # P2P pairing codes
    "pystray",        # system tray
}

STDLIB = set(sys.stdlib_module_names)


def _iter_pyfiles():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS and not d.startswith(".")]
        if any(s in root for s in SKIP_DIRS):
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _imports_of(path):
    """Yield (module, lineno, guarded_by_importerror)."""
    try:
        tree = ast.parse(open(path, encoding="utf-8",
                              errors="replace").read())
    except Exception:
        return
    # Collect all try-blocks that catch ImportError for quick lookup.
    guarded_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            catches = False
            for h in node.handlers:
                if h.type is None:
                    catches = True
                elif isinstance(h.type, ast.Name) and h.type.id in (
                        "ImportError", "Exception"):
                    catches = True
                elif isinstance(h.type, ast.Tuple) and any(
                        isinstance(e, ast.Name) and e.id in (
                            "ImportError", "Exception")
                        for e in h.type.elts):
                    catches = True
            if catches:
                for child in ast.walk(node):
                    guarded_nodes.add(id(child))
    for node in ast.walk(tree):
        mod = None
        if isinstance(node, ast.Import):
            for a in node.names:
                mod = a.name.split(".")[0]
                yield mod, node.lineno, id(node) in guarded_nodes
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mod = node.module.split(".")[0]
                yield mod, node.lineno, id(node) in guarded_nodes


def _local_modules():
    local = set()
    for entry in os.listdir(REPO):
        if entry.startswith("."):
            continue
        full = os.path.join(REPO, entry)
        if os.path.isdir(full) and os.path.exists(
                os.path.join(full, "__init__.py")):
            local.add(entry)
        elif entry.endswith(".py"):
            local.add(entry[:-3])
    return local


def test_no_undeclared_hard_imports():
    """The httpx-class bug: a third-party import that is neither declared
    in pyproject nor on the acknowledged-optional list."""
    local = _local_modules()
    problems = []
    for path in _iter_pyfiles():
        for mod, lineno, guarded in _imports_of(path):
            if mod in STDLIB or mod in local:
                continue
            if mod in DECLARED or mod in OPTIONAL or mod == "setuptools":
                continue
            rel = os.path.relpath(path, REPO)
            problems.append(
                f"{rel}:{lineno}: '{mod}' is neither declared in "
                f"pyproject nor an acknowledged optional import")
    assert not problems, "\n".join(problems)


def test_optional_imports_have_guarded_probe():
    """Each optional module must have at least one try/except ImportError
    probe site — the pattern the rest of the code relies on."""
    probes = {m: False for m in OPTIONAL}
    for path in _iter_pyfiles():
        for mod, lineno, guarded in _imports_of(path):
            if mod in probes and guarded:
                probes[mod] = True
    missing = sorted(m for m, ok in probes.items() if not ok)
    assert not missing, f"optional imports without a guarded probe: {missing}"


def test_server_requirements_cover_server_imports():
    req_path = os.path.join(REPO, "server", "requirements.txt")
    reqs = set()
    for line in open(req_path):
        line = line.strip()
        if line and not line.startswith("#"):
            reqs.add(line.split(">=")[0].split("==")[0].strip().lower())
    missing = []
    for f in os.listdir(os.path.join(REPO, "server")):
        if not f.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(REPO, "server", f)).read())
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.Import):
                for a in node.names:
                    mod = a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 \
                    and node.module:
                mod = node.module.split(".")[0]
            if mod and mod not in STDLIB and mod != "server" \
                    and mod.lower() not in reqs:
                missing.append(f"server/{f}: '{mod}'")
    assert not missing, "\n".join(missing)
