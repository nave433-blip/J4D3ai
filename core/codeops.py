"""Project-aware agentic coding loop (Track 2: Coding-first CLI features).

Linux-first (keeps macOS working). Stdlib only — no new dependencies.

Capabilities
------------
- Per-project config at ``<root>/.jade/config.json`` (model, sandbox policy,
  extra ignore patterns).
- Project file enumeration that respects ``.gitignore``: uses ``git ls-files``
  (``--cached --others --exclude-standard``) when ``git`` is available and the
  root is inside a work tree, otherwise falls back to ``os.walk`` with a simple
  ``.gitignore`` interpreter. The fallback only understands simple patterns
  (``*.ext``, ``dir/``, exact names, and plain ``path/`` patterns); it does NOT
  support negation (``!``), character classes beyond fnmatch, or per-directory
  ``.gitignore`` files nested deeper than the root. This limitation is
  deliberate and documented — the git path is preferred whenever available.
- Context builder ("repo tree + file contents") with a hard character budget.
- Checkpoints: snapshot files before applying a model-generated diff, rewind
  on demand (confirm-gated), prune old checkpoints.
- Unified-diff proposal/parse/apply. Only diffs that *modify existing files*
  are supported — new-file diffs (``--- /dev/null``) and renames are rejected
  with an honest error, never silently half-applied.
- Shell-command suggestion / explanation via the model. Suggested commands are
  NEVER executed automatically; the user copies and runs them.
- Sandboxed code execution via ``tools.sandbox.run_sandboxed``. Model output
  never runs outside the sandbox. When the sandbox is unavailable (non-Linux,
  unknown backend), the honest "unavailable" result is surfaced, never faked.

Security notes
--------------
- ``confirm()`` (auto-approve aware) gates every destructive or irreversible
  action: applying a diff and rewinding a checkpoint always ask first unless
  auto-approve is on (in which case the approval is logged by ``confirm``).
- Diff paths are confined to the project root: absolute paths and ``..``
  escapes are rejected.
"""

from __future__ import annotations

import copy
import datetime
import fnmatch
import json
import os
import re
import shutil
import subprocess

from typing import Dict, List, Optional, Sequence

from core.brain import think_structured
from core.approvals import confirm
from tools.sandbox import run_sandboxed

try:
    from rich.console import Console
    from rich.panel import Panel
except Exception:  # pragma: no cover - rich is a hard dep of the CLI anyway
    Console = None  # type: ignore
    Panel = None  # type: ignore


CONFIG_DIR = ".jade"
CONFIG_FILE = "config.json"
CHECKPOINTS_DIR = "checkpoints"

MAX_FILE_SIZE = 256 * 1024  # skip files larger than 256 KiB
MAX_FILES = 2000            # hard cap on enumerated project files
_BINARY_SNIFF_BYTES = 8192  # null-byte sniff window

_ALWAYS_SKIP_DIR_PARTS = frozenset({".git", "__pycache__", "node_modules"})


def _console():
    return Console() if Console is not None else None


def _print_panel(title: str, body: str) -> None:
    c = _console()
    if c is not None and Panel is not None:
        c.print(Panel(body, title=title, expand=False))
    else:  # pragma: no cover
        print(f"== {title} ==\n{body}")


# ---------------------------------------------------------------------------
# Project config
# ---------------------------------------------------------------------------

def _default_project_config() -> Dict:
    return {
        "model": None,
        "sandbox": {
            "timeout": 30,
            "memory_mb": 512,
            "allow_network": False,
            "backend": "auto",
        },
        "ignore": [],
    }


def load_project_config(root: str) -> Dict:
    """Read ``<root>/.jade/config.json`` merged over defaults.

    Missing file or corrupt JSON yields the defaults (never raises).
    The returned dict is an isolated deep copy.
    """
    cfg = _default_project_config()
    path = os.path.join(root, CONFIG_DIR, CONFIG_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict):
        raw_sandbox = raw.get("sandbox")
        if isinstance(raw_sandbox, dict):
            cfg["sandbox"].update(raw_sandbox)
        if "model" in raw:
            cfg["model"] = raw["model"]
        if isinstance(raw.get("ignore"), list):
            cfg["ignore"] = list(raw["ignore"])
    return copy.deepcopy(cfg)


