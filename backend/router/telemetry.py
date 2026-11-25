"""Background telemetry poller, dynamic node registry, and WebSocket broadcaster."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from backend.desktop.icons import normalize_accent_hex
from backend.router.config import (
    SplitchConfig,
    default_data_root,
    load_control_state,
    save_control_state,
)
from backend.router.policy import (
    check_node_eligibility,
    is_splitch_recently_active,
    watched_gpu_metrics,
)
from backend.shared.engines import get_engine_status
from backend.shared.models import (
    AliasConfig,
    ControlState,
    DashboardSnapshot,
    NodeRegisterRequest,
    NodeStatus,
    NodeTelemetry,
    NodeUpdateRequest,
    TraceEntry,
    UiThemeConfig,
    UiThemeUpdate,
    UpdateSummary,
)
from backend.shared.secrets import (
    SecretVault,
    load_ed25519_private_key,
    public_key_to_compact,
    resolve_secret_value,
    sign_agent_request_ed25519,
)
from backend.shared.updater import check_update
from backend.version import __version__ as VERSION


class ClusterState:
    def __init__(self, config: SplitchConfig, data_dir: Path | None = None) -> None:
        self.config = config
        self.data_dir = data_dir
        self.started_at = time.time()
        self.state_path = Path(config.router.state_file)
        self.control: ControlState = load_control_state(self.state_path)
        self.vault = SecretVault(data_dir or (default_data_root() / "data"))
        try:
            self.agent_token: str = resolve_secret_value(
                os.getenv("SPLITCH_AGENT_TOKEN", ""), vault=self.vault
            )
        except (OSError, PermissionError, ValueError):
            self.agent_token = ""
        self.router_private_key = None
        self.router_public_key_compact: str = ""
        raw_priv = os.getenv("SPLITCH_ROUTER_PRIVATE_KEY", "").strip()
        if not raw_priv and data_dir is not None:
            default_priv = data_dir / "router_ed25519.pem"
            if default_priv.is_file():
                raw_priv = str(default_priv)
        if raw_priv:
            with contextlib.suppress(OSError, PermissionError, ValueError, TypeError):
                self.router_private_key = load_ed25519_private_key(raw_priv)
                self.router_public_key_compact = public_key_to_compact(
                    self.router_private_key.public_key()
                )
        self.nodes: dict[str, NodeStatus] = {
            n.id: NodeStatus(
                node_id=n.id,
                display_name=n.display_name,
                role=n.role,
                yield_on_gaming=n.yield_on_gaming,
                yield_gpu_indices=list(n.yield_gpu_indices),
                agent_url=n.agent_url.rstrip("/"),
                engine_url=n.engine_url.rstrip("/"),
                media_url=n.media_url.rstrip("/"),
            )
            for n in config.nodes
        }
        self._overridden_node_ids: set[str] = set()
        self._load_dynamic_nodes()
        self.theme: UiThemeConfig = self._load_ui_theme()
        self.aliases: dict[str, AliasConfig] = {a.alias: a for a in config.aliases}
        self.traces: deque[TraceEntry] = deque(maxlen=config.router.max_traces)
        self._ws_clients: set[WebSocket] = set()
        self._poll_client: httpx.AsyncClient | None = None
        self._proxy_client: httpx.AsyncClient | None = None
        self._poll_task: asyncio.Task | None = None
        self.update_summary: UpdateSummary | None = None
        self._update_check_task: asyncio.Task | None = None

    def agent_headers(self, method: str = "ANY", path: str = "*") -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.agent_token:
            headers["Authorization"] = f"Bearer {self.agent_token}"
        if self.router_private_key is not None:
            headers.update(sign_agent_request_ed25519(self.router_private_key, method, path))
        return headers

    def _dynamic_nodes_path(self) -> Path | None:
        if self.data_dir is None:
            return None
        return self.data_dir / "nodes.json"

    def _ui_theme_path(self) -> Path | None:
        if self.data_dir is None:
            return None
        return self.data_dir / "ui_theme.json"

    def _load_ui_theme(self) -> UiThemeConfig:
        p = self._ui_theme_path()
        if not p or not p.exists():
            return UiThemeConfig()
        try:
            return UiThemeConfig.model_validate(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError):
            return UiThemeConfig()

    async def _persist_ui_theme(self) -> None:
        p = self._ui_theme_path()
        if not p:
            return
        payload = self.theme.model_dump_json(indent=2)

        def _write_theme() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(payload + "\n", encoding="utf-8")
            if os.name != "nt":
                with contextlib.suppress(OSError):
                    p.chmod(0o600)

        await asyncio.to_thread(_write_theme)

    def _load_dynamic_nodes(self) -> None:
        p = self._dynamic_nodes_path()
        if not p or not p.exists():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    reg = NodeRegisterRequest.model_validate(item)
                    if reg.id in self.nodes:
                        self._overridden_node_ids.add(reg.id)
                    self.nodes[reg.id] = NodeStatus(
                        node_id=reg.id,
                        display_name=reg.display_name,
                        role=reg.role,
                        yield_on_gaming=reg.yield_on_gaming,
                        yield_gpu_indices=list(reg.yield_gpu_indices),
                        agent_url=reg.agent_url.rstrip("/"),
                        engine_url=reg.engine_url.rstrip("/"),
                        media_url=reg.media_url.rstrip("/"),
                    )
        except (OSError, ValueError, ValidationError):
            return

    async def _persist_dynamic_nodes(self) -> None:
        p = self._dynamic_nodes_path()
        if not p:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        static_ids = {n.id for n in self.config.nodes}
        dynamic = [
            NodeRegisterRequest(
                id=n.node_id,
                display_name=n.display_name,
                role=n.role,
                yield_on_gaming=n.yield_on_gaming,
                yield_gpu_indices=list(n.yield_gpu_indices),
                agent_url=n.agent_url,
                engine_url=n.engine_url,
                media_url=n.media_url,
            ).model_dump()
            for n in self.nodes.values()
            if n.node_id not in static_ids or n.node_id in self._overridden_node_ids
        ]
        def _write_nodes() -> None:
            p.write_text(json.dumps(dynamic, indent=2), encoding="utf-8")
            if os.name != "nt":
                with contextlib.suppress(OSError):
                    p.chmod(0o600)

        await asyncio.to_thread(_write_nodes)

    async def register_node(self, req: NodeRegisterRequest) -> NodeStatus:
        node = NodeStatus(
            node_id=req.id,
            display_name=req.display_name,
            role=req.role,
            yield_on_gaming=req.yield_on_gaming,
            yield_gpu_indices=list(req.yield_gpu_indices),
            agent_url=req.agent_url.rstrip("/"),
            engine_url=req.engine_url.rstrip("/"),
            media_url=req.media_url.rstrip("/"),
        )
        self.nodes[req.id] = node
        await self._persist_dynamic_nodes()
        if self._poll_client is not None:
            await self._poll_single(node)
        await self.broadcast_snapshot()
        return node

    async def update_node(self, node_id: str, patch: NodeUpdateRequest) -> NodeStatus | None:
        node = self.nodes.get(node_id)
        if node is None:
            return None
        if patch.display_name is not None and patch.display_name.strip():
            node.display_name = patch.display_name.strip()
        if patch.role is not None:
            node.role = patch.role
        if patch.yield_on_gaming is not None:
            node.yield_on_gaming = patch.yield_on_gaming
        if patch.yield_gpu_indices is not None:
            node.yield_gpu_indices = list(patch.yield_gpu_indices)
        if patch.agent_url is not None and patch.agent_url.strip():
            node.agent_url = patch.agent_url.strip().rstrip("/")
        if patch.engine_url is not None and patch.engine_url.strip():
            node.engine_url = patch.engine_url.strip().rstrip("/")
        if patch.media_url is not None:
            node.media_url = patch.media_url.strip().rstrip("/")
        self._overridden_node_ids.add(node_id)
        await self._persist_dynamic_nodes()
        if self._poll_client is not None:
            await self._poll_single(node)
        await self.broadcast_snapshot()
        return node

    async def remove_node(self, node_id: str) -> bool:
        if node_id not in self.nodes:
            return False
        del self.nodes[node_id]
        if self.control.force_node == node_id:
            self.control.force_node = "auto"
            await save_control_state(self.state_path, self.control)
        await self._persist_dynamic_nodes()
        await self.broadcast_snapshot()
        return True

    @property
    def proxy_client(self) -> httpx.AsyncClient:
        assert self._proxy_client is not None
        return self._proxy_client

    async def start(self) -> None:
        ca_bundle = (
            os.getenv("SPLITCH_CA_BUNDLE", "").strip() or self.config.tls.ca_bundle.strip()
        )
        verify: bool | str = ca_bundle if ca_bundle else True
        self._poll_client = httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, connect=1.5),
            verify=verify,
        )
        self._proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_keepalive_connections=16, max_connections=64),
            verify=verify,
        )
        await self.poll_all_nodes()
        self._poll_task = asyncio.create_task(self._poll_loop())
        if not os.getenv("PYTEST_CURRENT_TEST") and self._update_check_interval_s() > 0:
            self._update_check_task = asyncio.create_task(self._update_check_loop())

    async def stop(self) -> None:
        if self._update_check_task:
            self._update_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_check_task
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        if self._poll_client:
            await self._poll_client.aclose()
        if self._proxy_client:
            await self._proxy_client.aclose()

    @staticmethod
    def _update_check_interval_s() -> float:
        raw = os.getenv("SPLITCH_UPDATE_CHECK_INTERVAL_S", "21600").strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 21600.0

    async def refresh_update_summary(self, *, fetch_remote: bool = True) -> UpdateSummary:
        up_status = None
        eng_status = None
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            up_status = await asyncio.to_thread(check_update, fetch_remote=fetch_remote)
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            eng_status = await asyncio.to_thread(get_engine_status, check_remote=fetch_remote)
        summary = UpdateSummary(
            update_available=bool(up_status and up_status.update_available),
            behind_count=int(up_status.behind_count) if up_status else 0,
            local_sha=(up_status.local_sha if up_status else "") or "",
            remote_sha=(up_status.remote_sha if up_status else "") or "",
            remote_version=(up_status.remote_version if up_status else "") or "",
            checked_at=time.time(),
            engine_update_available=bool(eng_status and eng_status.update_available),
            latest_llama_tag=(eng_status.latest_llama_tag if eng_status else "") or "",
        )
        self.update_summary = summary
        await self.broadcast_snapshot()
        return summary

    async def _update_check_loop(self) -> None:
        await asyncio.sleep(15.0)
        while True:
            await self.refresh_update_summary(fetch_remote=True)
            interval = self._update_check_interval_s()
            if interval <= 0:
                return
            await asyncio.sleep(interval)

    async def _auto_evict_loaded_models(
        self, node: NodeStatus, telemetry: NodeTelemetry
    ) -> None:
        if self._poll_client is None or node.in_flight > 0:
            return
        loaded = [m for m in telemetry.models if m.status == "loaded"]
        for m in loaded:
            with contextlib.suppress(httpx.HTTPError, OSError):
                resp = await self._poll_client.post(
                    f"{node.agent_url}/models/unload",
                    json={"model": m.id},
                    headers=self.agent_headers("POST", "/models/unload"),
                    timeout=10.0,
                )
                if resp.status_code < 400:
                    m.status = "unloaded"
        if telemetry.strata_healthy:
            with contextlib.suppress(httpx.HTTPError, OSError):
                resp = await self._poll_client.post(
                    f"{node.agent_url}/strata/stop",
                    json={},
                    headers=self.agent_headers("POST", "/strata/stop"),
                    timeout=12.0,
                )
                if resp.status_code < 400:
                    telemetry.strata_healthy = False
                    telemetry.strata_active_model = ""
                    telemetry.strata_models = []

    async def persist_aliases(self) -> None:
        base_dir = self.data_dir or (default_data_root() / "data")
        aliases_file = base_dir / "aliases.json"
        base_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([a.model_dump() for a in self.aliases.values()], indent=2)
        await asyncio.to_thread(aliases_file.write_text, payload, "utf-8")

    async def auto_heal_aliases_for_node(self, node: NodeStatus) -> list[str]:
        """Rebind broken/placeholder aliases targeting `node` once real models are indexed."""
        t = node.telemetry
        if t is None or not t.models:
            return []
        real_ids = [
            m.id
            for m in t.models
            if m.id and not any(skip in m.id.lower() for skip in ("mmproj", "imatrix"))
        ]
        if not real_ids:
            return []
        available_set = set(real_ids)
        embed_tokens = ("embed", "bge-", "bge_", "nomic", "e5-", "gte-", "mxbai", "minilm")
        embed_ids = [m for m in real_ids if any(tok in m.lower() for tok in embed_tokens)]
        gen_ids = [m for m in real_ids if m not in embed_ids]

        healed: list[str] = []
        for alias in self.aliases.values():
            replacement = (embed_ids[0] if embed_ids else None) if alias.task_kind == "embed" else (gen_ids[0] if gen_ids else None)
            if not replacement:
                continue
            changed = False
            if alias.primary_node == node.node_id and alias.primary_model not in available_set:
                alias.primary_model = replacement
                changed = True
            if (
                alias.fallback_node == node.node_id
                and alias.fallback_model
                and alias.fallback_model not in available_set
            ):
                alias.fallback_model = replacement
                changed = True
            if changed:
                healed.append(alias.alias)

        if healed:
            with contextlib.suppress(OSError):
                await self.persist_aliases()
        return healed

    async def _poll_single(self, node: NodeStatus) -> None:
        assert self._poll_client is not None
        was_online = node.online
        t0 = time.perf_counter()
        try:
            resp = await self._poll_client.get(
                f"{node.agent_url}/metrics",
                headers=self.agent_headers("GET", "/metrics"),
            )
            resp.raise_for_status()
            telemetry = NodeTelemetry.model_validate(resp.json())
            node.online = True
            node.telemetry = telemetry
            node.latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            if (
                not was_online
                and self.theme.updated_at > 0
                and not os.getenv("PYTEST_CURRENT_TEST")
                and os.getenv("SPLITCH_SYNC_DESKTOP_ON_ROUTER", "1") != "0"
            ):
                asyncio.create_task(
                    self._push_theme_to_node(node, self.theme.model_dump(mode="json"))
                )
            if telemetry.models:
                await self.auto_heal_aliases_for_node(node)
            if node.in_flight > 0:
                node.last_splitch_work_ts = time.time()
            non_llm_mb, gpu_util, _, _ = watched_gpu_metrics(node)
            is_ext_gpu_busy = node.role == "gpu" and (
                non_llm_mb >= self.config.guards.non_llm_vram_threshold_mb
                or (
                    gpu_util >= self.config.guards.max_external_gpu_util_percent
                    and not is_splitch_recently_active(node)
                )
            )
            if is_ext_gpu_busy and node.yield_on_gaming:
                if self.config.guards.yield_cooldown_s > 0:
                    node.yield_until_ts = time.time() + self.config.guards.yield_cooldown_s
                if self.config.guards.auto_evict_on_yield and node.in_flight == 0:
                    await self._auto_evict_loaded_models(node, telemetry)
            node.busy_reason = check_node_eligibility(
                node, "", self.control, self.config.guards
            )
        except (httpx.HTTPError, ValueError, ValidationError):
            node.online = False
            node.latency_ms = 0.0
            node.busy_reason = f"{node.node_id}_offline"

    async def poll_all_nodes(self) -> None:
        if not self.nodes:
            return
        await asyncio.gather(*(self._poll_single(n) for n in self.nodes.values()))
        await self.broadcast_snapshot()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.router.poll_interval_s)
            await self.poll_all_nodes()

    def build_snapshot(self) -> DashboardSnapshot:
        for node in self.nodes.values():
            node.busy_reason = check_node_eligibility(
                node, "", self.control, self.config.guards
            )
        return DashboardSnapshot(
            version=VERSION,
            uptime_s=round(time.time() - self.started_at, 1),
            control=self.control,
            theme=self.theme,
            nodes=list(self.nodes.values()),
            aliases=list(self.aliases.values()),
            traces=list(self.traces),
            update_summary=self.update_summary,
        )

    async def _push_theme_to_node(
        self, node: NodeStatus, theme_payload: dict[str, Any]
    ) -> bool:
        """Best-effort signed push of active UI theme to a worker node's desktop icons."""
        client = self._poll_client
        if client is None or not node.online:
            return False
        try:
            resp = await client.post(
                f"{node.agent_url}/desktop/sync-theme",
                json=theme_payload,
                headers=self.agent_headers("POST", "/desktop/sync-theme"),
                timeout=8.0,
            )
            return resp.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    async def update_theme(self, update: UiThemeUpdate) -> UiThemeConfig:
        if update.accent is not None:
            raw = update.accent.strip()
            if raw and not normalize_accent_hex(raw):
                raise ValueError("accent must be empty or a 6-digit hex color like #a78bfa")
            self.theme.accent = normalize_accent_hex(raw)
        if update.scheme is not None:
            self.theme.scheme = update.scheme
        if update.surface is not None:
            self.theme.surface = update.surface
        if update.resolved_light is not None:
            self.theme.resolved_light = update.resolved_light
        self.theme.updated_at = time.time()
        await self._persist_ui_theme()
        await self.broadcast_snapshot()
        # Best-effort dual-path icon sync:
        # 1) Local host shortcuts (without fragile DISPLAY env gating under systemd/launchd)
        # 2) Signed push to all online agents so remote PCs/Macs/Windows sync even without tray
        if (
            not os.getenv("PYTEST_CURRENT_TEST")
            and os.getenv("SPLITCH_SYNC_DESKTOP_ON_ROUTER", "1") != "0"
        ):
            theme_copy = self.theme.model_copy()

            def _sync_local_host_icons() -> None:
                from backend.desktop.shortcuts import (
                    desktop_shortcuts_installed,
                    sync_desktop_icons,
                )

                if (
                    os.getenv("SPLITCH_NO_DESKTOP", "").strip().lower() not in ("1", "true", "yes")
                    and desktop_shortcuts_installed()
                ):
                    with contextlib.suppress(OSError, RuntimeError, ValueError):
                        sync_desktop_icons(theme_copy)

            asyncio.create_task(asyncio.to_thread(_sync_local_host_icons))
            theme_json = theme_copy.model_dump(mode="json")
            for node in list(self.nodes.values()):
                if node.online:
                    asyncio.create_task(self._push_theme_to_node(node, theme_json))
        return self.theme

    async def update_control(
        self, gaming_mode: bool | None = None, force_node: str | None = None
    ) -> ControlState:
        if gaming_mode is not None:
            self.control.gaming_mode = gaming_mode
        if force_node is not None and (force_node == "auto" or force_node in self.nodes):
            self.control.force_node = force_node
        await save_control_state(self.state_path, self.control)
        await self.broadcast_snapshot()
        return self.control

    async def record_trace(self, entry: TraceEntry) -> None:
        self.traces.appendleft(entry)
        await self.broadcast_snapshot()

    def register_ws(self, ws: WebSocket) -> None:
        self._ws_clients.add(ws)

    def unregister_ws(self, ws: WebSocket) -> None:
        self._ws_clients.discard(ws)

    async def broadcast_snapshot(self) -> None:
        if not self._ws_clients:
            return
        payload = self.build_snapshot().model_dump(mode="json")
        dead: list[WebSocket] = []
        for ws in list(self._ws_clients):
            try:
                await ws.send_json(payload)
            except (WebSocketDisconnect, RuntimeError, OSError):
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    async def proxy_model_action(
        self, node_id: str, action: str, model: str
    ) -> tuple[int, dict[str, Any]]:
        node = self.nodes.get(node_id)
        if not node or not node.online:
            return 503, {"detail": f"Node '{node_id}' is offline"}
        assert self._poll_client is not None
        try:
            resp = await self._poll_client.post(
                f"{node.agent_url}/models/{action}",
                json={"model": model},
                headers=self.agent_headers("POST", f"/models/{action}"),
                timeout=60.0 if action == "load" else 15.0,
            )
            await self._poll_single(node)
            await self.broadcast_snapshot()
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, {"detail": resp.text[:300]}
        except httpx.HTTPError as exc:
            return 502, {"detail": str(exc)}
