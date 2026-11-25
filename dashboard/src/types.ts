// Exact 1:1 mirror of backend/shared/models.py

export type NodeRole = "gpu" | "unified" | "cpu";
export type EngineKind = "llama-server" | "ollama" | "mlx-lm" | "strata" | "openai" | "unknown";
export type MediaEngineKind = "sd-server" | "comfyui" | "openai-images" | "none";
export type MediaKind = "image" | "video";
export type GpuVendor = "nvidia" | "amd" | "intel" | "apple" | "other";

export interface LoadedModel {
  id: string;
  status: "loaded" | "loading" | "unloaded";
}

export interface GpuProcess {
  pid: number;
  name: string;
  vram_mb: number;
  is_llm: boolean;
  gpu_index?: number;
  gpu_bus_id?: string | null;
}

export interface GpuDevice {
  index: number;
  vendor: GpuVendor;
  name: string;
  bus_id: string | null;
  vram_total_mb: number;
  vram_used_mb: number;
  vram_free_mb: number;
  util_percent: number;
  temp_c: number | null;
  non_llm_vram_mb: number;
  is_unified: boolean;
  backend_hint: string;
}

export interface NodeTelemetry {
  node_id: string;
  role: NodeRole;
  platform: string;
  timestamp: number;
  online: boolean;
  version?: string;
  cpu_percent: number;
  ram_total_mb: number;
  ram_used_mb: number;
  ram_available_mb: number;
  gpu_name: string | null;
  vram_total_mb: number;
  vram_used_mb: number;
  vram_free_mb: number;
  gpu_util_percent: number;
  gpu_temp_c: number | null;
  gpu_processes: GpuProcess[];
  non_llm_vram_mb: number;
  gpus?: GpuDevice[];
  gpu_count?: number;
  gpu_vendors?: string[];
  recommended_backend?: string;
  storage_path: string;
  storage_mounted: boolean;
  storage_total_gb: number;
  storage_free_gb: number;
  engine: EngineKind;
  engine_healthy: boolean;
  engine_url: string;
  media_engine?: MediaEngineKind;
  media_healthy?: boolean;
  media_url?: string;
  media_models?: string[];
  context_length: number | null;
  tools_enabled: boolean;
  workspace_root: string;
  cmd_sandbox?: string;
  models: LoadedModel[];
  strata_installed?: boolean;
  strata_healthy?: boolean;
  strata_active_model?: string;
  strata_models?: LoadedModel[];
}

export interface NodeStatus {
  node_id: string;
  display_name: string;
  role: NodeRole;
  yield_on_gaming: boolean;
  yield_gpu_indices?: number[];
  agent_url: string;
  engine_url: string;
  media_url?: string;
  online: boolean;
  latency_ms: number;
  in_flight: number;
  yield_until_ts?: number;
  busy_reason: string | null;
  telemetry: NodeTelemetry | null;
}

export interface AliasConfig {
  alias: string;
  task_kind: "chat" | "code" | "fim" | "embed" | "image" | "video";
  primary_node: string;
  primary_model: string;
  fallback_node: string | null;
  fallback_model: string | null;
}

export interface ControlState {
  gaming_mode: boolean;
  force_node: string;
}

export interface UiThemeConfig {
  scheme: "system" | "dark" | "light";
  accent: string;
  surface: "default" | "oled" | "slate" | "charcoal";
  resolved_light: boolean;
  updated_at?: number;
}

export interface TraceEntry {
  id: string;
  timestamp: number;
  endpoint: string;
  requested_model: string;
  resolved_node: string;
  resolved_model: string;
  fallback_reason: string | null;
  status_code: number;
  stream: boolean;
  ttft_ms: number | null;
  duration_ms: number;
  completion_tokens: number;
  tokens_per_sec: number | null;
  error: string | null;
}

export interface UpdateSummary {
  update_available: boolean;
  behind_count: number;
  local_sha: string;
  remote_sha: string;
  remote_version?: string;
  checked_at: number;
  engine_update_available: boolean;
  latest_llama_tag: string;
}

