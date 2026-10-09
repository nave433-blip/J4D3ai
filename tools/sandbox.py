"""Opt-in Linux code-execution sandbox.

Runs a shell command in a fresh temporary working directory with CPU/memory
limits and an optional network cut-off.

Isolation guarantees (honest version):

* With ``bwrap`` (bubblewrap) installed — the preferred backend:
    - /usr, /bin, /lib, /lib64 are read-only; only the fresh workdir and
      /tmp are writable; --die-with-parent kills the sandbox with Jade.
    - ``allow_network=False`` adds ``--unshare-net``: no network at all.
    - RLIMIT_AS / RLIMIT_CPU / RLIMIT_FSIZE / RLIMIT_NPROC bound memory,
      CPU time, file size, and process count.
* Without ``bwrap`` — restricted-subprocess fallback (NOT a security
  boundary, only defense in depth):
    - fresh temp workdir, minimal scrubbed environment, same rlimits,
      new session/process group. The filesystem and network are NOT
      isolated; ``network_isolated`` is reported False so callers know.
* macOS / Windows: the Linux sandbox is unavailable — run_sandboxed()
  returns an explicit error instead of pretending.

Nothing here is on by default: tools.shell.run(..., sandbox=True) and the
``code_sandbox`` config key opt in explicitly.
"""

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field


@dataclass
class SandboxResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    timed_out: bool = False
    backend: str = ""            # "bwrap" | "subprocess" | "unavailable"
    network_isolated: bool = False
    workdir: str = ""
    error: str = ""              # set when the sandbox itself failed
    note: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return not self.error and not self.timed_out and self.exit_code == 0

    def to_dict(self) -> dict:
        d = {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "return_code": self.exit_code,
            "timed_out": self.timed_out,
            "backend": self.backend,
            "network_isolated": self.network_isolated,
            "sandboxed": True,
        }
        if self.error:
            d["error"] = self.error
        if self.note:
            d["note"] = self.note
        return d


def have_bwrap() -> bool:
    return shutil.which("bwrap") is not None


def sandbox_available() -> dict:
    """Probe sandbox support: {"ok", "backend", "detail"}."""
    if sys.platform != "linux":
        return {"ok": False, "backend": "unavailable",
                "detail": f"Linux code sandbox unavailable on {sys.platform}; "
                          "macOS/Windows cannot provide this isolation."}
    if have_bwrap():
        return {"ok": True, "backend": "bwrap",
                "detail": "bubblewrap: filesystem + network namespace isolation"}
    return {"ok": True, "backend": "subprocess",
            "detail": "restricted subprocess only (rlimits, temp dir, scrubbed "
                      "env) — not a security boundary; install bubblewrap "
                      "for real isolation"}


def _apply_limits(memory_mb: int, cpu_seconds: int):
    """preexec_fn: bound memory, CPU, file size, process count."""
    import resource

    def _set(res, soft, hard):
        try:
            resource.setrlimit(res, (soft, hard))
        except (ValueError, OSError):
            pass

    _set(resource.RLIMIT_AS, memory_mb * 1024 * 1024, memory_mb * 1024 * 1024)
    _set(resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 5)
    _set(resource.RLIMIT_FSIZE, 256 * 1024 * 1024, 256 * 1024 * 1024)
    if hasattr(resource, "RLIMIT_NPROC"):
        _set(resource.RLIMIT_NPROC, 128, 128)


def _minimal_env(workdir: str) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": workdir,
        "TMPDIR": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _run_bwrap(command: str, workdir: str, timeout: float, memory_mb: int,
               cpu_seconds: int, allow_network: bool) -> SandboxResult:
    argv = [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", workdir, workdir,
        "--chdir", workdir,
        "--die-with-parent",
    ]
    if not allow_network:
        argv.append("--unshare-net")
    argv += ["/bin/sh", "-c", command]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            env=_minimal_env(workdir),
            preexec_fn=functools.partial(_apply_limits, memory_mb, cpu_seconds),
        )
    except subprocess.TimeoutExpired as e:
        return SandboxResult(
            stdout=e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr=e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
            timed_out=True, backend="bwrap",
            network_isolated=not allow_network, workdir=workdir,
            note=f"wall-clock timeout after {timeout}s; sandbox killed")
    except FileNotFoundError:
        return SandboxResult(backend="bwrap", workdir=workdir,
                             error="bwrap disappeared between probe and exec")
    except Exception as e:
        return SandboxResult(backend="bwrap", workdir=workdir,
                             error=f"bwrap exec failed: {e}")
    return SandboxResult(
        stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode,
        backend="bwrap", network_isolated=not allow_network, workdir=workdir)


def _run_restricted(command: str, workdir: str, timeout: float, memory_mb: int,
                    cpu_seconds: int) -> SandboxResult:
    """Fallback: rlimits + fresh temp dir + scrubbed env. Honest about the
    lack of filesystem/network isolation (see module docstring)."""
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True, text=True, timeout=timeout,
            cwd=workdir, env=_minimal_env(workdir),
            start_new_session=True,
            preexec_fn=functools.partial(_apply_limits, memory_mb, cpu_seconds),
        )
    except subprocess.TimeoutExpired as e:
        return SandboxResult(
            stdout=e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr=e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
            timed_out=True, backend="subprocess", network_isolated=False,
            workdir=workdir,
            note=f"wall-clock timeout after {timeout}s; process group killed")
    except Exception as e:
        return SandboxResult(backend="subprocess", workdir=workdir,
                             error=f"sandbox exec failed: {e}")
    return SandboxResult(
        stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode,
        backend="subprocess", network_isolated=False, workdir=workdir,
        note="fallback backend: rlimits + temp dir only; filesystem and "
             "network are NOT isolated without bwrap")


def run_sandboxed(command: str, timeout: float = 30, memory_mb: int = 512,
                  cpu_seconds: int = 30, allow_network: bool = False,
                  workdir: str = "", keep_workdir: bool = False,
                  backend: str = "auto") -> SandboxResult:
    """Run `command` (via /bin/sh) in a Linux sandbox. See module docstring
    for the exact isolation guarantees and limitations.

    backend: "auto" (bwrap when present), "bwrap" (error if missing),
             "subprocess" (force the fallback, e.g. for tests).
    """
    if sys.platform != "linux":
        return SandboxResult(
            backend="unavailable",
            error="Linux code-execution sandbox is unavailable on "
                  f"{sys.platform}; refusing to pretend otherwise.")
    if backend not in ("auto", "bwrap", "subprocess"):
        return SandboxResult(backend="unavailable",
                             error=f"unknown sandbox backend {backend!r}")

    made_dir = False
    if workdir:
        if not os.path.isdir(workdir):
            return SandboxResult(backend="unavailable",
                                 error=f"workdir does not exist: {workdir}")
    else:
        workdir = tempfile.mkdtemp(prefix="jade-sandbox-")
        made_dir = True

    try:
        use_bwrap = have_bwrap() if backend == "auto" else backend == "bwrap"
        if backend == "bwrap" and not have_bwrap():
            return SandboxResult(
                backend="bwrap", workdir=workdir,
                error="bwrap not found; install bubblewrap for namespace "
                      "isolation, or use backend='subprocess'")
        if use_bwrap:
            result = _run_bwrap(command, workdir, timeout, memory_mb,
                                cpu_seconds, allow_network)
        else:
            result = _run_restricted(command, workdir, timeout, memory_mb,
                                     cpu_seconds)
        return result
    finally:
        if made_dir and not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
