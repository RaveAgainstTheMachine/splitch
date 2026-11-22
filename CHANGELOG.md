# Changelog

All notable changes to **Splitch** are documented in this file.
This project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html), enforced automatically across `backend/version.py`, `pyproject.toml`, and `dashboard/package.json` by `./scripts/preflight.sh` and GitHub Actions CI.

## [0.7.0] - 2026-11-22

### Desktop, Tray & Theme Synchronization
- **Router-Synced UI Theme (`GET/PATCH /api/theme` + `DashboardSnapshot.theme`):** Appearance settings (`scheme`, `accent`, `surface`, `resolved_light`) now persist on the router (`ui_theme.json`), broadcast live over `/ws/status` across browsers/devices, and serve dynamically recolored `/favicon.svg`.
- **Dynamic Pure-Python Icon Rasterizer (`backend/desktop/icons.py`):** Mirrors the Web UI accent, surface, and light/dark palette 1:1 across PNG, ARGB32 SNI pixmaps, Windows `.ico`, and SVG assets with content-hashed cache-busting filenames.
- **Live System Tray & Desktop/App-Menu/Launcher Icon Sync:** `TrayController` (`backend/desktop/tray.py`) updates system tray icons instantaneously on theme changes (D-Bus SNI `NewIcon()` + `pystray`) and debounces atomic desktop/app-menu/Start-Menu/autostart icon updates with KDE/Freedesktop (`kbuildsycoca6`, `KIconLoader.iconChanged`), macOS (`lsregister`), and Windows (`SHChangeNotify`) cache refreshes.
- **CLI Support (`splitch desktop sync-icon`):** Added `splitch desktop sync-icon` to sync OS shortcut and tray icons on demand from the router or explicit color flags.

## [0.6.0] - 2026-11-22

### Security & Supply Chain
- **Safe Loopback Defaults & Auth-Enforced LAN Binds:** Defaulted `RouterSettings.host`, `splitch router --host`, `splitch agent --host`, and `splitch init` to `127.0.0.1`. Binding non-loopback interfaces without `SPLITCH_API_KEY`/`SPLITCH_API_KEY_HASH` (router) or `SPLITCH_ROUTER_PUBLIC_KEY`/`SPLITCH_AGENT_TOKEN` (agent) is now refused unless `--allow-unauthenticated` / `SPLITCH_ALLOW_UNAUTH_LAN=1` is explicitly set.
- **Eliminated Loopback Approval Header Bypass:** `splitch up` now auto-mints an ephemeral Ed25519 keypair for local router ↔ agent authentication and signed tool approvals. Unsigned mutating tool calls are refused by default unless `SPLITCH_ALLOW_UNAUTH_TOOLS=1` is explicitly enabled on loopback.
- **OS-Level `run_command` Sandbox (`SPLITCH_CMD_SANDBOX=auto|bwrap|seatbelt|none`):** Wraps shell commands in Linux Bubblewrap (`bwrap`: read-only root, writable workspace bind, ephemeral `/tmp`, unshared network by default) or macOS Seatbelt (`sandbox-exec`) when available.
- **SHA-256 Verified Installers:** Replaced piped remote `uv` installer scripts in `install.sh` and `install.ps1` with pinned `uv` (`0.13.5`) release archives verified against `.sha256` checksums, and added official `SHASUMS256.txt` verification for standalone Node.js (`22.14.0`).
- **Clean Pre-Flight Leak Scanner:** Moved machine-specific leak patterns out of tracked `scripts/preflight.sh` into optional gitignored `.preflight.local.patterns`, keeping generic private-key and token detectors tracked.

### Architecture & Documentation
- **Modular Backend & Frontend Splits:** Extracted `backend/agent/tools.py`, `backend/agent/sync.py`, `backend/router/chat_store.py`, `backend/router/chat_mcp.py`, `dashboard/src/components/chat/*`, and `dashboard/src/components/settings/*` to reduce monolith file sizes without changing behavior.
- **Concise README:** Trimmed `README.md` by ~60% to focus on the 3 core pillars, screenshots, quickstart, security model, and wiki links.

## [0.5.0] - 2026-11-22

### Security (Cryptographic Secrets, Vault & Asymmetric Ed25519)
- **Salted Secret Hashing (`SPLITCH_API_KEY_HASH`) & Independent Session Secret:** Added `scrypt` (with random 16-byte salt + non-plaintext HMAC-keyed 60 s verification cache) and salted `sha256` verification in `backend/shared/secrets.py`. Session cookies are now signed with an independent auto-generated `0600` secret (`data_dir/.session_secret` / `SPLITCH_SESSION_SECRET`), decoupling sessions from admin credentials.
- **Hashed Join Tokens in RAM:** `JoinTokenManager` (`backend/router/provision_api.py`) now stores only salted SHA-256 hashes of `spj_*` join tokens in memory, exposing plaintext only once at mint time.
- **Config Secret Indirection (`env:`, `${VAR}`, `file:`, `vault:`):** Added `resolve_secret_value()` with strict POSIX `0600` permission checks on `file:` targets (compatible with Docker Secrets and systemd `LoadCredential=`).
- **AES-256-GCM Local Secret Vault & MCP Auto-Vaulting:** Added `SecretVault` (`data_dir/secrets.enc`, AAD-bound per secret name, `0600` master key or `SPLITCH_MASTER_KEY` / `SPLITCH_MASTER_KEY_FILE` / optional OS keyring). Secret-looking MCP server `env` keys are automatically encrypted into the vault on save, masked (`••••••••`) in API responses, preserved across UI edits, redacted (`${KEY}`) on client export, and purged on delete.
- **Asymmetric Ed25519 Router ↔ Agent Signing:** Added Ed25519 request authentication (`X-Splitch-Auth-Ts` / `X-Splitch-Auth-Sig` bound to HTTP method + path) and Ed25519 HITL tool-approval assertions (`SPLITCH_ROUTER_PRIVATE_KEY` on router, public-only `SPLITCH_ROUTER_PUBLIC_KEY` on worker agents — zero shared secrets on worker nodes).
- **CLI Secret Manager (`splitch secret`):** Added `splitch secret hash`, `set`, `list`, `delete`, and `init-agent-keys`.

