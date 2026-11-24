#!/usr/bin/env sh
# Atomically bump Splitch's SemVer 2.0.0 across all manifests.
# Usage: ./scripts/bump-version.sh [patch|minor|major|X.Y.Z]

set -eu

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 [patch|minor|major|X.Y.Z]" >&2
  exit 1
fi

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$REPO_ROOT"

python3 - "$1" <<'PY'
import json
import re
import sys
from pathlib import Path

from backend.version import __version__ as cur_ver, is_valid_semver

arg = sys.argv[1].strip().lstrip("v")
maj, min_, pat = (int(x) for x in cur_ver.split("-", 1)[0].split("."))

if arg == "patch":
    new_ver = f"{maj}.{min_}.{pat + 1}"
elif arg == "minor":
    new_ver = f"{maj}.{min_ + 1}.0"
elif arg == "major":
    new_ver = f"{maj + 1}.0.0"
elif is_valid_semver(arg):
    new_ver = arg
else:
    print(f"Invalid version or bump kind: {arg}", file=sys.stderr)
    sys.exit(1)

# 1. backend/version.py
vp = Path("backend/version.py")
vp.write_text(
    re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{new_ver}"', vp.read_text(encoding="utf-8")),
    encoding="utf-8",
)

# 2. pyproject.toml
pp = Path("pyproject.toml")
pp.write_text(
    re.sub(r'(?m)^version\s*=\s*"[^"]+"', f'version = "{new_ver}"', pp.read_text(encoding="utf-8"), count=1),
    encoding="utf-8",
)

# 3. dashboard/package.json
dp = Path("dashboard/package.json")
pkg = json.loads(dp.read_text(encoding="utf-8"))
pkg["version"] = new_ver
dp.write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")

# 4. AGENTS.md banner version
ap = Path("AGENTS.md")
if ap.is_file():
    ap.write_text(
        re.sub(r"\*\*Splitch\*\* \(`v[^`]+`\)", f"**Splitch** (`v{new_ver}`)", ap.read_text(encoding="utf-8"), count=1),
        encoding="utf-8",
    )

print(f"Bumped Splitch version: v{cur_ver} -> v{new_ver}")
PY
