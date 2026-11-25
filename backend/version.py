"""Single source of truth for Splitch's Semantic Version (SemVer 2.0.0)."""

from __future__ import annotations

import re

__version__ = "0.23.1"

SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$"
)


def is_valid_semver(version: str) -> bool:
    return bool(SEMVER_RE.fullmatch(version.strip()))
