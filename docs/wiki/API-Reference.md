# API Reference

All Router endpoints are served by `splitch-router` (default `:8080`). When `SPLITCH_API_KEY` is configured, include `Authorization: Bearer <key>` (or `X-API-Key: <key>`) on `/v1/*`, `/api/*`, and `/infill`, and pass `?token=<key>` on `/ws/status`.

---

## 1. OpenAI, Responses, Anthropic & FIM Inference API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/v1/models` | Lists `auto`, all logical aliases, and `<node>/<model>` targets across online nodes. |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions (`stream: true` or JSON) routed through Splitch policy. |
| `POST` | `/v1/responses` | OpenAI Responses API passthrough (Codex CLI, OpenAI Agents SDK) routed through Splitch policy. |
| `POST` | `/v1/messages` | Anthropic Messages API passthrough (Claude Code, Anthropic SDK) forwarding `anthropic-version` / `anthropic-beta` headers. |
| `POST` | `/v1/messages/count_tokens` | Anthropic token-counting passthrough for Claude Code and Anthropic SDKs. |
| `POST` | `/v1/completions` | Legacy text completions proxied through Splitch policy. |
| `POST` | `/v1/embeddings` | Embeddings requests (defaults to `embed` alias when `model: "auto"`). |
| `POST` | `/infill` | `llama.cpp`-compatible Fill-in-the-Middle (FIM) completion endpoint. |

```bash
curl http://localhost:8080/v1/models
```

---

## 2. Cluster Health, Control & Dynamic Nodes

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Unauthenticated liveness probe returning `version`, `online_nodes`, `gaming_mode`, `force_node`. |
| `GET` | `/api/auth/status` | Check whether `SPLITCH_API_KEY` authentication is required and if the session is authenticated. |
| `POST` | `/api/auth/login` | Validate `{"api_key": "..."}` and set an `HttpOnly`, `SameSite=Strict` session cookie for the Web UI. |
| `POST` | `/api/auth/logout` | Clear the Web UI session cookie. |
| `GET` | `/api/status` | Full `DashboardSnapshot` (control state, node telemetry, aliases, recent routing traces). |
| `GET` | `/api/system/doctor?check_updates=false` | Structured environment diagnostics (Python, `uv`, Node, GPU, engine binary, local engines, SQLite, auth). |
| `GET` | `/api/system/engine?check_remote=false` | Hardware accelerator detection, installed engine binaries/build tags, and matched upstream `llama.cpp` release asset. |
| `GET` | `/api/system/update-status?fetch=false` | Current version, git branch/SHA, dirty status, and upstream commit diff (`fetch=true` runs `git fetch`). |
| `POST` | `/api/control` | Update `gaming_mode` (`bool`) and/or `force_node` (`"auto"` or node ID). |
| `POST` | `/api/nodes` | Dynamically register or update a compute node (`NodeRegisterRequest`). |
| `DELETE` | `/api/nodes/{node_id}` | Remove a compute node from the cluster registry. |
| `POST` | `/api/nodes/{node_id}/models/{action}` | Load or unload a model (`action` = `load` \| `unload`, body `{"model": "..."}`). |
| `GET` | `/api/nodes/{node_id}/dirs?subpath=...` | Browse sandboxed subdirectories inside a node's `SPLITCH_WORKSPACE_ROOT`. |
| `WS` | `/ws/status` | Real-time push stream of `DashboardSnapshot` JSON frames on telemetry/control updates. |

```bash
# Register a node over HTTP
curl -X POST http://localhost:8080/api/nodes \
  -H "Content-Type: application/json" \
  -d '{
    "id": "gpu-rig",
    "display_name": "GPU Workstation",
    "role": "gpu",
    "yield_on_gaming": true,
    "agent_url": "http://192.168.1.50:8090",
    "engine_url": "http://192.168.1.50:8081"
  }'
```

---

