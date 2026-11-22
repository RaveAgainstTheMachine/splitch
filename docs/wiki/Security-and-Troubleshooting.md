# Security & Troubleshooting

---

## 1. Security Model & Defense in Depth

Splitch is designed for trusted LAN / Tailscale home and studio networks while maintaining strict guardrails around code execution, web fetches, and generated previews:

### Workspace Tools (Opt-In Only)
- **Disabled by default:** Every `splitch-agent` boots with `SPLITCH_ENABLE_TOOLS=0`. Both `/tools/execute` and `/workspace/dirs` return HTTP `403` until you explicitly opt in (`--enable-tools` or `SPLITCH_ENABLE_TOOLS=1`).
- **Hard path sandboxing:** Every path argument is resolved via `Path.resolve()` and verified with `Path.is_relative_to(SPLITCH_WORKSPACE_ROOT)`. Symlink escapes and `../` traversal attempts are rejected.
- **Human-in-the-Loop (HITL) approvals:** Read-only tools (`list_dir`, `read_file`, `grep_search`) run immediately; mutating tools (`write_file`, `run_command`) pause for explicit inline **Allow / Deny** confirmation unless you enable **Auto-Approve** on that conversation.

### Bearer Authentication, Rate Limiting & Web UI Session Login
When exposing Splitch beyond `127.0.0.1` (shared LAN, Tailscale, or reverse proxy):
1. Set `SPLITCH_API_KEY=<strong-random-secret>` on `splitch-router`.
   - Protects `/v1/*`, `/api/*`, `/infill` via constant-time `hmac.compare_digest` on `Authorization: Bearer`, `X-API-Key`, or the `HttpOnly`, `SameSite=Strict` (and `Secure` over HTTPS/proxy) HMAC-SHA256 `splitch_session` cookie issued by `POST /api/auth/login`.
   - `POST /api/auth/login` enforces per-IP sliding-window brute-force rate limiting (default: 10 failed attempts / 60s → HTTP `429` + `Retry-After`).
   - The Web UI automatically displays a mandatory **Unlock Splitch** `<dialog>` modal (`closedby="none"`) when `SPLITCH_API_KEY` is active and provides a topbar **Lock** button (`POST /api/auth/logout`).
   - Protects `/ws/status` via either the `splitch_session` cookie or `?token=<key>`.
2. Set `SPLITCH_AGENT_TOKEN=<strong-random-secret>` identically on `splitch-router` and every `splitch-agent`.
   - Authenticates every router ↔ agent telemetry, model pull, managed `sd-server` lifecycle, sync, and tool execution call.
3. **HTTPS & External Reverse Proxies:** See **[HTTPS, TLS & External Reverse Proxies](HTTPS-and-Reverse-Proxies.md)** for direct self-signed / custom CA / Let's Encrypt HTTPS (`splitch tls init`, `splitch tls letsencrypt`) and production configs for Nginx, Nginx Proxy Manager, Caddy, Apache, HAProxy, and Traefik.

