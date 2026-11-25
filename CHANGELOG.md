# Changelog

All notable changes to **Splitch** are documented in this file.
This project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html), enforced automatically across `backend/version.py`, `pyproject.toml`, and `dashboard/package.json` by `./scripts/preflight.sh` and GitHub Actions CI.

## [0.13.0] - 2026-11-25

### Strata Sparse-MoE Engine Phase 2 (Managed Install, Sidecar Proxy, 4-Tier Fit & Quick Tune)
- **Managed SHA-256-Verified Strata CLI Installer (`backend/shared/strata_manager.py`, `engines.py`, `updater.py`, `cli.py`):** Added `splitch engine install strata` / `update strata` fetching official `Niko1221/Strata` `strata-control` releases (`linux-x86_64`, `linux-aarch64`, `macos-arm64`, `windows-x86_64`) with strict SHA-256 verification against `install-manifest.txt`.
- **Loopback Sidecar (`:8085`) + Ed25519-Signed Streaming Proxy (`backend/agent/main.py`, `policy.py`, `proxy.py`, `telemetry.py`):** Runs managed Strata on `127.0.0.1:8085` alongside `llama-server` / `mlx-lm` with optional auto-eviction of primary VRAM on start, Gaming-Mode auto-stop, `/v1/models` discovery, and zero-copy SSE proxying over `/strata/proxy/{path}`.
- **4-Tier Hardware Fit & Web UI Control Plane (`StrataPane.tsx`, `ModelsTab.tsx`, `strata_api.py`):** Added **Strata (Sparse MoE)** mode in **Models** with curated MoE catalog, 4-tier VRAM+Host RAM+Unified+SSD fit badges (`Optimal`, `Low-RAM`, `Tight`, `Insufficient`), **Preview Plan**, 1-click **Quick Tune (`--tune quick`)**, **Launch / Switch Model**, **Stop Strata**, and live job log streaming.

## [0.12.0] - 2026-11-24

### Bulletproof Cross-OS Desktop & Tray Icon Sync
- **Atomic Icon Rasterizer & Native macOS `.icns` (`icons.py`, `shortcuts.py`):** Switched all PNG/SVG/ICO/ICNS writes to atomic temp-file + `os.replace()` with LRU caching, multi-resolution Windows `.ico` (16–256px), and pure-Python Apple `.icns` containers (`icp4`–`ic08`) updating `Splitch.app/Contents/Info.plist` `CFBundleIconFile` + LaunchServices/`NSWorkspace`.
- **Universal API Key Vault Resolution & Dual-Path Sync (`shortcuts.py`, `tray.py`, `telemetry.py`, `agent/main.py`):** Added `resolve_desktop_api_key()` (`SPLITCH_API_KEY` → encrypted `SecretVault` → `tray.env`/`router.env`) + `splitch desktop set-api-key`, signed router → agent `POST /desktop/sync-theme` fan-out, GNOME `metadata::trusted` re-application, and `--extra desktop` preservation in `splitch update`.

## [0.11.2] - 2026-11-24
- Fixed URL-encoding of special characters (`&`, `%`, `$`) in system tray WebSocket `/ws/status?token=...` queries (`tray.py`).

## [0.11.1] - 2026-11-24
- Surfaced running cluster version (`vX.Y.Z`) in the topbar and target remote version (`Update → vX.Y.Z`) in the UI and update panel.

## [0.11.0] - 2026-11-24
- Added **Cluster Self-Update & Rollback** (`POST /api/system/update-cluster`, `rollback-cluster`), **Apple Silicon `mlx-lm`** managed engine (`mlx_server.py`), **Strata Phase 1** external detection & Gaming-Mode allowlist, **Agent Chat Workspace File Tree & Git Review Explorer**, and strict SemVer 2.0.0 preflight enforcement.

## [0.10.3] - 2026-11-24

### MCP Discovery
- **Combined Official Registry + npm Search & Smart Query Normalization (`mcp_api.py`, `McpTab.tsx`):** Defaulted MCP discovery to `All (Official + npm)`, stripped trailing filler `mcp` and auto-hyphenated multi-word queries for `registry.modelcontextprotocol.io` (e.g. `reverse engineer anything` → `reverse-engineer-anything`), stopped duplicating `mcp` in npm queries, and ranked results by name/description token relevance + local `stdio` priority.

