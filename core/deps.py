import sys
import os
import subprocess
import importlib.util
import shutil
import time
import tempfile
import tarfile
from typing import Dict, List, Tuple, Optional
from pathlib import Path
from rich.console import Console
from rich.table import Table
from core.config import CONFIG_DIR

console = Console()

# NOTE: key = pip package name, value = import name to test
PYTHON_DEPS: Dict[str, str] = {
    "typer": "typer",
    "rich": "rich",
    "requests": "requests",
    "watchdog": "watchdog",
    "sounddevice": "sounddevice",
    "scipy": "scipy",
    "numpy": "numpy",
    "faiss-cpu": "faiss",
    "SpeechRecognition": "speech_recognition",
    "prompt_toolkit": "prompt_toolkit",
    "dropbox": "dropbox",
    "paramiko": "paramiko",
    "psutil": "psutil",
    "ddgs": "ddgs",
    "keyring": "keyring",
    "litellm": "litellm",
}

# System deps mapping (platform tool -> package name hint)
SYSTEM_PACKAGES = {
    "portaudio": {
        "mac": {"pkg": "portaudio", "check_paths": ["/usr/local/include/portaudio.h", "/opt/homebrew/include/portaudio.h"]},
        "debian": {"pkg": "portaudio19-dev"},
        "fedora": {"pkg": "portaudio-devel"},
    }
}

PIP_INSTALL_RETRIES = 3
PIP_RETRY_DELAY = 3  # seconds

# Cache directory for downloaded artifacts and backups
def get_cache_dir() -> Path:
    # Prefer ~/.jade/cache, fallback to repo-local .jade_cache
    home_cache = CONFIG_DIR / "cache"
    repo_cache = Path(os.path.dirname(os.path.abspath(__file__))) / ".." / ".jade_cache"
    try:
        home_cache.mkdir(parents=True, exist_ok=True)
        return home_cache
    except Exception:
        repo_cache = repo_cache.resolve()
        repo_cache.mkdir(parents=True, exist_ok=True)
        return repo_cache

CACHE_DIR = get_cache_dir()


def _which_pip_executable() -> List[str]:
    """
    Returns the recommended pip invocation for the current interpreter.
    """
    return [sys.executable, "-m", "pip"]


def _pip_install(packages: List[str], extra_args: Optional[List[str]] = None) -> Tuple[bool, str]:
    """
    Install pip packages with retries. Returns (ok, stdout+stderr).
    """
    extra_args = extra_args or []
    cmd_base = _which_pip_executable()
    for attempt in range(1, PIP_INSTALL_RETRIES + 1):
        cmd = cmd_base + ["install"] + extra_args + packages
        try:
            console.print(f"[dim]Running: {' '.join(cmd)} (attempt {attempt})[/dim]")
            completed = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True, completed.stdout + completed.stderr
        except subprocess.CalledProcessError as e:
            console.print(f"[yellow]pip install attempt {attempt} failed: {e}[/yellow]")
            if attempt < PIP_INSTALL_RETRIES:
                time.sleep(PIP_RETRY_DELAY * attempt)
            else:
                return False, (e.stdout or "") + (e.stderr or "") + f"\nFinal return code: {e.returncode}"
        except Exception as e:
            return False, str(e)
    return False, "Unknown pip install error"


def dry_run_download_packages(packages: List[str], include_deps: bool = True, timeout: int = 120) -> Dict:
    """
    Perform a dry-run that downloads package artifacts (wheels/sdists) to a temp dir using `pip download`.
    """
    report = {"requested": packages, "files": [], "error": None}
    tmp = Path(tempfile.mkdtemp(prefix="jade_pip_download_"))
    try:
        cmd = _which_pip_executable() + ["download", "--dest", str(tmp)]
        if not include_deps:
            cmd.append("--no-deps")
        cmd += packages
        console.print(f"[dim]Simulating pip resolution via: {' '.join(cmd)}[/dim]")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stdout = proc.stdout + proc.stderr
        # collect downloaded files
        files = [str(p.name) for p in tmp.iterdir() if p.is_file()]
        report["files"] = files
        # try to infer versions from filenames (best-effort)
        inferred = {}
        for name in files:
            parts = name.split("-")
            if len(parts) >= 2:
                pkg = parts[0]
                ver = parts[1]
                inferred.setdefault(pkg, []).append(ver)
        report["inferred_versions"] = inferred
        report["stdout"] = stdout
        # move artifacts into cache for offline install
        if files:
            saved = save_cache_artifacts(list(tmp.glob("*")))
            report["cached"] = saved
    except subprocess.TimeoutExpired:
        report["error"] = f"pip download timed out after {timeout}s"
    except Exception as e:
        report["error"] = str(e)
    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass
    return report


