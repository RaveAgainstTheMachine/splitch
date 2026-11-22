#!/usr/bin/env sh
# Splitch Pre-Flight Audit & Verification Harness
# Usage:
#   ./scripts/preflight.sh [--quick]
#
# --quick skips the full dashboard vite build and headless Chromium UI E2E test.

set -eu

QUICK=0
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    *) echo "[preflight] Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "[preflight 1/5] Checking tracked files for private LAN IPs, paths, or secrets..."
python3 - <<'PY'
import re, subprocess, sys
from pathlib import Path

files = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
# Split patterns so preflight.sh itself does not contain literal personal strings
ip_pat = re.compile(r"\b10\.10\." + r"(?:10|20)\.\d+\b")
path_pat = re.compile(r"(/Volumes/MacMini" + r"1TB|/games/Code" + r"Projects|/home/fros" + r"ty)")
key_pat = re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC) PRIVATE KEY-----")

leaks: list[str] = []
for rel in files:
    p = Path(rel)
    if not p.is_file() or p.suffix in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff2"}:
        continue
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        continue
    for idx, line in enumerate(text.splitlines(), 1):
        if ip_pat.search(line) or path_pat.search(line) or key_pat.search(line):
            leaks.append(f"  {rel}:{idx}: {line.strip()[:120]}")

if leaks:
    print("[preflight] ERROR: Potential private data leak in tracked files:", file=sys.stderr)
    print("\n".join(leaks), file=sys.stderr)
    sys.exit(1)
print("  ✓ 0 private data leaks across", len(files), "tracked files")

import json, tomllib
from backend.version import __version__ as py_ver, is_valid_semver

assert is_valid_semver(py_ver), f"Invalid SemVer in backend/version.py: {py_ver}"
proj_ver = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
dash_ver = json.loads(Path("dashboard/package.json").read_text(encoding="utf-8"))["version"]
if not (py_ver == proj_ver == dash_ver):
    print(
        f"[preflight] ERROR: Version drift detected: backend/version.py={py_ver}, "
        f"pyproject.toml={proj_ver}, dashboard/package.json={dash_ver}",
        file=sys.stderr,
    )
    sys.exit(1)
print(f"  ✓ SemVer 2.0.0 parity verified: v{py_ver}")
PY

echo "[preflight 2/5] Validating POSIX shell scripts..."
sh -n install.sh
sh -n scripts/preflight.sh
echo "  ✓ install.sh and scripts/preflight.sh syntax valid"

echo "[preflight 3/5] Running Ruff linter..."
uv run --no-sync ruff check backend tests
echo "  ✓ ruff check passed"

if [ "$QUICK" = "0" ]; then
  echo "[preflight 4/5] Building frontend TypeScript & Vite bundle..."
  (cd dashboard && npm run build >/dev/null)
  echo "  ✓ dashboard build passed"

  echo "[preflight 5/5] Running full pytest suite..."
  uv run --no-sync pytest -q
else
  echo "[preflight 4/5] Skipped dashboard build (--quick)"
  echo "[preflight 5/5] Running fast unit/API pytest suite (--quick)..."
  uv run --no-sync pytest --ignore=tests/test_ui_e2e.py -q
fi

echo "[preflight] All checks passed!"
