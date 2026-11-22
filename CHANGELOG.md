# Changelog

All notable changes to **Splitch** are documented in this file.
This project adheres to [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html), enforced automatically across `backend/version.py`, `pyproject.toml`, and `dashboard/package.json` by `./scripts/preflight.sh` and GitHub Actions CI.

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
