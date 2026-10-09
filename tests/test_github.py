"""Tests for tools/github.py — gh CLI path, REST path, not-connected, write gating.

All network and subprocess interaction is stubbed; no real gh calls, no real
HTTP, no keyring writes.
"""

import io
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

import tools.github as ghmod
from tools.github import (
    actions_status,
    code_search,
    comment_issue,
    create_issue,
    get_pr,
    github_connect,
    handle_github_command,
    is_connected,
    list_issues,
    list_prs,
    list_repos,
)

REPO = "octocat/hello-world"


# ------------------------------------------------------------------ gh stubs

GH_REPOS = [
    {"name": "hello-world", "nameWithOwner": "octocat/hello-world",
     "isPrivate": False, "updatedAt": "2026-09-20T10:00:00Z",
     "url": "https://github.com/octocat/hello-world"},
]

GH_ISSUES = [
    {"number": 7, "title": "Bug report", "state": "OPEN",
     "author": {"login": "octocat"},
     "url": "https://github.com/octocat/hello-world/issues/7",
     "labels": [{"name": "bug"}]},
]

GH_PRS = [
    {"number": 3, "title": "Fix thing", "state": "OPEN",
     "author": {"login": "octocat"},
     "url": "https://github.com/octocat/hello-world/pull/3",
     "isDraft": True},
]

GH_PR_VIEW = {
    "number": 3, "title": "Fix thing", "state": "OPEN",
    "author": {"login": "octocat"},
    "url": "https://github.com/octocat/hello-world/pull/3",
    "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
    "statusCheckRollup": [
        {"name": "test", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "in_progress", "conclusion": ""},
    ],
}

GH_RUNS = [
    {"name": "CI", "status": "completed", "conclusion": "success",
     "headBranch": "main", "url": "https://github.com/octocat/hello-world/actions/runs/1"},
]

GH_SEARCH = [
    {"repository": {"nameWithOwner": "octocat/hello-world"},
     "path": "src/main.py", "url": "https://github.com/octocat/hello-world/blob/main/src/main.py"},
]


def _completed(payload, returncode=0, stderr=""):
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def make_fake_gh_run(extra=None):
    """subprocess.run stub dispatching on the gh subcommand."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert isinstance(cmd, list), "gh args must be a list (no shell=True)"
        assert cmd[0] == "gh"
        sub = cmd[1:]
        if sub[:2] == ["auth", "status"]:
            return _completed("✓ Logged in to github.com account octocat (keyring)\n")
        if sub[:2] == ["repo", "list"]:
            return _completed(GH_REPOS)
        if sub[:2] == ["issue", "list"]:
            return _completed(GH_ISSUES)
        if sub[:2] == ["pr", "list"]:
            return _completed(GH_PRS)
        if sub[:2] == ["pr", "view"]:
            return _completed(GH_PR_VIEW)
        if sub[:2] == ["run", "list"]:
            return _completed(GH_RUNS)
        if sub[:2] == ["issue", "create"]:
            return _completed("https://github.com/octocat/hello-world/issues/42\n")
        if sub[:2] == ["issue", "comment"]:
            return _completed("")
        if sub[:2] == ["search", "code"]:
            return _completed(GH_SEARCH)
        if extra:
            return extra(sub)
        raise AssertionError(f"unexpected gh call: {sub}")

    fake_run.calls = calls
    return fake_run


@pytest.fixture
def gh_backend(monkeypatch):
    """Pretend gh exists and is authenticated."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    fake_run = make_fake_gh_run()
    monkeypatch.setattr(subprocess, "run", fake_run)
    return fake_run


# ------------------------------------------------------------------ REST stubs

REST_REPOS = [
    {"name": "hello-world", "full_name": "octocat/hello-world", "private": True,
     "updated_at": "2026-09-19T08:00:00Z", "html_url": "https://github.com/octocat/hello-world"},
]

REST_ISSUES = [
    {"number": 7, "title": "Bug report", "state": "open",
     "user": {"login": "octocat"},
     "html_url": "https://github.com/octocat/hello-world/issues/7",
     "labels": [{"name": "bug"}]},
    # Issues endpoint also returns PRs; these must be filtered out.
    {"number": 3, "title": "Fix thing", "state": "open",
     "user": {"login": "octocat"},
     "html_url": "https://github.com/octocat/hello-world/pull/3",
     "labels": [], "pull_request": {"url": "x"}},
]

