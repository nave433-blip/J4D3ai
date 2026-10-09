"""
PTY-backed Gemini-style multi-box execution engine.

- On Unix (macOS/Linux) uses os.openpty + subprocess with a pty master/slave.
- Spawns a reader thread per box to capture terminal output.
- run_command writes a wrapper to detect command completion and exit code.
- run_script_in_box writes a temp script and runs it inside the box.
- Falls back to subprocess.run when ptys are unavailable (Windows).
"""

import os
import time
import tempfile
import threading
import subprocess
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from rich.console import Console

console = Console()

_JADE_END_MARKER = "__JADE_END__"
_JADE_EXIT_PREFIX = "__JADE_EXIT_CODE:"

@dataclass
class BoxLogEntry:
    ts: float
    command: str
    output: str
    exit_code: int
    duration: float

@dataclass
class _Box:
    name: str
    shell: str
    master_fd: Optional[int] = None
    slave_fd: Optional[int] = None
    proc: Optional[subprocess.Popen] = None
    buffer: List[str] = field(default_factory=list)
    logs: List[BoxLogEntry] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader_thread: Optional[threading.Thread] = None
    alive: bool = False
    start_time: float = 0.0

class GeminiSession:
    def __init__(self, default_shell: str = "/bin/bash"):
        self._boxes: Dict[str, _Box] = {}
        self.default_shell = default_shell
        self.trusted = False  # when True, skip confirmations in UI

    def list_boxes(self) -> List[str]:
        return list(self._boxes.keys())

    def create_box(self, name: str, shell: Optional[str] = None) -> bool:
        if name in self._boxes:
            return False
        shell = shell or self.default_shell
        box = _Box(name=name, shell=shell)
        # Try PTY spawn
        try:
            master_fd, slave_fd = os.openpty()
            # Start a login interactive shell for a nicer environment
            proc = subprocess.Popen([shell, "-i"], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True, preexec_fn=os.setsid)
            box.master_fd = master_fd
            box.slave_fd = slave_fd
            box.proc = proc
            box.alive = True
            box.start_time = time.time()
            # Start reader thread
            t = threading.Thread(target=self._reader_loop, args=(name,), daemon=True)
            box.reader_thread = t
            t.start()
        except Exception:
            # fallback: mark as non-pty box (we'll use subprocess.run on each command)
            box.master_fd = None
            box.slave_fd = None
            box.proc = None
            box.alive = False
        self._boxes[name] = box
        return True

    def _reader_loop(self, name: str):
        box = self._boxes.get(name)
        if not box or box.master_fd is None:
            return
        try:
            fd = box.master_fd
            while True:
                try:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    text = data.decode(errors="ignore")
                    with box.lock:
                        box.buffer.append(text)
                        # keep buffer size bounded
                        if sum(len(s) for s in box.buffer) > 200_000:
                            # trim oldest
                            while sum(len(s) for s in box.buffer) > 100_000:
                                box.buffer.pop(0)
                except OSError:
                    break
                # stop if process ended
                if box.proc and box.proc.poll() is not None:
                    break
        except Exception:
            pass
        finally:
            box.alive = False

    def _collect_buffer(self, name: str) -> str:
        box = self._boxes.get(name)
        if not box:
            return ""
        with box.lock:
            out = "".join(box.buffer)
            # optionally keep buffer persisted
            box.buffer = [out[-100_000:]]  # keep last chunk
        return out

    def close_box(self, name: str) -> bool:
        box = self._boxes.get(name)
        if not box:
            return False
        try:
            if box.proc:
                try:
                    box.proc.terminate()
                    time.sleep(0.1)
                    if box.proc.poll() is None:
                        box.proc.kill()
                except Exception:
                    pass
            if box.master_fd:
                try:
                    os.close(box.master_fd)
                except Exception:
                    pass
            if box.slave_fd:
                try:
                    os.close(box.slave_fd)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            box.alive = False
            if box.reader_thread:
                try:
                    box.reader_thread.join(timeout=0.2)
                except Exception:
                    pass
            del self._boxes[name]
        return True

    def tail_box(self, name: str, lines: int = 200) -> str:
        content = self._collect_buffer(name)
        if not content:
            return f"No logs for box '{name}'."
        fl = content.splitlines()
        return "\n".join(fl[-lines:])

    def run_in_box(self, name: str, command: str, timeout: int = 60) -> Dict[str, Any]:
        """
        Run a single command in the box. Returns dict with stdout, exit_code, duration, ok flag.
        For PTY boxes, send wrapper markers to detect completion.
        For non-pty boxes, fallback to subprocess.run.
        """
        box = self._boxes.get(name)
        if not box:
            return {"ok": False, "error": f"Box '{name}' not found."}
        start = time.time()
        # if no pty, fallback to run per-command
        if box.master_fd is None:
            try:
                completed = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, executable=box.shell if box.shell else None)
                duration = time.time() - start
                entry = BoxLogEntry(ts=time.time(), command=command, output=(completed.stdout or "") + (completed.stderr or ""), exit_code=completed.returncode, duration=duration)
                box.logs.append(entry)
                return {"ok": True, "stdout": completed.stdout, "stderr": completed.stderr, "exit_code": completed.returncode, "duration": duration}
            except subprocess.TimeoutExpired as te:
                return {"ok": False, "error": "timeout", "stdout": te.stdout, "stderr": te.stderr}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        # PTY path: write wrapper to print exit code and end marker
        wrapper = f"{command}\nprintf '\\n{_JADE_EXIT_PREFIX}%s\\n' \"$?\"\nprintf '{_JADE_END_MARKER}\\n'\n"
        try:
            os.write(box.master_fd, wrapper.encode())
        except Exception as e:
            return {"ok": False, "error": f"write failed: {e}"}
        # Wait for the end marker within timeout
        end_time = time.time() + timeout
        collected = ""
        exit_code = None
        while time.time() < end_time:
            time.sleep(0.05)
            collected = self._collect_buffer(name)
            if _JADE_END_MARKER in collected:
                # parse exit code
                # find last occurrence of exit prefix
                idx = collected.rfind(_JADE_EXIT_PREFIX)
                try:
                    if idx != -1:
                        start_idx = idx + len(_JADE_EXIT_PREFIX)
                        # next line contains code
                        rest = collected[start_idx:].splitlines()
                        if rest:
                            exit_code = int(rest[0].strip())
                except Exception:
                    exit_code = None
                break
            # detect process death
            if box.proc and box.proc.poll() is not None:
                time.sleep(0.05)
                collected = self._collect_buffer(name)
                if _JADE_END_MARKER in collected:
                    idx = collected.rfind(_JADE_EXIT_PREFIX)
                    try:
                        if idx != -1:
                            start_idx = idx + len(_JADE_EXIT_PREFIX)
                            rest = collected[start_idx:].splitlines()
                            if rest:
                                exit_code = int(rest[0].strip())
                    except Exception:
                        exit_code = None
                break
        duration = time.time() - start
        # cleanup marker(s) from output
        raw = collected
        raw = raw.replace(_JADE_END_MARKER, "")
        raw = raw.replace(_JADE_EXIT_PREFIX, "[EXIT_PREFIX]")
        # attempt to extract stdout/stderr combined; PTY mixes both
        entry = BoxLogEntry(ts=time.time(), command=command, output=raw, exit_code=(exit_code if exit_code is not None else -1), duration=duration)
        box.logs.append(entry)
        return {"ok": True, "output": raw, "exit_code": (exit_code if exit_code is not None else -1), "duration": duration}

    def run_script_in_box(self, name: str, script_text: str, timeout: int = 120) -> Dict[str, Any]:
        # write to temp file in current working dir of Jade process
        box = self._boxes.get(name)
        if not box:
            return {"ok": False, "error": f"Box '{name}' not found."}
        fd, path = tempfile.mkstemp(prefix="jade_gemini_", suffix=".sh", text=True)
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(script_text)
            os.chmod(path, 0o700)
            cmd = f"{path}"
            return self.run_in_box(name, cmd, timeout=timeout)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    def show_box_output(self, name: str) -> List[Dict[str, Any]]:
        box = self._boxes.get(name)
        if not box:
            return []
        return [{"ts": l.ts, "command": l.command, "output": l.output, "exit_code": l.exit_code, "duration": l.duration} for l in box.logs]

session = GeminiSession()
