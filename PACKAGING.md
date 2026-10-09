# Packaging & Release Checklist

How JADE ships: one-line bash installer, Homebrew formula, pip (PyPI), pipx,
and install docs. This file is the runbook — follow it on every release.

## Version bump

Single source of truth lives in two files (kept in sync by `tests/test_packaging.py`):

1. `pyproject.toml` → `version = "X.Y.Z"`
2. `setup.py` → `version="X.Y.Z"`
3. `README.md` title line → `# JADE: The Ultimate Proactive AI Assistant (vX.Y.Z)`

## Tag the release

```bash
git tag vX.Y.Z
git push origin vX.Y.Z          # creates the tarball Homebrew points at
```

GitHub auto-generates:
`https://github.com/nave433-blip/jarvis-dev/archive/refs/tags/vX.Y.Z.tar.gz`

## Publish to PyPI (`pip install jarvis-dev`)

The name `jarvis-dev` is currently **available** on PyPI (checked 2026-09-25).

```bash
# one-time: create a PyPI account, then a scoped API token at
# https://pypi.org/manage/account/token/  (scope: project jarvis-dev)
python -m pip install build twine

# from the repo root, on the release tag:
python -m build            # creates dist/jarvis-dev-X.Y.Z.tar.gz + .whl
twine check dist/*         # README must render clean — do not skip
twine upload dist/*        # paste the API token when asked

# verify (fresh venv):
python -m venv /tmp/piptest && /tmp/piptest/bin/pip install jarvis-dev
/tmp/piptest/bin/jade --help
```

Notes:
- `requires-python = ">=3.12"` — pip will refuse older Pythons with a clear error.
- The `jade` console script (`cli:app`) is registered in both
  `pyproject.toml` `[project.scripts]` and `setup.py` `entry_points`.
- Never commit `dist/` or `*.egg-info` churn from a release build.

## Homebrew formula (`jade.rb`)

On every release, update the formula **before** announcing:

1. `url` → the new tag tarball URL.
2. `sha256` → `curl -fsSL <tarball-url> | sha256sum` (must match the download
   exactly — `brew install` refuses on mismatch).
3. Commit the formula change.

Install today (no tap yet — direct formula URL):

```bash
brew install --formula https://raw.githubusercontent.com/nave433-blip/jarvis-dev/main/jarvis.rb
```

**Future: proper tap.** Create `nave433-blip/homebrew-tap` with the formula at
`Formula/jade.rb`, then users get the short form:

```bash
brew tap nave433-blip/tap
brew install jade
```

Until the tap exists, do not document the short form anywhere.

## Bash one-liner

Release:

```bash
curl -fsSL https://raw.githubusercontent.com/nave433-blip/jarvis-dev/main/install.sh | bash
```

Testing an unmerged branch (e.g. `audit/fix`) — the script installs `$JADE_REF`
when set, so pin both:

```bash
curl -fsSL https://raw.githubusercontent.com/nave433-blip/jarvis-dev/audit/fix/install.sh | JARVIS_REF=audit/fix bash
```

Useful knobs (documented in `install.sh` header):
`JADE_REF`, `JADE_DIR`, `JADE_SKIP_SYSTEM_DEPS=1`, `JADE_SKIP_GLOBAL_LINK=1`.

## pipx

```bash
pipx install git+https://github.com/nave433-blip/jarvis-dev.git
# after the PyPI release:
pipx install jarvis-dev
```

## AUR (Arch Linux)

`skeleton only, UNTESTED` — see `packaging/aur/PKGBUILD`. A trusted Arch user
still needs to build, test, and submit it. Do not claim Arch support until then.
