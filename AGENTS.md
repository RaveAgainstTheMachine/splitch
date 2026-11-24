# AGENTS.md — Splitch Engineering & Agent Harness Guide

This repository contains **Splitch** (`v0.12.0`), a multi-node local AI orchestrator, OpenAI/Anthropic-compatible gateway, 3-column Agent Chat IDE, and Media Studio.

## 1. Mandatory Pre-Flight Harness

Before committing, opening a PR, or deploying, run the workspace pre-flight verification harness:

```bash
./scripts/preflight.sh
```

`scripts/preflight.sh` enforces:
1. **Public-Repo Zero-Leak & Strict SemVer 2.0.0 Gate:** Scans all tracked files (`git ls-files`) for personal LAN IPs, private usernames, local mount paths, and secret tokens, asserts strict SemVer parity across `backend/version.py`, `pyproject.toml`, and `dashboard/package.json`, and **enforces SemVer progression** against the latest `vX.Y.Z` git tag (`feat:` → `MINOR` bump, `fix:`/code changes → `PATCH` bump). Use `./scripts/bump-version.sh patch|minor|major` to bump all manifests atomically.
2. **POSIX Installer Syntax:** Validates `sh -n install.sh` and `sh -n scripts/preflight.sh`.
3. **Python Static Analysis:** Runs `uv run --no-sync ruff check backend tests`.
4. **Frontend Type & Bundle Check:** Runs `(cd dashboard && npm run build)` (`tsc -b && vite build`).
5. **Automated Test Suite:** Runs `uv run --no-sync pytest -q` across unit, integration, and headless Chromium E2E tests.

## 2. Architecture & Cross-Boundary Schema Invariants

- **Producer -> Router -> Frontend Parity:**
  - Shared Pydantic models live in `backend/shared/models.py`.
  - Frontend TypeScript interfaces live in `dashboard/src/types.ts`.
  - Whenever a serialized field is added or modified on a backend response model, update `dashboard/src/types.ts` and verify all 3 layers (`backend/agent` -> `backend/router` -> `dashboard/src`).
- **Non-Blocking Hot Paths:**
  - `splitch-agent` (`backend/agent/main.py`) refreshes hardware/GPU/engine telemetry on a background asyncio loop (`poll_once()`). `/metrics` and `/health` must return cached state immediately without spawning subprocesses on the request thread.
  - SQLite operations in `backend/router/chat_api.py` (`ChatStore`) must execute via `asyncio.to_thread` with WAL mode enabled.
- **Sandboxed Workspace Tools & Post-Edit Verification:**
  - Workspace filesystem tools (`list_dir`, `read_file`, `write_file`, `edit_file`, `grep_search`, `run_command`) in `backend/agent/main.py` are opt-in (`--enable-tools` / `SPLITCH_ENABLE_TOOLS=1`) and strictly confined to `WORKSPACE_ROOT` via `Path.resolve().is_relative_to(...)`.
  - `write_file` and `edit_file` automatically trigger `_run_post_edit_verification()` so syntax/lint diagnostics are returned in the tool result payload for immediate self-correction.
- **Public Repository Hygiene:**
  - Never commit personal LAN IPs, hostnames, usernames, or machine-specific mount paths to tracked files. Keep local cluster overrides in gitignored `splitch.local.toml` and `scripts/deploy-local.sh`.

## 3. Browser Support Policy

- **Browser Support:** Allow Newly Available web platform features (`<dialog closedby="any">`, Popover API, `<input type="checkbox" switch>`, CSS `light-dark()`, CSS Grid Subgrid, container scroll-state queries) with the progressive-enhancement polyfills bundled in `dashboard/src/main.tsx`.