## [0.10.2] - 2026-11-22

### Smart Alias Management
- **Auto-Heal Placeholder/Broken Aliases (`ClusterState.auto_heal_aliases_for_node`):** When a node indexes real models on `llama-server`/Ollama, any alias targeting that node whose model ID does not exist (such as default placeholders `coder-heavy` / `architect-dense`) automatically rebinds to the indexed model and persists to `aliases.json` — while preserving existing valid aliases and keeping `embed` aliases separated from generative models.
- **Update Aliases Dialog (`ModelsTab.tsx`):** Added an **Update Aliases…** action on completed downloads in the **Downloads Dock** and an **Aliases** button on every indexed row in **Node Models**, opening a checkbox dialog to rebind selected aliases on that node to a newly added model without silently overwriting working setups.

## [0.10.1] - 2026-11-22

### Bug Fixes
- **Media Studio HF Search Results Squishing:** Switched `.media-hf-results` from `display: grid` to `display: flex; flex-direction: column` with `flex-shrink: 0` on `.media-hf-repo` so repo rows keep their full height and scroll cleanly inside the results container instead of compressing into 15px strips.

## [0.10.0] - 2026-11-22

### Media Studio: Multi-File DiT Bundles, Native Video Gen & HF Model Browser
- **Multi-File DiT Bundle Auto-Wiring (`backend/agent/main.py`):** `POST /media/engine/start` now auto-wires companion files (`--diffusion-model`, `--vae`, `--t5xxl`, `--clip_l`, `--llm`, `--flash-attn`) for **Z-Image-Turbo**, **FLUX.1-schnell**, and **Wan 2.1** alongside single-file SDXL/SD1.5 checkpoints.
- **Native `sd-cli -M vid_gen` Video Generation (`POST /media/video/generate`):** Media Studio video generation now runs natively via `sd-cli -M vid_gen` (Wan 2.1 T2V 1.3B / 14B) without requiring ComfyUI, temporarily yielding `sd-server` VRAM during render and returning WebM video.
- **Upgraded Starter Bundles & Built-In Hugging Face Media Browser (`MediaTab.tsx`, `/api/media/hf/search`, `/api/media/hf/files`):** Added 1-click multi-file starter bundles (**Z-Image-Turbo**, **FLUX.1-schnell**, **SDXL Turbo**, **Wan 2.1 T2V 1.3B**, **Wan 2.1 T2V 14B**) plus an inline Hugging Face Image/Video search browser with quick-pick chips (`Z-Image`, `FLUX`, `SDXL`, `Wan 2.1`, `LTX-Video`) and 1-click `.gguf`/`.safetensors` Pull directly inside Media Studio.

## [0.9.5] - 2026-11-22

### UI Polish
- **Media Studio Seed Field Alignment:** Replaced wrapping `.ws-input-row` with nowrap `.media-seed-row` inside `.media-params-grid` so the `Seed` input and randomize button stay on one line, aligned on the same horizontal baseline as `Width`, `Height`, `Steps`, `CFG Scale`, `Frames`, and `FPS`.

## [0.9.4] - 2026-11-22

### Bug Fixes
- **Eliminated False `gpu_cooldown` After Media / LLM Jobs:** Added a 6-second post-job telemetry settle window (`last_splitch_work_ts` / `is_splitch_recently_active`) so cached `nvidia-smi` GPU utilization spikes immediately after a Splitch image/video or LLM generation completes no longer trigger a false 60-second `pc_gpu_cooldown` (real non-LLM games with high `non_llm_vram_mb` still yield immediately).

## [0.9.3] - 2026-11-22

### Bug Fixes
- **System Tray WebSocket Auto-Reconnect (`backend/desktop/tray.py`):** Caught `websockets.exceptions.WebSocketException` (including `ConnectionClosedError`, which does not inherit from `OSError`) inside `TrayController.watch_status_loop()`, preventing the tray from permanently losing live theme/icon sync after a router restart.

## [0.9.2] - 2026-11-22

