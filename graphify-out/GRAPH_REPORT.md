# Graph Report - ai_orchestrator  (2026-11-22)

## Corpus Check
- 111 files · ~138,723 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 15 file(s) not represented in the graph (top: (none) 4, .toml 2, .conf 2)

## Summary
- 1957 nodes · 5470 edges · 83 communities (72 shown, 11 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 355 edges (avg confidence: 0.91)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `21b27682`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- shortcuts.py
- app.py
- test_policy.py
- media_api.py
- mcp_api.py
- gpu_probe.py
- main.py
- cli.py
- telemetry.py
- icons.py
- TrayController
- models.py
- provision_api.py
- run_linux_sni_tray
- App.tsx
- splitch-https
- re
- react
- ChatTab.tsx
- with
- build_chat_router
- test_settings_multigpu_provision.py
- TerminalProgress
- updater.py
- ClusterState
- types.ts
- json
- engines.py
- install_or_update_engine
- secrets.py
- skills_api.py
- custom
- test_security.py
- McpTab.tsx
- CLI Agents & Terminal Tools
- time
- ChatStore
- README.md
- asyncio
- .poll_once
- chat_api.py
- SecretVault
- Installation & Deployment
- 2. Troubleshooting Recipes
- _is_public_url
- opencode.example.json
- ModelsTab.tsx
- compilerOptions
- install.sh
- Changelog
- web_search.py
- SettingsDialog.tsx
- 3. External HTTPS Reverse Proxies
- test_ui_e2e.py
- ConversationCreate
- useDialogLightDismiss
- test_installer.py
- 2. `splitch` CLI Subcommands
- 2. MCP Servers
- splitch-router
- _run_post_edit_verification
- chat_store.py
- Agent Chat Guide
- Configuration & Routing
- install.ps1
- _run_pystray_tray
- test_media_api_sd_server_vram_eviction_and_gallery_lifecycle
- proxy_local_media_engine
- _ReadableTextExtractor
- Splitch Wiki
- _find_item
- polyfills.d.ts
- Quickstart
- _register_with_watchers
- [0.8.0] - 2026-11-22
- nginx-proxy-manager.md
- _push_updates
- preflight.sh
- splitch

## God Nodes (most connected - your core abstractions)
1. `ClusterState` - 85 edges
2. `build_chat_router()` - 63 edges
3. `run_linux_sni_tray()` - 60 edges
4. `NodeStatus` - 48 edges
5. `default_data_root()` - 46 edges
6. `TrayController` - 37 edges
7. `build_media_router()` - 33 edges
8. `ChatStore` - 32 edges
9. `build_mcp_router()` - 31 edges
10. `install_desktop_integration()` - 30 edges

## Surprising Connections (you probably didn't know these)
- `2. Cluster Health, Control & Dynamic Nodes` --references--> `NodeRegisterRequest`  [INFERRED]
  docs/wiki/API-Reference.md → backend/shared/models.py
- `Reliability & CI (Sprint B)` --references--> `perform_update()`  [INFERRED]
  CHANGELOG.md → backend/shared/updater.py
- `Security Hardening & Supply Chain` --references--> `_enforce_auth_on_exposed_bind()`  [INFERRED]
  CHANGELOG.md → backend/cli.py
- `Desktop, Tray & Theme Synchronization` --references--> `TrayController`  [INFERRED]
  CHANGELOG.md → backend/desktop/tray.py
- `2. Architecture & Cross-Boundary Schema Invariants` --references--> `ChatStore`  [INFERRED]
  AGENTS.md → backend/router/chat_store.py

## Import Cycles
- None detected.

## Communities (83 total, 11 thin omitted)

### Community 0 - "shortcuts.py"
Cohesion: 0.08
Nodes (36): _atomic_write_text(), _check_router_healthy(), copy_to_clipboard(), _create_windows_shortcut(), desktop_config_path(), desktop_shortcuts_installed(), DesktopConfig, _detect_local_user_service() (+28 more)

### Community 1 - "app.py"
Cohesion: 0.07
Nodes (44): anthropic_count_tokens(), anthropic_messages(), _auth_enabled(), _auth_method(), browse_node_workspace(), chat_completions(), _client_ip(), completions() (+36 more)

### Community 2 - "test_policy.py"
Cohesion: 0.08
Nodes (38): _anth_sse(), anthropic_to_openai_request(), _extract_system_text(), handle_anthropic_count_tokens(), handle_anthropic_messages(), _stream_anthropic(), _close_active_block(), map_anthropic_model_name() (+30 more)

### Community 3 - "media_api.py"
Cohesion: 0.08
Nodes (23): _build_default_comfy_image_graph(), _build_default_comfy_video_graph(), build_media_router(), get_node_media_engine(), install_node_media_engine(), openai_images_generations(), openai_videos_generations(), pull_node_media_model() (+15 more)

### Community 4 - "mcp_api.py"
Cohesion: 0.08
Nodes (40): annotate_mcp_egress(), build_mcp_router(), build_custom_mcp(), _save_entry(), delete_mcp_server(), _delete(), export_config(), import_mcp_json() (+32 more)

### Community 5 - "gpu_probe.py"
Cohesion: 0.09
Nodes (28): classify_vendor(), collect_all_gpus(), _collect_intel_xpu_async(), _collect_mac_gpu_async(), _collect_nvidia_async(), _collect_rocm_async(), _collect_vulkan_async(), _collect_windows_cim_async() (+20 more)

### Community 6 - "main.py"
Cohesion: 0.09
Nodes (30): browse_workspace_dirs(), _exec_fs_tool(), execute_workspace_tool(), get_metrics(), health(), list_model_files(), _resolve_workspace_path(), _scan_local_ggufs() (+22 more)

### Community 7 - "cli.py"
Cohesion: 0.11
Nodes (29): _cmd_agent(), _cmd_desktop(), _cmd_doctor(), _cmd_engine(), _cmd_init(), _cmd_launch(), _cmd_rollback(), _cmd_router() (+21 more)

### Community 8 - "telemetry.py"
Cohesion: 0.10
Nodes (20): _default_single_node_config(), _discover_config_path(), load_config(), load_control_state(), NodeConfig, RouterSettings, save_control_state(), SecuritySettings (+12 more)

### Community 9 - "icons.py"
Cohesion: 0.10
Nodes (25): _blend(), _build_smooth_s_segments(), _clamp_byte(), _cubic_bezier(), ensure_tray_icon_theme(), _extract_theme_fields(), _hex_to_rgb(), IconPalette (+17 more)

### Community 10 - "TrayController"
Cohesion: 0.08
Nodes (11): run_tray(), TrayController, _worker(), _worker(), _worker(), add(), _worker(), TrayMenuItem (+3 more)

### Community 11 - "models.py"
Cohesion: 0.09
Nodes (37): apply_persisted_settings(), _build_client_payload(), build_settings_router(), _current_settings(), get_settings(), _load_mcps(), patch_settings(), probe_client_protocol() (+29 more)

### Community 12 - "provision_api.py"
Cohesion: 0.08
Nodes (22): _build_bootstrap_commands(), build_provision_router(), create_join_token(), _ensure_remote_provision_allowed(), provision_node_ssh(), _stream(), test_ssh_target(), _guess_lan_ip() (+14 more)

### Community 13 - "run_linux_sni_tray"
Cohesion: 0.05
Nodes (4): run_linux_sni_tray(), __init__(), LayoutUpdated(), refresh()

### Community 14 - "App.tsx"
Cohesion: 0.10
Nodes (31): [0.9.0] - 2026-11-22, Branding, App(), fmtGb(), fmtTime(), fmtUptime(), NodeCard(), TAB_ORDER (+23 more)

### Community 15 - "splitch-https"
Cohesion: 0.05
Nodes (38): X-Forwarded-Proto, dialTimeout, idleConnTimeout, responseHeaderTimeout, customRequestHeaders, http, middlewares, routers (+30 more)

### Community 16 - "re"
Cohesion: 0.08
Nodes (22): _slugify(), _build_gguf_file_list(), build_models_router(), delete_alias(), get_hf_repo_details(), get_inventory(), list_hf_ggufs(), _persist_aliases() (+14 more)

### Community 17 - "react"
Cohesion: 0.07
Nodes (34): dependencies, input-switch-polyfill@^1.12.0, interestfor@^1.0.14, lucide-react@^0.469.0, @oddbird/popover-polyfill@^0.7.3, react@^19.0.0, react-dom@^19.0.0, react-markdown@^10.1.0 (+26 more)

### Community 18 - "ChatTab.tsx"
Cohesion: 0.13
Nodes (31): InspectedArtifact, ChatComposer(), ChatComposerProps, ContextMemoryDialog(), ContextMemoryDialogProps, WorkspaceBrowserDialog(), WorkspaceBrowserDialogProps, DEFAULT_DRAFT (+23 more)

### Community 19 - "with"
Cohesion: 0.07
Nodes (37): name, runs-on, steps, strategy, timeout-minutes, jobs, cross-platform, ui-e2e (+29 more)

### Community 20 - "build_chat_router"
Cohesion: 0.09
Nodes (23): build_chat_router(), bulk_delete_conversations(), bulk_permanent_delete_conversations(), bulk_restore_conversations(), compact_conversation(), _estimate_msgs_tokens(), get_scopes(), _load_mcp_configs() (+15 more)

### Community 21 - "test_settings_multigpu_provision.py"
Cohesion: 0.10
Nodes (19): _apply_workflow_placeholders(), _walk(), _resolve_node_media_url(), check_node_eligibility(), estimate_model_vram_mb(), watched_gpu_metrics(), LoadedModel, NodeStatus (+11 more)

### Community 22 - "TerminalProgress"
Cohesion: 0.11
Nodes (10): _download_file(), DownloadBar, _fmt_bytes(), _fmt_duration(), null_download(), null_step(), _NullDownloadBar, StepSpinner (+2 more)

### Community 23 - "updater.py"
Cohesion: 0.18
Nodes (23): check_update(), _detect_gpu_summary(), DoctorCheck, DoctorReport, _find_node_bin(), fix_doctor_issues(), _meta_path(), _parse_major() (+15 more)

### Community 24 - "ClusterState"
Cohesion: 0.14
Nodes (8): patch_ui_theme(), register_node(), ClusterState, DashboardSnapshot, NodeRegisterRequest, UiThemeConfig, UiThemeUpdate, test_dynamic_node_persistence()

### Community 25 - "types.ts"
Cohesion: 0.08
Nodes (29): fmtInstalls(), SkillsTab(), BenchTargetResult, DoctorCheckItem, DoctorReport, DownloadJob, EngineBinaryInfo, EngineKind (+21 more)

### Community 26 - "json"
Cohesion: 0.14
Nodes (16): merge_client_config(), sync_skills_to_dir(), _cmd_bench(), run_cluster_benchmark(), append_bench_history(), BenchReport, BenchRunRequest, BenchTargetResult (+8 more)

### Community 27 - "engines.py"
Cohesion: 0.17
Nodes (20): detect_installed_engines(), _engine_meta_path(), EngineActionResult, EngineBinaryInfo, EngineStatus, _find_binary_in_dir(), install_llama_from_archives(), _install_or_update_ollama() (+12 more)

### Community 28 - "install_or_update_engine"
Cohesion: 0.12
Nodes (17): _compare_build_tags(), detect_engine_hardware(), fetch_latest_llama_release(), fetch_latest_sd_release(), get_engine_status(), install_or_update_engine(), _norm_arch(), _norm_os() (+9 more)

### Community 29 - "secrets.py"
Cohesion: 0.12
Nodes (10): _assert_private_file_permissions(), _derive_32b_key(), generate_ed25519_keypair(), hash_secret_scrypt(), load_ed25519_private_key(), load_or_create_session_secret(), _try_keyring_master_key(), write_private_bytes() (+2 more)

### Community 30 - "skills_api.py"
Cohesion: 0.15
Nodes (23): build_skills_router(), delete_skill(), _delete(), import_skill(), install_from_registry(), list_skills(), _load_meta(), _save_meta() (+15 more)

### Community 31 - "custom"
Cohesion: 0.07
Nodes (26): context_length, display_name, api_key, api_mode, base_url, context_length, default_model, discover_models (+18 more)

### Community 32 - "test_security.py"
Cohesion: 0.10
Nodes (9): inspect_certificate(), issue_letsencrypt_cert(), is_valid_semver(), test_agent_mutating_tool_execution_requires_approval_boundary(), test_dns_ssrf_node_registration_blocked(), test_hmac_tool_approval_and_non_loopback_rejection(), test_run_command_process_group_kill_and_output_cap(), test_self_signed_tls_generation_san_and_permissions() (+1 more)

### Community 33 - "McpTab.tsx"
Cohesion: 0.09
Nodes (22): Props, ConfirmDialog(), ConfirmState, DialogMode, EXTERNAL_ENV_PREFIXES, EXTERNAL_PKG_TOKENS, MCP_PRESETS, McpPreset (+14 more)

### Community 34 - "CLI Agents & Terminal Tools"
Cohesion: 0.08
Nodes (25): 10. Continue (VS Code / JetBrains), 11. Cursor, 12. Open WebUI, 13. LiteLLM Proxy, 14. Python `openai` SDK (Chat, Streaming, Tools, Embeddings, Images), 15. TypeScript / Node `openai` SDK, 16. Python `anthropic` SDK, 17. LangChain / CrewAI (+17 more)

### Community 35 - "time"
Cohesion: 0.15
Nodes (11): _b64_decode_nopad(), load_ed25519_public_key(), public_key_to_compact(), sign_agent_request_ed25519(), sign_tool_approval_ed25519(), verify_agent_request_ed25519(), verify_tool_approval_ed25519(), _canonical_tool_payload() (+3 more)

### Community 37 - "README.md"
Cohesion: 0.23
Nodes (8): Splitch Wiki, Acknowledgments, Core Pillars, Development & Contributing, Documentation Wiki, License, Screenshots, Security Model

### Community 38 - "asyncio"
Cohesion: 0.20
Nodes (14): _build_media_engine_status(), get_media_engine_status(), install_media_engine(), _worker(), model_action(), start_media_engine(), stop_media_engine(), sync_client_config() (+6 more)

### Community 39 - ".poll_once"
Cohesion: 0.15
Nodes (12): AgentState, _collect_engine_models(), _collect_media_engine(), _collect_storage(), delete_model_file(), _normalize_models(), _normalize_ollama_models(), _probe_single_media_url() (+4 more)

### Community 40 - "chat_api.py"
Cohesion: 0.18
Nodes (7): lifespan(), _sse(), call_mcp_tool(), fetch_mcp_tools(), execute_web_fetch(), McpServerConfig, resolve_env_map()

### Community 41 - "SecretVault"
Cohesion: 0.26
Nodes (6): _cmd_secret(), _safe_resolve_env_secret(), resolve_secret_value(), SecretVault, Security (Cryptographic Secrets, Vault & Asymmetric Ed25519), test_secret_indirection_and_vault_aes256gcm_aad()

### Community 42 - "Installation & Deployment"
Cohesion: 0.11
Nodes (19): 1. One-Command Installer & Updater (Recommended), 2. Manual Prerequisites (If Not Using `install.sh`), 3. Prepare Your Inference Backend, 4. Single-Machine Manual Quickstart (Zero Config), 5. Multi-Node Cluster (LAN or Tailscale), 6. Docker & Docker Compose, 7. Persistent OS Background Services, A. `llama-server` in Router Mode (Recommended for Multi-Model Nodes) (+11 more)

### Community 43 - "2. Troubleshooting Recipes"
Cohesion: 0.11
Nodes (19): 1. Security Model & Defense in Depth, 2. Troubleshooting Recipes, Agent Chat Says "Tools Disabled on Target Node", Bearer Authentication, Rate Limiting & Web UI Session Login, Context Window Fills Quickly or Compaction Doesn't Trigger, Dashboard Shows Blank Page or Old Assets After Pull, GPU Node Keeps Yielding Unexpectedly, Node Shows `OFFLINE` in Overview (+11 more)

### Community 44 - "_is_public_url"
Cohesion: 0.11
Nodes (15): _is_public_url(), 1. Install Dependencies, 2. Run Backend + Frontend with Hot Reload, Backend (Python / FastAPI), Code of Conduct, Contributing to Splitch, Engineering & Security Invariants, Frontend (React / TypeScript / CSS) (+7 more)

### Community 45 - "opencode.example.json"
Cohesion: 0.11
Nodes (17): name, name, name, model, auto, fast, heavy, apiKey (+9 more)

### Community 46 - "ModelsTab.tsx"
Cohesion: 0.18
Nodes (16): fmtCompact(), fmtContextLen(), fmtRelativeDate(), ModelsTab(), nodeBudgetGb(), runWithViewTransition(), safeExternalHref(), SORT_LABELS (+8 more)

### Community 47 - "compilerOptions"
Cohesion: 0.12
Nodes (16): compilerOptions, allowImportingTsExtensions, isolatedModules, jsx, lib, module, moduleDetection, moduleResolution (+8 more)

### Community 48 - "install.sh"
Cohesion: 0.29
Nodes (16): cleanup_installer(), die(), install_pinned_uv(), log(), ok(), osc_progress(), PATH, pinned_node_sha256() (+8 more)

### Community 49 - "Changelog"
Cohesion: 0.12
Nodes (15): NewIcon(), [0.3.0] - 2026-11-20, [0.4.0] - 2026-11-22, [0.5.0] - 2026-11-22, [0.6.0] - 2026-11-22, [0.7.0] - 2026-11-22, [0.9.1] - 2026-11-22, Added (since 0.3.0) (+7 more)

### Community 50 - "web_search.py"
Cohesion: 0.24
Nodes (9): _clean_snippet(), execute_web_search(), _format_results(), _search_brave(), _search_open_sources(), _ddg_ia(), _wiki(), _search_searxng() (+1 more)

### Community 51 - "SettingsDialog.tsx"
Cohesion: 0.23
Nodes (13): BenchmarksPane(), buildClientSnippets(), ClientId, CLIENTS, PUSHABLE_CLIENTS, SETTINGS_TABS, SettingsTabId, NodesProvisionPane() (+5 more)

### Community 52 - "3. External HTTPS Reverse Proxies"
Cohesion: 0.12
Nodes (16): 1. Direct Native HTTPS — Without Let's Encrypt, 2. Direct Native HTTPS — With Let's Encrypt, 3. External HTTPS Reverse Proxies, 4. Encrypting Router ↔ Node Agent Traffic (Optional Internal TLS), 5. Production Hardening Checklist, Apache HTTP Server, Caddy, HAProxy (+8 more)

### Community 53 - "test_ui_e2e.py"
Cohesion: 0.17
Nodes (6): _build_test_spa_app(), _free_port(), test_ui_confirm_delete_trash_bulk_and_pin_e2e(), cdp(), eval_js(), wait_until()

### Community 54 - "ConversationCreate"
Cohesion: 0.13
Nodes (15): CompactResponse, ConversationCreate, 1. OpenAI, Responses, Anthropic & FIM Inference API, 2. Cluster Health, Control & Dynamic Nodes, 3. Agent Chat API (`/api/chat`), 4. Models & Aliases API (`/api`), 5. MCP Servers API (`/api/mcp`), 6. Skills API (`/api/skills`) (+7 more)

### Community 55 - "useDialogLightDismiss"
Cohesion: 0.29
Nodes (13): ArtifactPreview(), ArtifactSplitPane(), buildSrcDoc(), inferArtifactTitle(), isRunnableArtifact(), LogEntry, OriginConstructor, SandboxFrame() (+5 more)

### Community 56 - "test_installer.py"
Cohesion: 0.19
Nodes (7): parse_llama_build_tag(), test_doctor_and_system_api_endpoints(), test_engine_asset_selection_and_tag_parsing(), test_git_check_and_perform_update_end_to_end(), test_install_sh_syntax_and_local_install_uninstall(), test_managed_llama_archive_install_shim_and_tarslip_guard(), test_updater_auto_rollback_on_sync_failure_and_manual_rollback()

### Community 57 - "2. `splitch` CLI Subcommands"
Cohesion: 0.14
Nodes (14): 1. Universal Installers (`install.sh` & `install.ps1`), 2. `splitch` CLI Subcommands, CLI, Installer & Updater Reference, Installer Flags & Environment Overrides, `splitch agent` — Run Node Telemetry, Pull & Tools Agent, `splitch doctor` — Environment, Hardware & Engine Diagnostics, `splitch engine` — Install, Inspect & Update Inference & Media Engines, `splitch init` — Generate Starter `splitch.toml` (+6 more)

### Community 58 - "2. MCP Servers"
Cohesion: 0.14
Nodes (14): 1. Models & Logical Aliases, 2. MCP Servers, 3. Skills Library, 4 Ways to Add an MCP Server (Local `stdio` Prioritized Over Remote), Authoring & Importing Skills, External Data Egress Advisories & HITL Protection, Global & Per-Project (Workspace) Scoping, Hot-Reloadable Aliases (+6 more)

### Community 59 - "splitch-router"
Cohesion: 0.15
Nodes (12): SPLITCH_AGENT_TOKEN, SPLITCH_API_KEY, SPLITCH_CONFIG, services, splitch-router, build, environment, ports (+4 more)

### Community 60 - "_run_post_edit_verification"
Cohesion: 0.20
Nodes (6): 1. Mandatory Pre-Flight Harness, 2. Architecture & Cross-Boundary Schema Invariants, 3. Browser Support Policy, AGENTS.md — Splitch Engineering & Agent Harness Guide, _run_post_edit_verification(), test_harness_3_edit_file_and_post_edit_verification()

### Community 61 - "chat_store.py"
Cohesion: 0.28
Nodes (3): send_chat(), ChatMessageRecord, ConversationUpdate

### Community 62 - "Agent Chat Guide"
Cohesion: 0.22
Nodes (9): 1. Pre-Prompt Configuration & Toolbar, 2. Bottom Composer Bar: Web Search, Context Ring & Compaction, 3. Contextual Right Split Artifact Pane ("Run in Place"), 4. Organizing Chats: Pinning, Multi-Select Bulk Delete & Trash Bin, 5. Appearance & Theme Customization, Agent Chat Guide, Context Progress Ring, Non-Destructive Auto-Compact & Manual Compact (+1 more)

### Community 63 - "Configuration & Routing"
Cohesion: 0.22
Nodes (9): 1. Config File Discovery Order, 2. Annotated `splitch.toml`, 3. Routing Policy Decision Tree, 4. GPU Auto-Yield, Auto-VRAM Eviction & Cooldown Hysteresis, 5. Complete Environment Variable Reference, Automatic Detection + Auto-VRAM Eviction, Configuration & Routing, How `auto` Classifies Requests (+1 more)

### Community 64 - "install.ps1"
Cohesion: 0.39
Nodes (6): Assert-Sha256Match(), Download-WithProgress(), Ensure-Node(), Set-OscProgress(), Show-StepBar(), Write-Ok()

### Community 65 - "_run_pystray_tray"
Cohesion: 0.36
Nodes (5): _run_pystray_tray(), _build_menu(), _get_image(), _sync_icon(), _to_pystray_items()

### Community 66 - "test_media_api_sd_server_vram_eviction_and_gallery_lifecycle"
Cohesion: 0.36
Nodes (5): test_agent_managed_media_engine_status_and_validation(), test_media_api_sd_server_vram_eviction_and_gallery_lifecycle(), fake_handler(), patched_async_client(), test_select_sd_assets_matrix_and_zip_install()

### Community 69 - "Splitch Wiki"
Cohesion: 0.29
Nodes (7): Acknowledgments & Upstream Credits, Core Design Philosophy, Documentation Map, Hardware & Backend Support Matrix, Quick CLI Cheat Sheet, Splitch Wiki, System Architecture

### Community 70 - "_find_item"
Cohesion: 0.47
Nodes (6): _find_item(), GetGroupProperties(), GetLayout(), GetProperty(), _item_props(), _item_to_layout()

### Community 71 - "polyfills.d.ts"
Cohesion: 0.50
Nodes (3): input-switch-polyfill, interestfor, @oddbird/popover-polyfill

### Community 72 - "Quickstart"
Cohesion: 0.50
Nodes (4): Connect Any Client, Multi-Node LAN / Tailscale Cluster, Quickstart, Single Machine (Localhost)

### Community 73 - "_register_with_watchers"
Cohesion: 0.67
Nodes (3): _list_bus_names(), _on_name_owner_changed(), _register_with_watchers()

### Community 74 - "[0.8.0] - 2026-11-22"
Cohesion: 0.67
Nodes (3): [0.8.0] - 2026-11-22, Frontend Modularity, Security Hardening & Supply Chain

## Knowledge Gaps
- **2 isolated node(s):** `PATH`, `splitch`
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 644 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **11 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Security Hardening & Supply Chain` connect `[0.8.0] - 2026-11-22` to `ChatTab.tsx`, `cli.py`?**
  _High betweenness centrality (0.186) - this node is a cross-community bridge._
- **Are the 31 inferred relationships involving `ClusterState` (e.g. with `handle_anthropic_count_tokens()` and `handle_anthropic_messages()`) actually correct?**
  _`ClusterState` has 31 INFERRED edges - model-reasoned connections that need verification._
- **What connects `PATH`, `splitch` to the rest of the system?**
  _2 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `shortcuts.py` be split into smaller, more focused modules?**
  _Cohesion score 0.08181126331811263 - nodes in this community are weakly interconnected._
- **Why does `PendingApproval` connect `ChatTab.tsx` to `types.ts`, `[0.8.0] - 2026-11-22`?**
  _High betweenness centrality (0.177) - this node is a cross-community bridge._
- **Are the 9 inferred relationships involving `build_chat_router()` (e.g. with `MediaService` and `ClusterState`) actually correct?**
  _`build_chat_router()` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Should `app.py` be split into smaller, more focused modules?**
  _Cohesion score 0.06666666666666667 - nodes in this community are weakly interconnected._