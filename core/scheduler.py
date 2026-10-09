"""In-Jade persistent task scheduler.

Runs scheduled jobs in-process on a background thread — no system cron,
no new dependencies. Jobs and run history survive restarts.

Job store: ``~/.jade/scheduler_jobs.json`` (0600, atomic writes).
Run log:   ``~/.jade/scheduler_runs.jsonl`` (append-only, every run logged).

Supported schedules:
  - one-shot:   {"kind": "once", "at": "<ISO datetime>"}
  - interval:   {"kind": "interval", "seconds": N}
  - cron:       {"kind": "cron", "expr": "0 3 * * *"}  (minimal 5-field parser)

Supported actions (whitelist — nothing else runs):
  - "/brief" / "/brief now"      proactive brief digest (skipped if briefs off)
  - "/ollama auto-pull"          pull tracked Ollama models with digest changelog
  - "/research <topic>"          deep-research report on a topic
  - "/health"                    system health check summary
  - "shell: <command>"           shell via tools/sandbox.py ONLY (Linux; refuses elsewhere)

Missed runs are never silently skipped: on load, any enabled job whose
next_run is older than the grace window gets a "missed" entry in the run
log and its next_run advanced to the next future occurrence. One-shot
jobs that missed their window are logged as missed and disabled.

Security notes:
  - Shell actions execute exclusively through the Linux sandbox
    (resource limits, isolated workdir). On non-Linux they fail loudly.
  - Do not embed secrets in scheduled actions — the action string is
    stored in plaintext in the jobs file and appears in the run log.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

JOBS_FILE = Path(os.path.expanduser("~/.jade/scheduler_jobs.json"))
RUNS_FILE = Path(os.path.expanduser("~/.jade/scheduler_runs.jsonl"))
MISSED_GRACE_SECONDS = 120
_TICK_SECONDS = 15
_MAX_WORKERS = 2
_SUMMARY_LIMIT = 800


# ---------------------------------------------------------------------------
# Minimal cron support (5-field: minute hour dom month dow)
# ---------------------------------------------------------------------------

_CRON_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]  # dow: 0=Sunday


def _parse_cron_field(field: str, lo: int, hi: int) -> set:
    """Parse one cron field into a set of ints. Supports *, */n, n, n-m, n,m."""
    values: set = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty cron field part in {field!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError(f"bad cron step in {field!r}")
        if part == "*" or part == "":
            start, end = lo, hi
        elif "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(part)
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ValueError(f"cron value out of range in {field!r}")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str) -> Tuple[set, set, set, set, set]:
    """Parse a 5-field cron expression. Raises ValueError on bad input."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression needs 5 fields (minute hour dom month dow), got {len(fields)}: {expr!r}")
    return tuple(_parse_cron_field(f, lo, hi) for f, (lo, hi) in zip(fields, _CRON_RANGES))


def next_cron_occurrence(expr: str, after: datetime) -> Optional[datetime]:
    """Next datetime strictly after `after` matching `expr`. None if none in ~2y."""
    mins, hours, doms, months, dows = parse_cron(expr)
    # Sunday handling: allow 7 as Sunday too.
    cand = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(366 * 2 * 24 * 60):  # ~2 years of minutes, safety cap
        py_dow = (cand.weekday() + 1) % 7  # Monday=0 -> Sunday=0
        if (cand.minute in mins and cand.hour in hours
                and cand.day in doms and cand.month in months
                and (py_dow in dows or (py_dow == 0 and 7 in dows))):
            return cand
        cand += timedelta(minutes=1)
    return None


def next_occurrence(schedule: Dict[str, Any], after: datetime) -> Optional[datetime]:
    kind = schedule.get("kind")
    if kind == "once":
        at = datetime.fromisoformat(schedule["at"])
        return at if at > after else None
    if kind == "interval":
        secs = float(schedule["seconds"])
        if secs <= 0:
            raise ValueError("interval must be positive")
        return after + timedelta(seconds=secs)
    if kind == "cron":
        return next_cron_occurrence(schedule["expr"], after)
    raise ValueError(f"unknown schedule kind: {kind!r}")


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

