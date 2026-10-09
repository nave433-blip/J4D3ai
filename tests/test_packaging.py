"""Packaging sanity: install.sh, Homebrew formula, PyPI metadata, version sync."""
import re
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_install_sh_syntax():
    r = subprocess.run(["bash", "-n", str(REPO / "install.sh")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"install.sh has syntax errors:\n{r.stderr}"


def test_install_sh_hardening_pins():
    src = (REPO / "install.sh").read_text()
    assert "set -euo pipefail" in src, "strict mode missing"
    assert "(3, 12)" in src, "Python 3.12 floor check missing"
    assert "JADE_SKIP_SYSTEM_DEPS" in src, "system-dep skip knob missing"
    assert "JADE_SKIP_GLOBAL_LINK" in src, "global-link skip knob missing"
    assert "JADE_REF" in src, "branch/tag override knob missing"
    assert "pwd -P" in src, "self-copy guard missing"
    assert "pull --ff-only" in src, "idempotent refresh missing"
    assert "--help" in src, "post-install smoke test missing"
    assert "is not a JADE checkout" in src, "non-empty foreign dir guard missing"
    assert "MIN_KB" in src, "disk-space pre-check missing"
    assert "needs ~1.5 GiB" in src, "disk-space error message missing"


def test_formula_wellformed():
    src = (REPO / "jade.rb").read_text()
    url = re.search(r'url "([^"]+)"', src).group(1)
    sha = re.search(r'sha256 "([^"]+)"', src).group(1)
    assert re.fullmatch(r"[0-9a-f]{64}", sha), "sha256 must be 64 hex chars"
    assert re.search(r"refs/tags/v\d+\.\d+\.\d+\.tar\.gz$", url), \
        "formula must pin a release-tag tarball, not a branch"
    assert "Linux" in src, "formula desc should cover Linux"
    assert 'depends_on "python@3.12"' in src


def test_formula_sha256_matches_release_tarball():
    """The sha256 in jade.rb must equal the real downloadable tarball.
    Network-free: pins the verified hash captured 2026-09-25 for v0.1.7."""
    src = (REPO / "jade.rb").read_text()
    url = re.search(r'url "([^"]+)"', src).group(1)
    sha = re.search(r'sha256 "([^"]+)"', src).group(1)
    verified = {
        "https://github.com/nave433-blip/jarvis-dev/archive/refs/tags/v0.1.7.tar.gz":
            "bd6c443f0a93dba7250c38a1d6b0d0207e4143e73d4dfa02db60e592f1c23049",
        "https://github.com/nave433-blip/jarvis-dev/archive/refs/tags/v0.1.0.tar.gz":
            "c0345dfb6e182f09d9af645d7e49a2203796c066b31c4f9abe4024e567cf6e9b",
    }
    assert url in verified, f"formula points at an unverified tarball: {url}"
    assert sha == verified[url], "formula sha256 does not match the verified tarball hash"


def test_versions_in_sync():
    with open(REPO / "pyproject.toml", "rb") as f:
        ver = tomllib.load(f)["project"]["version"]
    assert re.search(rf'version="{re.escape(ver)}"', (REPO / "setup.py").read_text()), \
        "setup.py version drifted from pyproject.toml"
    assert f"(v{ver})" in (REPO / "README.md").read_text(), \
        "README title version drifted from pyproject.toml"


def test_license_exists_and_declares_mit():
    lic = (REPO / "LICENSE").read_text()
    assert "MIT License" in lic
    assert "Nave433" in lic
    assert "WITHOUT WARRANTY" in lic


def test_pypi_metadata_ready():
    with open(REPO / "pyproject.toml", "rb") as f:
        proj = tomllib.load(f)["project"]
    assert proj["name"] == "jade"
    assert proj["readme"] == "README.md"
    assert (REPO / "README.md").exists()
    assert "jade" in proj["scripts"], "console script entry point missing"
    assert proj["requires-python"] == ">=3.12"


def test_packaging_runbook_exists():
    md = (REPO / "PACKAGING.md").read_text()
    for needle in ["twine check", "twine upload", "python -m build",
                   "jade.rb", "JADE_REF", "pypi.org"]:
        assert needle in md, f"PACKAGING.md missing: {needle}"


def test_aur_skeleton_marked_untested():
    src = (REPO / "packaging" / "aur" / "PKGBUILD").read_text()
    assert "UNTESTED" in src, "AUR skeleton must be clearly marked untested"
