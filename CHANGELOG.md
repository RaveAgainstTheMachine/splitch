# Changelog

All notable changes to **Splitch** are documented in this file.
This project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html), enforced automatically across `backend/version.py`, `pyproject.toml`, and `dashboard/package.json` by `./scripts/preflight.sh` and GitHub Actions CI.

## [0.25.4] - 2026-11-25

### Structural Refactor (Agent, Chat Context & Dashboard Styles)
- **Node Agent Split (`backend/agent/downloads.py`, `backend/agent/media_routes.py`, `backend/agent/strata_routes.py`):** Extracted Hugging Face `hf_xet`/HTTP downloaders, local media engine lifecycle/proxy routes, and Strata Sparse-MoE lifecycle/Quick Tune/proxy routes out of `backend/agent/main.py` (2,657 → 1,584 lines) while preserving all route contracts and test/module exports.
- **Agent Chat Context Split (`backend/router/chat_context.py`):** Extracted system/rules/scoped-extension assembly, auto-compaction, approval/reasoning helpers, and `suggest_mcp`/`suggest_skill` discovery execution out of `backend/router/chat_api.py` (1,486 → 958 lines).
- **Dashboard CSS Partials (`dashboard/src/index.css`, `dashboard/src/styles/`):** Split `index.css` (5,086 lines) into ordered `@import` partials (`base.css`, `panels.css`, `chat.css`, `studio-settings.css`) preserving exact CSS cascade order and zero-pill constraints.
- **Parity Verification (`tests/test_refactor_parity.py`):** Added 6 integration/parity tests covering agent mounted routers, download tracker/protocol fallbacks, chat context/approval/compaction helpers, and CSS cascade order.

## [0.25.3] - 2026-11-25

### Supply-Chain Hardening & Feature Maturity Docs
- **Signed Update Gate (`backend/shared/updater.py`):** Added `SPLITCH_REQUIRE_SIGNED_UPDATES=1` to require `git verify-commit` signature verification before `perform_update()` fast-forwards any node.
- **Documentation Clarity (`README.md`):** Added a **Feature Maturity (What Works Today)** table (Core/Stable vs Beta/Experimental) and inspect-before-run pinned-tag installation instructions.

## [0.25.2] - 2026-11-25

### Security Hardening (DNS Rebinding, Nonce Replay & Body Binding)
- **Host-Header DNS Rebinding Guard (`backend/shared/security.py`, `backend/router/app.py`, `backend/agent/main.py`):** Added `is_allowed_host_header()` middleware returning `421 Misdirected Request` on untrusted `Host` headers while allowing loopback, raw IPv4/IPv6 literals, and `SPLITCH_ALLOWED_HOSTS` / `public_url` / `allowed_origins`.
- **Single-Use Nonce Replay Cache & Ed25519 Body Binding (`backend/shared/security.py`, `backend/shared/secrets.py`, `chat_api.py`, `swarm.py`, `telemetry.py`):** Added bounded TTL `NonceReplayCache` for HMAC/Ed25519 tool approvals (`X-Splitch-Approval-Nonce`) and Ed25519 agent requests (`X-Splitch-Auth-Nonce` + `X-Splitch-Auth-Body-Sha256`), with rolling-upgrade legacy fallback.
- **Test Shim Isolation (`backend/shared/security.py`):** Gated `"testclient"` in `is_loopback_host()` behind `PYTEST_CURRENT_TEST`.

## [0.25.1] - 2026-11-25

### Models Tab Layout & Engine Badges
- **Node Models First (`dashboard/src/components/ModelsTab.tsx`):** Moved **Node Models** above **Hugging Face** search and added an **Engine** column (`llama.cpp`, `MLX`, `Strata`, or `llama.cpp / Strata`) to each model row.

## [0.25.0] - 2026-11-25

### Hugging Face `hf_xet` Download Protocol + Automatic HTTP Fallback
- **`huggingface-hub[hf_xet]` Integration (`pyproject.toml`, `backend/agent/main.py`):** Added `hf_xet` parallel chunk-deduplicating downloads for GGUF, Media, and MLX (`snapshot_download`), with live byte/speed progress via `_make_job_tqdm` and automatic fallback to async `httpx` streaming (`SPLITCH_HF_PROTOCOL=auto|xet|http`).
- **Protocol Badge (`backend/shared/models.py`, `dashboard/src/types.ts`, `ModelsTab.tsx`, `MediaTab.tsx`):** Added `protocol` (`XET` / `HTTP`) to `DownloadJob` and displayed it alongside live `MB/s` in the Downloads dock and Media Studio.

