"""
Canonical cross-boundary schemas shared across:
  splitch-agent (:8090) -> splitch-router (:8080) -> dashboard (TypeScript).
Keep field names strictly mirrored in dashboard/src/types.ts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

NodeRole = Literal["gpu", "unified", "cpu"]
EngineKind = Literal["llama-server", "ollama", "openai", "unknown"]
MediaEngineKind = Literal["sd-server", "comfyui", "openai-images", "none"]
MediaKind = Literal["image", "video"]
GpuVendor = Literal["nvidia", "amd", "intel", "apple", "other"]


class LoadedModel(BaseModel):
    id: str
    status: Literal["loaded", "loading", "unloaded"] = "unloaded"


class GpuProcess(BaseModel):
    pid: int
    name: str
    vram_mb: float
    is_llm: bool = False
    gpu_index: int = 0
    gpu_bus_id: str | None = None


class GpuDevice(BaseModel):
    index: int = 0
    vendor: GpuVendor = "other"
    name: str
    bus_id: str | None = None
    vram_total_mb: float = 0.0
    vram_used_mb: float = 0.0
    vram_free_mb: float = 0.0
    util_percent: float = 0.0
    temp_c: float | None = None
    non_llm_vram_mb: float = 0.0
    is_unified: bool = False
    backend_hint: str = ""


class NodeTelemetry(BaseModel):
    node_id: str
    role: NodeRole = "gpu"
    platform: str
    timestamp: float
    online: bool = True
    cpu_percent: float = 0.0
    ram_total_mb: float = 0.0
    ram_used_mb: float = 0.0
    ram_available_mb: float = 0.0
    gpu_name: str | None = None
    vram_total_mb: float = 0.0
    vram_used_mb: float = 0.0
    vram_free_mb: float = 0.0
    gpu_util_percent: float = 0.0
    gpu_temp_c: float | None = None
    gpu_processes: list[GpuProcess] = Field(default_factory=list)
    non_llm_vram_mb: float = 0.0
    gpus: list[GpuDevice] = Field(default_factory=list)
    gpu_count: int = 0
    gpu_vendors: list[str] = Field(default_factory=list)
    recommended_backend: str = ""
    storage_path: str = ""
    storage_mounted: bool = True
    storage_total_gb: float = 0.0
    storage_free_gb: float = 0.0
    engine: EngineKind = "unknown"
    engine_healthy: bool = False
    engine_url: str = ""
    media_engine: MediaEngineKind = "none"
    media_healthy: bool = False
    media_url: str = ""
    media_models: list[str] = Field(default_factory=list)
    context_length: int | None = None
    tools_enabled: bool = False
    workspace_root: str = ""
    cmd_sandbox: str = ""
    models: list[LoadedModel] = Field(default_factory=list)


class NodeStatus(BaseModel):
    node_id: str
    display_name: str
    role: NodeRole = "gpu"
    yield_on_gaming: bool = False
    yield_gpu_indices: list[int] = Field(default_factory=list)
    agent_url: str
    engine_url: str
    media_url: str = ""
    online: bool = False
    latency_ms: float = 0.0
    in_flight: int = 0
    last_splitch_work_ts: float = Field(default=0.0, exclude=True)
    yield_until_ts: float = 0.0
    busy_reason: str | None = None
    telemetry: NodeTelemetry | None = None


class NodeRegisterRequest(BaseModel):
    id: str
    display_name: str
    role: NodeRole = "gpu"
    yield_on_gaming: bool = False
    yield_gpu_indices: list[int] = Field(default_factory=list)
    agent_url: str
    engine_url: str
    media_url: str = ""
    storage_path: str = ""


class AliasConfig(BaseModel):
    alias: str
    task_kind: Literal["chat", "code", "fim", "embed", "image", "video"] = "chat"
    primary_node: str
    primary_model: str
    fallback_node: str | None = None
    fallback_model: str | None = None


class ControlState(BaseModel):
    gaming_mode: bool = False
    force_node: str = "auto"


class UiThemeConfig(BaseModel):
    scheme: Literal["system", "dark", "light"] = "system"
    accent: str = ""
    surface: Literal["default", "oled", "slate", "charcoal"] = "default"
    resolved_light: bool = False
    updated_at: float = 0.0


class UiThemeUpdate(BaseModel):
    scheme: Literal["system", "dark", "light"] | None = None
    accent: str | None = None
    surface: Literal["default", "oled", "slate", "charcoal"] | None = None
    resolved_light: bool | None = None


class TraceEntry(BaseModel):
    id: str
    timestamp: float
    endpoint: str
    requested_model: str
    resolved_node: str
    resolved_model: str
    fallback_reason: str | None = None
    status_code: int
    stream: bool = False
    ttft_ms: float | None = None
    duration_ms: float
    completion_tokens: int = 0
    tokens_per_sec: float | None = None
    error: str | None = None


class DashboardSnapshot(BaseModel):
    version: str
    uptime_s: float
    control: ControlState
    theme: UiThemeConfig = Field(default_factory=UiThemeConfig)
    nodes: list[NodeStatus]
    aliases: list[AliasConfig]
    traces: list[TraceEntry]


class ControlUpdate(BaseModel):
    gaming_mode: bool | None = None
    force_node: str | None = None


class ModelActionRequest(BaseModel):
    model: str


# ── Models Management Schemas ────────────────────────────────────────────────


class LocalModelFile(BaseModel):
    filename: str
    path: str
    size_gb: float
    managed: bool = True


class DownloadJob(BaseModel):
    id: str
    node_id: str
    repo_id: str
    filename: str
    status: Literal["downloading", "completed", "failed"] = "downloading"
    downloaded_bytes: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    error: str | None = None


class NodeModelInventory(BaseModel):
    node_id: str
    storage_path: str
    files: list[LocalModelFile] = Field(default_factory=list)
    downloads: list[DownloadJob] = Field(default_factory=list)


class PullModelRequest(BaseModel):
    node_id: str = ""
    repo_id: str
    filename: str


class HfRepoResult(BaseModel):
    repo_id: str
    downloads: int = 0
    likes: int = 0
    trending_score: float = 0.0
    last_modified: str | None = None
    created_at: str | None = None
    pipeline_tag: str | None = None
    license: str | None = None
    param_size: str | None = None
    parameters_b: float | None = None
    architecture: str | None = None
    context_length: int | None = None
    est_min_gb: float | None = None
    est_q4_gb: float | None = None
    fits_nodes: list[str] = Field(default_factory=list)
    gguf_count: int = 0
    gated: bool = False
    capabilities: list[str] = Field(default_factory=list)


class HfGgufFile(BaseModel):
    filename: str
    size_bytes: int
    size_gb: float
    quant: str
    fits_nodes: list[str] = Field(default_factory=list)
    fits_single_gpu_nodes: list[str] = Field(default_factory=list)
    fits_multi_gpu_nodes: list[str] = Field(default_factory=list)


class HfRepoDetails(BaseModel):
    repo_id: str
    description: str = ""
    description_truncated: bool = False
    base_model: str | None = None
    architecture: str | None = None
    context_length: int | None = None
    parameters_b: float | None = None
    license: str | None = None
    tags: list[str] = Field(default_factory=list)
    files: list[HfGgufFile] = Field(default_factory=list)


# ── Skills Management Schemas ────────────────────────────────────────────────


class SkillItem(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool = True
    content: str
    source_url: str | None = None
    updated_at: float = 0.0


class SkillUpsertRequest(BaseModel):
    id: str
    name: str
    description: str = ""
    content: str
    enabled: bool = True
    source_url: str | None = None


class SkillImportRequest(BaseModel):
    url: str
    id: str | None = None


class SkillSyncPayload(BaseModel):
    skills: list[dict[str, str]]
    target_dirs: list[str] | None = None


class SyncResult(BaseModel):
    node_id: str
    ok: bool
    synced_count: int = 0
    paths: list[str] = Field(default_factory=list)
    error: str | None = None


# ── MCP Management Schemas ───────────────────────────────────────────────────


class McpServerConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    transport: Literal["stdio", "http", "sse"] = "stdio"
    scope: str = "shared"
    enabled: bool = True
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    egress: Literal["local", "external_api", "remote_mcp"] = "local"
    egress_target: str | None = None


class ScopeRule(BaseModel):
    skills: list[str] = Field(default_factory=list)
    mcps: list[str] = Field(default_factory=list)


class ScopedExtensionsConfig(BaseModel):
    global_scope: ScopeRule = Field(default_factory=ScopeRule)
    workspaces: dict[str, ScopeRule] = Field(default_factory=dict)


class WorkspaceScopeUpdate(BaseModel):
    workspace: str
    skills: list[str] = Field(default_factory=list)
    mcps: list[str] = Field(default_factory=list)


class AuthLoginRequest(BaseModel):
    api_key: str


class AuthStatusResponse(BaseModel):
    auth_required: bool = False
    authenticated: bool = True


class McpTestResult(BaseModel):
    id: str
    ok: bool
    protocol_version: str | None = None
    server_name: str | None = None
    tools: list[str] = Field(default_factory=list)
    latency_ms: float = 0.0
    error: str | None = None


class ClientConfigSyncRequest(BaseModel):
    client: Literal["opencode", "zed", "hermes", "codex", "claude_code", "aider"] = "opencode"
    config: dict = Field(default_factory=dict)
    raw_text: str | None = None


class McpImportJsonRequest(BaseModel):
    raw_json: str
    default_scope: str = "shared"


class McpToolParamSpec(BaseModel):
    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""
    required: bool = True


class McpToolSpec(BaseModel):
    name: str
    description: str = ""
    parameters: list[McpToolParamSpec] = Field(default_factory=list)


class McpBuildRequest(BaseModel):
    id: str
    name: str
    description: str = ""
    scope: str = "shared"
    tools: list[McpToolSpec] = Field(default_factory=list)
    code: str | None = None


class McpScriptSyncRequest(BaseModel):
    id: str
    code: str


class McpEnvVarSpec(BaseModel):
    name: str
    description: str = ""
    is_required: bool = False
    is_secret: bool = False
    default: str | None = None


class McpRegistryResult(BaseModel):
    id: str
    registry_name: str
    description: str = ""
    source: Literal["official", "npm"] = "official"
    version: str = ""
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    repo_url: str | None = None
    website_url: str | None = None
    downloads_weekly: int | None = None
    updated_at: str | None = None
    env_vars: list[McpEnvVarSpec] = Field(default_factory=list)


class SkillRegistryResult(BaseModel):
    id: str
    source: str
    skill_id: str
    name: str
    installs: int = 0


class SkillRegistryInstallRequest(BaseModel):
    source: str
    skill_id: str
    name: str | None = None


# ── Agent Chat & Workspace Tool Schemas ──────────────────────────────────────


class ToolExecRequest(BaseModel):
    tool: Literal["list_dir", "read_file", "write_file", "edit_file", "grep_search", "run_command"]
    arguments: dict = Field(default_factory=dict)
    workspace_subpath: str = ""


class ToolExecResponse(BaseModel):
    ok: bool
    output: str
    diff: str | None = None
    exit_code: int | None = None
    verification: dict | None = None
    sandbox: str | None = None


class ConversationSummary(BaseModel):
    id: str
    title: str
    model: str = "auto"
    exec_node: str = ""
    workspace_subpath: str = ""
    tools_enabled: bool = True
    web_search_enabled: bool = False
    auto_approve: bool = False
    auto_compact: bool = True
    pinned: bool = False
    compacted_summary: str = ""
    compacted_through_ts: float = 0.0
    enabled_skills: list[str] = Field(default_factory=list)
    enabled_mcps: list[str] = Field(default_factory=list)
    system_prompt: str = ""
    deleted_at: float | None = None
    created_at: float
    updated_at: float


class ConversationCreate(BaseModel):
    title: str = "New Chat"
    model: str = "auto"
    exec_node: str = ""
    workspace_subpath: str = ""
    tools_enabled: bool = True
    web_search_enabled: bool = False
    auto_approve: bool = False
    auto_compact: bool = True
    pinned: bool = False
    enabled_skills: list[str] = Field(default_factory=list)
    enabled_mcps: list[str] = Field(default_factory=list)
    system_prompt: str = ""


class WorkspaceDirListing(BaseModel):
    root: str
    subpath: str
    parent: str | None = None
    dirs: list[str] = Field(default_factory=list)


class ConversationUpdate(BaseModel):
    title: str | None = None
    model: str | None = None
    exec_node: str | None = None
    workspace_subpath: str | None = None
    tools_enabled: bool | None = None
    web_search_enabled: bool | None = None
    auto_approve: bool | None = None
    auto_compact: bool | None = None
    pinned: bool | None = None
    enabled_skills: list[str] | None = None
    enabled_mcps: list[str] | None = None
    system_prompt: str | None = None


class CompactResponse(BaseModel):
    ok: bool = True
    compacted: bool = False
    compacted_messages: int = 0
    kept_messages: int = 0
    summary: str = ""
    compacted_through_ts: float = 0.0
    estimated_tokens_before: int = 0
    estimated_tokens_after: int = 0
    detail: str | None = None


class ConversationBulkRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, min_length=1, max_length=500)


class ConversationBulkResponse(BaseModel):
    ok: bool = True
    affected_count: int = 0
    ids: list[str] = Field(default_factory=list)


class ChatMessageRecord(BaseModel):
    id: str
    conversation_id: str
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    reasoning: str = ""
    images: list[str] = Field(default_factory=list)
    tool_calls: list[dict] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    meta: dict = Field(default_factory=dict)
    created_at: float


class ChatSendRequest(BaseModel):
    content: str
    images: list[str] = Field(default_factory=list)
    temperature: float | None = None


class ApprovalDecision(BaseModel):
    approved: bool


# ── Image & Video Generation (Unified Media Plane) ───────────────────────────


class MediaGenerateRequest(BaseModel):
    kind: MediaKind = "image"
    prompt: str = Field(..., min_length=1, max_length=8000)
    negative_prompt: str = Field(default="", max_length=4000)
    node_id: str = "auto"
    model: str = ""
    width: int = Field(default=1024, ge=128, le=2048)
    height: int = Field(default=1024, ge=128, le=2048)
    steps: int = Field(default=20, ge=1, le=150)
    cfg_scale: float = Field(default=7.0, ge=0.0, le=30.0)
    seed: int | None = None
    frames: int = Field(default=16, ge=2, le=240)
    fps: int = Field(default=8, ge=1, le=60)
    init_image: str | None = None
    strength: float = Field(default=0.7, ge=0.05, le=1.0)
    evict_llm_vram: bool = True
    workflow_json: str | None = None


class MediaJob(BaseModel):
    id: str
    kind: MediaKind = "image"
    status: Literal["queued", "evicting_vram", "running", "completed", "error"] = "queued"
    progress: int = 0
    stage: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    node_id: str = ""
    media_engine: MediaEngineKind = "none"
    model: str = ""
    width: int = 1024
    height: int = 1024
    steps: int = 20
    cfg_scale: float = 7.0
    seed: int | None = None
    frames: int = 16
    fps: int = 8
    filename: str | None = None
    file_url: str | None = None
    mime_type: str | None = None
    duration_ms: float | None = None
    error: str | None = None
    created_at: float
    completed_at: float | None = None


class MediaBackendNode(BaseModel):
    node_id: str
    display_name: str
    role: NodeRole
    online: bool
    media_engine: MediaEngineKind
    media_healthy: bool
    media_url: str
    media_models: list[str] = Field(default_factory=list)
    vram_free_mb: float = 0.0
    vram_total_mb: float = 0.0
    busy_reason: str | None = None


class NodeMediaEngineStatus(BaseModel):
    node_id: str
    sd_installed: bool = False
    sd_binary: str = ""
    sd_version: str = ""
    sd_backend: str = ""
    sd_managed_running: bool = False
    sd_running_model: str = ""
    sd_error: str | None = None
    install_in_progress: bool = False
    install_step: str = ""
    media_engine: MediaEngineKind = "none"
    media_healthy: bool = False
    media_url: str = ""
    local_media_models: list[LocalModelFile] = Field(default_factory=list)
    active_downloads: list[DownloadJob] = Field(default_factory=list)


class StartMediaEngineRequest(BaseModel):
    model_file: str = Field(..., min_length=1, max_length=256)
    port: int = Field(default=8082, ge=1024, le=65535)
    vae: str = Field(default="", max_length=256)
    t5xxl: str = Field(default="", max_length=256)
    clip_l: str = Field(default="", max_length=256)
    llm: str = Field(default="", max_length=256)


class SdCliVideoRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    negative_prompt: str = ""
    model_file: str = Field(default="", max_length=256)
    width: int = Field(default=832, ge=128, le=1920)
    height: int = Field(default=480, ge=128, le=1920)
    steps: int = Field(default=20, ge=1, le=100)
    cfg_scale: float = Field(default=6.0, ge=0.0, le=30.0)
    seed: int = 42
    frames: int = Field(default=17, ge=5, le=241)
    fps: int = Field(default=16, ge=1, le=60)


class MediaHfFile(BaseModel):
    filename: str
    size_gb: float = 0.0
    format: Literal["gguf", "safetensors"] = "safetensors"


class MediaHfRepoResult(BaseModel):
    repo_id: str
    downloads: int = 0
    likes: int = 0
    pipeline_tag: str = ""
    last_modified: str | None = None
    gated: bool = False
    file_count: int = 0


class InstallMediaEngineRequest(BaseModel):
    backend: Literal["auto", "cuda", "rocm", "vulkan", "metal", "cpu"] = "auto"
    force: bool = False


class OpenAIImageRequest(BaseModel):
    model: str = "auto"
    prompt: str = Field(..., min_length=1, max_length=8000)
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    response_format: Literal["url", "b64_json"] = "b64_json"
    negative_prompt: str = ""
    steps: int = Field(default=20, ge=1, le=150)
    cfg_scale: float = Field(default=7.0, ge=0.0, le=30.0)
    seed: int | None = None
    node_id: str = "auto"
    evict_llm_vram: bool = True


class OpenAIVideoRequest(BaseModel):
    model: str = "auto"
    prompt: str = Field(..., min_length=1, max_length=8000)
    negative_prompt: str = ""
    size: str = "768x512"
    frames: int = Field(default=16, ge=2, le=240)
    fps: int = Field(default=8, ge=1, le=60)
    steps: int = Field(default=20, ge=1, le=150)
    cfg_scale: float = Field(default=6.0, ge=0.0, le=30.0)
    seed: int | None = None
    init_image: str | None = None
    node_id: str = "auto"
    evict_llm_vram: bool = True
    workflow_json: str | None = None


# ── Runtime Settings, Client Connect & Node Provisioning Schemas ─────────────


class RuntimeSettings(BaseModel):
    non_llm_vram_threshold_mb: float = 4096.0
    min_vram_free_mb: float = 4096.0
    max_external_gpu_util_percent: float = 90.0
    yield_cooldown_s: float = 60.0
    auto_evict_on_yield: bool = True
    poll_interval_s: float = 3.0
    auto_heavy_char_threshold: int = 12000
    max_traces: int = 120
    public_url: str = ""
    searxng_url: str = ""
    brave_api_key_set: bool = False
    tavily_api_key_set: bool = False
    auth_enabled: bool = False
    agent_token_enabled: bool = False
    tls_enabled: bool = False
    hsts_enabled: bool = True
    trusted_proxies: str = "127.0.0.1,::1"
    allowed_origins: list[str] = Field(default_factory=list)
    login_max_attempts: int = 10
    login_window_s: float = 60.0
    remote_provision_enabled: bool = False


class RuntimeSettingsPatch(BaseModel):
    non_llm_vram_threshold_mb: float | None = Field(default=None, ge=256.0, le=262144.0)
    min_vram_free_mb: float | None = Field(default=None, ge=0.0, le=262144.0)
    max_external_gpu_util_percent: float | None = Field(default=None, ge=10.0, le=100.0)
    yield_cooldown_s: float | None = Field(default=None, ge=0.0, le=3600.0)
    auto_evict_on_yield: bool | None = None
    poll_interval_s: float | None = Field(default=None, ge=1.0, le=60.0)
    auto_heavy_char_threshold: int | None = Field(default=None, ge=500, le=500000)
    max_traces: int | None = Field(default=None, ge=10, le=1000)
    public_url: str | None = None
    searxng_url: str | None = None
    hsts_enabled: bool | None = None
    trusted_proxies: str | None = None
    allowed_origins: list[str] | None = None
    login_max_attempts: int | None = Field(default=None, ge=1, le=100)
    login_window_s: float | None = Field(default=None, ge=5.0, le=3600.0)


class ClientProbeRequest(BaseModel):
    protocol: Literal[
        "models",
        "openai_chat",
        "openai_responses",
        "anthropic_messages",
        "anthropic_count_tokens",
    ] = "models"
    model: str = "auto"


class ClientProbeResponse(BaseModel):
    ok: bool
    protocol: str
    endpoint: str
    status_code: int = 0
    latency_ms: float = 0.0
    resolved_model: str = ""
    snippet: str = ""
    error: str | None = None


class ClientPushRequest(BaseModel):
    client: Literal["opencode", "zed", "hermes", "codex", "claude_code", "aider"]
    node_id: str
    model: str = "heavy"
    router_url: str = ""


class JoinTokenCreateRequest(BaseModel):
    ttl_seconds: int = Field(default=900, ge=60, le=3600)
    node_id: str = ""
    role: NodeRole = "gpu"
    yield_on_gaming: bool = True
    engine: Literal["auto", "llama.cpp", "ollama", "none"] = "auto"
    backend: Literal["auto", "cuda", "rocm", "vulkan", "metal", "cpu"] = "auto"
    with_media: bool = False
    enable_tools: bool = False
    router_url: str = ""


class JoinTokenResponse(BaseModel):
    id: str
    token: str
    expires_at: float
    ttl_seconds: int
    router_url: str
    linux_macos_cmd: str
    windows_ps_cmd: str


class JoinTokenInfo(BaseModel):
    id: str
    node_hint: str = ""
    created_at: float
    expires_at: float
    used: bool = False


class NodeSshTestRequest(BaseModel):
    ssh_target: str
    ssh_port: int = Field(default=22, ge=1, le=65535)


class NodeSshTestResponse(BaseModel):
    ok: bool
    ssh_target: str
    os_info: str = ""
    gpu_summary: str = ""
    splitch_installed: bool = False
    error: str | None = None


class NodeSshProvisionRequest(BaseModel):
    ssh_target: str
    ssh_port: int = Field(default=22, ge=1, le=65535)
    node_id: str = ""
    display_name: str = ""
    role: NodeRole = "gpu"
    yield_on_gaming: bool = True
    yield_gpu_indices: list[int] = Field(default_factory=list)
    engine: Literal["auto", "llama.cpp", "ollama", "none"] = "auto"
    backend: Literal["auto", "cuda", "rocm", "vulkan", "metal", "cpu"] = "auto"
    with_media: bool = False
    enable_tools: bool = False
    install_service: bool = True
    router_url: str = ""
    agent_port: int = Field(default=8090, ge=1024, le=65535)
    engine_port: int = Field(default=8081, ge=1024, le=65535)