## 3. Agent Chat API (`/api/chat`)

### Conversations, Pinning, Bulk Actions & Trash

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/chat/conversations` | List active conversations (`ORDER BY pinned DESC, updated_at DESC`). |
| `GET` | `/api/chat/scopes` | Get Global (`global_scope`) and Per-Project Workspace (`workspaces`) default Skills & MCPs. |
| `PUT` | `/api/chat/scopes/global` | Update global default Skills & MCPs (`{"skills": [...], "mcps": [...]}`). |
| `PUT` | `/api/chat/scopes/workspace` | Update per-workspace project Skills & MCPs (`{"workspace": "...", "skills": [...], "mcps": [...]}`). |
| `POST` | `/api/chat/conversations` | Create a new conversation (`ConversationCreate`). |
| `PATCH` | `/api/chat/conversations/{conv_id}` | Update title, model, exec_node, workspace, toggles, `auto_compact`, `pinned`, skills, MCPs, system prompt. |
| `DELETE` | `/api/chat/conversations/{conv_id}` | Soft-delete conversation into Trash bin (`deleted_at` set). |
| `POST` | `/api/chat/conversations/bulk-delete` | Soft-delete multiple conversations (`{"ids": [...]}`). |
| `POST` | `/api/chat/conversations/bulk-restore` | Restore multiple trashed conversations (`{"ids": [...]}`). |
| `POST` | `/api/chat/conversations/bulk-permanent-delete` | Permanently erase multiple conversations (`{"ids": [...]}`). |
| `GET` | `/api/chat/trash` | List soft-deleted conversations in Trash. |
| `POST` | `/api/chat/conversations/{conv_id}/restore` | Restore a single conversation from Trash. |
| `DELETE` | `/api/chat/conversations/{conv_id}/permanent` | Permanently delete a conversation and its messages. |
| `DELETE` | `/api/chat/trash` | Permanently empty all conversations in Trash. |

### Messages, Compaction, Streaming & Approvals

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/chat/conversations/{conv_id}/messages` | List all messages in chronological order (includes compacted history). |
| `DELETE` | `/api/chat/messages/{message_id}` | Trim/branch conversation from a message onward (auto-invalidates stale compaction). |
| `POST` | `/api/chat/conversations/{conv_id}/compact?keep_recent=4` | Manually compact older turns non-destructively (`CompactResponse`). |
| `DELETE` | `/api/chat/conversations/{conv_id}/compact` | Clear compacted summary and restore full-history sending. |
| `POST` | `/api/chat/conversations/{conv_id}/send` | Send user message (`content`, optional `images[]`, `temperature`) and stream SSE events. |
| `POST` | `/api/chat/approvals/{approval_id}` | Resolve a pending HITL tool approval (`{"approved": true | false}`). |

#### SSE Events Emitted by `POST /api/chat/conversations/{conv_id}/send`
Each event is formatted as `event: <name>\ndata: <json>\n\n`:
- `user_message` — persisted user message record
- `compacting` / `compacted` — auto-compaction started / completed (includes token counts & summary)
- `route` — resolved node, model, and optional fallback reason
- `reasoning` — incremental `reasoning_content` token delta
- `token` — incremental assistant content token delta
- `tool_call` — tool invocation request (`id`, `name`, `arguments`)
- `approval_required` — pauses mutating tool awaiting `POST /api/chat/approvals/{approval_id}`
- `tool_result` — tool execution output, exit code, and optional unified diff
- `done` — final persisted assistant message + real token usage & speed metrics
- `error` — error detail if inference failed

```bash
# Pin a conversation to the top
curl -X PATCH http://localhost:8080/api/chat/conversations/<conv_id> \
  -H "Content-Type: application/json" \
  -d '{"pinned": true}'

# Trigger manual non-destructive compaction
curl -X POST "http://localhost:8080/api/chat/conversations/<conv_id>/compact?keep_recent=4"
```