## [0.24.0] - 2026-11-25

### Live Model Download Speeds (`MB/s` / `KB/s`)
- **Agent Speed Tracker (`backend/shared/models.py`, `backend/agent/main.py`):** Added `speed_bytes_per_sec` to `DownloadJob` and EMA-smoothed `_DownloadSpeedTracker` across `_run_hf_download` (GGUF/Media) and `_run_hf_mlx_download` (MLX).
- **Dashboard Progress UI (`dashboard/src/types.ts`, `ModelsTab.tsx`, `MediaTab.tsx`):** Displays live transfer rates (`MB/s` / `KB/s`) in the expanded Downloads dock, collapsed summary badge, and Media Studio progress rows.

## [0.23.5] - 2026-11-25

### Settings CA Bundle Download, Windows Doctor Fix & Chromium E2E Hardening
- **Settings TLS CA Bundle (`backend/router/settings_api.py`, `dashboard/src/components/settings/SecuritySystemPane.tsx`):** `POST /api/system/tls/self-signed` now mirrors the public cert into `cluster-ca.pem`, and added `GET /api/system/tls/ca-bundle` + **Download Cluster CA (.pem)** button in **Settings → Security & System**.
- **Windows `OSError` Guard (`backend/shared/engines.py`):** `_run_cmd()` now catches `OSError` (e.g. `[WinError 193]`) cleanly so invalid binaries never crash `splitch doctor` on Windows.
- **Headless Chromium CI Hardening (`tests/test_ui_e2e.py`):** Set `DBUS_SESSION_BUS_ADDRESS=unix:path=/dev/null`, added headless stability flags, and raised CDP startup timeout to 35s.

## [0.23.4] - 2026-11-25

### Doctor & CLI Desktop CA Bundle Verification
- **`splitch doctor` & `--fix` (`backend/shared/updater.py`):** Added `desktop_router` health/TLS diagnostics (`cluster-ca.pem` / `ca.crt` / `splitch-selfsigned.crt` / `SPLITCH_CA_BUNDLE`) and automatic `http://` → `https://` upgrade of `desktop.json` via `splitch doctor --fix`.
- **`splitch desktop sync-icon` (`backend/cli.py`):** Wired `_resolve_ca_bundle()` and `http://` → `https://` fallback into remote `/api/theme` fetches.

## [0.23.3] - 2026-11-25

### Tray & Launcher Cluster CA Auto-Discovery + HTTPS Upgrade
- **Cluster CA Bundle Lookup (`backend/desktop/tray.py`, `backend/desktop/shortcuts.py`):** Added `cluster-ca.pem`, `ca.crt`, and `splitch-selfsigned.crt` fallback lookup under `<data_root>/certs/` when `SPLITCH_CA_BUNDLE` is unset, plus `http://` → `https://` auto-upgrade in `ensure_splitch_running()` so app launcher, desktop icon, and tray all connect seamlessly to HTTPS routers.

## [0.23.2] - 2026-11-25

### System Tray Menu Icons, Deterministic IDs & HTTPS Auto-Upgrade
- **Distinct Web Launchers & Menu Icons (`backend/desktop/tray.py`, `backend/desktop/icons.py`, `backend/desktop/_sni_dbus.py`):** Split **Open Web App** (app mode) and **Open in Browser** (default browser) with distinct icons (`web-app` Splitch badge vs `web-browser` globe) and added pure-Python 24×24 PNG `icon-data` + freedesktop `icon-name` across all actionable tray menu items.
- **Deterministic Menu IDs:** Replaced sequential `next_id` numbering with `_stable_menu_id(key)` so dynamic status rows never shift IDs, duplicate rows, or leak checkboxes onto unrelated items in `com.canonical.dbusmenu`.
- **TLS CA Bundle & HTTP→HTTPS Auto-Upgrade (`backend/desktop/tray.py`, `backend/desktop/shortcuts.py`):** Wired `SPLITCH_CA_BUNDLE` / `<data_root>/certs/ca.crt` into tray `httpx` and `wss://` WebSocket connections, auto-upgraded `http://` endpoints to `https://` when routers enforce TLS, and surfaced `TLS Cert Untrusted` diagnostics instead of reporting `Offline`.

