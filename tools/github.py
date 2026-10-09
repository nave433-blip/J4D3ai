"""GitHub connector: repos, issues, PRs, Actions runs, code search.

Backend preference
------------------
1. The ``gh`` CLI, when it is installed *and* authenticated
   (``gh auth status`` succeeds).
2. The GitHub REST API via stdlib ``urllib.request`` with a personal access
   token. The token is stored in the OS keyring (service ``"jade-dev"``,
   account ``"jade-github"``); when no keyring backend exists it falls back
   to the ``JADE_GITHUB_TOKEN`` environment variable. The token is never
   printed or logged — entry is via ``getpass`` and it is stored only after it
   validates against ``GET /user``.

Every public function returns ``{"ok": bool, "data"?, "error"?, "next_steps"?}``
and never raises on API/transport errors.
"""

import getpass
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import keyring
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from core.approvals import confirm

console = Console()

KEYRING_SERVICE = "jade-dev"
KEYRING_ACCOUNT = "jade-github"
ENV_TOKEN_VAR = "JADE_GITHUB_TOKEN"
API_BASE = "https://api.github.com"
REST_TIMEOUT = 20
GH_TIMEOUT = 20

NOT_CONNECTED_STEPS = (
    "Run `/github connect` to link GitHub "
    "(authenticates the `gh` CLI or stores a personal access token)."
)


# ------------------------------------------------------------------ token store

def _keyring_backend_ok() -> bool:
    """True when a real keyring backend (not the fail/no-op one) is usable."""
    try:
        from keyring.backends import fail
        return not isinstance(keyring.get_keyring(), fail.Keyring)
    except Exception:
        return False


def _get_token():
    """Return (token, source) where source is "keyring"/"env", or (None, None).

    Keyring first; ``JADE_GITHUB_TOKEN`` env var when no keyring backend
    (or no keyring entry) is available.
    """
    if _keyring_backend_ok():
        try:
            token = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            if token:
                return token, "keyring"
        except Exception:
            pass
    token = os.environ.get(ENV_TOKEN_VAR, "").strip()
    if token:
        return token, "env"
    return None, None


def _store_token(token: str) -> dict:
    """Persist the token in the keyring; never prints key material."""
    if not _keyring_backend_ok():
        return {
            "ok": True,
            "stored_in": "none",
            "next_steps": (
                "No OS keyring backend is available, so the token was not "
                f"persisted. Set it in your shell environment to keep it: "
                f"export {ENV_TOKEN_VAR}=<your-token>"
            ),
        }
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, token)
        return {"ok": True, "stored_in": "keyring"}
    except Exception as e:
        return {
            "ok": False,
            "error": f"Could not store token in keyring: {e}",
            "next_steps": f"Set {ENV_TOKEN_VAR} in your shell environment instead.",
        }


# ------------------------------------------------------------------ transport

def _gh_available() -> bool:
    return bool(shutil.which("gh"))