REST_PRS = [
    {"number": 3, "title": "Fix thing", "state": "open",
     "user": {"login": "octocat"},
     "html_url": "https://github.com/octocat/hello-world/pull/3",
     "draft": False},
]

REST_PR_DETAIL = {
    "number": 3, "title": "Fix thing", "state": "open",
    "user": {"login": "octocat"},
    "html_url": "https://github.com/octocat/hello-world/pull/3",
    "draft": False, "mergeable": True, "mergeable_state": "clean",
    "head": {"sha": "abc123"},
}

REST_CHECK_RUNS = {
    "check_runs": [
        {"name": "test", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "completed", "conclusion": "failure"},
    ]
}

REST_RUNS = {
    "workflow_runs": [
        {"name": "CI", "status": "completed", "conclusion": "success",
         "head_branch": "main",
         "html_url": "https://github.com/octocat/hello-world/actions/runs/1"},
    ]
}

REST_SEARCH = {
    "items": [
        {"repository": {"full_name": "octocat/hello-world"},
         "path": "src/main.py",
         "html_url": "https://github.com/octocat/hello-world/blob/main/src/main.py"},
    ]
}


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def make_fake_urlopen(captured, routes=None):
    routes = routes or {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured.setdefault("urls", []).append(req.full_url)
        assert timeout == 20, f"expected 20s timeout, got {timeout}"
        url = req.full_url
        for prefix, payload in routes.items():
            if prefix in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResp(payload)
        # default REST fixtures
        if url.endswith("/user") or "/user?" in url:
            return FakeResp({"login": "octocat"})
        if "/user/repos" in url:
            return FakeResp(REST_REPOS)
        if "/issues/" in url and url.endswith("/comments"):
            return FakeResp({"html_url": "https://github.com/o/r/issues/7#c1"})
        if "/repos/" in url and "/issues" in url:
            return FakeResp(REST_ISSUES)
        if "/pulls/3" in url:
            return FakeResp(REST_PR_DETAIL)
        if "/pulls" in url:
            return FakeResp(REST_PRS)
        if "/check-runs" in url:
            return FakeResp(REST_CHECK_RUNS)
        if "/actions/runs" in url:
            return FakeResp(REST_RUNS)
        if "/search/code" in url:
            return FakeResp(REST_SEARCH)
        raise AssertionError(f"unexpected REST call: {url} {req.get_method()}")

    return fake_urlopen


@pytest.fixture
def rest_backend(monkeypatch):
    """No gh CLI; token comes from the env var (keyring backend missing)."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(ghmod, "_keyring_backend_ok", lambda: False)
    monkeypatch.setenv(ghmod.ENV_TOKEN_VAR, "envtoken123")
    captured = {}
    monkeypatch.setattr(urllib.request, "urlopen", make_fake_urlopen(captured))
    return captured


# ------------------------------------------------------------------ gh path

def test_gh_repos_parsed(gh_backend):
    res = list_repos()
    assert res["ok"]
    assert res["data"] == [
        {"name": "hello-world", "full_name": "octocat/hello-world",
         "private": False, "updated_at": "2026-09-20T10:00:00Z",
         "url": "https://github.com/octocat/hello-world"}
    ]
    assert gh_backend.calls[1][:3] == ["gh", "repo", "list"]


def test_gh_issues_parsed(gh_backend):
    res = list_issues(REPO)
    assert res["ok"]
    assert res["data"] == [
        {"number": 7, "title": "Bug report", "state": "open",
         "user": "octocat",
         "url": "https://github.com/octocat/hello-world/issues/7",
         "labels": ["bug"]}
    ]


def test_gh_prs_parsed(gh_backend):
    res = list_prs(REPO)
    assert res["ok"]
    assert res["data"][0]["draft"] is True
    assert res["data"][0]["number"] == 3


def test_gh_pr_detail_checks(gh_backend):
    res = get_pr(REPO, 3)
    assert res["ok"]
    data = res["data"]
    assert data["mergeable"] == "MERGEABLE"
    assert len(data["checks"]) == 2
    assert "1/2 successful" in data["checks_summary"]
    assert "1 pending" in data["checks_summary"]


def test_gh_actions_parsed(gh_backend):
    res = actions_status(REPO)
    assert res["ok"]
    assert res["data"] == [
        {"name": "CI", "status": "completed", "conclusion": "success",
         "branch": "main",
         "url": "https://github.com/octocat/hello-world/actions/runs/1"}
    ]


def test_gh_is_connected(gh_backend):
    st = is_connected()
    assert st == {"connected": True, "via": "gh",
                  "detail": "Authenticated via gh CLI (user: octocat)."}


def test_gh_code_search(gh_backend):
    res = code_search("def main")
    assert res["ok"]
    assert res["data"] == [
        {"repo": "octocat/hello-world", "path": "src/main.py",
         "url": "https://github.com/octocat/hello-world/blob/main/src/main.py"}
    ]


# ------------------------------------------------------------------ REST path

def test_rest_repos_parsed_and_auth_header(rest_backend):
    res = list_repos()
    assert res["ok"]
    assert res["data"] == [
        {"name": "hello-world", "full_name": "octocat/hello-world",
         "private": True, "updated_at": "2026-09-19T08:00:00Z",
         "url": "https://github.com/octocat/hello-world"}
    ]
    req = rest_backend["req"]
    assert req.get_header("Authorization") == "Bearer envtoken123"
    assert req.get_header("Accept") == "application/vnd.github+json"
    assert "/user/repos" in req.full_url
    assert "affiliation=owner" in req.full_url


def test_rest_issues_filter_prs(rest_backend):
    res = list_issues(REPO)
    assert res["ok"]
    assert [i["number"] for i in res["data"]] == [7]
    assert res["data"][0]["labels"] == ["bug"]


def test_rest_prs_parsed(rest_backend):
    res = list_prs(REPO)
    assert res["ok"]
    assert res["data"][0]["draft"] is False


def test_rest_pr_detail_checks(rest_backend):
    res = get_pr(REPO, 3)
    assert res["ok"]
    data = res["data"]
    assert data["mergeable"] is True
    assert len(data["checks"]) == 2
    assert "failed: lint" in data["checks_summary"]
    urls = rest_backend["urls"]
    assert any("/check-runs" in u for u in urls)


def test_rest_actions_parsed(rest_backend):
    res = actions_status(REPO)
    assert res["ok"]
    assert res["data"][0]["branch"] == "main"


def test_rest_code_search(rest_backend):
    res = code_search("def main")
    assert res["ok"]
    assert res["data"][0]["repo"] == "octocat/hello-world"


def test_rest_is_connected_via_token(rest_backend):
    st = is_connected()
    assert st["connected"] is True
    assert st["via"] == "token"
    assert ghmod.ENV_TOKEN_VAR in st["detail"]


def test_rest_http_error_is_honest(monkeypatch, rest_backend):
    err = urllib.error.HTTPError(
        "https://api.github.com/repos/o/r/issues", 404, "Not Found", {},
        io.BytesIO(b'{"message": "Not Found"}'))
    monkeypatch.setattr(urllib.request, "urlopen",
                        make_fake_urlopen({}, routes={"/repos/": err}))
    res = list_issues("o/r")
    assert res["ok"] is False
    assert "404" in res["error"]


# ------------------------------------------------------------------ not connected

@pytest.fixture
def no_backend(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(ghmod, "_keyring_backend_ok", lambda: False)
    monkeypatch.delenv(ghmod.ENV_TOKEN_VAR, raising=False)


def test_not_connected_status(no_backend):
    st = is_connected()
    assert st["connected"] is False
    assert st["via"] is None
    assert "/github connect" in st["detail"]


def test_not_connected_reads_fail_honestly(no_backend):
    for fn in (lambda: list_repos(), lambda: list_issues(REPO),
               lambda: list_prs(REPO), lambda: get_pr(REPO, 1),
               lambda: actions_status(REPO), lambda: code_search("x")):
        res = fn()
        assert res["ok"] is False
        assert "/github connect" in res["next_steps"]


def test_handle_github_command_not_connected_prints_next_steps(no_backend, capsys):
    res = handle_github_command(["repos"])
    assert res["ok"] is False
    out = capsys.readouterr().out
    assert "/github connect" in out


def test_invalid_repo_rejected(gh_backend):
    res = list_issues("not-a-repo")
    assert res["ok"] is False
    assert "owner/name" in res["error"]


# ------------------------------------------------------------------ write gating

def _no_transport(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("transport must not run before confirm passes")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)


def test_create_issue_aborts_when_confirm_declines(monkeypatch, gh_backend):
    calls = []
    monkeypatch.setattr(ghmod, "confirm",
                        lambda prompt, default=False, **kw: calls.append((prompt, default)) or False)
    _no_transport(monkeypatch)
    res = create_issue(REPO, "My title", "body")
    assert res == {"ok": False, "error": "cancelled by user"}
    assert calls == [(f"Create issue 'My title' in {REPO}?", False)]


def test_create_issue_proceeds_when_confirm_allows(monkeypatch, gh_backend):
    calls = []
    monkeypatch.setattr(ghmod, "confirm",
                        lambda prompt, default=False, **kw: calls.append(prompt) or True)
    res = create_issue(REPO, "My title", "body")
    assert res["ok"] is True
    assert res["data"]["url"] == "https://github.com/octocat/hello-world/issues/42"
    assert len(calls) == 1


def test_comment_issue_aborts_when_confirm_declines(monkeypatch, rest_backend):
    calls = []
    monkeypatch.setattr(ghmod, "confirm",
                        lambda prompt, default=False, **kw: calls.append((prompt, default)) or False)

    def boom(*a, **k):
        raise AssertionError("transport must not run before confirm passes")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    res = comment_issue(REPO, 7, "nice")
    assert res == {"ok": False, "error": "cancelled by user"}
    assert calls == [(f"Post comment on #7 in {REPO}?", False)]


def test_comment_issue_proceeds_when_confirm_allows(monkeypatch, rest_backend):
    monkeypatch.setattr(ghmod, "confirm", lambda *a, **k: True)
    res = comment_issue(REPO, 7, "nice")
    assert res["ok"] is True
    assert res["data"]["commented"] is True
    req = rest_backend["req"]
    assert req.get_method() == "POST"
    assert req.full_url.endswith(f"/repos/{REPO}/issues/7/comments")


# ------------------------------------------------------------------ connect flow

def test_env_token_used_when_keyring_backend_missing(monkeypatch):
    import keyring

    def boom(*a, **k):
        raise RuntimeError("no backend")
    monkeypatch.setattr(keyring, "get_password", boom)
    monkeypatch.setattr(ghmod, "_keyring_backend_ok", lambda: True)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv(ghmod.ENV_TOKEN_VAR, "envtoken123")
    captured = {}
    monkeypatch.setattr(urllib.request, "urlopen", make_fake_urlopen(captured))
    res = list_repos()
    assert res["ok"] is True
    assert captured["req"].get_header("Authorization") == "Bearer envtoken123"


def test_github_connect_validates_and_stores(monkeypatch):
    import keyring
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(ghmod, "_keyring_backend_ok", lambda: True)
    stored = {}
    monkeypatch.setattr(keyring, "set_password",
                        lambda svc, acct, tok: stored.update(service=svc, account=acct))
    captured = {}
    monkeypatch.setattr(urllib.request, "urlopen", make_fake_urlopen(captured))
    res = github_connect("ghp_testtoken")
    assert res["ok"] is True
    assert stored == {"service": "jade-dev", "account": "jade-github"}
    req = captured["req"]
    assert req.full_url == "https://api.github.com/user"
    assert req.get_header("Authorization") == "Bearer ghp_testtoken"


def test_github_connect_bad_credentials_not_stored(monkeypatch):
    import keyring
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(ghmod, "_keyring_backend_ok", lambda: True)
    stored = []
    monkeypatch.setattr(keyring, "set_password",
                        lambda *a: stored.append(a))
    err = urllib.error.HTTPError(
        "https://api.github.com/user", 401, "Unauthorized", {},
        io.BytesIO(b'{"message": "Bad credentials"}'))
    monkeypatch.setattr(urllib.request, "urlopen",
                        make_fake_urlopen({}, routes={"/user": err}))
    res = github_connect("ghp_bad")
    assert res["ok"] is False
    assert "401" in res["error"]
    assert stored == []


def test_handle_unknown_subcommand(gh_backend, capsys):
    res = handle_github_command(["frobnicate"])
    assert res["ok"] is False
    assert "unknown subcommand" in res["error"]
