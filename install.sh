#!/usr/bin/env sh
# Splitch Universal Installer & Updater (macOS, Linux, WSL)
#
# Install or update in one command:
#   curl -fsSL https://raw.githubusercontent.com/RaveAgainstTheMachine/splitch/main/install.sh | sh
#
# Local checkout install / update:
#   ./install.sh --local
#
# Headless GPU/compute worker install (skips Node.js & dashboard build):
#   curl -fsSL https://raw.githubusercontent.com/RaveAgainstTheMachine/splitch/main/install.sh | SPLITCH_AGENT_ONLY=1 sh

set -eu

SPLITCH_REPO="${SPLITCH_REPO:-https://github.com/RaveAgainstTheMachine/splitch.git}"
SPLITCH_BRANCH="${SPLITCH_BRANCH:-main}"
SPLITCH_DATA_ROOT="${SPLITCH_DATA_ROOT:-$HOME/.local/share/splitch}"
SPLITCH_HOME="${SPLITCH_HOME:-$SPLITCH_DATA_ROOT/app}"
SPLITCH_BIN_DIR="${SPLITCH_BIN_DIR:-$HOME/.local/bin}"
SPLITCH_PYTHON="${SPLITCH_PYTHON:-3.12}"
SPLITCH_UV_VERSION="${SPLITCH_UV_VERSION:-0.13.5}"
SPLITCH_NODE_VERSION="${SPLITCH_NODE_VERSION:-22.14.0}"
SPLITCH_ENGINE="${SPLITCH_ENGINE:-auto}"
SPLITCH_ENGINE_BACKEND="${SPLITCH_ENGINE_BACKEND:-auto}"
SPLITCH_WITH_MEDIA="${SPLITCH_WITH_MEDIA:-0}"
SPLITCH_AGENT_ONLY="${SPLITCH_AGENT_ONLY:-0}"
SPLITCH_NO_UI_BUILD="${SPLITCH_NO_UI_BUILD:-0}"
SPLITCH_LOCAL="${SPLITCH_LOCAL:-0}"
SPLITCH_JOIN_URL="${SPLITCH_JOIN_URL:-}"
SPLITCH_JOIN_TOKEN="${SPLITCH_JOIN_TOKEN:-}"
SPLITCH_NODE_ID_OPT="${SPLITCH_NODE_ID_OPT:-}"
SPLITCH_NODE_NAME_OPT="${SPLITCH_NODE_NAME_OPT:-}"
SPLITCH_NODE_ROLE_OPT="${SPLITCH_NODE_ROLE_OPT:-}"
SPLITCH_YIELD_GAMING="${SPLITCH_YIELD_GAMING:-0}"
SPLITCH_YIELD_GPUS="${SPLITCH_YIELD_GPUS:-}"
SPLITCH_ENABLE_TOOLS_OPT="${SPLITCH_ENABLE_TOOLS_OPT:-0}"
SPLITCH_AGENT_PORT_OPT="${SPLITCH_AGENT_PORT_OPT:-8090}"
SPLITCH_ENGINE_PORT_OPT="${SPLITCH_ENGINE_PORT_OPT:-8081}"
SPLITCH_INSTALL_SERVICE="${SPLITCH_INSTALL_SERVICE:-0}"
SPLITCH_NO_DESKTOP="${SPLITCH_NO_DESKTOP:-0}"
SPLITCH_UNINSTALL=0
SPLITCH_PURGE=0

usage() {
  cat <<'EOF'
Splitch Installer & Updater

Usage:
  ./install.sh [options]

Options:
  --local                  Install/update using the current working directory checkout
  --agent-only             Install node agent only (skip Node.js and dashboard UI build)
  --no-ui-build            Skip building the dashboard bundle
  --engine <target>        Inference engine to install/update: auto | llama.cpp | ollama | none (default: auto)
  --backend <accel>        Engine accelerator: auto | cuda | rocm | vulkan | metal | cpu (default: auto)
  --with-media             Also install/update managed stable-diffusion.cpp (`sd-server`) for image/video gen
  --dir <path>             Target installation directory (default: ~/.local/share/splitch/app)
  --bin-dir <path>         Directory for global `splitch` & `llama-server` shims (default: ~/.local/bin)
  --repo <url>             Git repository URL to clone/update from
  --branch <name>          Git branch or ref to track (default: main)
  --python <version>       Python version managed via uv (default: 3.12)
  --uninstall              Remove Splitch CLI shims, managed app & managed engine
  --purge                  With --uninstall, also remove ~/.local/share/splitch data & models
  -h, --help               Show this help text

Environment overrides:
  SPLITCH_REPO, SPLITCH_BRANCH, SPLITCH_HOME, SPLITCH_DATA_ROOT, SPLITCH_BIN_DIR,
  SPLITCH_PYTHON, SPLITCH_ENGINE, SPLITCH_ENGINE_BACKEND, SPLITCH_AGENT_ONLY=1, SPLITCH_NO_UI_BUILD=1
EOF
}

IS_TTY=0
if [ -t 1 ] && [ "${TERM:-}" != "dumb" ]; then
  IS_TTY=1