export interface DashboardSnapshot {
  version: string;
  uptime_s: number;
  control: ControlState;
  theme?: UiThemeConfig;
  nodes: NodeStatus[];
  aliases: AliasConfig[];
  traces: TraceEntry[];
  update_summary?: UpdateSummary | null;
}

export interface LocalModelFile {
  filename: string;
  path: string;
  size_gb: number;
  managed: boolean;
  format?: "gguf" | "mlx";
}

export interface DownloadJob {
  id: string;
  node_id: string;
  repo_id: string;
  filename: string;
  status: "downloading" | "completed" | "failed";
  downloaded_bytes: number;
  total_bytes: number;
  percent: number;
  error: string | null;
}

export interface NodeModelInventory {
  node_id: string;
  storage_path: string;
  files: LocalModelFile[];
  downloads: DownloadJob[];
}

export interface HfRepoResult {
  repo_id: string;
  downloads: number;
  likes: number;
  trending_score: number;
  last_modified: string | null;
  created_at: string | null;
  pipeline_tag: string | null;
  license: string | null;
  param_size: string | null;
  parameters_b?: number | null;
  architecture?: string | null;
  context_length?: number | null;
  est_min_gb?: number | null;
  est_q4_gb?: number | null;
  fits_nodes?: string[];
  gguf_count: number;
  gated: boolean;
  capabilities: string[];
}

export interface HfGgufFile {
  filename: string;
  size_bytes: number;
  size_gb: number;
  quant: string;
  fits_nodes: string[];
  fits_single_gpu_nodes?: string[];
  fits_multi_gpu_nodes?: string[];
}

export interface HfRepoDetails {
  repo_id: string;
  description: string;
  description_truncated: boolean;
  base_model: string | null;
  architecture: string | null;
  context_length: number | null;
  parameters_b: number | null;
  license: string | null;
  tags: string[];
  files: HfGgufFile[];
}

export interface SkillItem {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  content: string;
  source_url: string | null;
  updated_at: number;
}

export interface SyncResult {
  node_id: string;
  ok: boolean;
  synced_count: number;
  paths: string[];
  error: string | null;
}

export interface McpServerConfig {
  id: string;
  name: string;
  description: string;
  transport: "stdio" | "http" | "sse";
  scope: string;
  enabled: boolean;
  command: string | null;
  args: string[];
  url: string | null;
  env: Record<string, string>;
  egress?: "local" | "external_api" | "remote_mcp";
  egress_target?: string | null;
}

export interface ScopeRule {
  skills: string[];
  mcps: string[];
}

export interface ScopedExtensionsConfig {
  global_scope: ScopeRule;
  workspaces: Record<string, ScopeRule>;
}

export interface AuthStatusResponse {
  auth_required: boolean;
  authenticated: boolean;
}

export interface McpTestResult {
  id: string;
  ok: boolean;
  protocol_version: string | null;
  server_name: string | null;
  tools: string[];
  latency_ms: number;
  error: string | null;
}

export interface McpToolParamSpec {
  name: string;
  type: "string" | "integer" | "number" | "boolean";
  description: string;
  required: boolean;
}

export interface McpToolSpec {
  name: string;
  description: string;
  parameters: McpToolParamSpec[];
}

export interface McpEnvVarSpec {
  name: string;
  description: string;
  is_required: boolean;
  is_secret: boolean;
  default: string | null;
}

export interface McpRegistryResult {
  id: string;
  registry_name: string;
  description: string;
  source: "official" | "npm";
  version: string;
  transport: "stdio" | "http" | "sse";
  command: string | null;
  args: string[];
  url: string | null;
  repo_url: string | null;
  website_url: string | null;
  downloads_weekly: number | null;
  updated_at: string | null;
  env_vars: McpEnvVarSpec[];
}

export interface SkillRegistryResult {
  id: string;
  source: string;
  skill_id: string;
  name: string;
  installs: number;
}

// ── Agent Chat Schemas ───────────────────────────────────────────────────────