---

## 4. Models & Aliases API (`/api`)

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/models/search?q=...&sort=downloads&direction=-1` | Search Hugging Face GGUF repositories (with `parameters_b`, `architecture`, `context_length`, `est_min_gb`, `est_q4_gb`, and `fits_nodes`). |
| `GET` | `/api/models/details?repo_id=...` | Fetch cleaned Hugging Face `README.md` description, `base_model`, `architecture`, `context_length`, `parameters_b`, and per-node/multi-GPU GGUF file list concurrently. |
| `GET` | `/api/models/files?repo_id=...` | List GGUF files in repo with per-node and multi-GPU hardware fit estimates. |
| `GET` | `/api/models/inventory` | Aggregate on-disk GGUF inventory and active download jobs across online nodes. |
| `POST` | `/api/models/pull` | Start streaming GGUF download on target node (`node_id`, `repo_id`, `filename`). |
| `DELETE` | `/api/models/{node_id}/{filename}` | Delete a managed GGUF file from a node (auto-refreshes `llama-server` index). |
| `GET` | `/api/aliases` | List all configured routing aliases. |
| `PUT` | `/api/aliases/{alias_name}` | Create or update an alias (hot-reloaded immediately). |
| `DELETE` | `/api/aliases/{alias_name}` | Delete an alias. |
| `GET` | `/api/theme` | Retrieve persisted UI appearance theme (`scheme`, `accent`, `surface`, `resolved_light`). |
| `PATCH` | `/api/theme` | Persist UI appearance theme, broadcast over `/ws/status`, and sync `/favicon.svg`, system tray, and desktop/menu icons. |
| `GET` | `/api/media/hf/search?q=...&kind=image\|video` | Search Hugging Face for `.gguf` and `.safetensors` image/video diffusion repositories. |
| `GET` | `/api/media/hf/files?repo_id=...` | List `.gguf` and `.safetensors` files with sizes inside a Hugging Face media repository. |

---

## 5. MCP Servers API (`/api/mcp`)

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/mcp/search?q=...&source=registry\|npm` | Live search across Official MCP Registry or npm. |
| `GET` | `/api/mcp` | List registered MCP servers. |
| `POST` | `/api/mcp` | Create or update an MCP server config. |
| `POST` | `/api/mcp/{server_id}/toggle` | Enable/disable an MCP server. |
| `DELETE` | `/api/mcp/{server_id}` | Delete an MCP server config. |
| `POST` | `/api/mcp/{server_id}/test` | Run live JSON-RPC `initialize` + `tools/list` probe. |
| `GET` | `/api/mcp/export/{client}` | Generate client config JSON (`opencode` or `zed`). |
| `POST` | `/api/mcp/import-json` | Bulk-import standard `mcpServers` JSON snippets. |
| `POST` | `/api/mcp/build` | Generate & register a custom zero-dependency Python stdio MCP server. |
| `GET` | `/api/mcp/{server_id}/script` | Retrieve source code of a generated custom MCP script. |
| `POST` | `/api/mcp/sync/{node_id}/{client}` | Push merged client config (and custom scripts) to an online node. |

---

