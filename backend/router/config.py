"""Configuration loader, zero-config single-node defaults, and runtime state persistence."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
import tomllib
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field, ValidationError

from backend.shared.models import AliasConfig, ControlState, NodeRole


class NodeConfig(BaseModel):
    id: str
    display_name: str
    role: NodeRole = "gpu"
    yield_on_gaming: bool = False
    yield_gpu_indices: list[int] = Field(default_factory=list)
    agent_url: str
    engine_url: str = Field(validation_alias=AliasChoices("engine_url", "llama_url"))
    media_url: str = ""
    storage_path: str = ""


class GuardConfig(BaseModel):
    non_llm_vram_threshold_mb: float = Field(
        default=4096.0,
        validation_alias=AliasChoices("non_llm_vram_threshold_mb", "non_llama_vram_threshold_mb"),
    )
    min_vram_free_mb: float = Field(
        default=4096.0,
        validation_alias=AliasChoices("min_vram_free_mb", "min_pc_vram_free_mb"),
    )
    max_external_gpu_util_percent: float = 90.0
    yield_cooldown_s: float = 60.0
    auto_evict_on_yield: bool = True


class RouterSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    poll_interval_s: float = 3.0
    max_traces: int = 120
    auto_heavy_char_threshold: int = 12000
    state_file: str = ".splitch_state.json"
    public_url: str = ""


class TlsSettings(BaseModel):
    enabled: bool = False
    self_signed: bool = False
    certfile: str = ""
    keyfile: str = ""
    letsencrypt_domain: str = ""
    ca_bundle: str = ""


class SecuritySettings(BaseModel):
    trusted_proxies: str = "127.0.0.1,::1"
    allowed_origins: list[str] = Field(default_factory=list)
    hsts_enabled: bool = True
    login_max_attempts: int = 10
    login_window_s: float = 60.0


class SplitchConfig(BaseModel):
    router: RouterSettings = Field(default_factory=RouterSettings)
    guards: GuardConfig = Field(default_factory=GuardConfig)
    tls: TlsSettings = Field(default_factory=TlsSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    nodes: list[NodeConfig] = Field(default_factory=list)
    aliases: list[AliasConfig] = Field(default_factory=list)


def default_data_root() -> Path:
    explicit = os.getenv("SPLITCH_DATA_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "win32":
        local_app = os.getenv("LOCALAPPDATA", "").strip()
        if local_app:
            return Path(local_app) / "splitch"
    xdg = os.getenv("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg) / "splitch"
    return Path.home() / ".local" / "share" / "splitch"


def _default_single_node_config() -> SplitchConfig:
    """Zero-config local fallback so `splitch up` works out of the box on any single machine."""
    role: NodeRole = "unified" if sys.platform == "darwin" else "gpu"
    host_label = platform.node() or "Local Machine"
    engine_url = os.getenv("SPLITCH_ENGINE_URL", "http://127.0.0.1:8081")
    agent_port = int(os.getenv("SPLITCH_AGENT_PORT", "8090"))
    return SplitchConfig(
        nodes=[
            NodeConfig(
                id="local",
                display_name=host_label,
                role=role,
                yield_on_gaming=(role == "gpu"),
                agent_url=f"http://127.0.0.1:{agent_port}",
                engine_url=engine_url,
                storage_path=str(default_data_root() / "models"),
            )
        ]
    )


def _discover_config_path(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    env_cfg = os.getenv("SPLITCH_CONFIG")
    if env_cfg:
        p = Path(env_cfg)
        return p if p.exists() else None
    for candidate in (
        Path("splitch.local.toml"),
        Path("splitch.toml"),
        Path.home() / ".config" / "splitch" / "splitch.toml",
    ):
        if candidate.exists():
            return candidate
    return None


def load_config(path: str | Path | None = None) -> SplitchConfig:
    cfg_path = _discover_config_path(path)
    if cfg_path is None:
        return _default_single_node_config()
    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)
    return SplitchConfig.model_validate(raw)


def load_control_state(state_path: Path) -> ControlState:
    if not state_path.exists():
        return ControlState()
    try:
        return ControlState.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError):
        return ControlState()


async def save_control_state(state_path: Path, control: ControlState) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = control.model_dump_json(indent=2)

    def _write() -> None:
        state_path.write_text(payload, encoding="utf-8")
        if sys.platform != "win32":
            try:
                state_path.chmod(0o600)
            except OSError:
                pass

    await asyncio.to_thread(_write)