### Models & Downloads UX
- **Instant Pull Feedback:** Clicking `Pull → <node>` on any Hugging Face GGUF immediately shows a `Starting…` spinner, transitions to live `Downloading XX%` progress, and switches to `On <node>` / `Load` / `Unload` once on disk.
- **Pinned Floating Downloads Dock:** Replaced the buried bottom-of-results downloads box with a fixed bottom-right collapsible Downloads Activity Dock (`downloads-dock`) featuring custom-styled `<progress>` bars (with indeterminate state), live GB/%, `Clear done`, and 1-click `Load` buttons.
- **Auto-Refresh `llama-server` Model Index & 1-Click Load in Node Models:** `splitch-agent` now automatically refreshes `llama-server` (`--models-dir`) when a `.gguf` finishes downloading or is deleted (`SPLITCH_ENGINE_RELOAD_CMD` or `splitch-llama.service` / `launchctl`), and the **Node Models** table now shows live `Loaded` / `Unloaded` status with 1-click `Load` / `Unload` controls.

## [0.9.1] - 2026-11-22

### UX
- **AI Disclaimer Under Chat Composer:** Added a muted, centered notice below the chatbox (`.chat-disclaimer`): Splitch is AI and can make mistakes, including in text, code, images and video.
- **Readable README Badges:** Switched shields.io badges to `style=for-the-badge` at 30px height so they stay legible on 1440p+ displays.

## [0.9.0] - 2026-11-22

### Branding
- **Unbounded Shear-Cut Wordmark:** Replaced the plain-text `SPLITCH` header with `SplitchWordmark` (`dashboard/src/components/SplitchWordmark.tsx`) — Unbounded Bold (SIL OFL 1.1) glyph outlines as inline SVG with the same Shear Cut as the emblem (upper half follows `--text`, lower half follows the live `--accent`). No font files shipped, so no `font-src` dependency.
- **Auto-Cycling README Banner:** Added `docs/assets/splitch-banner.svg` (emblem + wordmark) and made `docs/assets/splitch-logo.svg` cycle through all six accent presets (Violet → Cyan → Emerald → Amber → Rose → Cobalt) with pure CSS keyframes gated by `prefers-reduced-motion: no-preference` (static Violet otherwise).

## [0.8.0] - 2026-11-22

### Security Hardening & Supply Chain
- **Fail-Closed OS Command Sandbox (`SPLITCH_CMD_SANDBOX=required` Default):** Changed the default `run_command` sandbox mode from `auto` to `required` so missing/broken `bwrap` or macOS `sandbox-exec` refuses execution rather than silently falling back to an unsandboxed host shell (`SPLITCH_CMD_SANDBOX=none` or `auto` remains available as an explicit opt-out).
- **Approval Card Sandbox Visibility:** Exposed `cmd_sandbox` on `NodeTelemetry` and `sandbox` on `approval_required` SSE events (`PendingApproval`), displaying a prominent `Sandbox: bwrap/seatbelt (no net)` or `UNSANDBOXED HOST SHELL` badge inside the HITL approval card before the user clicks Allow.
- **Read-Isolation Hardening (`bwrap` & Seatbelt):** Overlaid `--tmpfs $HOME`, `--tmpfs /root`, and `--tmpfs $SPLITCH_DATA_ROOT` before binding `<ws_root>` in Linux `bwrap` (plus post-workspace masks on `~/.ssh`, `~/.gnupg`, `~/.aws`, browser profiles, and vault/key dirs when `WORKSPACE_ROOT == $HOME`), and added explicit `(deny file-read* ...)` rules for sensitive `$HOME`/Keychain/data paths in macOS Seatbelt.
- **In-Script Pinned SHA-256 Tables:** Hardcoded verified upstream SHA-256 digests for `uv 0.13.5` and `Node.js 22.14.0` across Linux/macOS/Windows `x64`/`arm64` inside `install.sh` and `install.ps1` (independent of artifact download origin).
- **Agent Bind-Check Order & Expanded Security Tests:** Moved `_enforce_auth_on_exposed_bind` ahead of `--join` in `splitch agent` so unauthenticated non-loopback binds exit `2` before registering, and added dual-path tests covering fail-closed `required`, `auto` fallback, `$HOME`/vault read blocking in `bwrap`, Seatbelt profiles, and CLI exit codes.

### Frontend Modularity
- **Further `ChatTab.tsx` Split:** Extracted `dashboard/src/components/chat/ChatMessageList.tsx` and `dashboard/src/components/chat/ChatComposer.tsx`, reducing `ChatTab.tsx` from 2,136 → ~1,580 lines (down 45% from 2,882).

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