def save_project_config(root: str, cfg: Dict) -> Dict:
    """Write ``cfg`` to ``<root>/.jade/config.json``. Returns ok/error."""
    try:
        cfg_dir = os.path.join(root, CONFIG_DIR)
        os.makedirs(cfg_dir, exist_ok=True)
        path = os.path.join(cfg_dir, CONFIG_FILE)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return {"ok": True, "path": path}
    except OSError as exc:
        return {"ok": False, "error": f"could not save project config: {exc}"}


# ---------------------------------------------------------------------------
# File enumeration
# ---------------------------------------------------------------------------

def _load_gitignore_patterns(root: str) -> List[str]:
    """Read the root ``.gitignore`` (fallback-walker use only).

    Returns simple patterns; negation (``!``) lines are ignored and this is
    documented as a limitation of the fallback path.
    """
    path = os.path.join(root, ".gitignore")
    patterns: List[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return patterns
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line)
    return patterns


def _pattern_matches(relpath: str, patterns: Sequence[str]) -> bool:
    """Simple .gitignore-style matching for a '/'-separated relative path."""
    basename = relpath.rsplit("/", 1)[-1]
    parts = relpath.split("/")
    for pat in patterns:
        if pat.endswith("/"):
            if pat[:-1] in parts:
                return True
        elif "/" in pat:
            p = pat.lstrip("/")
            if fnmatch.fnmatch(relpath, p):
                return True
        else:
            if fnmatch.fnmatch(basename, pat):
                return True
    return False


def _always_skip(relpath: str) -> bool:
    if relpath == CONFIG_DIR or relpath.startswith(CONFIG_DIR + "/checkpoints"):
        # Skip our own checkpoints from project context (config.json itself is
        # harmless to include, but checkpoints are just copies of files).
        return True
    return any(part in _ALWAYS_SKIP_DIR_PARTS for part in relpath.split("/"))


def _is_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True  # unreadable -> treat as unusable, skip


def _git_ls_files(root: str) -> Optional[List[str]]:
    """``git ls-files`` for tracked + untracked (gitignore-respecting) files.

    Returns None when git is unavailable or root is not in a work tree.
    """
    try:
        probe = subprocess.run(
            ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10)
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return None
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-z",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return [p for p in out.stdout.split("\x00") if p]
    except (OSError, subprocess.SubprocessError):
        return None


def _walk_files(root: str, gitignore_patterns: Sequence[str]) -> List[str]:
    """Fallback ``os.walk`` enumeration honoring simple .gitignore patterns."""
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        # Prune skipped directories in place.
        pruned = []
        for d in dirnames:
            rel = (rel_dir + "/" + d) if rel_dir != "." else d
            if _always_skip(rel) or _pattern_matches(rel + "/", gitignore_patterns):
                pruned.append(d)
        for d in pruned:
            dirnames.remove(d)
        for name in filenames:
            rel = (rel_dir + "/" + name) if rel_dir != "." else name
            if _always_skip(rel) or _pattern_matches(rel, gitignore_patterns):
                continue
            found.append(rel)
    return found


def iter_project_files(root: str, extra_ignore: Sequence[str] = ()) -> List[str]:
    """Enumerate project files as '/'-separated paths relative to ``root``.

    Uses ``git ls-files`` when possible, else a fallback walker with simple
    ``.gitignore`` support (see module docstring for its limitations).

    Always skips: ``.git/``, ``__pycache__/``, ``node_modules/``,
    ``.jade/`` (config + checkpoints), project-config ``ignore`` entries
    and ``extra_ignore`` patterns, files >256KB, binary files (null-byte
    sniff). Capped at 2000 files.
    """
    root = os.path.abspath(root)
    cfg = load_project_config(root)
    ignore_patterns = list(cfg.get("ignore") or []) + list(extra_ignore or ())

    rels = _git_ls_files(root)
    if rels is None:
        rels = _walk_files(root, _load_gitignore_patterns(root))

    out: List[str] = []
    for rel in rels:
        rel = rel.replace(os.sep, "/")
        if _always_skip(rel):
            continue
        if ignore_patterns and _pattern_matches(rel, ignore_patterns):
            continue
        full = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(full):
            continue
        try:
            if os.path.getsize(full) > MAX_FILE_SIZE:
                continue
        except OSError:
            continue
        if _is_binary(full):
            continue
        out.append(rel)
        if len(out) >= MAX_FILES:
            break
    return sorted(out)


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def build_context(root: str, max_chars: int = 60000) -> str:
    """Build a "repo tree + file contents" context string for the model.

    Truncates at ``max_chars`` and appends an explicit note naming how many
    files were omitted, so truncation is never silent.
    """
    root = os.path.abspath(root)
    files = iter_project_files(root)
    parts: List[str] = ["Project root: " + root, "", "Files:"]
    parts.extend("  " + f for f in files)
    parts.append("")
    parts.append("Contents:")
    text = "\n".join(parts) + "\n"
    included = 0
    for rel in files:
        full = os.path.join(root, *rel.split("/"))
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        block = f"=== {rel} ===\n{content}\n"
        if len(text) + len(block) > max_chars:
            break
        text += block
        included += 1
    omitted = len(files) - included
    if omitted > 0:
        text += (f"\n[...truncated: context budget of {max_chars} characters "
                 f"reached; {omitted} more file(s) omitted]\n")
    return text


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