def _write_private_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load_jobs() -> List[Dict[str, Any]]:
    if not JOBS_FILE.exists():
        return []
    try:
        with open(JOBS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Actions (whitelist)
# ---------------------------------------------------------------------------

def _action_brief(_arg: str) -> str:
    from core import briefs
    if not briefs.is_enabled():
        return "skipped: proactive briefs are off (/brief on to enable)"
    items = briefs.compile_brief(mark_seen=True)
    return f"brief compiled: {len(items)} item(s)"


def _action_ollama_autopull(_arg: str) -> str:
    from core.ollama_mgmt import OllamaManager
    results = OllamaManager().auto_pull()
    if not results:
        return "auto-pull: nothing tracked"
    parts = []
    for r in results:
        parts.append(f"{r.get('model')}: {r.get('status')}")
    return "auto-pull: " + "; ".join(parts)


def _action_research(arg: str) -> str:
    topic = arg.strip()
    if not topic:
        return "error: /research needs a topic"
    from core.research import run_research
    result = run_research(topic)
    n_src = len(getattr(result, "sources", []) or [])
    return f"research on {topic!r}: {n_src} source(s)"


def _action_health(_arg: str) -> str:
    from core.health import check_system_health
    results = check_system_health()
    if isinstance(results, dict):
        bad = [k for k, v in results.items()
               if isinstance(v, dict) and v.get("status") not in ("ok", "healthy", "pass", True)]
        return f"health: {len(results)} check(s), {len(bad)} issue(s)" + (
            f" ({', '.join(bad)})" if bad else "")
    return f"health: {results}"


def _action_shell(arg: str) -> str:
    cmd = arg.strip()
    if not cmd:
        return "error: shell action needs a command"
    from tools.sandbox import run_sandboxed
    res = run_sandboxed(cmd, timeout=120, memory_mb=512)
    if res.error:
        return f"shell error: {res.error}"
    out = (res.stdout or "").strip()
    tail = out[-200:] if len(out) > 200 else out
    return f"shell exit={res.exit_code} backend={res.backend} out={tail!r}"


_ACTIONS = {
    "/brief": _action_brief,
    "/ollama auto-pull": _action_ollama_autopull,
    "/research": _action_research,
    "/health": _action_health,
    "shell:": _action_shell,
}


def resolve_action(action: str) -> Tuple[Optional[Any], str]:
    """Map an action string to (callable, arg). Returns (None, reason) if unknown."""
    a = action.strip()
    for prefix, fn in _ACTIONS.items():
        if a == prefix or a.startswith(prefix + " ") or (prefix.endswith(":") and a.startswith(prefix)):
            arg = a[len(prefix):].strip()
            return fn, arg
    known = ", ".join(sorted(_ACTIONS))
    return None, f"unknown action {a!r}. Supported: {known}"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: List[Dict[str, Any]] = _load_jobs()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool = ThreadPoolExecutor(max_workers=_MAX_WORKERS,
                                        thread_name_prefix="jade-sched")
        self._detect_missed()

    # -- persistence ------------------------------------------------------
    def _save(self) -> None:
        with self._lock:
            jobs = list(self._jobs)
        _write_private_json(JOBS_FILE, jobs)

    def log_run(self, job_id: str, name: str, action: str,
                status: str, summary: str) -> None:
        entry = {"ts": _now_iso(), "job_id": job_id, "name": name,
                 "action": action, "status": status,
                 "summary": summary[:_SUMMARY_LIMIT]}
        RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RUNS_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

    # -- missed runs -------------------------------------------------------
    def _detect_missed(self) -> None:
        now = datetime.now()
        changed = False
        for job in self._jobs:
            if not job.get("enabled", True):
                continue
            try:
                nxt = datetime.fromisoformat(job["next_run"])
            except (KeyError, ValueError):
                continue
            if (now - nxt).total_seconds() > MISSED_GRACE_SECONDS:
                self.log_run(job["id"], job["name"], job["action"],
                             "missed",
                             f"missed scheduled run at {job['next_run']} (Jade was not running)")
                nxt2 = next_occurrence(job["schedule"], now)
                if nxt2 is None:  # one-shot in the past
                    job["enabled"] = False
                    job["last_status"] = "missed"
                else:
                    job["next_run"] = nxt2.isoformat(timespec="seconds")
                    job["last_status"] = "missed"
                changed = True
        if changed:
            self._save()

    # -- job management ----------------------------------------------------
    def add_job(self, name: str, action: str, schedule: Dict[str, Any]) -> Dict[str, Any]:
        fn, reason = resolve_action(action)
        if fn is None:
            raise ValueError(reason)
        # Validate the schedule computes a future occurrence.
        nxt = next_occurrence(schedule, datetime.now())
        if nxt is None:
            raise ValueError("schedule has no future occurrence (one-shot time is in the past?)")
        job = {
            "id": uuid.uuid4().hex[:8],
            "name": name, "action": action.strip(),
            "schedule": schedule,
            "enabled": True,
            "created_at": _now_iso(),
            "next_run": nxt.isoformat(timespec="seconds"),
            "last_run": None, "last_status": "never", "run_count": 0,
        }
        with self._lock:
            self._jobs.append(job)
        self._save()
        return job

    def find_job(self, ident: str) -> Optional[Dict[str, Any]]:
        ident = ident.strip().lower()
        with self._lock:
            for j in self._jobs:
                if j["id"].lower() == ident or j["name"].lower() == ident:
                    return j
        return None

    def remove_job(self, ident: str) -> bool:
        with self._lock:
            for j in self._jobs:
                if j["id"].lower() == ident.strip().lower() or j["name"].lower() == ident.strip().lower():
                    self._jobs.remove(j)
                    removed = True
                    break
            else:
                return False
        self._save()
        return removed

    def set_enabled(self, ident: str, enabled: bool) -> bool:
        job = self.find_job(ident)
        if job is None:
            return False
        job["enabled"] = enabled
        if enabled:
            nxt = next_occurrence(job["schedule"], datetime.now())
            if nxt:
                job["next_run"] = nxt.isoformat(timespec="seconds")
        self._save()
        return True

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(j) for j in self._jobs]

    def recent_runs(self, n: int = 10) -> List[Dict[str, Any]]:
        if not RUNS_FILE.exists():
            return []
        lines = RUNS_FILE.read_text().splitlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # -- execution ---------------------------------------------------------
    def run_job_now(self, ident: str) -> Tuple[str, str]:
        job = self.find_job(ident)
        if job is None:
            return "error", f"no job {ident!r}"
        return self._execute(job)

    def _execute(self, job: Dict[str, Any]) -> Tuple[str, str]:
        fn, arg = resolve_action(job["action"])
        if fn is None:
            status, summary = "error", arg
        else:
            try:
                summary = str(fn(arg))
                status = "ok" if not summary.startswith("error") else "error"
            except Exception as e:  # a job must never take the scheduler down
                status, summary = "error", f"{type(e).__name__}: {e}"
        job["last_run"] = _now_iso()
        job["last_status"] = status
        job["run_count"] = job.get("run_count", 0) + 1
        nxt = next_occurrence(job["schedule"], datetime.now())
        if nxt is None:
            job["enabled"] = False  # one-shot completed
        else:
            job["next_run"] = nxt.isoformat(timespec="seconds")
        self._save()
        self.log_run(job["id"], job["name"], job["action"], status, summary)
        return status, summary

    def _due_jobs(self) -> List[Dict[str, Any]]:
        now = datetime.now()
        with self._lock:
            return [j for j in self._jobs
                    if j.get("enabled", True)
                    and datetime.fromisoformat(j["next_run"]) <= now]

    def tick(self) -> List[Tuple[str, str, str]]:
        """Run all due jobs. Returns [(job_id, status, summary)]."""
        results = []
        for job in self._due_jobs():
            fut = self._pool.submit(self._execute, job)
            try:
                status, summary = fut.result(timeout=600)
            except Exception as e:
                status, summary = "error", f"scheduler worker failed: {e}"
            results.append((job["id"], status, summary))
        return results

    # -- background thread --------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="jade-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False)

    def _loop(self) -> None:
        while not self._stop.wait(_TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                continue  # never die on a bad tick

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


_scheduler: Optional[Scheduler] = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> Scheduler:
    """Process-wide singleton. Starts lazily; call maybe_start_scheduler() in REPL."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = Scheduler()
        return _scheduler


def maybe_start_scheduler() -> Scheduler:
    sched = get_scheduler()
    if not sched.running:
        sched.start()
    return sched


def scheduler_status() -> Dict[str, Any]:
    try:
        sched = get_scheduler()
        jobs = sched.list_jobs()
        return {
            "running": sched.running,
            "jobs": len(jobs),
            "enabled": sum(1 for j in jobs if j.get("enabled")),
        }
    except Exception as e:
        return {"running": False, "jobs": 0, "enabled": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# CLI handler: /schedule ...
# ---------------------------------------------------------------------------

def _fmt_schedule(sched: Dict[str, Any]) -> str:
    kind = sched.get("kind")
    if kind == "once":
        return f"once at {sched.get('at')}"
    if kind == "interval":
        return f"every {sched.get('seconds')}s"
    if kind == "cron":
        return f"cron {sched.get('expr')!r}"
    return str(sched)


def handle_schedule(raw_args: str) -> None:
    """Handle `/schedule add|list|remove|run|pause|resume|log ...`."""
    from rich.console import Console
    from rich.table import Table
    console = Console()
    try:
        parts = shlex.split(raw_args) if raw_args.strip() else []
    except ValueError as e:
        console.print(f"[red]Could not parse arguments: {e}[/red]")
        return
    if not parts:
        parts = ["list"]
    sub = parts[0].lower()
    sched = get_scheduler()

    if sub == "list":
        jobs = sched.list_jobs()
        if not jobs:
            console.print("[dim]No scheduled jobs. Add one with:[/dim]\n"
                          '  /schedule add --name "nightly pull" --action "/ollama auto-pull" --cron "0 3 * * *"')
            return
        table = Table(title="Scheduled jobs")
        table.add_column("ID"); table.add_column("Name")
        table.add_column("Action"); table.add_column("Schedule")
        table.add_column("Next run"); table.add_column("Status")
        for j in jobs:
            table.add_row(j["id"], j["name"], j["action"],
                          _fmt_schedule(j["schedule"]),
                          j.get("next_run", "?") if j.get("enabled") else "[dim]paused[/dim]",
                          ("[green]on[/green]" if j.get("enabled") else "[yellow]paused[/yellow]")
                          + f" · last: {j.get('last_status')}")
        console.print(table)
        return

    if sub == "add":
        # /schedule add --name "x" --action "/brief now" (--every N | --cron "e" | --at "iso")
        args = parts[1:]
        opts: Dict[str, str] = {}
        i = 0
        while i < len(args):
            if args[i].startswith("--") and i + 1 < len(args):
                opts[args[i][2:]] = args[i + 1]
                i += 2
            else:
                i += 1
        name = opts.get("name", "").strip()
        action = opts.get("action", "").strip()
        if not name or not action:
            console.print('[yellow]Usage: /schedule add --name "my job" --action "/brief now" '
                          '[--every 3600 | --cron "0 3 * * *" | --at "2026-09-26T03:00:00"][/yellow]')
            console.print("[dim]Actions: /brief, /ollama auto-pull, /research <topic>, /health, shell: <cmd>[/dim]")
            return
        try:
            if "every" in opts:
                schedule: Dict[str, Any] = {"kind": "interval", "seconds": int(opts["every"])}
            elif "cron" in opts:
                parse_cron(opts["cron"])  # validate now
                schedule = {"kind": "cron", "expr": opts["cron"]}
            elif "at" in opts:
                at = datetime.fromisoformat(opts["at"])
                schedule = {"kind": "once", "at": at.isoformat(timespec="seconds")}
            else:
                console.print("[yellow]Pick a schedule: --every SECONDS, --cron \"0 3 * * *\", or --at \"ISO datetime\"[/yellow]")
                return
            job = sched.add_job(name, action, schedule)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return
        console.print(f"[green]Scheduled '{job['name']}' ({job['id']}) — next run {job['next_run']}[/green]")
        return

    if sub in ("remove", "rm", "delete"):
        ident = " ".join(parts[1:]).strip()
        if not ident:
            console.print("[yellow]Usage: /schedule remove <id or name>[/yellow]")
            return
        console.print("[green]Removed.[/green]" if sched.remove_job(ident)
                      else f"[red]No job {ident!r}.[/red]")
        return

    if sub == "run":
        ident = " ".join(parts[1:]).strip()
        if not ident:
            console.print("[yellow]Usage: /schedule run <id or name>[/yellow]")
            return
        status, summary = sched.run_job_now(ident)
        color = "green" if status == "ok" else "red"
        console.print(f"[{color}]{status}: {summary}[/{color}]")
        return

    if sub in ("pause", "resume"):
        ident = " ".join(parts[1:]).strip()
        if not ident:
            console.print(f"[yellow]Usage: /schedule {sub} <id or name>[/yellow]")
            return
        ok = sched.set_enabled(ident, sub == "resume")
        console.print(f"[green]{'Resumed' if sub == 'resume' else 'Paused'}.[/green]" if ok
                      else f"[red]No job {ident!r}.[/red]")
        return

    if sub == "log":
        n = 10
        if len(parts) > 1:
            try:
                n = max(1, min(100, int(parts[1])))
            except ValueError:
                pass
        runs = sched.recent_runs(n)
        if not runs:
            console.print("[dim]No job runs logged yet.[/dim]")
            return
        table = Table(title=f"Last {len(runs)} job run(s)")
        table.add_column("Time"); table.add_column("Job")
        table.add_column("Status"); table.add_column("Summary")
        for r in runs:
            color = {"ok": "green", "error": "red", "missed": "yellow"}.get(r["status"], "white")
            table.add_row(r["ts"], r["name"], f"[{color}]{r['status']}[/{color}]", r["summary"])
        console.print(table)
        return

    console.print("[yellow]Usage: /schedule add|list|remove|run|pause|resume|log[/yellow]")