## [0.4.0] - 2026-11-22

### Security (Sprint A Hardening)
- **Cryptographic Tool-Approval Boundary (S2):** Replaced plain `X-Splitch-Router-Approved` assertions with short-lived, payload-bound HMAC-SHA256 signatures (`X-Splitch-Approval-Ts` + `X-Splitch-Approval-Sig`, 60 s window) derived from `SPLITCH_AGENT_TOKEN` (`backend/shared/security.py`). Unauthenticated remote mutating workspace tool calls (`write_file`, `edit_file`, `run_command`) are now refused with HTTP `403` when `SPLITCH_AGENT_TOKEN` is unset.
- **Trusted-Proxy Gating (S4):** Gated `X-Forwarded-For` and `X-Forwarded-Proto` handling behind `config.security.trusted_proxies` (`SPLITCH_TRUSTED_PROXIES`, default `127.0.0.1,::1`) so spoofed client headers cannot bypass login brute-force rate limiting or forge HTTPS/HSTS detection.
- **DNS-Resolved SSRF Protection (S3):** Added asynchronous off-thread DNS resolution (`socket.getaddrinfo`) and IPv4-mapped IPv6 unwrapping on `POST /api/nodes` to block hostnames resolving to link-local (`169.254.0.0/16`, `fe80::/10`), multicast, or unspecified addresses.
- **Tightened Credentialed CORS & CSRF Guard (S5):** Restricted default credentialed CORS origins to loopback (`localhost`, `127.0.0.1`, `[::1]`) plus explicit `SPLITCH_ALLOWED_ORIGINS`, and added `Origin` / `Sec-Fetch-Site` CSRF validation on cookie-authenticated mutations.
- **Safer Deployment Defaults (S1):** Bound `docker-compose.yml` port exposure to `127.0.0.1:8080:8080` by default and added startup security warnings in `splitch up`, `splitch router`, and `splitch agent` when binding non-loopback interfaces without `SPLITCH_API_KEY` or `SPLITCH_AGENT_TOKEN`.
- **Process-Group Isolation & Bounded Output:** `run_command` now launches POSIX commands in an isolated session (`start_new_session=True`), kills the entire process group (`os.killpg(..., SIGKILL)`) on timeout or flood, and caps stdout buffering at 64 KB RAM (4 MB drain fuse, 12,000-char formatted output).

### Reliability & CI (Sprint B)
- **Bounded Multi-Hop & Pre-First-Chunk Streaming Failover:** `backend/router/proxy.py` tracks `attempted_nodes` across a bounded retry loop (eliminating retry loops back to a dead primary) and peeks the first non-empty SSE chunk before committing `StreamingResponse` so pre-first-token stream drops fail over transparently.
- **Transactional Self-Updater Rollback & `splitch rollback`:** `perform_update()` records the previous full SHA before `git pull --ff-only`, automatically rolls back via `git reset --hard` if `uv sync` or dashboard build fails, persists `previous_full_sha` in `install_meta.json`, and exposes manual `splitch rollback [--to <sha>]`.
- **Expanded CI Matrix:** Added a dedicated headless Chromium `ui-e2e` job and cross-platform `macos-latest` / `windows-latest` verification jobs to `.github/workflows/ci.yml`.
- **SemVer Single Source of Truth:** Added `backend/version.py` (`__version__ = "0.4.0"` + strict SemVer 2.0.0 validator) imported across router, agent, desktop tray/shortcuts, web search, and verified in pre-flight & CI.

### Added (since 0.3.0)
- **Benchmarks GUI & CLI Harness:** `splitch bench`, `POST /api/bench/run`, `GET /api/bench/history`, and the **Settings → Benchmarks** panel + **Overview → Run Benchmark** trigger.
- **Anthropic Messages API & Coding Harness:** Native `/v1/messages` and `/v1/messages/count_tokens` compatibility, targeted `edit_file` tool, and automatic post-edit syntax/lint verification diagnostics.
- **Dynamic Color-Matched Favicon & Expanded Settings:** Live theme-matched SVG favicon, configurable login rate-limit controls, Software & Engine Updates card, and native switch toggles.

## [0.3.0] - 2026-11-20
- Multi-node hardware-aware AI orchestrator (`splitch-router`, `splitch-agent`), Gaming Mode auto-yield, 3-column Agent Chat IDE, Media Studio (`sd-server` / ComfyUI), scoped MCP/Skills management, native TLS/HTTPS, and cross-platform desktop/tray integration.