### Strict HTTP Headers, Local-First MCP & Hardened File Permissions
- **External MCP Data Egress Advisories & HITL Gate:** Every MCP server is classified into `local` (`stdio` without external cloud tokens or LAN-only HTTP/SSE), `external_api` (`stdio` with cloud tokens), or `remote_mcp` (non-LAN URL). Any tool call to a non-local MCP server pauses for explicit **Allow / Deny** approval unless **Auto-Approve** is enabled on that chat.
- **Hardened File Permissions & Confinement:** Private TLS keys, SQLite chat history (`chats.db`), state/node registries (`.splitch_state.json`, `nodes.json`), MCP configs (`mcp_servers.json`), and Media Studio job logs/artifacts are written with `0600` POSIX permissions; generated Python MCP scripts use `0700`; `/sync/skills` enforces strict `$HOME` path confinement; `POST /api/nodes` validates `http://`/`https://` URLs only.
- **Strict Security Headers:** Router responses emit `Content-Security-Policy` (`script-src 'self'`, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, and automatic `Strict-Transport-Security` (HSTS) on HTTPS requests. Agent responses emit `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Cache-Control: no-store`.

### Sandboxed HTML/JS/SVG Preview Isolation
Generated previews in `ArtifactPreview.tsx` cannot access your Splitch session, `localStorage`, or LAN APIs:
- Rendered inside `<iframe sandbox="allow-scripts">` **without** `allow-same-origin` (unique opaque origin).
- Strict injected CSP `<meta>` header sets `default-src 'none'; connect-src 'none';` (blocks `fetch`, `XMLHttpRequest`, and `WebSocket` exfiltration).
- `referrerPolicy="no-referrer"` and `allow="camera 'none'; microphone 'none'; geolocation 'none'; focus-without-user-activation 'none'"`.
- Console bridge validates incoming `postMessage` frames via opaque origin pinning (`Origin.from(event)`) + `event.source === iframe.contentWindow`.

### Web Search & SSRF Guard
`web_fetch` in `backend/router/web_search.py` validates schemes (`http`/`https` only) and resolves hostnames before connecting, blocking loopback (`127.0.0.0/8`, `::1`), RFC1918 private LANs (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local (`169.254.0.0/16`), and cloud metadata endpoints.

### Read-Only Remote System Diagnostics
`GET /api/system/doctor` and `GET /api/system/update-status` are intentionally read-only. Self-updating (`splitch update`, `install.sh`, `install.ps1`) is local-CLI-only so no remote HTTP caller can trigger code pulls, package installations, or service restarts over the network.

---

## 2. Troubleshooting Recipes

### Start Here: Run `splitch doctor`
Before digging through logs, run:
```bash
splitch doctor
```
Or query `GET http://<ROUTER_IP>:8080/api/system/doctor`. It immediately flags missing dependencies, unreachable local engines, unmounted storage, missing dashboard builds, and security configuration.

### `splitch: command not found` After Running `install.sh`
Add `~/.local/bin` to your shell `PATH`:
```bash
export PATH="$HOME/.local/bin:$PATH"
```
Append that line to `~/.bashrc` or `~/.zshrc` and restart your shell. On Windows, open a new PowerShell window so the updated User `PATH` (`%LOCALAPPDATA%\splitch\bin`) takes effect.

### `splitch update` Says Working Tree Has Uncommitted Changes
`splitch update` refuses to overwrite local modifications by default. Either stash/commit your local changes (`git stash`) or pass `--force`:
```bash
splitch update --force
```
For non-git installations (e.g., extracted release archives), update by re-running `install.sh` / `install.ps1`.

### Node Shows `OFFLINE` in Overview
1. Verify the agent is running on the remote host:
   ```bash
   curl http://<NODE_IP>:8090/health
   ```
2. Check firewall rules (allow TCP `8090` and your engine port, e.g., `8081` or `11434`, from the router host).
3. If `SPLITCH_AGENT_TOKEN` is set, confirm the exact same token is set on both router and agent.

### Node Shows `ONLINE` but `ENGINE_UNHEALTHY` or Empty Models
- Confirm your inference backend is bound to `0.0.0.0` (not only `127.0.0.1`) if the router is on a separate host, because inference requests stream directly from `splitch-router` to `node.engine_url`.
- Test directly from the router host:
  ```bash
  curl http://<NODE_IP>:8081/v1/models
  ```
- For Ollama, make sure `OLLAMA_HOST=0.0.0.0:11434` is set in the Ollama service environment.

### GPU Node Keeps Yielding Unexpectedly
- Inspect live `non_llm_vram_mb` and `gpu_util_percent` on the node card in **Overview**.
- Desktop compositors, browsers with hardware acceleration, or background video tools can consume 2–4 GB VRAM. Raise `non_llm_vram_threshold_mb` in `splitch.toml` (e.g., from `4096` to `6144`).
- If `storage_mounted` is `false`, verify the external drive / NFS mount at `SPLITCH_STORAGE_PATH` is mounted and readable by the agent user.

### Agent Chat Says "Tools Disabled on Target Node"
- Start the target node's agent with `--enable-tools --workspace /path/to/projects` (or set `SPLITCH_ENABLE_TOOLS=1` and `SPLITCH_WORKSPACE_ROOT=/path/to/projects` in its `systemd` / `launchd` unit), then restart the agent.

### Context Window Fills Quickly or Compaction Doesn't Trigger
- Check that the **Auto-Compact** checkbox next to the bottom Context Ring is checked.
- Auto-Compact triggers automatically when active context crosses **80%** and there are more than 7 uncompacted messages. You can click **Compact** manually anytime once a thread has at least 6 messages.
- For smoother background compaction on multi-node setups, define a `fast` alias so Splitch uses your fast standby model to summarize older turns instead of stalling a large coding model.

### Web Search Returns Limited Results
- By default Splitch uses zero-key open APIs (DuckDuckGo Instant Answer, Wikipedia, Hacker News Algolia), which avoid CAPTCHAs but are narrower than full web indexes.
- For full web search quality, point `SPLITCH_SEARXNG_URL` at a self-hosted SearXNG instance (with `json` format enabled in `settings.yml`) or set `BRAVE_API_KEY` / `TAVILY_API_KEY`.

### Dashboard Shows Blank Page or Old Assets After Pull
Rebuild the Vite bundle on the router host:
```bash
(cd dashboard && npm install && npm run build)
```
Then hard-refresh your browser (`Ctrl+Shift+R` / `Cmd+Shift+R`).

---

Back to **[Wiki Home](Home.md)**