## [0.23.1] - 2026-11-25

### Open Web GitHub Repository Search
- **GitHub Repo Search in Open Web (`backend/router/web_search.py`):** Added zero-key `api.github.com/search/repositories` search alongside DDG Instant Answer, Wikipedia, and Hacker News (`Open Web (DDG IA + GitHub + Wikipedia + HN)`).

## [0.23.0] - 2026-11-25

### Skill Awareness, `suggest_skill` Tool & Deep-Link Cards
- **Skill Awareness in System Prompt (`backend/router/skills_api.py`, `chat_api.py`):** Scans installed `SKILL.md` folders via `scan_installed_skills()`, summarizes installed-but-inactive skills in the chat system prompt, and instructs the agent to use `suggest_skill(query, reason)`.
- **`suggest_skill` Discovery Tool (`backend/router/chat_store.py`, `skills_api.py`, `chat_api.py`):** Searches locally installed skills (`enabled_in_chat` status) and `skills.sh` via shared `search_skills_registry_items()`.
- **Interactive Skill Recommendation Card & Deep-Link (`dashboard/src/components/chat/ChatMessageList.tsx`, `ChatTab.tsx`, `SkillsTab.tsx`, `App.tsx`):** Unified recommendation card rendering 1-click **Enable in Chat** (for installed skills) and **Install in Skills Tab** / **Open in Skills Tab** buttons that switch straight to **Skills Library** and pre-run the `skills.sh` search.

## [0.22.0] - 2026-11-25

### MCP Awareness, Recommendations & Deep-Link Navigation
- **Explicit MCP System-Prompt Awareness (`backend/router/chat_api.py`):** Summarizes enabled MCP servers (with discovered `mcp__*` tool names) and installed-but-inactive MCP servers in the chat system prompt.
- **`suggest_mcp` Discovery Tool (`backend/router/chat_store.py`, `mcp_api.py`, `chat_api.py`):** Searches both locally installed MCPs and the live MCP Registry (`registry.modelcontextprotocol.io` + npm) via shared `search_mcp_registry_items()`.
- **Interactive MCP Recommendation Card & Deep-Link (`dashboard/src/components/chat/ChatMessageList.tsx`, `ChatTab.tsx`, `McpTab.tsx`, `App.tsx`):** Renders 1-click **Enable in Chat** (for installed-but-inactive MCPs) and **Install in MCP Tab** / **Open in MCP Tab** buttons that switch directly to **MCP Servers** and pre-run the registry search.

## [0.21.1] - 2026-11-25

### Router-to-Agent HTTPS CA Bundle Fix
- **Unified `cluster.httpx_verify` (`backend/router/telemetry.py`, `chat_api.py`, `workspace_api.py`, `app.py`, `models_api.py`, `strata_api.py`, `media_api.py`, `settings_api.py`):** Wired `ClusterState.httpx_verify` (`SPLITCH_CA_BUNDLE` / `tls.ca_bundle`) to every `httpx.AsyncClient` calling `node.agent_url`, fixing `[SSL: CERTIFICATE_VERIFY_FAILED]` on chat tool execution, workspace git/file browsing, model inventory/pull, Strata, and media controls.

## [0.21.0] - 2026-11-25

### Multi-Agent Swarm Orchestration (Phase 1)
- **Router-Native Swarm Engine (`backend/router/swarm.py`, `chat_store.py`, `chat_api.py`):** Added `SwarmManager` with `spawn_agent`, `wait_agents`, `list_agents`, and `cancel_agent` coordinator tools, SQLite WAL persistence (`swarm_runs`, `swarm_agents`, `swarm_agent_messages`), `asyncio.Semaphore(max_parallel_agents)` concurrency cap, `readonly=True` write/shell tool enforcement, unified HITL approval bubbling, and multi-node routing via `resolve_route()`.
- **Swarm Chat UI & Inspector (`dashboard/src/components/chat/SwarmCard.tsx`, `SwarmInspectorPane.tsx`, `ChatTab.tsx`, `ChatMessageList.tsx`):** Added per-conversation `Swarm` toolbar toggle (auto-enabled when >1 node is configured), inline Swarm Run Card with live worker status/step/phase + inline approvals/cancel, and right-pane Swarm Inspector showing worker roster and live streaming transcripts.
- **Settings Integration (`backend/router/config.py`, `settings_api.py`, `AgentChatSettingsPane.tsx`):** Added `Max Parallel Swarm Agents` (`1–6`, default `2`) to **Settings -> Agent & Chat**.