class CheckpointManager:
    """Snapshot / rewind / prune project files under ``<root>/.jade/checkpoints/``."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.base = os.path.join(self.root, CONFIG_DIR, CHECKPOINTS_DIR)

    def _snap_dir(self, snap_id: str) -> str:
        return os.path.join(self.base, snap_id)

    def snapshot(self, paths: Sequence[str]) -> str:
        """Copy ``paths`` (relative or absolute) into a new checkpoint.

        Preserves relative paths. Returns the snapshot id.
        """
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snap_id = stamp
        n = 1
        while os.path.exists(self._snap_dir(snap_id)):
            n += 1
            snap_id = f"{stamp}-{n}"
        for p in paths:
            full = p if os.path.isabs(p) else os.path.join(self.root, p)
            rel = os.path.relpath(os.path.abspath(full), self.root)
            if rel.startswith(".."):
                continue  # outside project root: never snapshot
            if not os.path.isfile(full):
                continue
            dest = os.path.join(self._snap_dir(snap_id), rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(full, dest)
        os.makedirs(self._snap_dir(snap_id), exist_ok=True)
        return snap_id

    def list(self) -> List[str]:
        """Return checkpoint ids, oldest first."""
        try:
            entries = os.listdir(self.base)
        except OSError:
            return []
        return sorted(e for e in entries
                      if os.path.isdir(self._snap_dir(e)))

    def rewind(self, snap_id: str) -> Dict:
        """Restore files from a checkpoint.

        Asks for confirmation before overwriting anything (auto-approve aware).
        Returns an honest ok/error dict.
        """
        snap_dir = self._snap_dir(snap_id)
        if snap_id not in self.list():
            return {"ok": False, "error": f"no such checkpoint: {snap_id!r}"}
        backed_up: List[str] = []
        for dirpath, _dirnames, filenames in os.walk(snap_dir):
            for name in filenames:
                src = os.path.join(dirpath, name)
                rel = os.path.relpath(src, snap_dir)
                backed_up.append(rel)
        if not backed_up:
            return {"ok": False, "error": f"checkpoint {snap_id!r} is empty"}
        if not confirm(
                f"Rewind {len(backed_up)} file(s) to checkpoint {snap_id}? "
                "This overwrites current files.",
                default=False):
            return {"ok": False, "error": "rewind cancelled by user"}
        restored: List[str] = []
        for rel in backed_up:
            src = os.path.join(snap_dir, rel)
            dest = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            restored.append(rel)
        return {"ok": True, "snap_id": snap_id, "restored": sorted(restored)}

    def prune(self, keep: int = 10) -> Dict:
        """Delete oldest checkpoints, keeping the newest ``keep``."""
        ids = self.list()
        doomed = ids[:max(0, len(ids) - keep)]
        for snap_id in doomed:
            shutil.rmtree(self._snap_dir(snap_id), ignore_errors=True)
        return {"ok": True, "removed": doomed, "kept": self.list()}


# ---------------------------------------------------------------------------
# Unified diff parsing / application
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_diff_path(raw: str) -> str:
    raw = raw.strip()
    for prefix in ("a/", "b/"):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _strip_fences(text: str) -> str:
    lines = text.splitlines()
    while lines and lines[0].lstrip().startswith("```"):
        lines.pop(0)
    while lines and lines[-1].lstrip().startswith("```"):
        lines.pop()
    return "\n".join(lines)


def parse_unified_diff(text: str):
    """Parse a unified diff into ``[{path, hunks:[{old_start, old_lines, new_lines}]}]``.

    Only diffs that modify existing files are accepted. Returns an honest
    error dict ``{"ok": False, "error": ...}`` when the input is malformed,
    targets a new file (``--- /dev/null``), or is a rename — new files and
    renames are explicitly NOT supported.
    """
    if not text or not text.strip():
        return {"ok": False, "error": "empty diff"}
    text = _strip_fences(text)
    lines = text.splitlines()
    files: List[Dict] = []
    current = None
    i = 0
    # Tolerate leading prose: start at the first '--- ' line.
    while i < len(lines) and not lines[i].startswith("--- "):
        i += 1
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("--- "):
            old_raw = _strip_diff_path(line[4:])
            if old_raw == "/dev/null":
                return {"ok": False,
                        "error": "new-file diffs are not supported; "
                                 "only modifications to existing files"}
            if i + 1 >= n or not lines[i + 1].startswith("+++ "):
                return {"ok": False,
                        "error": f"malformed diff: '--- ' at line {i + 1} "
                                 "not followed by '+++ '"}
            new_raw = _strip_diff_path(lines[i + 1][4:])
            if new_raw == "/dev/null":
                return {"ok": False,
                        "error": "deletion diffs are not supported; "
                                 "only modifications to existing files"}
            if _strip_diff_path(old_raw) != _strip_diff_path(new_raw):
                return {"ok": False,
                        "error": "rename diffs are not supported; "
                                 "only modifications to existing files"}
            current = {"path": _strip_diff_path(new_raw), "hunks": []}
            files.append(current)
            i += 2
            continue
        m = _HUNK_RE.match(line)
        if m:
            if current is None:
                return {"ok": False,
                        "error": f"malformed diff: hunk at line {i + 1} "
                                 "before any file header"}
            old_start = int(m.group(1))
            hunk = {"old_start": old_start, "old_lines": [], "new_lines": []}
            current["hunks"].append(hunk)
            i += 1
            while i < n and not lines[i].startswith("@@") \
                    and not lines[i].startswith("--- "):
                hline = lines[i]
                if hline.startswith("\\"):
                    i += 1
                    continue  # "\ No newline at end of file"
                if not hline or hline[0] not in (" ", "-", "+"):
                    return {"ok": False,
                            "error": f"malformed diff: bad hunk line {i + 1}: "
                                     f"{hline[:40]!r}"}
                body = hline[1:]
                if hline[0] in (" ", "-"):
                    hunk["old_lines"].append(body)
                if hline[0] in (" ", "+"):
                    hunk["new_lines"].append(body)
                i += 1
            if not hunk["old_lines"] and not hunk["new_lines"]:
                return {"ok": False,
                        "error": f"malformed diff: empty hunk at line {i + 1}"}
            continue
        if line.startswith("+++ "):
            return {"ok": False,
                    "error": f"malformed diff: stray '+++ ' at line {i + 1}"}
        if line.strip() == "":
            i += 1
            continue
        return {"ok": False,
                "error": f"malformed diff: unexpected line {i + 1}: "
                         f"{line[:60]!r}"}
    if not files:
        return {"ok": False, "error": "no file diffs found in input"}
    if any(not f["hunks"] for f in files):
        bad = [f["path"] for f in files if not f["hunks"]]
        return {"ok": False,
                "error": f"file header(s) without hunks: {', '.join(bad)}"}
    return files


def _confine(root: str, relpath: str) -> Optional[str]:
    """Resolve a diff path inside ``root``; None when it escapes or is absolute."""
    if os.path.isabs(relpath):
        return None
    full = os.path.normpath(os.path.join(root, relpath))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


def apply_diff(root: str, parsed) -> Dict:
    """Apply a parsed diff to files under ``root`` (two-pass: verify, write).

    Returns ``{"ok": True, "files": [...]}`` or an honest error dict. On any
    verification failure nothing is written.
    """
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        return parsed  # pass parse errors through untouched
    if not isinstance(parsed, list) or not parsed:
        return {"ok": False, "error": "nothing to apply: empty parsed diff"}
    root = os.path.abspath(root)
    new_contents: Dict[str, str] = {}
    for entry in parsed:
        rel = entry.get("path", "")
        full = _confine(root, rel)
        if full is None:
            return {"ok": False,
                    "error": f"diff path escapes project root: {rel!r}"}
        if not os.path.isfile(full):
            return {"ok": False,
                    "error": f"diff targets missing file {rel!r}; only "
                             "modifications to existing files are supported"}
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                original = fh.read()
        except OSError as exc:
            return {"ok": False, "error": f"cannot read {rel!r}: {exc}"}
        trailing_nl = original.endswith("\n")
        lines = original.split("\n")
        if trailing_nl:
            lines = lines[:-1]
        shift = 0
        for hunk in entry.get("hunks", []):
            expected = hunk["old_start"] - 1 + shift
            old_lines = hunk["old_lines"]
            match_at = None
            for cand in range(max(0, expected - 5), expected + 6):
                if lines[cand:cand + len(old_lines)] == old_lines:
                    match_at = cand
                    break
            if match_at is None:
                return {"ok": False,
                        "error": f"hunk does not match {rel!r} near line "
                                 f"{hunk['old_start']} (file changed since "
                                 "the diff was proposed?); nothing was written"}
            lines[match_at:match_at + len(old_lines)] = hunk["new_lines"]
            shift += len(hunk["new_lines"]) - len(old_lines)
        new_contents[full] = "\n".join(lines) + ("\n" if trailing_nl else "")
    written: List[str] = []
    for full, content in new_contents.items():
        try:
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            return {"ok": False,
                    "error": f"wrote {len(written)} file(s), then failed on "
                             f"{os.path.relpath(full, root)!r}: {exc}"}
        written.append(os.path.relpath(full, root))
    return {"ok": True, "files": sorted(written)}


# ---------------------------------------------------------------------------
# Model-backed features
# ---------------------------------------------------------------------------

def _model_error(res: Dict, what: str) -> Dict:
    return {"ok": False,
            "error": res.get("error") or f"{what}: model request failed"}


def propose_diff(task: str, root: str, model: Optional[str] = None) -> Dict:
    """Build context, ask the model for a unified diff, parse it.

    Returns ``{"ok", "diff", "parsed", "provider"}`` or an honest error dict.
    """
    cfg = load_project_config(root)
    if model is None:
        model = cfg.get("model")
    context = build_context(root)
    prompt = (
        "You are a senior engineer editing a codebase. Given the task and the "
        "project context below, respond with ONLY a unified diff "
        "(---/+++/@@ hunks), no prose, no code fences, no explanations. "
        "Modify existing files only; do not create new files, delete files, "
        "or rename files.\n\nTask: " + task
    )
    res = think_structured(context, prompt, model=model)
    if not res.get("ok"):
        return _model_error(res, "could not propose diff")
    text = (res.get("text") or "").strip()
    parsed = parse_unified_diff(text)
    if isinstance(parsed, dict) and parsed.get("ok") is False:
        return {"ok": False,
                "error": f"model output was not a usable diff: "
                         f"{parsed['error']}",
                "raw": text}
    return {"ok": True, "diff": text, "parsed": parsed,
            "provider": res.get("provider")}


def suggest_shell(task: str, model: Optional[str] = None) -> Dict:
    """Ask the model to suggest a shell command. Never executes it.

    Returns ``{"ok", "command", "explanation"}`` or an honest error dict.
    """
    prompt = (
        "Suggest a single shell command for the task below. Respond with ONLY "
        "the shell command on the first line, then optionally a one-line "
        "explanation on the second line. No code fences, no prose beyond that."
        "\n\nTask: " + task
    )
    res = think_structured(
        "You suggest safe, Linux-first shell commands for a terminal assistant.",
        prompt, model=model)
    if not res.get("ok"):
        return _model_error(res, "could not suggest a command")
    lines = [ln for ln in (res.get("text") or "").strip().splitlines()
             if ln.strip() and not ln.strip().startswith("```")]
    if not lines:
        return {"ok": False, "error": "model returned an empty suggestion"}
    command = lines[0].strip().strip("`").strip()
    explanation = lines[1].strip() if len(lines) > 1 else ""
    return {"ok": True, "command": command, "explanation": explanation,
            "provider": res.get("provider")}


def explain_command(command: str, model: Optional[str] = None) -> Dict:
    """Ask the model to explain a shell command.

    Returns ``{"ok", "explanation"}`` or an honest error dict.
    """
    prompt = ("Explain what this shell command does, concisely: what it runs, "
              "what the key flags/arguments mean, and any risks or side "
              f"effects.\n\nCommand: {command}")
    res = think_structured(
        "You explain shell commands clearly and concisely.", prompt,
        model=model)
    if not res.get("ok"):
        return _model_error(res, "could not explain command")
    text = (res.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "model returned an empty explanation"}
    return {"ok": True, "explanation": text, "provider": res.get("provider")}


def run_code(command: str, root: str, model: Optional[str] = None) -> Dict:
    """Run ``command`` in the sandbox, in ``root`` as workdir.

    Sandbox policy (timeout, memory, network, backend) comes from the project
    config. Returns the ``SandboxResult`` dict plus ``"ok"``. Unavailable
    backends are surfaced honestly, never faked.
    """
    cfg = load_project_config(root)
    policy = cfg.get("sandbox") or {}
    result = run_sandboxed(
        command,
        timeout=policy.get("timeout", 30),
        memory_mb=policy.get("memory_mb", 512),
        cpu_seconds=30,
        allow_network=bool(policy.get("allow_network", False)),
        workdir=os.path.abspath(root),
        backend=policy.get("backend", "auto"),
    )
    d = result.to_dict()
    d["ok"] = result.ok
    return d


# ---------------------------------------------------------------------------
# CLI entry points (wired by the parent into cli.py)
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"\bin\s+(\S+)\s*$")


def handle_code_command(args: str) -> Dict:
    """/code <task> [in <dir>]: propose a diff, confirm, checkpoint, apply."""
    args = (args or "").strip()
    if not args:
        msg = "usage: /code <task> [in <dir>]"
        print(msg)
        return {"ok": False, "error": msg}
    m = _CODE_RE.search(args)
    if m:
        task = args[:m.start()].strip()
        root = os.path.abspath(
            os.path.join(os.getcwd(), os.path.expanduser(m.group(1))))
    else:
        task = args
        root = os.getcwd()
    if not task:
        msg = "usage: /code <task> [in <dir>]"
        print(msg)
        return {"ok": False, "error": msg}
    if not os.path.isdir(root):
        msg = f"not a directory: {root}"
        print(msg)
        return {"ok": False, "error": msg}

    print("Building project context and asking the model for a diff...")
    res = propose_diff(task, root)
    if not res.get("ok"):
        print(f"Could not propose a diff: {res.get('error')}")
        return res
    _print_panel("Proposed diff", res["diff"])
    if not confirm("Apply this diff?", default=False):
        print("Diff not applied.")
        return {"ok": False, "error": "user declined to apply diff"}

    parsed = res["parsed"]
    paths = [e["path"] for e in parsed]
    cm = CheckpointManager(root)
    snap_id = cm.snapshot(paths)
    applied = apply_diff(root, parsed)
    if not applied.get("ok"):
        print(f"Apply failed: {applied.get('error')}")
        print(f"Checkpoint {snap_id} still holds the pre-apply files.")
        return applied
    print(f"Applied to {len(applied['files'])} file(s): "
          f"{', '.join(applied['files'])}")
    print(f"Checkpoint: {snap_id}")
    print(f"To undo: CheckpointManager({root!r}).rewind({snap_id!r})")
    return {"ok": True, "snap_id": snap_id, "files": applied["files"],
            "provider": res.get("provider")}


def handle_suggest_command(args: str) -> Dict:
    """/suggest <natural language task>: print a suggested command.

    The command is displayed only — it is NEVER executed automatically.
    """
    task = (args or "").strip()
    if not task:
        msg = "usage: /suggest <describe what you want to do>"
        print(msg)
        return {"ok": False, "error": msg}
    res = suggest_shell(task)
    if not res.get("ok"):
        print(f"Could not suggest a command: {res.get('error')}")
        return res
    _print_panel("Suggested command (not executed)", res["command"])
    if res.get("explanation"):
        print(res["explanation"])
    print("This command was NOT run. Copy it and run it yourself, or use "
          "/explain to understand it first.")
    return res


def handle_explain_command(args: str) -> Dict:
    """/explain <command...>: print a plain-language explanation."""
    command = (args or "").strip()
    if not command:
        msg = "usage: /explain <command>"
        print(msg)
        return {"ok": False, "error": msg}
    res = explain_command(command)
    if not res.get("ok"):
        print(f"Could not explain: {res.get('error')}")
        return res
    _print_panel("Explanation", res["explanation"])
    return res
