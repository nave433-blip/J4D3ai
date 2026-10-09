#!/bin/bash
#
# JADE: one-line installer for macOS & Linux
#
#   curl -fsSL https://raw.githubusercontent.com/nave433-blip/jarvis-dev/main/install.sh | bash
#
# What it does:
#   1. Installs system audio/Python deps via your package manager (sudo)
#   2. Fetches the sources into ~/.jade-app (clone, or refresh on re-run)
#   3. Creates an isolated Python 3.12+ venv and installs JADE into it
#   4. Links the `jade` command into /usr/local/bin
#   5. Smoke-tests the install
#
# Env knobs:
#   JADE_REF=ref            git branch/tag to install (default: repo default branch)
#   JADE_DIR=path           install location (default: ~/.jade-app)
#   JADE_SKIP_SYSTEM_DEPS=1 skip the sudo system-package step (containers/CI)
#   JADE_SKIP_GLOBAL_LINK=1 skip linking /usr/local/bin/jade
# (The old JARVIS_REF/JARVIS_DIR/JARVIS_SKIP_* names still work as deprecated aliases.)
#
# Re-running is safe: sources are refreshed (git pull / re-copy) and the
# install is redone in place.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[0;33m'; NC='\033[0m'
info() { echo -e "${BLUE}$*${NC}"; }
ok()   { echo -e "${GREEN}$*${NC}"; }
warn() { echo -e "${YELLOW}$*${NC}"; }
die()  { echo -e "${RED}❌ $*${NC}" >&2; exit 1; }

JADE_REF="${JADE_REF:-${CORTANA_REF:-${JARVIS_REF:-}}}"
# Default install dir is ~/.jade-app; an existing pre-rename ~/.cortana-app
# or ~/.jarvis-app checkout is reused in place so re-running the installer
# never orphans it.
JADE_DIR="${JADE_DIR:-${JARVIS_DIR:-}}"
if [[ -z "$JADE_DIR" ]]; then
    if [[ -d "$HOME/.jade-app" ]]; then
        JADE_DIR="$HOME/.jade-app"
    elif [[ -d "$HOME/.cortana-app" ]]; then
        JADE_DIR="$HOME/.cortana-app"
    elif [[ -d "$HOME/.jarvis-app" ]]; then
        JADE_DIR="$HOME/.jarvis-app"
    else
        JADE_DIR="$HOME/.jade-app"
    fi
fi
JARVIS_DIR="$JADE_DIR"  # internal alias: rest of script uses JARVIS_DIR
JARVIS_REF="$JADE_REF"    # internal alias
SKIP_DEPS="${JADE_SKIP_SYSTEM_DEPS:-${JARVIS_SKIP_SYSTEM_DEPS:-0}}"
SKIP_LINK="${JADE_SKIP_GLOBAL_LINK:-${JARVIS_SKIP_GLOBAL_LINK:-0}}"
OS_TYPE="$(uname -s | tr '[:upper:]' '[:lower:]')"
REPO_URL="https://github.com/nave433-blip/jarvis-dev.git"

info "🚀 Installing JADE on ${OS_TYPE} → ${JARVIS_DIR}"