export interface ToolCallRecord {
  id: string;
  type: "function";
  function: {
    name: string;
    arguments: string;
  };
}

export interface ConversationSummary {
  id: string;
  title: string;
  model: string;
  exec_node: string;
  workspace_subpath: string;
  tools_enabled: boolean;
  web_search_enabled: boolean;
  auto_approve: boolean;
  auto_compact: boolean;
  pinned: boolean;
  compacted_summary: string;
  compacted_through_ts: number;
  enabled_skills: string[];
  enabled_mcps: string[];
  system_prompt: string;
  deleted_at: number | null;
  created_at: number;
  updated_at: number;
}

export interface WorkspaceDirListing {
  root: string;
  subpath: string;
  parent: string | null;
  dirs: string[];
}

export interface ChatMessageRecord {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  reasoning: string;
  images: string[];
  tool_calls: ToolCallRecord[];
  tool_call_id: string | null;
  tool_name: string | null;
  meta: {
    node?: string;
    model?: string;
    fallback_reason?: string | null;
    ttft_ms?: number | null;
    duration_ms?: number;
    tokens_per_sec?: number | null;
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    context_window?: number;
    ok?: boolean;
    diff?: string | null;
    verification?: {
      checked: boolean;
      ok: boolean;
      checker: string;
      output: string;
    } | null;
    sandbox?: string | null;
    stopped?: boolean;
    media_url?: string | null;
    media_kind?: MediaKind | null;
  };
  created_at: number;
}

export interface MediaJob {
  id: string;
  kind: MediaKind;
  status: "queued" | "evicting_vram" | "running" | "completed" | "error";
  progress: number;
  stage: string;
  prompt: string;
  negative_prompt: string;
  node_id: string;
  media_engine: MediaEngineKind;
  model: string;
  width: number;
  height: number;
  steps: number;
  cfg_scale: number;
  seed: number | null;
  frames: number;
  fps: number;
  filename: string | null;
  file_url: string | null;
  mime_type: string | null;
  duration_ms: number | null;
  error: string | null;
  created_at: number;
  completed_at: number | null;
}

export interface MediaBackendNode {
  node_id: string;
  display_name: string;
  role: NodeRole;
  online: boolean;
  media_engine: MediaEngineKind;
  media_healthy: boolean;
  media_url: string;
  media_models: string[];
  vram_free_mb: number;
  vram_total_mb: number;
  busy_reason: string | null;
}

export interface NodeMediaEngineStatus {
  node_id: string;
  sd_installed: boolean;
  sd_binary: string;
  sd_version: string;
  sd_backend: string;
  sd_managed_running: boolean;
  sd_running_model: string;
  sd_error: string | null;
  install_in_progress: boolean;
  install_step: string;
  media_engine: MediaEngineKind;
  media_healthy: boolean;
  media_url: string;
  local_media_models: LocalModelFile[];
  active_downloads: DownloadJob[];
}

export interface MediaHfFile {
  filename: string;
  size_gb: number;
  format: "gguf" | "safetensors";
}

export interface MediaHfRepoResult {
  repo_id: string;
  downloads: number;
  likes: number;
  pipeline_tag: string;
  last_modified: string | null;
  gated: boolean;
  file_count: number;
}

export interface PendingApproval {
  approval_id: string;
  tool_call_id: string;
  name: string;
  arguments: Record<string, unknown>;
  exec_node: string;
  sandbox?: string | null;
}

// ── Settings, Connect Clients & Node Provisioning ────────────────────────────

export type ApprovalPolicy = "ask" | "auto_edits" | "turbo_safe" | "full_auto";
export type ThinkingDisplay = "collapsed" | "expanded" | "hidden";

export interface PromptPersona {
  id: string;
  name: string;
  prompt: string;
}

export interface ChatRulesConfig {
  global_rules: string;
  personas: PromptPersona[];
}

export interface AgentsMdPayload {
  content: string;
  exists: boolean;
  path: string;
}

