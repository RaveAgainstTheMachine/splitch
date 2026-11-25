"""Router control-plane endpoints for Managed Strata Sparse-MoE Engine across cluster nodes."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, HTTPException

from backend.router.telemetry import ClusterState
from backend.shared.models import (
    NodeStrataStatus,
    StrataCalibrateRequest,
    StrataCatalogEntry,
    StrataImportPlanRequest,
    StrataInstallRequest,
    StrataLaunchRequest,
)
from backend.shared.strata_manager import STRATA_CATALOG, assess_strata_fit


def build_strata_router(cluster: ClusterState) -> APIRouter:
    router = APIRouter(prefix="/api/strata", tags=["strata"])

    @router.get("/catalog", response_model=list[StrataCatalogEntry])
    async def get_strata_catalog() -> list[StrataCatalogEntry]:
        entries: list[StrataCatalogEntry] = []
        for spec in STRATA_CATALOG:
            fits = {
                nid: assess_strata_fit(spec, node) for nid, node in cluster.nodes.items()
            }
            entries.append(StrataCatalogEntry(spec=spec, fits=fits))
        return entries

    @router.get("/status", response_model=list[NodeStrataStatus])
    async def get_cluster_strata_status() -> list[NodeStrataStatus]:
        out: list[NodeStrataStatus] = []
        headers = cluster.agent_headers("GET", "/strata/status")

        async def _fetch(node_id: str, agent_url: str) -> None:
            try:
                async with httpx.AsyncClient(timeout=6.0, headers=headers) as client:
                    r = await client.get(f"{agent_url}/strata/status")
                    if r.status_code == 200:
                        out.append(NodeStrataStatus.model_validate(r.json()))
                        return
            except (httpx.HTTPError, ValueError):
                pass
            out.append(NodeStrataStatus(node_id=node_id, error="Agent unreachable"))

        await asyncio.gather(
            *(_fetch(n.node_id, n.agent_url) for n in cluster.nodes.values() if n.online)
        )
        out.sort(key=lambda s: s.node_id)
        return out

    async def _proxy_node_post(
        node_id: str, agent_path: str, payload: dict | None = None, *, timeout: float = 20.0
    ) -> dict:
        node = cluster.nodes.get(node_id)
        if not node or not node.online:
            raise HTTPException(status_code=503, detail=f"Node '{node_id}' is offline")
        try:
            async with httpx.AsyncClient(
                timeout=timeout, headers=cluster.agent_headers("POST", agent_path)
            ) as client:
                r = await client.post(f"{node.agent_url}{agent_path}", json=payload or {})
                if r.status_code >= 400:
                    detail = r.text[:400]
                    try:
                        detail = str(r.json().get("detail", detail))
                    except ValueError:
                        pass
                    raise HTTPException(status_code=r.status_code, detail=detail)
                return r.json()
        except HTTPException:
            raise
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/nodes/{node_id}/install", response_model=NodeStrataStatus)
    async def install_strata_on_node(
        node_id: str, req: StrataInstallRequest | None = None
    ) -> dict:
        return await _proxy_node_post(
            node_id, "/strata/install", (req or StrataInstallRequest()).model_dump()
        )

    @router.post("/nodes/{node_id}/plan")
    async def plan_strata_on_node(node_id: str, req: StrataLaunchRequest) -> dict:
        return await _proxy_node_post(
            node_id, "/strata/plan", req.model_dump(), timeout=20.0
        )

    @router.post("/nodes/{node_id}/launch", response_model=NodeStrataStatus)
    async def launch_strata_on_node(node_id: str, req: StrataLaunchRequest) -> dict:
        return await _proxy_node_post(
            node_id, "/strata/launch", req.model_dump(), timeout=25.0
        )

    @router.post("/nodes/{node_id}/calibrate", response_model=NodeStrataStatus)
    async def calibrate_strata_on_node(node_id: str, req: StrataCalibrateRequest) -> dict:
        return await _proxy_node_post(
            node_id, "/strata/calibrate", req.model_dump(), timeout=25.0
        )

    @router.post("/nodes/{node_id}/stop", response_model=NodeStrataStatus)
    async def stop_strata_on_node(node_id: str) -> dict:
        return await _proxy_node_post(node_id, "/strata/stop", {}, timeout=20.0)

    @router.post("/nodes/{node_id}/import-plan")
    async def import_strata_plan_on_node(
        node_id: str, req: StrataImportPlanRequest
    ) -> dict:
        return await _proxy_node_post(
            node_id, "/strata/import-plan", req.model_dump(), timeout=20.0
        )

    return router
