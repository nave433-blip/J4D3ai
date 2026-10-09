"""Tests for core/scheduler.py — persistent in-Jade task scheduler.

All filesystem state is redirected to tmp_path; no real jobs run, no
network, no sleeping.
"""
import json
import os
import stat
from datetime import datetime, timedelta

import pytest

import core.scheduler as sched_mod
from core.scheduler import (
    Scheduler, parse_cron, next_cron_occurrence, next_occurrence,
    resolve_action, handle_schedule,
)


@pytest.fixture()
def iso_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sched_mod, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(sched_mod, "RUNS_FILE", tmp_path / "runs.jsonl")
    # Register a harmless test action so tests never touch real subsystems.
    monkeypatch.setitem(sched_mod._ACTIONS, "/test-echo",
                        lambda arg: f"echo:{arg}")
    return tmp_path


def _sched():
    return Scheduler()


# --- cron parsing ----------------------------------------------------------

def test_parse_cron_valid():
    mins, hours, doms, months, dows = parse_cron("*/15 9-17 * * 1-5")
    assert mins == set(range(0, 60, 15))
    assert hours == set(range(9, 18))
    assert dows == {1, 2, 3, 4, 5}


def test_parse_cron_bad_field_count():
    with pytest.raises(ValueError):
        parse_cron("0 9 * *")


def test_parse_cron_out_of_range():
    with pytest.raises(ValueError):
        parse_cron("99 9 * * *")


def test_next_cron_occurrence_same_day():
    after = datetime(2026, 9, 25, 8, 0, 0)
    nxt = next_cron_occurrence("0 9 * * *", after)
    assert nxt == datetime(2026, 9, 25, 9, 0, 0)


def test_next_cron_occurrence_next_day():
    after = datetime(2026, 9, 25, 10, 0, 0)
    nxt = next_cron_occurrence("0 9 * * *", after)
    assert nxt == datetime(2026, 9, 26, 9, 0, 0)


def test_next_cron_occurrence_weekly():
    # 2026-09-25 is a Friday. Next Monday 09:00 -> 2026-09-28.
    after = datetime(2026, 9, 25, 8, 0, 0)
    nxt = next_cron_occurrence("0 9 * * 1", after)
    assert nxt == datetime(2026, 9, 28, 9, 0, 0)


def test_next_occurrence_interval():
    after = datetime(2026, 9, 25, 8, 0, 0)
    nxt = next_occurrence({"kind": "interval", "seconds": 3600}, after)
    assert nxt == after + timedelta(seconds=3600)


def test_next_occurrence_once_future_and_past():
    after = datetime(2026, 9, 25, 8, 0, 0)
    assert next_occurrence({"kind": "once", "at": "2026-09-25T09:00:00"}, after) == \
        datetime(2026, 9, 25, 9, 0, 0)
    assert next_occurrence({"kind": "once", "at": "2026-09-25T07:00:00"}, after) is None


# --- actions ----------------------------------------------------------------

def test_resolve_action_whitelist(iso_env):
    fn, arg = resolve_action("/test-echo hello")
    assert callable(fn) and arg == "hello"
    fn, reason = resolve_action("/delete-everything")
    assert fn is None and "Supported" in reason


def test_resolve_action_shell_prefix():
    fn, arg = resolve_action("shell: uptime")
    assert callable(fn) and arg == "uptime"


# --- job lifecycle -----------------------------------------------------------

def test_add_and_find_job(iso_env):
    s = _sched()
    job = s.add_job("demo", "/test-echo hi", {"kind": "interval", "seconds": 60})
    assert job["id"] and job["enabled"]
    assert s.find_job(job["id"])["name"] == "demo"
    assert s.find_job("DEMO")["id"] == job["id"]


def test_add_job_rejects_unknown_action(iso_env):
    s = _sched()
    with pytest.raises(ValueError, match="unknown action"):
        s.add_job("bad", "/rm -rf /", {"kind": "interval", "seconds": 60})


def test_add_job_rejects_past_oneshot(iso_env):
    s = _sched()
    with pytest.raises(ValueError, match="no future occurrence"):
        s.add_job("past", "/test-echo x",
                  {"kind": "once", "at": "2020-01-01T00:00:00"})


def test_jobs_file_is_private(iso_env):
    s = _sched()
    s.add_job("demo", "/test-echo hi", {"kind": "interval", "seconds": 60})
    mode = stat.S_IMODE(os.stat(sched_mod.JOBS_FILE).st_mode)
    assert mode == 0o600


def test_jobs_survive_reload(iso_env):
    s = _sched()
    job = s.add_job("demo", "/test-echo hi", {"kind": "interval", "seconds": 60})
    s2 = Scheduler()
    assert s2.find_job(job["id"]) is not None


def test_remove_pause_resume(iso_env):
    s = _sched()
    job = s.add_job("demo", "/test-echo hi", {"kind": "interval", "seconds": 60})
    assert s.set_enabled(job["id"], False)
    assert s.find_job(job["id"])["enabled"] is False
    assert s.set_enabled("demo", True)
    assert s.find_job(job["id"])["enabled"] is True
    assert s.remove_job(job["id"])
    assert s.find_job(job["id"]) is None
    assert not s.remove_job("nope")


# --- execution ----------------------------------------------------------------

