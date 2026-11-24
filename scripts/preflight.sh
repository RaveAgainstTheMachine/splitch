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
patterns = [
    re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PRIVATE) KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}\b"),
    re.compile(r"\bxox[baprs]-[0-9]{8,}-[A-Za-z0-9-]{16,}\b"),
]
local_pat_file = Path(".preflight.local.patterns")
if local_pat_file.is_file():
    for raw in local_pat_file.read_text(encoding="utf-8").splitlines():
        line_pat = raw.strip()
        if line_pat and not line_pat.startswith("#"):
            patterns.append(re.compile(line_pat))

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
        if any(pat.search(line) for pat in patterns):
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

def _parse_ver(v: str) -> tuple[int, int, int]:
    core = v.lstrip("v").split("-", 1)[0].split("+", 1)[0]
    parts = [int(x) for x in core.split(".")]
    return (parts[0], parts[1], parts[2])

tag_proc = subprocess.run(
    ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*.[0-9]*.[0-9]*"],
    capture_output=True,
    text=True,
    check=False,
)
if tag_proc.returncode == 0 and tag_proc.stdout.strip():
    last_tag = tag_proc.stdout.strip()
    tag_tuple = _parse_ver(last_tag)
    cur_tuple = _parse_ver(py_ver)
    subjects = subprocess.check_output(
        ["git", "log", f"{last_tag}..HEAD", "--format=%s"], text=True
    ).splitlines()
    changed_since_tag = subprocess.check_output(
        ["git", "diff", "--name-only", f"{last_tag}..HEAD"], text=True
    ).splitlines()
    dirty_files = subprocess.check_output(
        ["git", "diff", "--name-only", "HEAD"], text=True
    ).splitlines()
    code_prefixes = ("backend/", "dashboard/src/", "install.sh", "install.ps1")
    code_changed = any(
        f.startswith(code_prefixes) for f in (*changed_since_tag, *dirty_files)
    )
    has_feat = any(re.match(r"^feat(\([^)]+\))?!?:", s) for s in subjects)
    has_breaking = any(
        re.match(r"^[a-z]+(\([^)]+\))?!:", s) or "BREAKING CHANGE" in s for s in subjects
    )
    if has_breaking and cur_tuple[0] == 0 and cur_tuple[:2] <= tag_tuple[:2]:
        print(
            f"[preflight] ERROR: Breaking change since {last_tag} requires a MINOR bump (0.x) "
            f"from v{py_ver}. Run: ./scripts/bump-version.sh minor",
            file=sys.stderr,
        )
        sys.exit(1)
    if has_breaking and cur_tuple[0] > 0 and cur_tuple[0] <= tag_tuple[0]:
        print(
            f"[preflight] ERROR: Breaking change since {last_tag} requires a MAJOR bump "
            f"from v{py_ver}. Run: ./scripts/bump-version.sh major",
            file=sys.stderr,
        )
        sys.exit(1)
    if has_feat and cur_tuple[:2] <= tag_tuple[:2]:
        print(
            f"[preflight] ERROR: Feature commit(s) since {last_tag} require a MINOR bump "
            f"(still at v{py_ver}). Run: ./scripts/bump-version.sh minor",
            file=sys.stderr,
        )
        sys.exit(1)
    if code_changed and cur_tuple <= tag_tuple:
        print(
            f"[preflight] ERROR: Code changes since {last_tag} require at least a PATCH bump "
            f"(still at v{py_ver}). Run: ./scripts/bump-version.sh patch",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  ✓ SemVer progression verified: {last_tag} -> v{py_ver}")
PY

echo "[preflight 2/5] Validating POSIX shell scripts..."
sh -n install.sh
sh -n scripts/preflight.sh
sh -n scripts/bump-version.sh
echo "  ✓ install.sh, scripts/preflight.sh, and scripts/bump-version.sh syntax valid"

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