export interface RuntimeSettings {
  non_llm_vram_threshold_mb: number;
  min_vram_free_mb: number;
  max_external_gpu_util_percent: number;
  yield_cooldown_s: number;
  auto_evict_on_yield: boolean;
  poll_interval_s: number;
  auto_heavy_char_threshold: number;
  max_traces: number;
  public_url: string;
  searxng_url: string;
  web_search_provider: "auto" | "searxng" | "brave" | "tavily" | "off";
  brave_api_key_set: boolean;
  tavily_api_key_set: boolean;
  hf_token_set: boolean;
  auth_enabled: boolean;
  agent_token_enabled: boolean;
  tls_enabled: boolean;
  hsts_enabled: boolean;
  trusted_proxies: string;
  allowed_origins: string[];
  login_max_attempts: number;
  login_window_s: number;
  remote_provision_enabled: boolean;
  approval_policy: ApprovalPolicy;
  shell_allowlist: string[];
  shell_denylist: string[];
  max_agent_steps: number;
  auto_compact_threshold: number;
  thinking_display: ThinkingDisplay;
  completion_notifications: boolean;
}

export interface DesktopConfigView {
  launch_mode: "app" | "browser" | "firefoxpwa";
  auto_tray: boolean;
  autostart: boolean;
  desktop_icon: boolean;
}

export interface EngineRuntimeConfig {
  ctx_size: number;
  cache_type_k: "f16" | "q8_0" | "q4_0";
  cache_type_v: "f16" | "q8_0" | "q4_0";
  flash_attn: boolean;
  models_max: number;
  sleep_idle_seconds: number;
  parallel: number;
  apply_restart?: boolean;
  supported?: boolean;
  unit_name?: string;
  override_path?: string;
}

export interface StorageUsageResponse {
  storage_path: string;
  free_gb: number;
  models_gb: number;
  partial_gb: number;
  cache_gb: number;
  partial_count: number;
  cache_entries: number;
}

export interface StorageCleanResponse {
  removed_files: number;
  freed_gb: number;
}

export interface ClientProbeResponse {
  ok: boolean;
  protocol: string;
  endpoint: string;
  status_code: number;
  latency_ms: number;
  resolved_model: string;
  snippet: string;
  error: string | null;
}

export interface JoinTokenResponse {
  id: string;
  token: string;
  expires_at: number;
  ttl_seconds: number;
  router_url: string;
  linux_macos_cmd: string;
  windows_ps_cmd: string;
}

export interface JoinTokenInfo {
  id: string;
  node_hint: string;
  created_at: number;
  expires_at: number;
  used: boolean;
}

export interface NodeSshTestResponse {
  ok: boolean;
  ssh_target: string;
  os_info: string;
  gpu_summary: string;
  splitch_installed: boolean;
  error: string | null;
}

export interface DoctorCheckItem {
  name: string;
  status: "ok" | "warn" | "error" | "info";
  summary: string;
  detail?: string | null;
  fix?: string | null;
}

export interface DoctorReport {
  ok: boolean;
  version: string;
  platform: string;
  arch: string;
  app_dir: string;
  install_mode: string;
  data_root: string;
  checks: DoctorCheckItem[];
}

export interface EngineBinaryInfo {
  engine: string;
  path: string;
  version: string;
  build_tag: string;
  managed: boolean;
  backend: string;
}

export interface EngineStatus {
  platform: string;
  arch: string;
  recommended_backend: string;
  cuda_version: string | null;
  gpu_vendors?: string[];
  detected_gpus?: GpuDevice[];
  installed: EngineBinaryInfo[];
  latest_llama_tag: string | null;
  update_available: boolean;
  error: string | null;
}

export interface UpdateStatus {
  version: string;
  app_dir: string;
  install_mode: string;
  is_git_repo: boolean;
  branch: string;
  local_sha: string;
  remote_sha: string;
  remote_version?: string;
  dirty: boolean;
  behind_count: number;
  ahead_count: number;
  update_available: boolean;
  recent_remote_commits: string[];
  error: string | null;
}