## 6. Skills API (`/api/skills`)

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/skills/search?q=...` | Search community skills on `skills.sh`. |
| `POST` | `/api/skills/install-registry` | Install a skill directly from `skills.sh`. |
| `GET` | `/api/skills` | List local canonical skills (`SKILL.md`). |
| `POST` | `/api/skills` | Create or update a skill. |
| `POST` | `/api/skills/{skill_id}/toggle` | Enable/disable a skill. |
| `DELETE` | `/api/skills/{skill_id}` | Delete a skill from library. |
| `POST` | `/api/skills/import` | Import a `SKILL.md` from a GitHub URL. |
| `POST` | `/api/skills/sync` | Manifest-synced push of enabled skills to all online nodes. |

---

## 7. Node Agent API (`splitch-agent`, default `:8090`)

Authenticated with `SPLITCH_AGENT_TOKEN` when configured:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Node health, detected engine, and `tools_enabled` status. |
| `GET` | `/metrics` | Live `NodeTelemetry` (GPU/VRAM/non-LLM VRAM/RAM/CPU/disk/models). |
| `POST` | `/models/{action}` | Load or unload a model on the local engine (`llama-server` / Ollama). |
| `GET` | `/files/models` | List managed GGUF files and active pull progress. |
| `POST` | `/files/models/pull` | Download a Hugging Face `.gguf` or `.safetensors` file into `SPLITCH_STORAGE_PATH` (auto-refreshes `llama-server` index on `.gguf` completion). |
| `DELETE` | `/files/models/{filename}` | Delete a managed GGUF file (auto-refreshes `llama-server` index). |
| `POST` | `/media/engine/start` | Launch managed `sd-server` with single-file checkpoint or auto-wired multi-file DiT bundle (`model_file`, optional `vae`, `t5xxl`, `clip_l`, `llm`). |
| `POST` | `/media/video/generate` | Run native `sd-cli -M vid_gen` (Wan 2.1/2.2) and return WebM video bytes. |
| `POST` | `/sync/skills` | Apply manifest-tracked skills sync locally. |
| `POST` | `/sync/client-config` | Merge generated client config (`opencode`, `zed`, `hermes`, `codex`, `claude_code`, `aider`) into local user config path. |
| `POST` | `/sync/mcp-script` | Write a custom generated Python MCP script to local node. |
| `GET` | `/workspace/dirs` | List sandboxed subdirectories (requires `SPLITCH_ENABLE_TOOLS=1`). |
| `GET` | `/workspace/tree` | Lazy directory listing with file sizes (requires `SPLITCH_ENABLE_TOOLS=1`). |
| `GET` | `/workspace/file` | Read-only file viewer (256 KB cap, binary detection, language inference). |
| `GET` | `/workspace/git/status` | Git branch, ahead/behind, and staged/unstaged/untracked changes. |
| `GET` | `/workspace/git/diff` | Unified diff for staged or working-tree/untracked files. |
| `POST` | `/workspace/git/stage` | Stage selected workspace paths (`git add --`). |
| `POST` | `/workspace/git/unstage` | Unstage selected workspace paths (`git restore --staged --`). |
| `POST` | `/workspace/git/discard` | Discard working-tree changes or remove untracked files. |
| `POST` | `/workspace/git/commit` | Commit staged changes with message. |
| `POST` | `/system/update` | Signed self-update endpoint for worker node agents (`splitch-router` → `splitch-agent`). |
| `POST` | `/system/rollback` | Signed rollback endpoint for worker node agents. |
| `POST` | `/desktop/sync-theme` | Signed router → agent push to sync local desktop/app-menu/`.icns`/`.lnk` icons. |
| `GET` | `/strata/status` | Inspect node's managed Strata CLI install, sidecar `:8085` health, presets & active job. |
| `POST` | `/strata/install` | Background SHA-256-verified install/update of Strata CLI. |
| `POST` | `/strata/plan` | Run `strata launch --plan --output json` dry-run preview. |
| `POST` | `/strata/launch` | Launch/switch managed Strata model on loopback `:8085` (optional primary VRAM eviction). |
| `POST` | `/strata/calibrate` | Run exclusive-GPU Quick/Thorough Tune (`--tune quick\|thorough`). |
| `POST` | `/strata/stop` | Terminate active Strata job/server and release VRAM + RAM. |
| `ANY` | `/strata/proxy/{path}` | Signed zero-copy SSE streaming proxy to loopback `strata-server`. |
| `POST` | `/tools/execute` | Execute a sandboxed Agent Chat workspace tool (requires `SPLITCH_ENABLE_TOOLS=1`). |

---

## 8. Runtime Settings, Connect Clients & Node Provisioning

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/settings` | Read live runtime guards, router thresholds, search, TLS & security settings. |
| `PATCH` | `/api/settings` | Live-update and persist runtime settings to `SPLITCH_DATA_DIR/settings.json` (`0600`). |
| `POST` | `/api/clients/probe` | Live protocol verification against `/v1/models`, `/v1/chat/completions`, `/v1/responses`, or `/v1/messages`. |
| `POST` | `/api/clients/push` | Push generated client config (`hermes`, `claude_code`, `codex`, `opencode`, `aider`, `zed`) to an online node. |
| `GET` | `/install.sh` | Self-hosted Linux/macOS installer script (auth-exempt for LAN bootstrap). |
| `GET` | `/install.ps1` | Self-hosted Windows PowerShell installer script (auth-exempt for LAN bootstrap). |
| `POST` | `/api/provision/join-token` | Mint a single-use short-lived Join Token + ready-to-paste Linux/macOS & Windows bootstrap commands. |
| `GET` | `/api/provision/join-tokens` | List active unconsumed join tokens (prefix ID & expiry only; never exposes secret). |
| `DELETE` | `/api/provision/join-tokens/{id}` | Revoke an active join token. |
| `POST` | `/api/provision/ssh/test` | Preflight SSH connectivity, OS, and GPU check (`BatchMode=yes`, requires auth or `SPLITCH_ENABLE_REMOTE_PROVISION=1`). |
| `POST` | `/api/provision/ssh` | Stream SSE live `[1/7]`–`[7/7]` remote node install & auto-join over SSH (`BatchMode=yes`). |