# --- helpers -----------------------------------------------------------------
# Run "$@" with sudo when not root; fail clearly when sudo is unavailable.
need_sudo() {
    if [[ "$(id -u)" == "0" ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        die "Need root for system packages but 'sudo' is unavailable. Re-run as root or set JADE_SKIP_SYSTEM_DEPS=1."
    fi
}

# --- 1. system dependencies ----------------------------------------------------
if [[ "$SKIP_DEPS" == "1" ]]; then
    warn "⏭️  Skipping system dependencies (JADE_SKIP_SYSTEM_DEPS=1)."
elif [[ "$OS_TYPE" == "linux" ]]; then
    info "🔍 Installing Linux system dependencies..."
    if command -v apt-get >/dev/null 2>&1; then
        need_sudo apt-get update -y
        need_sudo apt-get install -y python3-venv python3-pip portaudio19-dev libffi-dev libssl-dev
    elif command -v dnf >/dev/null 2>&1; then
        need_sudo dnf install -y python3-devel portaudio-devel libffi-devel openssl-devel
    elif command -v pacman >/dev/null 2>&1; then
        need_sudo pacman -S --noconfirm --needed python python-pip portaudio
    elif command -v zypper >/dev/null 2>&1; then
        need_sudo zypper install -y python3 python3-pip portaudio-devel libffi-devel libopenssl-devel
    else
        warn "⚠️  No supported package manager (apt/dnf/pacman/zypper). Install Python 3.12+, pip and portaudio manually."
    fi
elif [[ "$OS_TYPE" == "darwin" ]]; then
    if ! command -v brew >/dev/null 2>&1; then
        warn "⚠️  Homebrew not found — some voice/audio features may need manual setup ('brew install portaudio')."
    elif ! brew list portaudio >/dev/null 2>&1; then
        info "📦 Installing portaudio via Homebrew..."
        brew install portaudio
    fi
fi

# --- 2. fetch sources (idempotent) ----------------------------------------------
command -v git >/dev/null 2>&1 || die "git is required but not found. Install git and re-run."

mkdir -p "$JARVIS_DIR"

# Dependencies (scipy, faiss, torch-adjacent wheels) need real disk room —
# fail early with a clear message instead of dying mid-pip.
MIN_KB=1572864  # 1.5 GiB
HAVE_KB="$(df -k "$JARVIS_DIR" 2>/dev/null | awk 'NR==2 {print $4}')"
if [[ -n "$HAVE_KB" ]] && [[ "$HAVE_KB" -lt "$MIN_KB" ]]; then
    die "Only $(( HAVE_KB / 1024 )) MiB free where ${JARVIS_DIR} lives — JADE needs ~1.5 GiB. Free space or set JADE_DIR elsewhere."
fi
if [[ "$(pwd -P)" == "$(cd "$JARVIS_DIR" && pwd -P)" ]]; then
    info "📂 Already inside ${JARVIS_DIR} — refreshing in place."
    if [[ -d "$JARVIS_DIR/.git" ]]; then
        git -C "$JARVIS_DIR" pull --ff-only >/dev/null 2>&1 \
            || warn "⚠️  git pull failed (offline or diverged?) — installing from existing files."
    fi
elif [[ -d .git ]]; then
    info "📂 Copying sources into ${JARVIS_DIR}..."
    # Tracked files only: keeps re-runs clean (no venvs, caches, or pycache copied).
    git archive HEAD | tar -x -C "$JARVIS_DIR"
elif [[ -d "$JARVIS_DIR/.git" ]]; then
    info "📂 Refreshing existing checkout in ${JARVIS_DIR}..."
    git -C "$JARVIS_DIR" pull --ff-only >/dev/null 2>&1 \
        || warn "⚠️  git pull failed (offline or diverged?) — installing from existing files."
elif [[ -n "$(ls -A "$JARVIS_DIR" 2>/dev/null)" ]]; then
    die "$JARVIS_DIR exists but is not a JADE checkout. Move it aside and re-run."
else
    info "📂 Cloning JADE${JARVIS_REF:+ (ref: $JARVIS_REF)}..."
    if [[ -n "$JARVIS_REF" ]]; then
        git clone --depth 1 -b "$JARVIS_REF" "$REPO_URL" "$JARVIS_DIR"
    else
        git clone --depth 1 "$REPO_URL" "$JARVIS_DIR"
    fi
fi

cd "$JARVIS_DIR"

# --- 3. python ------------------------------------------------------------------
PYTHON_BIN="$(command -v python3.12 || command -v python3 || true)"
[[ -n "$PYTHON_BIN" ]] || die "No python3 found. Install Python 3.12+ and re-run."
PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
    || die "JADE requires Python 3.12+ (found ${PY_VER} at ${PYTHON_BIN})."

# --- 4. venv + install ------------------------------------------------------------
info "🐍 Setting up isolated virtual environment..."
"$PYTHON_BIN" -m venv venv 2>/dev/null \
    || die "Could not create a virtualenv with ${PYTHON_BIN}. On Debian/Ubuntu: sudo apt install python3-venv — then re-run."
# shellcheck disable=SC1091
source "venv/bin/activate"

info "📦 Installing JADE and dependencies (a few minutes on first run)..."
pip install --quiet --upgrade pip
pip install --quiet -e .

# --- 5. global launcher -------------------------------------------------------------
if [[ "$SKIP_LINK" == "1" ]]; then
    warn "⏭️  Skipping global link (JADE_SKIP_GLOBAL_LINK=1). Run: ${JARVIS_DIR}/venv/bin/jade"
else
    info "🔗 Installing the 'jade' command..."
    BIN_PATH="/usr/local/bin/jade"
    WRAPPER_TMP="$(mktemp)"
    cat > "$WRAPPER_TMP" <<EOF
#!/bin/bash
# Generated by JADE install.sh — do not edit.
export JADE_ROOT="${JARVIS_DIR}"
source "\$JADE_ROOT/venv/bin/activate"
exec python "\$JADE_ROOT/cli.py" "\$@"
EOF
    chmod +x "$WRAPPER_TMP"
    if [[ -w /usr/local/bin ]]; then
        mv "$WRAPPER_TMP" "$BIN_PATH"
    else
        warn "⚠️  /usr/local/bin is not writable — installing the launcher with sudo..."
        need_sudo mv "$WRAPPER_TMP" "$BIN_PATH"
    fi
    ok "✅ 'jade' command installed at ${BIN_PATH}"
fi

# --- 6. smoke test ------------------------------------------------------------------
info "🧪 Verifying installation..."
if venv/bin/python cli.py --help >/dev/null 2>&1; then
    ok "✅ Smoke test passed."
else
    die "Install finished but 'jade --help' failed. Try: ${JARVIS_DIR}/venv/bin/python ${JARVIS_DIR}/cli.py --help"
fi

ok "✨ JADE installed! Start a new shell and run: jade"