export interface BenchTargetResult {
  requested_model: string;
  resolved_node: string;
  resolved_model: string;
  ok: boolean;
  ttft_ms: number | null;
  duration_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  pp_tokens_per_sec: number | null;
  tg_tokens_per_sec: number | null;
  aggregate_tokens_per_sec: number | null;
  tool_eval_passed: boolean | null;
  tool_eval_detail: string | null;
  json_eval_passed: boolean | null;
  error: string | null;
}

export interface BenchReport {
  id: string;
  timestamp: number;
  router_url: string;
  concurrency: number;
  max_tokens: number;
  results: BenchTargetResult[];
}

export type NodeUpdateStepStatus =
  | "pending"
  | "updating"
  | "restarting"
  | "done"
  | "rolled_back"
  | "failed";

export interface NodeUpdateRunResult {
  node_id: string;
  display_name: string;
  status: NodeUpdateStepStatus;
  ok: boolean;
  updated: boolean;
  rolled_back: boolean;
  previous_sha: string;
  current_sha: string;
  steps: string[];
  error: string | null;
}

export interface ClusterUpdateJob {
  id: string;
  action: "update" | "rollback";
  status: "idle" | "running" | "restarting" | "done" | "aborted" | "failed";
  started_at: number;
  finished_at: number | null;
  with_engine: boolean;
  router_restarting: boolean;
  rolled_back: boolean;
  error: string | null;
  nodes: NodeUpdateRunResult[];
}

export interface WorkspaceTreeEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size_bytes: number;
  git_status: string;
}

export interface WorkspaceTreeListing {
  root: string;
  subpath: string;
  dir_path: string;
  entries: WorkspaceTreeEntry[];
}

export interface WorkspaceFileContent {
  path: string;
  content: string;
  language: string;
  size_bytes: number;
  truncated: boolean;
  is_binary: boolean;
}

export interface GitFileChange {
  path: string;
  index_status: string;
  worktree_status: string;
  staged: boolean;
  untracked: boolean;
}

export interface GitStatusResponse {
  is_git_repo: boolean;
  branch: string;
  ahead: number;
  behind: number;
  changes: GitFileChange[];
  error: string | null;
}

export interface GitDiffResponse {
  path: string;
  staged: boolean;
  diff: string;
  truncated: boolean;
}

export interface GitActionResponse {
  ok: boolean;
  output: string;
  commit_sha: string;
  error: string | null;
}

export type StrataFitTier = "optimal" | "low_ram" | "tight" | "insufficient";

export interface StrataModelSpec {
  family: string;
  model: string;
  label: string;
  provider: string;
  download_gb: number;
  experts_ram_gb: number;
  min_vram_gb: number;
  ssd_shard_gb: number;
  supports_vision: boolean;
  supports_dflash2: boolean;
  notes: string;
}

export interface StrataFitAssessment {
  node_id: string;
  tier: StrataFitTier;
  reason: string;
  vram_gb: number;
  ram_gb: number;
  ssd_free_gb: number;
  is_unified: boolean;
}

export interface StrataCatalogEntry {
  spec: StrataModelSpec;
  fits: Record<string, StrataFitAssessment>;
}

export interface StrataInstalledModel {
  id: string;
  family: string;
  config_path: string;
  component_status: string;
  is_active: boolean;
}

export interface StrataJobState {
  job_id: string;
  action: "idle" | "install" | "launch" | "plan" | "stop" | "calibrate";
  status: "idle" | "running" | "completed" | "failed";
  step: string;
  log_tail: string[];
  plan_json: Record<string, unknown> | null;
  error: string | null;
  started_at: number;
  finished_at: number | null;
}

export interface NodeStrataStatus {
  node_id: string;
  installed: boolean;
  version: string;
  binary_path: string;
  running: boolean;
  healthy: boolean;
  port: number;
  active_model: string;
  models: LoadedModel[];
  installed_models: StrataInstalledModel[];
  presets: string[];
  job: StrataJobState;
  error: string | null;
}
