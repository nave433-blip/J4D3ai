import requests
import os
import sys
import subprocess
import time
import shutil
from datetime import datetime, timezone
from typing import List, Optional
from rich.console import Console

console = Console()

GITHUB_REPO = "nave433-blip/jarvis-dev"
# Upgraded version for the logic integration
CURRENT_VERSION = "0.3.0"


def _get_latest_release_from_github() -> Optional[str]:
    """Query GitHub Releases for latest tag/version. Fallbacks to raw pyproject if needed."""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        r = requests.get(url, timeout=6)
        if r.status_code == 200:
            data = r.json()
            tag = data.get("tag_name") or data.get("name")
            if tag:
                return str(tag).strip()
    except Exception:
        pass
    # fallback to pyproject on main branch
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/pyproject.toml"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            for line in r.text.splitlines():
                if "version" in line and "=" in line:
                    latest = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return latest
    except Exception:
        pass
    return None


def check_for_updates() -> Optional[str]:
    """Return latest version string if newer than CURRENT_VERSION, else None."""
    latest = _get_latest_release_from_github()
    if latest:
        try:
            from packaging.version import parse as vparse
            if vparse(latest) > vparse(CURRENT_VERSION):
                return latest
        except Exception:
            if latest != CURRENT_VERSION:
                return latest
    return None


def _git(cmd: List[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + cmd, cwd=cwd, capture_output=True, text=True)


def apply_update(run_tests: bool = True, target_branch: Optional[str] = None) -> bool:
    """
    Safer update procedure:
    - Ensure git present and working tree state handled (stash if needed)
    - Detect current branch if target_branch is None
    - Create a backup tag referencing current HEAD
    - Fetch and merge (fast-forward) from origin/target_branch
    - Reinstall dependencies (pip -e .)
    - Optionally run tests (pytest) and rollback to backup tag on failure
    """
    console.print("[bold blue]🚀 Starting update / self-repair sequence...[/bold blue]")
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Ensure git exists
    if not shutil.which("git"):
        console.print("[red]git is not available on PATH. Cannot auto-update.[/red]")
        return False

    # Detect branch
    if target_branch is None:
        cp_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=base_dir)
        target_branch = cp_branch.stdout.strip() or "main"
        console.print(f"[dim]Detected active branch: {target_branch}[/dim]")

    # Ensure a clean working tree or stash optionally
    cp_status = _git(["status", "--porcelain"], cwd=base_dir)
    if cp_status.stdout.strip():
        console.print("[yellow]Uncommitted changes detected. Automatically stashing local changes...[/yellow]")
        _git(["stash", "--include-untracked"], cwd=base_dir)
        stashed = True
    else:
        stashed = False

    # create a backup tag
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_tag = f"pre-update-{timestamp}"
    try:
        _git(["tag", backup_tag], cwd=base_dir)
        console.print(f"[dim]Created backup git tag: {backup_tag}[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: could not create backup tag: {e}[/yellow]")

    # fetch and pull changes from origin
    try:
        console.print("[yellow]Fetching latest changes from origin...[/yellow]")
        _git(["fetch", "origin"], cwd=base_dir)
        # Try to fast-forward or merge origin/target_branch
        r = _git(["rev-parse", f"origin/{target_branch}"], cwd=base_dir)
        if r.returncode != 0:
            console.print("[red]Failed to find origin branch. Aborting update.[/red]")
            return False
        # Attempt to reset to origin/branch (safe, because we stashed)
        _git(["reset", "--hard", f"origin/{target_branch}"], cwd=base_dir)
        console.print(f"[green]Code updated to origin/{target_branch}[/green]")
    except Exception as e:
        console.print(f"[red]Update failed during git fetch/reset: {e}[/red]")
        # Try rollback to tag
        _git(["reset", "--hard", backup_tag], cwd=base_dir)
        return False

    # Reinstall dependencies
    try:
        console.print("[yellow]Refreshing Python dependencies (editable install)...[/yellow]")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-e", base_dir])
    except Exception as e:
        console.print(f"[red]Dependency install failed: {e}[/red]")
        console.print("[bold yellow]Attempting rollback to backup tag...[/bold yellow]")
        _git(["reset", "--hard", backup_tag], cwd=base_dir)
        return False

    # Optional tests
    if run_tests and shutil.which("pytest"):
        console.print("[yellow]Running test suite (pytest)...[/yellow]")
        try:
            # run pytest in base_dir, disabling xonsh plugin which causes permission issues
            completed = subprocess.run(["pytest", "-q", "-p", "no:xonsh"], cwd=base_dir)
            if completed.returncode != 0:
                console.print("[red]Tests failed after update. Rolling back to previous version...[/red]")
                _git(["reset", "--hard", backup_tag], cwd=base_dir)
                # try to apply stashed changes back if we stashed
                if stashed:
                    _git(["stash", "pop"], cwd=base_dir)
                return False
            console.print("[green]Tests passed.[/green]")
        except Exception as e:
            console.print(f"[red]Error running tests: {e}[/red]")
            _git(["reset", "--hard", backup_tag], cwd=base_dir)
            return False

    console.print("[bold green]✅ Update successful. Please restart JADE to apply changes.[/bold green]")

    # Optionally pop stash if we stashed earlier
    if stashed:
        try:
            _git(["stash", "pop"], cwd=base_dir)
        except Exception:
            # If stash pop fails, warn but continue
            console.print("[yellow]Warning: failed to reapply stashed changes automatically.[/yellow]")

    return True


def auto_update_check():
    """
    Silently check for updates and prompt only when useful.
    """
    latest = check_for_updates()
    if latest:
        console.print(f"[bold green]A new version of JADE is available: {latest} (Current: {CURRENT_VERSION})[/bold green]")
        if console.input("Would you like to upgrade now? (y/n): ").lower() == "y":
            ok = apply_update()
            if ok:
                console.print("[bold green]Upgrade complete! Please restart JADE.[/bold green]")
                sys.exit(0)
            else:
                console.print("[red]Upgrade failed. See messages above.[/red]")


def manual_upgrade():
    """
    Manual trigger for the upgrade flow -- keeps the user in control.
    """
    latest = check_for_updates()
    if latest:
        console.print(f"[cyan]Latest available: {latest} (current: {CURRENT_VERSION})[/cyan]")
    else:
        console.print("[green]No remote release detected or already up to date.[/green]")
        if console.input("Force reinstall/update to origin/main anyway? (y/n): ").lower() != "y":
            return
    apply_update()