## [0.20.1] - 2026-11-25

### Compaction Performance & New-Tab Agent Links
- **Faster Auto-Compaction (`backend/router/chat_api.py`):** Routes compaction to `"heavy"` (fastest online GPU node) before falling back to `"fast"` / `conv.model`, disables thinking tokens (`enable_thinking: False`), and trims compaction payloads (`14,000` chars max, `1,000` chars/message, `max_tokens: 500`).
- **Agent Links Open in New Tab (`dashboard/src/components/ChatTab.tsx`, `dashboard/src/components/chat/ChatMessageList.tsx`):** Added `target="_blank" rel="noopener noreferrer"` to Markdown `<a>` links in both chat messages and compacted summaries.

## [0.20.0] - 2026-11-25

### HTTPS-Only Enforcement & Web Fetch HTTPS Preference
- **Native TLS by Default (`backend/router/config.py`, `backend/shared/tls.py`, `backend/cli.py`, `splitch.example.toml`):** Enabled `TlsSettings.enabled` and `TlsSettings.self_signed` by default so `splitch up`, `splitch router`, and `splitch agent` automatically serve HTTPS with SAN-equipped ECDSA certs unless `--behind-proxy` / `tls.behind_proxy = true` is set for an external reverse proxy.
- **Non-Loopback HTTPS Enforcement (`backend/router/app.py`, `backend/router/provision_api.py`, `backend/router/settings_api.py`):** Rejects plain `http://` for non-loopback `agent_url`, `router_url`, and `public_url` (while preserving loopback `http://127.0.0.1` for local inference engines).
- **Web Fetch HTTPS Preference (`backend/router/web_search.py`):** `execute_web_fetch` tries `https://` first for `http://` URLs and falls back to `http://` only if `https://` fails.

## [0.19.0] - 2026-11-25

### Live Agent Status Bar & Max-Steps Notice
- **Live Agent Status Bar (`dashboard/src/components/chat/ChatComposer.tsx`, `ChatTab.tsx`, `index.css`):** Added a live status bar above the composer while streaming showing current step (`Step N/M`), active phase (`Routing…`, `Thinking…`, `Generating response…`, `Running <tool>…`, `Waiting for approval: <tool>`), and active node/model.
- **Max Agent Steps Notice & Continue Action (`backend/router/chat_api.py`, `dashboard/src/components/chat/ChatMessageList.tsx`):** Emits `round`/`max_rounds` in SSE `meta`, persists `max_rounds_reached`, `steps_used`, and `max_steps` on the final assistant message when `max_agent_steps` is reached, and renders a warning badge + one-click **Continue** button on the message.

## [0.18.4] - 2026-11-25

### Reasoning Effort Compatibility & Fallback
- **Automatic `reasoning_effort` Fallback (`backend/router/chat_api.py`):** Added `_adjust_reasoning_effort_on_error()` + one-shot retry when an upstream `llama-server` Jinja chat template rejects `reasoning_effort` (`high` -> `xhigh`, `xhigh` -> `high`, `none` -> `off`, or strips unsupported values) instead of aborting the stream with HTTP 500.
- **`xhigh` Reasoning Effort Support (`backend/shared/models.py`, `dashboard/src/components/chat/ChatComposer.tsx`):** Added `xhigh` (`Extra High`) to `ChatSendRequest.reasoning_effort` and the composer Reasoning Effort selector.

## [0.18.3] - 2026-11-25

### CI & Cross-Platform Fixes (macOS, Windows & Headless Chrome)
- **PWA Static Fallback (`backend/router/app.py`):** Fallback to `dashboard/public/` for `/manifest.webmanifest`, `/sw.js`, `/favicon.svg`, and `/icons/*` when `dashboard/dist/` is not pre-built (cross-platform CI runners).
- **macOS App-Mode Precedence (`backend/desktop/shortcuts.py`):** Prefer user-local `~/Applications` before `/Applications` when probing macOS Chromium `.app` bundles.
- **Windows LF Preservation (`backend/agent/main.py`):** Write `AGENTS.md` with explicit `newline="\n"`.
- **Headless Chrome UI E2E (`.github/workflows/ci.yml`, `tests/test_ui_e2e.py`):** Use `browser-actions/setup-chrome@v1` instead of Ubuntu snap `chromium-browser` and strip `DBUS_SESSION_BUS_ADDRESS` from the Chromium subprocess environment.