---

## Unified Media Generation (Image & Video)

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/media/backends` | List nodes with detected `sd-server` (`:8082`), `ComfyUI` (`:8188`), or OpenAI image engines & models. |
| `GET` | `/api/media/nodes/{node_id}/engine` | Inspect node's managed `sd-server` install status, running state, local `.safetensors`/`.gguf` files, and active pulls. |
| `POST` | `/api/media/nodes/{node_id}/install` | 1-click background install of hardware-matched `stable-diffusion.cpp` (`sd-server`) on target node. |
| `POST` | `/api/media/nodes/{node_id}/start` | Launch supervised `sd-server` on target node (`:8082`) with selected local checkpoint. |
| `POST` | `/api/media/nodes/{node_id}/stop` | Stop managed `sd-server` on target node. |
| `POST` | `/api/media/nodes/{node_id}/pull` | Stream a Hugging Face `.safetensors` or `.gguf` diffusion checkpoint directly onto target node. |
| `POST` | `/api/media/generate` | Start an async image or video generation job (with automatic GPU LLM VRAM eviction). |
| `GET` | `/api/media/jobs` | List active and historical media generation jobs / gallery items. |
| `GET` | `/api/media/jobs/{job_id}` | Poll single job status and progress percentage. |
| `DELETE` | `/api/media/jobs/{job_id}` | Delete a job and its generated file from local storage. |
| `GET` | `/api/media/files/{filename}` | Serve generated PNG/JPEG/WebP/MP4/WebM artifacts safely. |
| `POST` | `/v1/images/generations` | OpenAI-compatible synchronous image generation (`b64_json` or `url`). |
| `POST` | `/v1/videos/generations` | Synchronous video generation via `ComfyUI` or video-enabled `sd-server`. |

---

## 9. Cluster Self-Update, Rollback & Workspace Git Explorer

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/system/update-cluster` | Update all online worker nodes first, roll back siblings on partial failure, then update router (requires auth, loopback, or `SPLITCH_ALLOW_UI_UPDATE=1`). |
| `POST` | `/api/system/rollback-cluster` | Roll back online worker nodes and router to their previous git revision. |
| `GET` | `/api/system/update-cluster/status` | Poll live per-node progress for the active cluster update/rollback job. |
| `GET` | `/api/nodes/{node_id}/workspace/tree` | Proxy lazy directory tree listing for a tools-enabled node. |
| `GET` | `/api/nodes/{node_id}/workspace/file` | Proxy read-only file content for a tools-enabled node. |
| `GET` | `/api/nodes/{node_id}/git/status` | Proxy Git repository status for a tools-enabled node. |
| `GET` | `/api/nodes/{node_id}/git/diff` | Proxy unified diff for a file (`staged=true\|false`). |
| `POST` | `/api/nodes/{node_id}/git/stage` | Stage selected paths on target node. |
| `POST` | `/api/nodes/{node_id}/git/unstage` | Unstage selected paths on target node. |
| `POST` | `/api/nodes/{node_id}/git/discard` | Discard uncommitted changes on target node. |
| `POST` | `/api/nodes/{node_id}/git/commit` | Commit staged changes on target node. |
| `POST` | `/api/nodes/{node_id}/git/generate-commit-message` | Summarize staged/working diff into a Conventional Commit message via `/v1/chat/completions`. |