fi
USE_COLOR=0
if [ "$IS_TTY" = "1" ] && [ -z "${NO_COLOR:-}" ]; then
  USE_COLOR=1
fi

TMP_TAR=""
SPIN_LOG=""
osc_progress() {
  if [ "$IS_TTY" = "1" ]; then
    printf '\033]9;4;%s;%s\033\\' "$1" "$2"
  fi
}
cleanup_installer() {
  osc_progress 0 0
  [ -n "$TMP_TAR" ] && rm -f "$TMP_TAR" 2>/dev/null || true
  [ -n "$SPIN_LOG" ] && rm -f "$SPIN_LOG" 2>/dev/null || true
}
trap cleanup_installer EXIT INT TERM

if [ "$USE_COLOR" = "1" ]; then
  C_ACCENT='\033[1;35m'
  C_OK='\033[1;32m'
  C_WARN='\033[1;33m'
  C_ERR='\033[1;31m'
  C_DIM='\033[90m'
  C_RST='\033[0m'
else
  C_ACCENT=''; C_OK=''; C_WARN=''; C_ERR=''; C_DIM=''; C_RST=''
fi

log() { printf "${C_ACCENT}[splitch]${C_RST} %s\n" "$*"; }
ok()  { printf "  ${C_OK}✓${C_RST} %s\n" "$*"; }
warn(){ printf "  ${C_WARN}!${C_RST} %s\n" "$*" >&2; }
die() { osc_progress 2 100; printf "${C_ERR}[splitch] Error:${C_RST} %s\n" "$*" >&2; exit 1; }

step_bar() {
  _cur="$1"
  _tot="$2"
  _msg="$3"
  _pct=$(( (_cur * 100) / _tot ))
  _w=20
  _fill=$(( (_cur * _w) / _tot ))
  _empty=$(( _w - _fill ))
  _fbar=""
  _ebar=""
  _i=0
  while [ "$_i" -lt "$_fill" ]; do _fbar="${_fbar}━"; _i=$((_i + 1)); done
  _i=0
  while [ "$_i" -lt "$_empty" ]; do _ebar="${_ebar}─"; _i=$((_i + 1)); done
  osc_progress 1 "$_pct"
  printf '\n'"${C_ACCENT}[%s/%s]${C_RST} ${C_ACCENT}%s${C_RST}${C_DIM}%s${C_RST} %3d%%  %s\n" \
    "$_cur" "$_tot" "$_fbar" "$_ebar" "$_pct" "$_msg"
}

run_spin() {
  _label="$1"
  shift
  if [ "$IS_TTY" != "1" ]; then
    "$@"
    return $?
  fi
  SPIN_LOG="$(mktemp -t splitch-step.XXXXXX)"
  _start_ts="$(date +%s)"
  "$@" >"$SPIN_LOG" 2>&1 &
  _pid=$!
  _idx=0
  while kill -0 "$_pid" 2>/dev/null; do
    case $((_idx % 10)) in
      0) _ch='⠋' ;; 1) _ch='⠙' ;; 2) _ch='⠹' ;; 3) _ch='⠸' ;; 4) _ch='⠼' ;;
      5) _ch='⠴' ;; 6) _ch='⠦' ;; 7) _ch='⠧' ;; 8) _ch='⠇' ;; *) _ch='⠏' ;;
    esac
    _el=$(( $(date +%s) - _start_ts ))
    printf "\r\033[2K  ${C_ACCENT}%s${C_RST} %s ${C_DIM}%ss${C_RST}" "$_ch" "$_label" "$_el"
    _idx=$((_idx + 1))
    sleep 0.1 2>/dev/null || sleep 1
  done
  if wait "$_pid"; then
    _el=$(( $(date +%s) - _start_ts ))
    printf "\r\033[2K  ${C_OK}✓${C_RST} %s ${C_DIM}(%ss)${C_RST}\n" "$_label" "$_el"
    rm -f "$SPIN_LOG"
    SPIN_LOG=""
    return 0
  else
    _rc=$?
    printf "\r\033[2K  ${C_ERR}✗${C_RST} %s\n" "$_label" >&2
    cat "$SPIN_LOG" >&2
    rm -f "$SPIN_LOG"
    SPIN_LOG=""
    return "$_rc"
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --local) SPLITCH_LOCAL=1; shift ;;
    --agent-only) SPLITCH_AGENT_ONLY=1; shift ;;
    --no-ui-build) SPLITCH_NO_UI_BUILD=1; shift ;;
    --engine) [ "$#" -ge 2 ] || die "--engine requires a target (auto|llama.cpp|ollama|none)"; SPLITCH_ENGINE="$2"; shift 2 ;;
    --backend) [ "$#" -ge 2 ] || die "--backend requires a value (auto|cuda|rocm|vulkan|metal|cpu)"; SPLITCH_ENGINE_BACKEND="$2"; shift 2 ;;
    --with-media) SPLITCH_WITH_MEDIA=1; shift ;;
    --dir) [ "$#" -ge 2 ] || die "--dir requires a path"; SPLITCH_HOME="$2"; shift 2 ;;
    --bin-dir) [ "$#" -ge 2 ] || die "--bin-dir requires a path"; SPLITCH_BIN_DIR="$2"; shift 2 ;;
    --repo) [ "$#" -ge 2 ] || die "--repo requires a URL"; SPLITCH_REPO="$2"; shift 2 ;;
    --branch) [ "$#" -ge 2 ] || die "--branch requires a name"; SPLITCH_BRANCH="$2"; shift 2 ;;
    --python) [ "$#" -ge 2 ] || die "--python requires a version"; SPLITCH_PYTHON="$2"; shift 2 ;;
    --join) [ "$#" -ge 2 ] || die "--join requires a router URL"; SPLITCH_JOIN_URL="$2"; shift 2 ;;
    --join-token) [ "$#" -ge 2 ] || die "--join-token requires a token"; SPLITCH_JOIN_TOKEN="$2"; shift 2 ;;
    --node-id) [ "$#" -ge 2 ] || die "--node-id requires a slug"; SPLITCH_NODE_ID_OPT="$2"; shift 2 ;;
    --node-name) [ "$#" -ge 2 ] || die "--node-name requires a name"; SPLITCH_NODE_NAME_OPT="$2"; shift 2 ;;
    --role) [ "$#" -ge 2 ] || die "--role requires gpu|unified|cpu"; SPLITCH_NODE_ROLE_OPT="$2"; shift 2 ;;
    --yield-on-gaming) SPLITCH_YIELD_GAMING=1; shift ;;
    --yield-gpus) [ "$#" -ge 2 ] || die "--yield-gpus requires comma-separated GPU indices"; SPLITCH_YIELD_GPUS="$2"; shift 2 ;;
    --enable-tools) SPLITCH_ENABLE_TOOLS_OPT=1; shift ;;
    --agent-port) [ "$#" -ge 2 ] || die "--agent-port requires a port"; SPLITCH_AGENT_PORT_OPT="$2"; shift 2 ;;
    --engine-port) [ "$#" -ge 2 ] || die "--engine-port requires a port"; SPLITCH_ENGINE_PORT_OPT="$2"; shift 2 ;;
    --install-service) SPLITCH_INSTALL_SERVICE=1; shift ;;
    --no-desktop) SPLITCH_NO_DESKTOP=1; shift ;;
    --uninstall) SPLITCH_UNINSTALL=1; shift ;;
    --purge) SPLITCH_PURGE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