def _gh_auth_status() -> dict:
    """{"ok": bool, "user": str|None, "error": str|None} for `gh auth status`."""
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=GH_TIMEOUT,
        )
    except FileNotFoundError:
        return {"ok": False, "user": None, "error": "gh CLI not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "user": None, "error": "gh auth status timed out"}
    except Exception as e:
        return {"ok": False, "user": None, "error": str(e)}
    out = (proc.stdout or "") + (proc.stderr or "")
    user = None
    for line in out.splitlines():
        line = line.strip()
        if "Logged in to" in line and "account" in line:
            # e.g. "✓ Logged in to github.com account octocat (keyring)"
            parts = line.split("account")
            if len(parts) > 1:
                user = parts[1].strip().split()[0].strip("()")
            break
    if proc.returncode == 0:
        return {"ok": True, "user": user, "error": None}
    return {"ok": False, "user": user,
            "error": out.strip().splitlines()[-1] if out.strip() else "gh auth failed"}


def _gh_run_raw(*args):
    """Run `gh <args>`; return (ok, stdout_text, error). Shared by _gh_json/_gh_text."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=GH_TIMEOUT,
        )
    except FileNotFoundError:
        return False, "", "gh CLI not found"
    except subprocess.TimeoutExpired:
        return False, "", "gh command timed out"
    except Exception as e:
        return False, "", f"gh failed to run: {e}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"gh exited with code {proc.returncode}"
        return False, "", err
    return True, (proc.stdout or ""), ""


def _gh_json(*args) -> dict:
    """Run `gh <args>` and parse stdout as JSON. Never raises."""
    ok, out, err = _gh_run_raw(*args)
    if not ok:
        return {"ok": False, "error": err}
    try:
        return {"ok": True, "data": json.loads(out or "null")}
    except ValueError:
        return {"ok": False, "error": "gh returned non-JSON output"}


def _gh_text(*args) -> dict:
    """Run `gh <args>` and return raw stdout text. Never raises."""
    ok, out, err = _gh_run_raw(*args)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True, "data": out.strip()}


def _rest_request(method: str, path: str, token: str, payload=None) -> dict:
    """One GitHub REST call. Returns {"ok", "status"?, "data"?/"error"?}."""
    url = API_BASE + path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "jade-github/0.1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REST_TIMEOUT) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = e.read().decode("utf-8", "replace")
            parsed = json.loads(body) if body else {}
            detail = parsed.get("message", "") if isinstance(parsed, dict) else ""
        except Exception:
            pass
        msg = detail or getattr(e, "reason", "") or "HTTP error"
        return {"ok": False, "status": e.code,
                "error": f"GitHub API returned {e.code}: {msg}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"Network error reaching api.github.com: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"Request failed: {e}"}
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return {"ok": False, "status": status, "error": "GitHub returned non-JSON response"}
    return {"ok": True, "status": status, "data": parsed}


def _select_backend():
    """("gh", None) | ("rest", token) | (None, None). Never raises."""
    try:
        if _gh_available():
            st = _gh_auth_status()
            if st["ok"]:
                return "gh", None
    except Exception:
        pass
    try:
        token, _src = _get_token()
    except Exception:
        token = None
    if token:
        return "rest", token
    return None, None


def _not_connected() -> dict:
    return {"ok": False, "error": "GitHub is not connected.",
            "next_steps": NOT_CONNECTED_STEPS}


def _check_repo(repo: str):
    """Validate "owner/name"; returns error dict or None."""
    if not isinstance(repo, str) or "/" not in repo.strip():
        return {"ok": False, "error": f"Invalid repo {repo!r}: expected 'owner/name'.",
                "next_steps": "Pass the repo as owner/name, e.g. octocat/hello-world."}
    owner, _, name = repo.strip().partition("/")
    if not owner or not name or "/" in name:
        return {"ok": False, "error": f"Invalid repo {repo!r}: expected 'owner/name'.",
                "next_steps": "Pass the repo as owner/name, e.g. octocat/hello-world."}
    return None


# ------------------------------------------------------------------ connection

def is_connected() -> dict:
    """{"connected": bool, "via": "gh"|"token"|None, "detail": str}."""
    try:
        if _gh_available():
            st = _gh_auth_status()
            if st["ok"]:
                who = f" (user: {st['user']})" if st.get("user") else ""
                return {"connected": True, "via": "gh",
                        "detail": f"Authenticated via gh CLI{who}."}
    except Exception:
        pass
    try:
        token, src = _get_token()
    except Exception:
        token, src = None, None
    if token:
        where = "OS keyring" if src == "keyring" else f"{ENV_TOKEN_VAR} env var"
        return {"connected": True, "via": "token",
                "detail": f"Personal access token present ({where}; not re-validated)."}
    return {"connected": False, "via": None,
            "detail": "No authenticated gh CLI and no token stored. " + NOT_CONNECTED_STEPS}


def github_connect(token=None) -> dict:
    """Validate and store a GitHub personal access token.

    If ``token`` is not given and the ``gh`` CLI is already authenticated,
    reports that (nothing is stored). Otherwise prompts with ``getpass``
    (never echoes), validates the token with ``GET /user``, and stores it in
    the keyring on success. Bad credentials produce an honest error and the
    token is not stored.
    """
    if token is None and _gh_available():
        try:
            st = _gh_auth_status()
        except Exception:
            st = {"ok": False}
        if st.get("ok"):
            who = f" as {st['user']}" if st.get("user") else ""
            return {"ok": True, "via": "gh",
                    "detail": f"Already authenticated via gh CLI{who}; no token stored."}
    if token is None:
        try:
            token = getpass.getpass("GitHub personal access token (paste, input hidden): ")
        except (EOFError, KeyboardInterrupt):
            return {"ok": False, "error": "cancelled by user"}
    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "No token entered.",
                "next_steps": "Create a token at https://github.com/settings/tokens and try again."}

    res = _rest_request("GET", "/user", token)
    if not res["ok"]:
        if res.get("status") == 401:
            return {"ok": False,
                    "error": "Token rejected by GitHub (401 Unauthorized). Check the token and try again.",
                    "next_steps": "Create a fresh token at https://github.com/settings/tokens."}
        return {"ok": False,
                "error": f"Could not validate token: {res['error']}",
                "next_steps": "Check your network connection and try again."}
    user = (res.get("data") or {}).get("login", "unknown")
    stored = _store_token(token)
    if not stored["ok"]:
        return stored
    out = {"ok": True, "via": "token",
           "detail": f"Token validated (GitHub user: {user}).",
           "stored_in": stored.get("stored_in", "keyring")}
    if stored.get("next_steps"):
        out["next_steps"] = stored["next_steps"]
    return out


# ------------------------------------------------------------------ read ops

def _map_repo_gh(r: dict) -> dict:
    return {"name": r.get("name"), "full_name": r.get("nameWithOwner"),
            "private": bool(r.get("isPrivate")), "updated_at": r.get("updatedAt"),
            "url": r.get("url")}


def _map_repo_rest(r: dict) -> dict:
    return {"name": r.get("name"), "full_name": r.get("full_name"),
            "private": bool(r.get("private")), "updated_at": r.get("updated_at"),
            "url": r.get("html_url")}


def list_repos(limit: int = 30, affiliation: str = "owner") -> dict:
    """List repos for the authenticated user. affiliation applies to REST only."""
    backend, token = _select_backend()
    if backend is None:
        return _not_connected()
    if backend == "gh":
        res = _gh_json("repo", "list", "--limit", str(limit),
                       "--json", "name,nameWithOwner,isPrivate,updatedAt,url")
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        return {"ok": True, "data": [_map_repo_gh(r) for r in (res["data"] or [])]}
    qs = urllib.parse.urlencode(
        {"per_page": limit, "sort": "updated", "affiliation": affiliation})
    res = _rest_request("GET", f"/user/repos?{qs}", token)
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    return {"ok": True, "data": [_map_repo_rest(r) for r in (res["data"] or [])]}


def _map_issue(item: dict) -> dict:
    user = item.get("user") or item.get("author") or {}
    labels = item.get("labels") or []
    return {"number": item.get("number"), "title": item.get("title"),
            "state": (item.get("state") or "").lower(), "user": user.get("login"),
            "url": item.get("html_url") or item.get("url"),
            "labels": [l.get("name") for l in labels if isinstance(l, dict)]}


def list_issues(repo: str, state: str = "open", limit: int = 20) -> dict:
    """List issues for owner/name (pull requests are filtered out)."""
    bad = _check_repo(repo)
    if bad:
        return bad
    backend, token = _select_backend()
    if backend is None:
        return _not_connected()
    if backend == "gh":
        res = _gh_json("issue", "list", "--repo", repo, "--state", state,
                       "--limit", str(limit),
                       "--json", "number,title,state,author,url,labels")
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        return {"ok": True, "data": [_map_issue(i) for i in (res["data"] or [])]}
    qs = urllib.parse.urlencode({"state": state, "per_page": limit})
    res = _rest_request("GET", f"/repos/{repo}/issues?{qs}", token)
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    items = [i for i in (res["data"] or []) if "pull_request" not in i]
    return {"ok": True, "data": [_map_issue(i) for i in items]}


def _map_pr(item: dict) -> dict:
    user = item.get("user") or item.get("author") or {}
    return {"number": item.get("number"), "title": item.get("title"),
            "state": (item.get("state") or "").lower(), "user": user.get("login"),
            "url": item.get("html_url") or item.get("url"),
            "draft": bool(item.get("draft", item.get("isDraft")))}


def list_prs(repo: str, state: str = "open", limit: int = 20) -> dict:
    """List pull requests for owner/name."""
    bad = _check_repo(repo)
    if bad:
        return bad
    backend, token = _select_backend()
    if backend is None:
        return _not_connected()
    if backend == "gh":
        res = _gh_json("pr", "list", "--repo", repo, "--state", state,
                       "--limit", str(limit),
                       "--json", "number,title,state,author,url,isDraft")
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        return {"ok": True, "data": [_map_pr(p) for p in (res["data"] or [])]}
    qs = urllib.parse.urlencode({"state": state, "per_page": limit})
    res = _rest_request("GET", f"/repos/{repo}/pulls?{qs}", token)
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    return {"ok": True, "data": [_map_pr(p) for p in (res["data"] or [])]}


def _summarize_checks(checks: list) -> str:
    total = len(checks)
    if not total:
        return "no checks reported"
    done = [c for c in checks if (c.get("status") or "").lower() in ("completed", "success", "done")]
    ok = [c for c in checks if (c.get("conclusion") or "").lower() in ("success", "neutral", "skipped")]
    pending = total - len(done)
    parts = [f"{len(ok)}/{total} successful"]
    if pending:
        parts.append(f"{pending} pending")
    failed = [c.get("name") for c in checks
              if (c.get("conclusion") or "").lower() in ("failure", "timed_out", "cancelled")]
    if failed:
        parts.append(f"failed: {', '.join(failed[:5])}")
    return "; ".join(parts)


def get_pr(repo: str, number) -> dict:
    """PR detail: number, title, state, mergeable, checks summary."""
    bad = _check_repo(repo)
    if bad:
        return bad
    backend, token = _select_backend()
    if backend is None:
        return _not_connected()
    if backend == "gh":
        res = _gh_json("pr", "view", str(number), "--repo", repo,
                       "--json",
                       "number,title,state,author,url,isDraft,mergeable,mergeStateStatus,statusCheckRollup")
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        pr = res["data"] or {}
        checks = [{"name": c.get("name"), "status": c.get("status"),
                   "conclusion": c.get("conclusion")}
                  for c in (pr.get("statusCheckRollup") or [])]
        data = _map_pr(pr)
        data.update({"mergeable": pr.get("mergeable"),
                     "merge_state": pr.get("mergeStateStatus"),
                     "checks": checks, "checks_summary": _summarize_checks(checks)})
        return {"ok": True, "data": data}
    res = _rest_request("GET", f"/repos/{repo}/pulls/{number}", token)
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    pr = res["data"] or {}
    sha = ((pr.get("head") or {}).get("sha")) or ""
    checks = []
    if sha:
        cr = _rest_request(
            "GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100", token)
        if cr["ok"]:
            checks = [{"name": c.get("name"), "status": c.get("status"),
                       "conclusion": c.get("conclusion")}
                      for c in ((cr["data"] or {}).get("check_runs") or [])]
    data = _map_pr(pr)
    data.update({"mergeable": pr.get("mergeable"),
                 "merge_state": pr.get("mergeable_state"),
                 "checks": checks, "checks_summary": _summarize_checks(checks)})
    return {"ok": True, "data": data}


def actions_status(repo: str, limit: int = 10) -> dict:
    """Recent workflow runs: name, status, conclusion, branch, url."""
    bad = _check_repo(repo)
    if bad:
        return bad
    backend, token = _select_backend()
    if backend is None:
        return _not_connected()
    if backend == "gh":
        res = _gh_json("run", "list", "--repo", repo, "--limit", str(limit),
                       "--json", "name,status,conclusion,headBranch,url")
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        runs = [{"name": r.get("name"), "status": r.get("status"),
                 "conclusion": r.get("conclusion"), "branch": r.get("headBranch"),
                 "url": r.get("url")} for r in (res["data"] or [])]
        return {"ok": True, "data": runs}
    qs = urllib.parse.urlencode({"per_page": limit})
    res = _rest_request("GET", f"/repos/{repo}/actions/runs?{qs}", token)
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    runs = [{"name": r.get("name"), "status": r.get("status"),
             "conclusion": r.get("conclusion"), "branch": r.get("head_branch"),
             "url": r.get("html_url")}
            for r in ((res["data"] or {}).get("workflow_runs") or [])]
    return {"ok": True, "data": runs}


def code_search(query: str, limit: int = 10) -> dict:
    """Code search → [{repo, path, url}]. REST code search needs a token."""
    if not (query or "").strip():
        return {"ok": False, "error": "Empty search query.",
                "next_steps": "Usage: /github search <query>"}
    backend, token = _select_backend()
    if backend is None:
        return _not_connected()
    if backend == "gh":
        res = _gh_json("search", "code", query.strip(), "--limit", str(limit),
                       "--json", "repository,path,url")
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        hits = [{"repo": (h.get("repository") or {}).get("nameWithOwner"),
                 "path": h.get("path"), "url": h.get("url")}
                for h in (res["data"] or [])]
        return {"ok": True, "data": hits}
    if not token:
        return {"ok": False,
                "error": "REST code search requires an authenticated token.",
                "next_steps": NOT_CONNECTED_STEPS}
    qs = urllib.parse.urlencode({"q": query.strip(), "per_page": limit})
    res = _rest_request("GET", f"/search/code?{qs}", token)
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    hits = [{"repo": (h.get("repository") or {}).get("full_name"),
             "path": h.get("path"), "url": h.get("html_url")}
            for h in ((res["data"] or {}).get("items") or [])]
    return {"ok": True, "data": hits}


# ------------------------------------------------------------------ write ops

def create_issue(repo: str, title: str, body: str = "", *, _confirm: bool = True) -> dict:
    """Create an issue. WRITE — gated by confirm() unless auto-approved."""
    bad = _check_repo(repo)
    if bad:
        return bad
    if not (title or "").strip():
        return {"ok": False, "error": "Issue title cannot be empty."}
    if _confirm and not confirm(f"Create issue '{title.strip()}' in {repo}?", default=False):
        return {"ok": False, "error": "cancelled by user"}
    backend, token = _select_backend()
    if backend is None:
        return _not_connected()
    title, body = title.strip(), body or ""
    if backend == "gh":
        res = _gh_text("issue", "create", "--repo", repo,
                       "--title", title, "--body", body)
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        url = res["data"] or ""
        return {"ok": True, "data": {"number": None, "title": title,
                                     "repo": repo, "url": url, "html_url": url}}
    res = _rest_request("POST", f"/repos/{repo}/issues", token,
                        {"title": title, "body": body})
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    issue = res["data"] or {}
    return {"ok": True, "data": {"number": issue.get("number"), "title": issue.get("title"),
                                 "repo": repo, "url": issue.get("html_url"),
                                 "html_url": issue.get("html_url")}}


def comment_issue(repo: str, number, body: str, *, _confirm: bool = True) -> dict:
    """Comment on an issue/PR. WRITE — gated by confirm() unless auto-approved."""
    bad = _check_repo(repo)
    if bad:
        return bad
    if not (body or "").strip():
        return {"ok": False, "error": "Comment body cannot be empty."}
    if _confirm and not confirm(f"Post comment on #{number} in {repo}?", default=False):
        return {"ok": False, "error": "cancelled by user"}
    backend, token = _select_backend()
    if backend is None:
        return _not_connected()
    body = body.strip()
    if backend == "gh":
        res = _gh_text("issue", "comment", str(number), "--repo", repo, "--body", body)
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        return {"ok": True, "data": {"repo": repo, "number": number, "commented": True}}
    res = _rest_request("POST", f"/repos/{repo}/issues/{number}/comments",
                        token, {"body": body})
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    c = res["data"] or {}
    return {"ok": True, "data": {"repo": repo, "number": number,
                                 "url": c.get("html_url"), "commented": True}}


# ------------------------------------------------------------------ CLI dispatcher

def _print_github_help() -> None:
    table = Table(title="GitHub connector", show_header=True, header_style="bold cyan")
    table.add_column("Command", style="white")
    table.add_column("What it does", style="dim")
    for cmd, desc in [
        ("/github connect", "Link GitHub (gh CLI auth or personal access token)"),
        ("/github repos", "List your repositories"),
        ("/github issues <owner/repo> [--state closed]", "List issues"),
        ("/github prs <owner/repo>", "List pull requests"),
        ("/github pr <owner/repo> <n>", "Show PR detail incl. checks"),
        ("/github issue create <owner/repo>", "Create an issue (asks first)"),
        ("/github comment <owner/repo> <n>", "Comment on an issue/PR (asks first)"),
        ("/github actions <owner/repo>", "Recent workflow runs"),
        ("/github search <query>", "Search code across GitHub"),
    ]:
        table.add_row(cmd, desc)
    console.print(table)


def _print_not_connected() -> None:
    console.print(Panel(
        "[bold]GitHub is not connected.[/bold]\n\n"
        "Run [bold cyan]/github connect[/bold cyan] to link GitHub — either "
        "authenticate the `gh` CLI (`gh auth login`) or store a personal access "
        "token (created at https://github.com/settings/tokens).",
        title="🔗 GitHub", border_style="yellow", expand=False,
    ))


def _cmd_connect(rest: list) -> dict:
    token = rest[0] if rest else None
    res = github_connect(token)
    if res["ok"]:
        console.print(Panel(
            f"[green]✔ {res['detail']}[/green]"
            + (f"\n[dim]Stored in: {res.get('stored_in')}[/dim]" if res.get("stored_in") else ""),
            title="🔗 GitHub connected", border_style="green", expand=False,
        ))
        if res.get("next_steps"):
            console.print(f"[yellow]{res['next_steps']}[/yellow]")
    else:
        console.print(f"[red]✘ {res['error']}[/red]")
        if res.get("next_steps"):
            console.print(f"[dim]{res['next_steps']}[/dim]")
    return res


def _table(title: str, columns: list, rows: list) -> None:
    table = Table(title=title, show_header=True, header_style="bold cyan")
    for col in columns:
        table.add_column(col, style="white", overflow="fold")
    for row in rows:
        table.add_row(*[str(c) if c is not None else "—" for c in row])
    console.print(table)


def _require_connected() -> bool:
    st = is_connected()
    if not st["connected"]:
        _print_not_connected()
        return False
    return True


def _cmd_repos() -> dict:
    if not _require_connected():
        return _not_connected()
    res = list_repos()
    if not res["ok"]:
        console.print(f"[red]✘ {res['error']}[/red]")
        return res
    repos = res["data"]
    if not repos:
        console.print("[dim]No repositories found.[/dim]")
        return res
    _table("Repositories",
           ["Repository", "Private", "Updated", "URL"],
           [(r["full_name"], "yes" if r["private"] else "no",
             (r["updated_at"] or "")[:10], r["url"]) for r in repos])
    return res


def _cmd_issues(rest: list) -> dict:
    if len(rest) < 1:
        console.print("[red]Usage: /github issues <owner/repo> [--state closed][/red]")
        return {"ok": False, "error": "missing repo argument"}
    repo = rest[0]
    state = "open"
    if "--state" in rest:
        i = rest.index("--state")
        if i + 1 < len(rest):
            state = rest[i + 1]
    if not _require_connected():
        return _not_connected()
    res = list_issues(repo, state=state)
    if not res["ok"]:
        console.print(f"[red]✘ {res['error']}[/red]")
        return res
    issues = res["data"]
    if not issues:
        console.print(f"[dim]No {state} issues in {repo}.[/dim]")
        return res
    _table(f"Issues — {repo} ({state})",
           ["#", "Title", "State", "Author", "Labels"],
           [(i["number"], i["title"], i["state"], i["user"],
             ", ".join(i["labels"])) for i in issues])
    return res


def _cmd_prs(rest: list) -> dict:
    if len(rest) < 1:
        console.print("[red]Usage: /github prs <owner/repo>[/red]")
        return {"ok": False, "error": "missing repo argument"}
    repo = rest[0]
    if not _require_connected():
        return _not_connected()
    res = list_prs(repo)
    if not res["ok"]:
        console.print(f"[red]✘ {res['error']}[/red]")
        return res
    prs = res["data"]
    if not prs:
        console.print(f"[dim]No open pull requests in {repo}.[/dim]")
        return res
    _table(f"Pull requests — {repo}",
           ["#", "Title", "State", "Author", "Draft"],
           [(p["number"], p["title"], p["state"], p["user"],
             "yes" if p["draft"] else "no") for p in prs])
    return res


def _cmd_pr(rest: list) -> dict:
    if len(rest) < 2:
        console.print("[red]Usage: /github pr <owner/repo> <number>[/red]")
        return {"ok": False, "error": "missing repo/number arguments"}
    repo, number = rest[0], rest[1]
    if not _require_connected():
        return _not_connected()
    res = get_pr(repo, number)
    if not res["ok"]:
        console.print(f"[red]✘ {res['error']}[/red]")
        return res
    pr = res["data"]
    console.print(Panel(
        f"[bold]{pr['title']}[/bold]\n"
        f"State: {pr['state']}   Author: {pr['user']}   "
        f"Draft: {'yes' if pr['draft'] else 'no'}\n"
        f"Mergeable: {pr['mergeable']} ({pr['merge_state']})\n"
        f"Checks: {pr['checks_summary']}\n"
        f"[dim]{pr['url']}[/dim]",
        title=f"PR #{pr['number']} — {repo}", border_style="cyan", expand=False,
    ))
    if pr["checks"]:
        _table("Checks", ["Name", "Status", "Conclusion"],
               [(c["name"], c["status"], c["conclusion"]) for c in pr["checks"]])
    return res


def _cmd_issue_create(rest: list) -> dict:
    if len(rest) < 1:
        console.print("[red]Usage: /github issue create <owner/repo>[/red]")
        return {"ok": False, "error": "missing repo argument"}
    repo = rest[0]
    if not _require_connected():
        return _not_connected()
    title = Prompt.ask("Issue title").strip()
    if not title:
        console.print("[red]Cancelled — empty title.[/red]")
        return {"ok": False, "error": "cancelled by user"}
    body = Prompt.ask("Body (optional)", default="").strip()
    res = create_issue(repo, title, body)
    if res["ok"]:
        console.print(f"[green]✔ Issue created: {res['data']['url']}[/green]")
    else:
        console.print(f"[red]✘ {res['error']}[/red]")
    return res


def _cmd_comment(rest: list) -> dict:
    if len(rest) < 2:
        console.print("[red]Usage: /github comment <owner/repo> <number>[/red]")
        return {"ok": False, "error": "missing repo/number arguments"}
    repo, number = rest[0], rest[1]
    if not _require_connected():
        return _not_connected()
    body = Prompt.ask("Comment").strip()
    if not body:
        console.print("[red]Cancelled — empty comment.[/red]")
        return {"ok": False, "error": "cancelled by user"}
    res = comment_issue(repo, number, body)
    if res["ok"]:
        console.print("[green]✔ Comment posted.[/green]")
    else:
        console.print(f"[red]✘ {res['error']}[/red]")
    return res


def _cmd_actions(rest: list) -> dict:
    if len(rest) < 1:
        console.print("[red]Usage: /github actions <owner/repo>[/red]")
        return {"ok": False, "error": "missing repo argument"}
    repo = rest[0]
    if not _require_connected():
        return _not_connected()
    res = actions_status(repo)
    if not res["ok"]:
        console.print(f"[red]✘ {res['error']}[/red]")
        return res
    runs = res["data"]
    if not runs:
        console.print(f"[dim]No workflow runs in {repo}.[/dim]")
        return res
    _table(f"Actions — {repo}",
           ["Workflow", "Status", "Conclusion", "Branch"],
           [(r["name"], r["status"], r["conclusion"], r["branch"]) for r in runs])
    return res


def _cmd_search(rest: list) -> dict:
    if not rest:
        console.print("[red]Usage: /github search <query>[/red]")
        return {"ok": False, "error": "missing query"}
    if not _require_connected():
        return _not_connected()
    query = " ".join(rest)
    res = code_search(query)
    if not res["ok"]:
        console.print(f"[red]✘ {res['error']}[/red]")
        return res
    hits = res["data"]
    if not hits:
        console.print(f"[dim]No code matches for {query!r}.[/dim]")
        return res
    _table(f"Code search — {query}", ["Repository", "Path"],
           [(h["repo"], h["path"]) for h in hits])
    return res


def handle_github_command(args) -> dict:
    """Subcommand dispatcher for `/github ...` (wired by the parent into the CLI).

    ``args`` is the token list after `/github`, e.g. ``["issues", "o/r"]``.
    Prints rich output and returns the underlying result dict.
    """
    tokens = list(args or [])
    if not tokens or tokens[0] in ("-h", "--help", "help"):
        _print_github_help()
        return {"ok": True, "data": None}
    sub, rest = tokens[0], tokens[1:]
    if sub == "connect":
        return _cmd_connect(rest)
    if sub == "repos":
        return _cmd_repos()
    if sub == "issues":
        return _cmd_issues(rest)
    if sub == "prs":
        return _cmd_prs(rest)
    if sub == "pr":
        return _cmd_pr(rest)
    if sub == "issue" and rest[:1] == ["create"]:
        return _cmd_issue_create(rest[1:])
    if sub == "comment":
        return _cmd_comment(rest)
    if sub == "actions":
        return _cmd_actions(rest)
    if sub == "search":
        return _cmd_search(rest)
    console.print(f"[red]Unknown /github subcommand: {sub}[/red]")
    _print_github_help()
    return {"ok": False, "error": f"unknown subcommand: {sub}"}


# ------------------------------------------------------------------ legacy API
# Kept so existing callers (core/agent.py, core/repair.py) keep working
# without modification. Delegates to the connector above; stdlib-only now.
# Note: write methods route through the confirm-gated functions above
# (auto-approve aware). repair.py already asks its own confirmation first.

class GitHubTool:
    """Legacy class-based wrapper around the GitHub connector."""

    def __init__(self):
        self.base_url = API_BASE

    def _token(self):
        token, _src = _get_token()
        return token

    def call(self, method, endpoint, data=None):
        token = self._token()
        if not token:
            return "Error: GitHub not connected. Run /github connect."
        res = _rest_request(method, "/" + endpoint.lstrip("/"), token, payload=data)
        if not res["ok"]:
            return f"GitHub API Error: {res['error']}"
        return res["data"]

    def get_repo_info(self, repo_full_name):
        return self.call("GET", f"repos/{repo_full_name}")

    def create_issue(self, repo_full_name, title, body, _confirm=True):
        # _confirm=False is for callers that already obtained the user's
        # consent (e.g. core/repair.py's crash-report prompt).
        res = create_issue(repo_full_name, title, body or "", _confirm=_confirm)
        if not res["ok"]:
            return f"Error: {res['error']}"
        return res["data"]

    def list_pull_requests(self, repo_full_name):
        return self.call("GET", f"repos/{repo_full_name}/pulls")

    def create_pr(self, repo_full_name, title, body, head, base="main"):
        if not confirm(f"Open pull request '{title}' in {repo_full_name}?", default=False):
            return "Error: cancelled by user."
        return self.call("POST", f"repos/{repo_full_name}/pulls",
                         {"title": title, "body": body or "", "head": head, "base": base})


github_tool = GitHubTool()