---

## 10. Strata Sparse-MoE Control Plane

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/strata/catalog` | Curated Strata MoE catalog enriched with live 4-tier per-node fit assessments. |
| `GET` | `/api/strata/status` | Aggregate `NodeStrataStatus` across all online nodes. |
| `POST` | `/api/strata/nodes/{node_id}/install` | Trigger SHA-256-verified Strata CLI install/update on target node. |
| `POST` | `/api/strata/nodes/{node_id}/plan` | Dry-run `strata launch --plan` preview on target node. |
| `POST` | `/api/strata/nodes/{node_id}/launch` | Launch/switch a Strata MoE model on target node's loopback `:8085` sidecar. |
| `POST` | `/api/strata/nodes/{node_id}/calibrate` | Run 1-click Quick Tune (`--tune quick`) or Thorough Tune on target node. |
| `POST` | `/api/strata/nodes/{node_id}/stop` | Stop Strata sidecar or active calibration job on target node. |
| `POST` | `/api/strata/nodes/{node_id}/import-plan` | Preview importing local GGUF shards inside `SPLITCH_STORAGE_PATH` into Strata. |

---

## 11. Settings, Vault Secrets, Engine Tuning, Cache & Agent Rules

| Method | Path | Description |
| --- | --- | --- |
| `POST` / `DELETE` | `/api/settings/secrets` | Write-only AES-256-GCM Vault storage/removal for `hf_token`, `brave_api_key`, `tavily_api_key`. |
| `GET` / `PATCH` | `/api/desktop/config` | Read or update `DesktopConfig` (`launch_mode`, `auto_tray`, `autostart`, `desktop_icon`). |
| `POST` | `/api/system/tls/self-signed` | Generate SAN ECDSA self-signed TLS cert & key in `SPLITCH_DATA_ROOT/certs`. |
| `PATCH` | `/api/nodes/{node_id}` | Live-edit node display name, role, GPU yield settings, or URLs (persists across restarts). |
| `GET` / `POST` | `/api/nodes/{node_id}/engine-config` | Read or update managed `llama-server` runtime flags via systemd drop-in / launchd plist. |
| `GET` | `/api/nodes/{node_id}/storage/usage` | Inspect node model, cache, partial download, and free disk usage. |
| `POST` | `/api/nodes/{node_id}/storage/clean-cache` | Safely delete incomplete `.part` pulls and HF/llama cache blobs without touching completed models. |
| `GET` / `PUT` | `/api/chat/rules` | Read or save `global_rules` (injected into all chat turns) and reusable `PromptPersona` presets. |
| `GET` / `PUT` | `/api/nodes/{node_id}/workspace/agents-md` | Read or write sandboxed `AGENTS.md` inside the target node's workspace. |

---

Next: **[Security & Troubleshooting →](Security-and-Troubleshooting.md)**