done

# ── Uninstall flow ───────────────────────────────────────────────────────────
if [ "$SPLITCH_UNINSTALL" = "1" ]; then
  log "Uninstalling Splitch..."
  if [ -x "$SPLITCH_BIN_DIR/splitch" ]; then
    "$SPLITCH_BIN_DIR/splitch" stop >/dev/null 2>&1 || true
    "$SPLITCH_BIN_DIR/splitch" desktop uninstall >/dev/null 2>&1 || true
  fi
  if [ -f "$SPLITCH_BIN_DIR/splitch" ]; then
    rm -f "$SPLITCH_BIN_DIR/splitch"
    ok "Removed CLI shim: $SPLITCH_BIN_DIR/splitch"
  fi
  if [ -f "$SPLITCH_DATA_ROOT/engine_meta.json" ] && [ -f "$SPLITCH_BIN_DIR/llama-server" ]; then
    rm -f "$SPLITCH_BIN_DIR/llama-server"
    ok "Removed managed llama-server shim"
  fi
  if [ -f "$SPLITCH_DATA_ROOT/sd_engine_meta.json" ]; then
    rm -f "$SPLITCH_BIN_DIR/sd-server" "$SPLITCH_BIN_DIR/sd-cli" "$SPLITCH_DATA_ROOT/sd_engine_meta.json"
    ok "Removed managed sd-server/sd-cli shims"
  fi
  if [ -f "$SPLITCH_DATA_ROOT/install_meta.json" ]; then
    rm -f "$SPLITCH_DATA_ROOT/install_meta.json"
  fi
  if [ -f "$SPLITCH_DATA_ROOT/engine_meta.json" ]; then
    rm -f "$SPLITCH_DATA_ROOT/engine_meta.json"
  fi
  if [ -d "$SPLITCH_DATA_ROOT/engines" ]; then
    rm -rf "$SPLITCH_DATA_ROOT/engines"
    ok "Removed managed inference engines"
  fi
  if [ -d "$SPLITCH_DATA_ROOT/app" ]; then
    rm -rf "$SPLITCH_DATA_ROOT/app"
    ok "Removed managed app directory: $SPLITCH_DATA_ROOT/app"
  fi
  if [ -d "$SPLITCH_DATA_ROOT/node" ]; then
    rm -rf "$SPLITCH_DATA_ROOT/node"
    ok "Removed managed Node.js runtime"
  fi
  if [ "$SPLITCH_PURGE" = "1" ] && [ -d "$SPLITCH_DATA_ROOT" ]; then
    rm -rf "$SPLITCH_DATA_ROOT"
    ok "Purged all user data in $SPLITCH_DATA_ROOT"
  else
    ok "Preserved user data (chats, skills, models) in $SPLITCH_DATA_ROOT (pass --purge to erase)"
  fi
  exit 0
fi