def test_run_job_now_logs_and_advances(iso_env):
    s = _sched()
    job = s.add_job("demo", "/test-echo hi", {"kind": "interval", "seconds": 3600})
    status, summary = s.run_job_now(job["id"])
    assert status == "ok" and "echo:hi" in summary
    runs = s.recent_runs(5)
    assert len(runs) == 1 and runs[0]["status"] == "ok"
    assert runs[0]["job_id"] == job["id"]
    # next_run advanced by the interval
    assert s.find_job(job["id"])["run_count"] == 1


def test_run_job_action_exception_is_captured(iso_env, monkeypatch):
    def _boom(_arg):
        raise RuntimeError("kablam")
    monkeypatch.setitem(sched_mod._ACTIONS, "/test-boom", _boom)
    s = _sched()
    job = s.add_job("boom", "/test-boom", {"kind": "interval", "seconds": 60})
    status, summary = s.run_job_now(job["id"])
    assert status == "error" and "kablam" in summary


def test_tick_runs_due_jobs(iso_env):
    s = _sched()
    job = s.add_job("demo", "/test-echo hi", {"kind": "interval", "seconds": 3600})
    # Force it due.
    job["next_run"] = (datetime.now() - timedelta(seconds=5)).isoformat(timespec="seconds")
    s._save()
    results = s.tick()
    assert len(results) == 1 and results[0][1] == "ok"


def test_missed_run_logged_not_silently_skipped(iso_env):
    s = _sched()
    job = s.add_job("demo", "/test-echo hi", {"kind": "interval", "seconds": 3600})
    # Simulate Jade being down for a day: next_run long past.
    job["next_run"] = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    s._save()
    s2 = Scheduler()  # load triggers missed detection
    runs = s2.recent_runs(5)
    assert any(r["status"] == "missed" for r in runs)
    # next_run advanced into the future
    assert datetime.fromisoformat(s2.find_job(job["id"])["next_run"]) > datetime.now()


def test_missed_oneshot_disabled(iso_env):
    s = _sched()
    future = (datetime.now() + timedelta(seconds=30)).isoformat(timespec="seconds")
    job = s.add_job("once", "/test-echo hi", {"kind": "once", "at": future})
    # Simulate Jade being down when the one-shot time passed:
    # both the scheduled time and next_run are now in the past.
    past = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    job["schedule"]["at"] = past
    job["next_run"] = past
    s._save()
    s2 = Scheduler()
    assert s2.find_job(job["id"])["enabled"] is False
    assert any(r["status"] == "missed" for r in s2.recent_runs(5))


def test_scheduler_thread_start_stop(iso_env):
    s = _sched()
    s.start()
    assert s.running
    s.stop()
    assert not s.running


def test_scheduler_status_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(sched_mod, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(sched_mod, "RUNS_FILE", tmp_path / "runs.jsonl")
    st = sched_mod.scheduler_status()
    assert set(st) >= {"running", "jobs", "enabled"}


# --- CLI ----------------------------------------------------------------------

def _sched_singleton(monkeypatch, tmp_path):
    monkeypatch.setattr(sched_mod, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(sched_mod, "RUNS_FILE", tmp_path / "runs.jsonl")
    monkeypatch.setitem(sched_mod._ACTIONS, "/test-echo", lambda arg: f"echo:{arg}")
    inst = Scheduler()
    monkeypatch.setattr(sched_mod, "_scheduler", inst)
    return inst


def test_cli_add_list_remove(monkeypatch, tmp_path, capsys):
    _sched_singleton(monkeypatch, tmp_path)
    handle_schedule('add --name "demo" --action "/test-echo hi" --every 60')
    out = capsys.readouterr().out
    assert "Scheduled" in out
    handle_schedule("list")
    assert "demo" in capsys.readouterr().out
    handle_schedule("remove demo")
    assert "Removed" in capsys.readouterr().out


def test_cli_add_bad_action(monkeypatch, tmp_path, capsys):
    _sched_singleton(monkeypatch, tmp_path)
    handle_schedule('add --name "x" --action "/nope" --every 60')
    assert "unknown action" in capsys.readouterr().out


def test_cli_add_bad_cron(monkeypatch, tmp_path, capsys):
    _sched_singleton(monkeypatch, tmp_path)
    handle_schedule('add --name "x" --action "/test-echo hi" --cron "nope"')
    assert "5 fields" in capsys.readouterr().out


def test_cli_run_and_log(monkeypatch, tmp_path, capsys):
    _sched_singleton(monkeypatch, tmp_path)
    handle_schedule('add --name "demo" --action "/test-echo hi" --every 60')
    capsys.readouterr()
    handle_schedule("run demo")
    assert "echo:hi" in capsys.readouterr().out
    handle_schedule("log 5")
    out = capsys.readouterr().out
    assert "demo" in out and "ok" in out


def test_cli_pause_resume(monkeypatch, tmp_path, capsys):
    _sched_singleton(monkeypatch, tmp_path)
    handle_schedule('add --name "demo" --action "/test-echo hi" --every 60')
    capsys.readouterr()
    handle_schedule("pause demo")
    assert "Paused" in capsys.readouterr().out
    handle_schedule("resume demo")
    assert "Resumed" in capsys.readouterr().out


def test_cli_usage_on_empty(monkeypatch, tmp_path, capsys):
    _sched_singleton(monkeypatch, tmp_path)
    handle_schedule("add")
    assert "Usage" in capsys.readouterr().out
    handle_schedule("bogus")
    assert "Usage" in capsys.readouterr().out