## [0.18.2] - 2026-11-25

### Docs, Wiki & Preflight Parity Gate
- **Preflight Docs & Examples Gate (`scripts/preflight.sh`):** Added automated checks validating `splitch.example.toml` against `SplitchConfig`, verifying `AGENTS.md` and `CHANGELOG.md` version parity, and checking internal `README.md`/`docs/wiki/` links.
- **Full Documentation & Example Sync:** Updated `CHANGELOG.md`, `splitch.example.toml`, and `docs/wiki/` (`Agent-Chat-Guide.md`, `API-Reference.md`, `Configuration-and-Routing.md`, `Models-MCP-and-Skills.md`) for `v0.14.0`–`v0.18.2`.

## [0.18.1] - 2026-11-25
- Removed all pill-shaped (`border-radius: 999px`) UI rules in `dashboard/src/index.css` in favor of sharp `4px`/`6px` corners and added a `preflight.sh` guard banning `999px`/`9999px`/`rounded-full`.

## [0.18.0] - 2026-11-25
- **Antigravity-Inspired Agent & Chat Settings (`AgentChatSettingsPane.tsx`, `chat_api.py`, `workspace_api.py`):** Added **Agent & Chat** settings tab with tiered `approval_policy` (`ask`/`auto_edits`/`turbo_safe`/`full_auto`), `shell_allowlist`/`shell_denylist` (denylist always prompts), `max_agent_steps`, `auto_compact_threshold` slider, `thinking_display` (`collapsed`/`expanded`/`hidden`), background `completion_notifications`, `GET/PUT /api/chat/rules` (global rules + named personas), and workspace `AGENTS.md` viewer/editor (`GET/PUT /api/nodes/{id}/workspace/agents-md`).

## [0.17.0] - 2026-11-25
- **Settings & Sampling Overhaul (`settings_api.py`, `ChatComposer.tsx`, `NodesProvisionPane.tsx`, `SecuritySystemPane.tsx`):** Added write-only Vault secret editors (`HF_TOKEN`, `brave_api_key`, `tavily_api_key`), `web_search_provider` selector, live `PATCH /api/nodes/{id}` node editor, `GET/PATCH /api/desktop/config`, self-signed TLS generator (`POST /api/system/tls/self-signed`), **Sampling & Reasoning** popover (`Coding`/`Balanced`/`Creative`/`Reasoning` presets + `top_p`/`top_k`/`min_p`/`repeat_penalty`/`max_tokens`/`seed`/`stop`/`reasoning_effort`/`enable_thinking`), `llama-server` runtime drop-in tuning (`/engine/runtime-config`), and safe cache cleanup (`/files/storage/clean-cache`).

## [0.16.0] - 2026-11-25
- **Dynamic Hugging Face Strata Search (`models_api.py`, `ModelsTab.tsx`, `StrataPane.tsx`):** Replaced hardcoded UI catalog with live `GET /api/models/search?format=strata` (`filter=strata`) + `.strata`/`.gguf` file inspection, VRAM+RAM+SSD streaming fit budget, and `.strata` pull support.

## [0.15.0] - 2026-11-25
- Expanded backend `STRATA_CATALOG` with 10 larger official packs (`Gyro-S/M`, `Unsloth UD-IQ4_XS`–`Q8_0`, `NVFP4`, `Hy4-Flash SQ-96/128`) up to 188 GB.

## [0.14.0] - 2026-11-25
- **PWA & Chromeless App-Mode Launcher (`manifest.webmanifest`, `sw.js`, `shortcuts.py`, `cli.py`):** Added installable PWA manifest + pass-through service worker and `open_url_in_app_mode()` (`firefoxpwa` → Chromium `--app=` → browser fallback) with `splitch launch --app | --browser`.

## [0.13.1] - 2026-11-25
- Added multi-GPU `yield_gpu_indices`, `media_url`, `mlx-lm`, `Strata`, and `image`/`video` alias examples to `splitch.example.toml` and `Configuration-and-Routing.md`.

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