# ── 1. Platform & Hardware Detection ─────────────────────────────────────────
OS_RAW="$(uname -s)"
ARCH_RAW="$(uname -m)"
case "$OS_RAW" in
  Linux*)  OS="linux" ;;
  Darwin*) OS="darwin" ;;
  *)       die "Unsupported OS: $OS_RAW (use install.ps1 on Windows)" ;;
esac
case "$ARCH_RAW" in
  x86_64|amd64)  ARCH="x64" ;;
  arm64|aarch64) ARCH="arm64" ;;
  *)             die "Unsupported architecture: $ARCH_RAW" ;;
esac

HW_SUMMARY="CPU"
if [ "$OS" = "darwin" ] && [ "$ARCH" = "arm64" ]; then
  HW_SUMMARY="Apple Silicon (Unified Memory)"
elif command -v nvidia-smi >/dev/null 2>&1; then
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1 || true)"
  HW_SUMMARY="NVIDIA CUDA (${GPU_NAME:-GPU})"
elif command -v rocm-smi >/dev/null 2>&1; then
  HW_SUMMARY="AMD ROCm GPU"
fi

TOTAL_STEPS=7
log "Installing / Updating Splitch on ${OS}/${ARCH} — ${HW_SUMMARY}"

verify_sha256() {
  _file="$1"
  _expected="$2"
  _label="${3:-$_file}"
  [ -n "$_expected" ] || die "Missing expected SHA-256 checksum for $_label"
  if command -v sha256sum >/dev/null 2>&1; then
    _actual="$(sha256sum "$_file" | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    _actual="$(shasum -a 256 "$_file" | awk '{print $1}')"
  elif command -v openssl >/dev/null 2>&1; then
    _actual="$(openssl dgst -sha256 "$_file" | awk '{print $NF}')"
  else
    die "No SHA-256 verification tool found (install sha256sum, shasum, or openssl)"
  fi
  if [ "$_actual" != "$_expected" ]; then
    die "SHA-256 verification failed for $_label (expected $_expected, got $_actual)"
  fi
  ok "SHA-256 verified: $_label"
}

pinned_uv_sha256() {
  # Verified upstream SHA-256 digests pinned in-script for uv 0.13.5 (independent of download origin)
  if [ "$SPLITCH_UV_VERSION" = "0.13.5" ]; then
    case "$1" in
      uv-x86_64-unknown-linux-gnu.tar.gz)  echo "f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f" ;;
      uv-aarch64-unknown-linux-gnu.tar.gz) echo "4bb69cf95ed13f23713bb7bc09f241896ea0c75b7c557e615a20604b82f9c403" ;;
      uv-x86_64-apple-darwin.tar.gz)       echo "b0f52b0e3945b4107d89024ea38e625683011fbfc05a0d98f9fa2d6dcee28c66" ;;
      uv-aarch64-apple-darwin.tar.gz)      echo "743f85035a1996ecb350659e01cd9faea9709829be09b1b4b03c73eaf3c9cd73" ;;
      *) echo "" ;;
    esac
  else
    echo ""
  fi
}

pinned_node_sha256() {
  # Verified upstream SHA-256 digests pinned in-script for Node.js v22.14.0
  if [ "$SPLITCH_NODE_VERSION" = "22.14.0" ]; then
    case "$1" in
      node-v22.14.0-linux-x64.tar.xz)   echo "69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec" ;;
      node-v22.14.0-linux-arm64.tar.xz) echo "08bfbf538bad0e8cbb0269f0173cca28d705874a67a22f60b57d99dc99e30050" ;;
      node-v22.14.0-darwin-x64.tar.gz)  echo "6698587713ab565a94a360e091df9f6d91c8fadda6d00f0cf6526e9b40bed250" ;;
      node-v22.14.0-darwin-arm64.tar.gz) echo "e9404633bc02a5162c5c573b1e2490f5fb44648345d64a958b17e325729a5e42" ;;
      *) echo "" ;;
    esac
  else
    echo ""
  fi
}

install_pinned_uv() {
  command -v curl >/dev/null 2>&1 || die "curl is required to install uv"
  case "${OS}/${ARCH}" in
    linux/x64)    _uv_triple="x86_64-unknown-linux-gnu" ;;
    linux/arm64)  _uv_triple="aarch64-unknown-linux-gnu" ;;
    darwin/x64)   _uv_triple="x86_64-apple-darwin" ;;
    darwin/arm64) _uv_triple="aarch64-apple-darwin" ;;
    *)            die "Unsupported platform for uv release archive: ${OS}/${ARCH}" ;;
  esac
  _uv_archive="uv-${_uv_triple}.tar.gz"
  _uv_base="https://github.com/astral-sh/uv/releases/download/${SPLITCH_UV_VERSION}"
  _uv_tmp="$(mktemp -t splitch-uv.XXXXXX)"
  _uv_sha="$(pinned_uv_sha256 "$_uv_archive")"
  if [ -z "$_uv_sha" ]; then
    _uv_sha="$(curl -fsSL "${_uv_base}/${_uv_archive}.sha256" | awk '{print $1}' | head -n 1)"
  fi
  curl -fsSL "${_uv_base}/${_uv_archive}" -o "$_uv_tmp"
  verify_sha256 "$_uv_tmp" "$_uv_sha" "$_uv_archive"
  _uv_dir="$(mktemp -d -t splitch-uv-extract.XXXXXX)"
  tar -xzf "$_uv_tmp" -C "$_uv_dir"
  mkdir -p "$SPLITCH_BIN_DIR"
  cp "$_uv_dir/uv-${_uv_triple}/uv" "$SPLITCH_BIN_DIR/uv"
  chmod +x "$SPLITCH_BIN_DIR/uv"
  if [ -f "$_uv_dir/uv-${_uv_triple}/uvx" ]; then
    cp "$_uv_dir/uv-${_uv_triple}/uvx" "$SPLITCH_BIN_DIR/uvx"
    chmod +x "$SPLITCH_BIN_DIR/uvx"
  fi
  rm -rf "$_uv_tmp" "$_uv_dir"
}