def save_cache_artifacts(paths: List[Path]) -> List[str]:
    """
    Move artifact files into the jade cache directory to be reused later.
    """
    saved = []
    cache_dir = Path(CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for p in paths:
        try:
            dest = cache_dir / p.name
            if dest.exists():
                saved.append(str(dest))
                continue
            shutil.move(str(p), str(dest))
            saved.append(str(dest))
        except Exception as e:
            console.print(f"[yellow]Warning: failed to cache artifact {p}: {e}[/yellow]")
    return saved


def check_python_deps() -> Tuple[bool, List[str]]:
    """
    Check and report missing Python deps. Returns (ok, missing_list).
    """
    missing = []
    for pip_name, import_name in PYTHON_DEPS.items():
        try:
            if importlib.util.find_spec(import_name) is None:
                missing.append(pip_name)
        except Exception:
            missing.append(pip_name)

    if missing:
        console.print(f"[yellow]Missing Python dependencies detected: {', '.join(missing)}[/yellow]")
    else:
        console.print("[green]All Python dependencies appear to be installed.[/green]")
    return (len(missing) == 0), missing


def install_python_deps(packages: List[str], editable_project_path: Optional[str] = None, dry_run: bool = False) -> Dict:
    """
    Install provided pip packages.
    """
    result = {"requested": packages, "installed": [], "failed": {}, "editable_reinstall": None, "dry_run": dry_run}
    if not packages:
        return result

    if dry_run:
        console.print("[cyan]Dry-run mode: resolving & downloading packages, not installing.[/cyan]")
        res = dry_run_download_packages(packages, include_deps=True)
        result["dry_run_report"] = res
        return result

    ok, out = _pip_install(packages)
    if ok:
        result["installed"].extend(packages)
    else:
        # attempt per-package install to give better diagnostics
        for pkg in packages:
            ok_pkg, out_pkg = _pip_install([pkg])
            if ok_pkg:
                result["installed"].append(pkg)
            else:
                result["failed"][pkg] = out_pkg

    if editable_project_path:
        # try reinstalling project in editable mode
        try:
            cmd = _which_pip_executable() + ["install", "-e", editable_project_path]
            console.print(f"[dim]Reinstalling project editable: {' '.join(cmd)}[/dim]")
            completed = subprocess.run(cmd, capture_output=True, text=True, check=True)
            result["editable_reinstall"] = {"ok": True, "output": completed.stdout + completed.stderr}
        except Exception as e:
            result["editable_reinstall"] = {"ok": False, "error": str(e)}
    return result


def _detect_package_manager() -> Optional[str]:
    if shutil.which("brew"):
        return "brew"
    if shutil.which("apt"):
        return "apt"
    if shutil.which("dnf"):
        return "dnf"
    if shutil.which("yum"):
        return "yum"
    return None


def check_system_deps(prompt_for_install: bool = False) -> Tuple[bool, Dict]:
    """
    Check system-level dependencies. Returns (ok, details).
    """
    details = {}
    ok = True
    pm = _detect_package_manager()
    details["package_manager"] = pm

    for key, val in SYSTEM_PACKAGES.items():
        found = False
        pkgname = None
        
        if sys.platform == "darwin":
            pkgname = val["mac"]["pkg"]
            for p in val["mac"].get("check_paths", []):
                if os.path.exists(p):
                    found = True
                    break
        elif pm == "apt":
            pkgname = val["debian"]["pkg"]
            # Check if package is installed via dpkg
            cp = subprocess.run(["dpkg", "-s", pkgname], capture_output=True, text=True)
            if cp.returncode == 0:
                found = True
        elif pm in ("dnf", "yum"):
            pkgname = val["fedora"]["pkg"]
            # Check if package is installed via rpm
            cp = subprocess.run(["rpm", "-q", pkgname], capture_output=True, text=True)
            if cp.returncode == 0:
                found = True

        details[key] = {"found": found, "package": pkgname}
        if not found:
            ok = False
            console.print(f"[yellow]System dependency missing: {key} (suggested package: {pkgname})[/yellow]")
            if prompt_for_install and pkgname:
                if pm == "brew" and console.input(f"Install {pkgname} via Homebrew? (y/n): ").lower() == "y":
                    console.print(f"[blue]Installing {pkgname} with brew...[/blue]")
                    subprocess.run(["brew", "install", pkgname])
                elif pm in ("apt", "dnf", "yum") and console.input(f"Install {pkgname} via {pm}? (y/n): ").lower() == "y":
                    try:
                        if pm == "apt":
                            subprocess.run(["sudo", "apt", "update"])
                            subprocess.run(["sudo", "apt", "install", "-y", pkgname])
                        elif pm == "dnf":
                            subprocess.run(["sudo", "dnf", "install", "-y", pkgname])
                        elif pm == "yum":
                            subprocess.run(["sudo", "yum", "install", "-y", pkgname])
                    except Exception as e:
                        console.print(f"[red]Failed to install {pkgname}: {e}[/red]")

    return ok, details


def create_repo_backup(base_dir: Optional[str] = None) -> Optional[str]:
    """
    Create a timestamped tar.gz backup of the repository.
    """
    try:
        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = Path(base_dir)
        cache = Path(CACHE_DIR)
        cache.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%S")
        tar_path = cache / f"repo_backup_{ts}.tar.gz"
        console.print(f"[dim]Creating code backup {tar_path}...[/dim]")
        with tarfile.open(tar_path, "w:gz") as tar:
            for p in base.rglob("*"):
                try:
                    if str(CACHE_DIR) in str(p):
                        continue
                    tar.add(str(p), arcname=os.path.relpath(str(p), str(base)))
                except Exception:
                    continue
        return str(tar_path)
    except Exception as e:
        console.print(f"[yellow]Warning: failed to create repo backup: {e}[/yellow]")
        return None


def rollback_from_cache(backup_tar: str, base_dir: Optional[str] = None) -> bool:
    """
    Restore a repository snapshot from backup tar.gz.
    """
    try:
        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = Path(base_dir)
        tmpdir = Path(tempfile.mkdtemp(prefix="jade_restore_"))
        console.print(f"[dim]Extracting backup {backup_tar} to temporary directory {tmpdir}[/dim]")
        with tarfile.open(backup_tar, "r:gz") as tar:
            tar.extractall(path=str(tmpdir))
        for item in tmpdir.iterdir():
            dest = base / item.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            shutil.move(str(item), str(dest))
        console.print("[green]Repository successfully restored from cache backup.[/green]")
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass
        return True
    except Exception as e:
        console.print(f"[red]Failed to restore from cache backup: {e}[/red]")
        return False


def ensure_all(prompt_for_system_install: bool = False, dry_run: bool = False, force: bool = False) -> bool:
    """
    Run all dependency checks and attempt to self-heal.
    Uses a cache to avoid slow checks on every startup.
    """
    cache_file = str(CONFIG_DIR / ".deps_checked")
    
    # Skip check if cached and not forced (cache valid for 24h)
    if not force and not dry_run and os.path.exists(cache_file):
        mtime = os.path.getmtime(cache_file)
        if (time.time() - mtime) < 86400: # 24 hours
            return True

    console.print("[bold cyan]Running dependency checks...[/bold cyan]")
    p_ok, missing = check_python_deps()
    s_ok, s_details = check_system_deps(prompt_for_system_install)

    editable_path = None
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.exists(os.path.join(base_dir, "pyproject.toml")) or os.path.exists(os.path.join(base_dir, "setup.py")):
            editable_path = base_dir
    except Exception:
        editable_path = None

    overall_ok = True
    if not p_ok:
        console.print("[bold blue]Attempting to install or simulate missing Python packages...[/bold blue]")
        install_res = install_python_deps(missing, editable_project_path=(editable_path if not dry_run else None), dry_run=dry_run)
        if install_res.get("failed"):
            console.print(f"[red]Some packages failed to install: {list(install_res['failed'].keys())}[/red]")
            overall_ok = False
        else:
            if dry_run:
                console.print("[cyan]Dry-run completed. Artifacts downloaded and cached; no changes applied.[/cyan]")
            else:
                console.print("[green]Python packages installed successfully.[/green]")
    if not s_ok:
        console.print("[yellow]One or more system dependencies may be missing. See details above.[/yellow]")
        overall_ok = False

    # Update cache if all ok
    if overall_ok and not dry_run:
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass

    return overall_ok


def self_heal_environment(auto_confirm: bool = False, reinstall_project: bool = True, dry_run: bool = False) -> Dict:
    report = {"timestamp": time.time(), "python_ok": False, "system_ok": False, "actions": [], "errors": [], "dry_run": dry_run}
    try:
        p_ok, missing = check_python_deps()
        report["python_missing"] = missing
        if not p_ok:
            report["actions"].append(f"Installing or resolving missing Python packages: {missing}")
            proceed = auto_confirm or (console.input("Install/resolve missing Python packages now? (y/n): ").lower() == "y")
            if proceed:
                editable_path = None
                try:
                    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    if os.path.exists(os.path.join(base_dir, "pyproject.toml")) or os.path.exists(os.path.join(base_dir, "setup.py")):
                        editable_path = base_dir
                except Exception:
                    editable_path = None
                install_res = install_python_deps(missing, editable_project_path=(editable_path if reinstall_project and not dry_run else None), dry_run=dry_run)
                report["install_result"] = install_res
                report["python_ok"] = (len(install_res.get("failed", {})) == 0)
            else:
                report["python_ok"] = False
        else:
            report["python_ok"] = True

        s_ok, s_details = check_system_deps(prompt_for_install=auto_confirm)
        report["system_ok"] = s_ok
        report["system_details"] = s_details

    except Exception as e:
        report["errors"].append(str(e))
        report["python_ok"] = False
        report["system_ok"] = False

    if report["python_ok"] and report["system_ok"]:
        console.print("[green]Self-heal succeeded: environment looks healthy.[/green]")
    else:
        console.print("[yellow]Self-heal completed with warnings/errors. Inspect report for details.[/yellow]")

    return report


if __name__ == "__main__":
    r = self_heal_environment(auto_confirm=False, dry_run=False)
    table = Table(title="Self-heal report")
    table.add_column("Key")
    table.add_column("Value")
    for k, v in r.items():
        table.add_row(str(k), str(v)[:200])
    console.print(table)