# ── 2. Ensure `uv` (handles isolated Python 3.12+ & all Python deps) ─────────
step_bar 1 "$TOTAL_STEPS" "Checking uv runtime"
export PATH="$SPLITCH_BIN_DIR:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  install_pinned_uv
  export PATH="$SPLITCH_BIN_DIR:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || die "uv installation failed; ensure ~/.local/bin is writable"
ok "uv ready: $(uv --version)"

# ── 3. Acquire or Update Splitch Source ──────────────────────────────────────
step_bar 2 "$TOTAL_STEPS" "Preparing Splitch source"
INSTALL_MODE="managed"
if [ "$SPLITCH_LOCAL" = "1" ]; then
  [ -f "./pyproject.toml" ] || die "--local requires running from a Splitch repository checkout"
  SPLITCH_HOME="$(pwd)"
  INSTALL_MODE="local"
  ok "Using local repository checkout: $SPLITCH_HOME"
else
  mkdir -p "$(dirname "$SPLITCH_HOME")"
  if [ -d "$SPLITCH_HOME/.git" ]; then
    command -v git >/dev/null 2>&1 || die "git is required to update $SPLITCH_HOME"
    CUR_ORIGIN="$(git -C "$SPLITCH_HOME" remote get-url origin 2>/dev/null || echo "")"
    if [ -z "$CUR_ORIGIN" ]; then
      git -C "$SPLITCH_HOME" remote add origin "$SPLITCH_REPO"
    elif [ "$CUR_ORIGIN" = "https://github.com/splitch-ai/splitch.git" ] || [ "$CUR_ORIGIN" = "https://github.com/splitch/splitch.git" ]; then
      git -C "$SPLITCH_HOME" remote set-url origin "$SPLITCH_REPO"
    fi
    OLD_SHA="$(git -C "$SPLITCH_HOME" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    run_spin "Fetching origin/$SPLITCH_BRANCH" git -C "$SPLITCH_HOME" fetch --quiet origin "$SPLITCH_BRANCH"
    run_spin "Pulling origin/$SPLITCH_BRANCH" git -C "$SPLITCH_HOME" pull --ff-only origin "$SPLITCH_BRANCH"
    NEW_SHA="$(git -C "$SPLITCH_HOME" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    if [ "$OLD_SHA" = "$NEW_SHA" ]; then
      ok "Already up to date at $SPLITCH_BRANCH@$NEW_SHA"
    else
      ok "Updated source: $OLD_SHA -> $NEW_SHA"
    fi
  else
    if command -v git >/dev/null 2>&1; then
      run_spin "Cloning Splitch ($SPLITCH_BRANCH)" git clone --depth 1 --branch "$SPLITCH_BRANCH" "$SPLITCH_REPO" "$SPLITCH_HOME"
      ok "Cloned repository into $SPLITCH_HOME"
    else
      die "git is required to clone $SPLITCH_REPO (install git or download a release tarball and run ./install.sh --local)"
    fi
  fi
fi

# ── 4. Sync Isolated Python Environment via uv ───────────────────────────────
step_bar 3 "$TOTAL_STEPS" "Syncing Python environment"
UV_DESKTOP_EXTRA=""
if [ "$SPLITCH_NO_DESKTOP" != "1" ] && { [ "$SPLITCH_AGENT_ONLY" != "1" ] || [ -f "$SPLITCH_DATA_ROOT/desktop.json" ]; }; then
  if [ "$OS" = "darwin" ] || [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${XDG_CURRENT_DESKTOP:-}" ]; then
    UV_DESKTOP_EXTRA="--extra desktop"
  fi
fi
if [ "$INSTALL_MODE" = "local" ]; then
  if [ -d "$SPLITCH_HOME/.venv" ]; then
    # shellcheck disable=SC2086
    run_spin "Syncing Python environment" sh -c 'cd "$1" && uv sync $2' _ "$SPLITCH_HOME" "$UV_DESKTOP_EXTRA"
  else
    # shellcheck disable=SC2086
    run_spin "Syncing Python $SPLITCH_PYTHON environment" sh -c 'cd "$1" && uv sync --python "$2" $3' _ "$SPLITCH_HOME" "$SPLITCH_PYTHON" "$UV_DESKTOP_EXTRA"
  fi
else
  # shellcheck disable=SC2086
  run_spin "Syncing isolated Python $SPLITCH_PYTHON environment" sh -c 'cd "$1" && uv sync --python "$2" --no-dev $3' _ "$SPLITCH_HOME" "$SPLITCH_PYTHON" "$UV_DESKTOP_EXTRA"
fi
ok "Python backend dependencies ready"

# ── 5. Ensure Node.js 20+ & Build Dashboard (Router installs) ────────────────
step_bar 4 "$TOTAL_STEPS" "Dashboard build"
ensure_node() {
  NODE_MAJOR=0
  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    NODE_MAJOR="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
  fi
  if [ "$NODE_MAJOR" -ge 20 ]; then
    ok "Node.js ready: $(node --version)"
    return 0
  fi

  MANAGED_NODE_DIR="$SPLITCH_DATA_ROOT/node"
  if [ -x "$MANAGED_NODE_DIR/bin/node" ]; then
    export PATH="$MANAGED_NODE_DIR/bin:$PATH"
    ok "Using managed Node.js: $(node --version)"
    return 0
  fi

  log "Downloading standalone Node.js v${SPLITCH_NODE_VERSION}..."
  command -v curl >/dev/null 2>&1 || die "curl is required to download Node.js"
  if [ "$OS" = "darwin" ]; then
    NODE_EXT="tar.gz"
    TAR_FLAGS="-xzf"
  else
    NODE_EXT="tar.xz"
    TAR_FLAGS="-xJf"
  fi
  NODE_BASENAME="node-v${SPLITCH_NODE_VERSION}-${OS}-${ARCH}"
  NODE_FILE="${NODE_BASENAME}.${NODE_EXT}"
  NODE_URL="https://nodejs.org/dist/v${SPLITCH_NODE_VERSION}/${NODE_FILE}"
  NODE_EXPECTED_SHA="$(pinned_node_sha256 "$NODE_FILE")"
  if [ -z "$NODE_EXPECTED_SHA" ]; then
    NODE_SHA_URL="https://nodejs.org/dist/v${SPLITCH_NODE_VERSION}/SHASUMS256.txt"
    NODE_EXPECTED_SHA="$(curl -fsSL "$NODE_SHA_URL" | awk -v f="$NODE_FILE" '$2 == f {print $1}' | head -n 1)"
  fi
  TMP_TAR="$(mktemp -t splitch-node.XXXXXX)"
  if [ "$IS_TTY" = "1" ]; then
    curl -fSL --progress-bar "$NODE_URL" -o "$TMP_TAR"
  else
    curl -fsSL "$NODE_URL" -o "$TMP_TAR"
  fi
  verify_sha256 "$TMP_TAR" "$NODE_EXPECTED_SHA" "$NODE_FILE"
  rm -rf "$MANAGED_NODE_DIR"
  mkdir -p "$MANAGED_NODE_DIR"
  run_spin "Extracting Node.js v${SPLITCH_NODE_VERSION}" tar "$TAR_FLAGS" "$TMP_TAR" -C "$MANAGED_NODE_DIR" --strip-components=1
  rm -f "$TMP_TAR"
  TMP_TAR=""
  export PATH="$MANAGED_NODE_DIR/bin:$PATH"
  ok "Installed standalone Node.js $(node --version) into $MANAGED_NODE_DIR"
}

if [ "$SPLITCH_AGENT_ONLY" = "1" ]; then
  ok "Agent-only mode: skipped Node.js & dashboard UI build"
elif [ "$SPLITCH_NO_UI_BUILD" = "1" ]; then
  ok "Skipped dashboard UI build (--no-ui-build)"
elif [ -f "$SPLITCH_HOME/dashboard/package.json" ]; then
  ensure_node
  if [ -f "$SPLITCH_HOME/dashboard/package-lock.json" ]; then
    run_spin "Installing dashboard packages" sh -c 'cd "$1" && npm ci --no-audit --no-fund' _ "$SPLITCH_HOME/dashboard"
  else
    run_spin "Installing dashboard packages" sh -c 'cd "$1" && npm install --no-audit --no-fund' _ "$SPLITCH_HOME/dashboard"
  fi
  run_spin "Building dashboard bundle" sh -c 'cd "$1" && npm run build' _ "$SPLITCH_HOME/dashboard"
  ok "Dashboard UI built cleanly"
fi

# ── 6. Install Global `splitch` CLI Shim & Metadata ──────────────────────────
step_bar 5 "$TOTAL_STEPS" "Installing CLI shim"
mkdir -p "$SPLITCH_BIN_DIR" "$SPLITCH_DATA_ROOT"
SHIM_PATH="$SPLITCH_BIN_DIR/splitch"
cat > "$SHIM_PATH" <<EOF
#!/usr/bin/env sh
exec "$SPLITCH_HOME/.venv/bin/splitch" "\$@"
EOF
chmod +x "$SHIM_PATH"
ok "Installed global CLI command: $SHIM_PATH"

_META_ROOT="$SPLITCH_DATA_ROOT" \
_META_HOME="$SPLITCH_HOME" \
_META_SHIM="$SHIM_PATH" \
_META_MODE="$INSTALL_MODE" \
_META_REPO="$SPLITCH_REPO" \
_META_BRANCH="$SPLITCH_BRANCH" \
_META_PYTHON="$SPLITCH_PYTHON" \
_META_AGENT="$SPLITCH_AGENT_ONLY" \
"$SPLITCH_HOME/.venv/bin/python" - <<'PY'
import json, os, time
from pathlib import Path
meta_path = Path(os.environ["_META_ROOT"]) / "install_meta.json"
existing = {}
if meta_path.exists():
    try:
        existing = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        existing = {}
now = time.time()
existing.update({
    "install_dir": os.environ["_META_HOME"],
    "bin_path": os.environ["_META_SHIM"],
    "mode": os.environ["_META_MODE"],
    "repo_url": os.environ["_META_REPO"],
    "branch": os.environ["_META_BRANCH"],
    "python": os.environ["_META_PYTHON"],
    "agent_only": os.environ["_META_AGENT"] == "1",
    "installed_at": existing.get("installed_at", now),
    "updated_at": now,
})
meta_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
PY

# ── 7. Install / Update Inference Engine Backend ─────────────────────────────
step_bar 6 "$TOTAL_STEPS" "Inference & media engines"
case "$SPLITCH_ENGINE" in
  none|skip|off)
    ok "Skipped inference engine install (--engine none)"
    ;;
  llama.cpp|ollama|mlx-lm|strata)
    log "Installing / updating inference engine ($SPLITCH_ENGINE, backend=$SPLITCH_ENGINE_BACKEND)..."
    SPLITCH_DATA_ROOT="$SPLITCH_DATA_ROOT" "$SPLITCH_HOME/.venv/bin/splitch" engine install "$SPLITCH_ENGINE" \
      --backend "$SPLITCH_ENGINE_BACKEND" \
      --bin-dir "$SPLITCH_BIN_DIR" || warn "Engine install returned non-zero (run 'splitch engine install $SPLITCH_ENGINE' to retry)"
    ;;
  auto)
    if [ -f "$SPLITCH_DATA_ROOT/engine_meta.json" ]; then
      log "Checking managed inference engine for updates..."
      SPLITCH_DATA_ROOT="$SPLITCH_DATA_ROOT" "$SPLITCH_HOME/.venv/bin/splitch" engine update \
        --backend "$SPLITCH_ENGINE_BACKEND" \
        --bin-dir "$SPLITCH_BIN_DIR" || warn "Managed engine update failed (run 'splitch engine update' to retry)"
    elif ! command -v llama-server >/dev/null 2>&1 && ! command -v ollama >/dev/null 2>&1; then
      log "No local inference engine found — installing managed llama.cpp (backend=$SPLITCH_ENGINE_BACKEND)..."
      SPLITCH_DATA_ROOT="$SPLITCH_DATA_ROOT" "$SPLITCH_HOME/.venv/bin/splitch" engine install llama.cpp \
        --backend "$SPLITCH_ENGINE_BACKEND" \
        --bin-dir "$SPLITCH_BIN_DIR" || warn "Managed llama.cpp install failed (run 'splitch engine install llama.cpp' to retry)"
    else
      ok "Existing local inference engine detected (pass --engine llama.cpp to install Splitch-managed llama.cpp)"
    fi
    ;;
  *)
    warn "Unknown --engine value '$SPLITCH_ENGINE' (expected auto|llama.cpp|ollama|none)"
    ;;
esac

if [ "$SPLITCH_WITH_MEDIA" = "1" ] || [ -f "$SPLITCH_DATA_ROOT/sd_engine_meta.json" ]; then
  log "Installing / updating managed media engine (sd.cpp, backend=$SPLITCH_ENGINE_BACKEND)..."
  SPLITCH_DATA_ROOT="$SPLITCH_DATA_ROOT" "$SPLITCH_HOME/.venv/bin/splitch" engine install sd.cpp \
    --backend "$SPLITCH_ENGINE_BACKEND" \
    --bin-dir "$SPLITCH_BIN_DIR" || warn "Managed sd.cpp install failed (install from Media Studio or run 'splitch engine install sd.cpp')"
fi

step_bar 7 "$TOTAL_STEPS" "Complete"
osc_progress 0 0

if [ "$SPLITCH_AGENT_ONLY" != "1" ] && [ "$SPLITCH_NO_DESKTOP" != "1" ]; then
  if [ "$OS" = "darwin" ] || [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${XDG_CURRENT_DESKTOP:-}" ]; then
    SPLITCH_DATA_ROOT="$SPLITCH_DATA_ROOT" "$SPLITCH_BIN_DIR/splitch" desktop install >/dev/null 2>&1 \
      && ok "Installed desktop launcher & application menu icon" \
      || warn "Skipped desktop icon setup (run 'splitch desktop install' later)"
  fi
fi

if [ -n "$SPLITCH_JOIN_URL" ]; then
  log "Registering and starting splitch-agent -> $SPLITCH_JOIN_URL..."
  AGENT_ARGS="agent --port $SPLITCH_AGENT_PORT_OPT --engine-url http://127.0.0.1:$SPLITCH_ENGINE_PORT_OPT"
  [ -n "$SPLITCH_NODE_ID_OPT" ] && AGENT_ARGS="$AGENT_ARGS --id $SPLITCH_NODE_ID_OPT"
  [ -n "$SPLITCH_NODE_ROLE_OPT" ] && AGENT_ARGS="$AGENT_ARGS --role $SPLITCH_NODE_ROLE_OPT"
  [ "$SPLITCH_YIELD_GAMING" = "1" ] && AGENT_ARGS="$AGENT_ARGS --yield-on-gaming"
  [ -n "$SPLITCH_YIELD_GPUS" ] && AGENT_ARGS="$AGENT_ARGS --yield-gpus $SPLITCH_YIELD_GPUS"
  [ "$SPLITCH_ENABLE_TOOLS_OPT" = "1" ] && AGENT_ARGS="$AGENT_ARGS --enable-tools"

  # 1. One-shot join using single-use token (never written into persistent service units)
  # shellcheck disable=SC2086
  if [ -n "$SPLITCH_NODE_NAME_OPT" ]; then
    SPLITCH_JOIN_TOKEN="$SPLITCH_JOIN_TOKEN" "$SPLITCH_BIN_DIR/splitch" $AGENT_ARGS --name "$SPLITCH_NODE_NAME_OPT" --join "$SPLITCH_JOIN_URL" --join-only || warn "Initial router registration failed (will retry when agent starts)"
  else
    SPLITCH_JOIN_TOKEN="$SPLITCH_JOIN_TOKEN" "$SPLITCH_BIN_DIR/splitch" $AGENT_ARGS --join "$SPLITCH_JOIN_URL" --join-only || warn "Initial router registration failed (will retry when agent starts)"
  fi

  # 2. Start splitch-agent as user service (systemd/launchd) or detached process
  SERVICE_STARTED=0
  if [ "$SPLITCH_INSTALL_SERVICE" = "1" ] && [ "$OS" = "linux" ] && command -v systemctl >/dev/null 2>&1; then
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    UNIT_FILE="$UNIT_DIR/splitch-agent.service"
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Splitch Node Agent
After=network-online.target

[Service]
Type=simple
ExecStart=$SPLITCH_BIN_DIR/splitch $AGENT_ARGS
Restart=always
RestartSec=3
Environment=PATH=$SPLITCH_BIN_DIR:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
${SPLITCH_AGENT_TOKEN:+Environment=SPLITCH_AGENT_TOKEN=$SPLITCH_AGENT_TOKEN}

[Install]
WantedBy=default.target
EOF
    chmod 600 "$UNIT_FILE"
    if systemctl --user daemon-reload >/dev/null 2>&1 && systemctl --user enable --now splitch-agent.service >/dev/null 2>&1; then
      ok "Installed & started systemd user service: splitch-agent.service"
      SERVICE_STARTED=1
    fi
  elif [ "$SPLITCH_INSTALL_SERVICE" = "1" ] && [ "$OS" = "darwin" ]; then
    PLIST_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$PLIST_DIR"
    PLIST_FILE="$PLIST_DIR/com.splitch.agent.plist"
    cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.splitch.agent</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-lc</string>
    <string>$SPLITCH_BIN_DIR/splitch $AGENT_ARGS</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
EOF
    chmod 600 "$PLIST_FILE"
    launchctl unload "$PLIST_FILE" >/dev/null 2>&1 || true
    if launchctl load -w "$PLIST_FILE" >/dev/null 2>&1; then
      ok "Installed & started LaunchAgent: com.splitch.agent"
      SERVICE_STARTED=1
    fi
  fi

  if [ "$SERVICE_STARTED" = "0" ]; then
    LOG_FILE="$SPLITCH_DATA_ROOT/agent.log"
    # shellcheck disable=SC2086
    nohup "$SPLITCH_BIN_DIR/splitch" $AGENT_ARGS >"$LOG_FILE" 2>&1 </dev/null &
    ok "Started splitch-agent in background (log: $LOG_FILE)"
  fi
fi

ENGINE_HINT=""
if command -v llama-server >/dev/null 2>&1; then
  ENGINE_HINT="llama-server ready ($(command -v llama-server))"
elif command -v ollama >/dev/null 2>&1; then
  ENGINE_HINT="ollama ready ($(command -v ollama))"
else
  ENGINE_HINT="No local inference engine detected — run 'splitch engine install llama.cpp'"
fi

echo ""
log "Splitch is ready!"
ok "Hardware: $HW_SUMMARY"
ok "Engine:   $ENGINE_HINT"

case ":$PATH:" in
  *":$SPLITCH_BIN_DIR:"*) ;;
  *)
    warn "$SPLITCH_BIN_DIR is not in your current PATH."
    printf '    Add this to your shell profile (~/.bashrc or ~/.zshrc):\n'
    printf '      export PATH="%s:$PATH"\n' "$SPLITCH_BIN_DIR"
    ;;
esac

echo ""
if [ "$SPLITCH_AGENT_ONLY" = "1" ]; then
  printf '  Next step (join an existing Splitch router):\n'
  printf '    splitch agent --yield-on-gaming --join http://<ROUTER_IP>:8080\n'
else
  printf '  Next steps:\n'
  printf '    splitch doctor --check-updates   # inspect hardware, engines, and updates\n'
  printf '    splitch up                       # launch local agent + router + UI on http://localhost:8080\n'
  printf '    splitch update --with-engine     # update Splitch + inference engine anytime\n'
fi
