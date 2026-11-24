"""Router proxy for Workspace File Tree, File Viewer, Safe Git Actions, and AI Commit Messages."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, HTTPException

from backend.router.telemetry import ClusterState
from backend.shared.models import (
    GitActionResponse,
    GitCommitMessageRequest,
    GitCommitMessageResponse,
    GitCommitRequest,
    GitDiffResponse,
    GitPathsRequest,
    GitStatusResponse,
    WorkspaceFileContent,
    WorkspaceTreeListing,
)


def build_workspace_router(
    cluster: ClusterState,
    *,
    internal_auth_headers_getter: Callable[[], dict[str, str]],
    app_ref: FastAPI,
) -> APIRouter:
    router = APIRouter(prefix="/api/nodes/{node_id}", tags=["workspace"])

    def _require_online_node(node_id: str) -> str:
        node = cluster.nodes.get(node_id)
        if not node or not node.online:
            raise HTTPException(status_code=503, detail=f"Node '{node_id}' is offline")
        return node.agent_url

    async def _proxy_agent(
        node_id: str,
        method: str,
        agent_path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        agent_url = _require_online_node(node_id)
        try:
            async with httpx.AsyncClient(
                timeout=12.0,
                headers=cluster.agent_headers(method, agent_path),
            ) as client:
                r = await client.request(
                    method,
                    f"{agent_url}{agent_path}",
                    params=params,
                    json=json_body,
                )
                if r.status_code >= 400:
                    detail = r.text[:300]
                    try:
                        detail = str(r.json().get("detail", detail))
                    except ValueError:
                        pass
                    raise HTTPException(status_code=r.status_code, detail=detail)
                return r.json()
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/workspace/tree", response_model=WorkspaceTreeListing)
    async def proxy_workspace_tree(
        node_id: str, subpath: str = "", dir_path: str = ""
    ) -> WorkspaceTreeListing:
        data = await _proxy_agent(
            node_id,
            "GET",
            "/workspace/tree",
            params={"subpath": subpath, "dir_path": dir_path},
        )
        return WorkspaceTreeListing.model_validate(data)

    @router.get("/workspace/file", response_model=WorkspaceFileContent)
    async def proxy_workspace_file(
        node_id: str, subpath: str = "", path: str = ""
    ) -> WorkspaceFileContent:
        data = await _proxy_agent(
            node_id,
            "GET",
            "/workspace/file",
            params={"subpath": subpath, "path": path},
        )
        return WorkspaceFileContent.model_validate(data)

    @router.get("/git/status", response_model=GitStatusResponse)
    async def proxy_git_status(node_id: str, subpath: str = "") -> GitStatusResponse:
        data = await _proxy_agent(
            node_id,
            "GET",
            "/workspace/git/status",
            params={"subpath": subpath},
        )
        return GitStatusResponse.model_validate(data)

    @router.get("/git/diff", response_model=GitDiffResponse)
    async def proxy_git_diff(
        node_id: str, subpath: str = "", path: str = "", staged: bool = False
    ) -> GitDiffResponse:
        data = await _proxy_agent(
            node_id,
            "GET",
            "/workspace/git/diff",
            params={"subpath": subpath, "path": path, "staged": staged},
        )
        return GitDiffResponse.model_validate(data)

    @router.post("/git/stage", response_model=GitActionResponse)
    async def proxy_git_stage(node_id: str, req: GitPathsRequest) -> GitActionResponse:
        data = await _proxy_agent(
            node_id, "POST", "/workspace/git/stage", json_body=req.model_dump()
        )
        return GitActionResponse.model_validate(data)

    @router.post("/git/unstage", response_model=GitActionResponse)
    async def proxy_git_unstage(node_id: str, req: GitPathsRequest) -> GitActionResponse:
        data = await _proxy_agent(
            node_id, "POST", "/workspace/git/unstage", json_body=req.model_dump()
        )
        return GitActionResponse.model_validate(data)

    @router.post("/git/discard", response_model=GitActionResponse)
    async def proxy_git_discard(node_id: str, req: GitPathsRequest) -> GitActionResponse:
        data = await _proxy_agent(
            node_id, "POST", "/workspace/git/discard", json_body=req.model_dump()
        )
        return GitActionResponse.model_validate(data)

    @router.post("/git/commit", response_model=GitActionResponse)
    async def proxy_git_commit(node_id: str, req: GitCommitRequest) -> GitActionResponse:
        data = await _proxy_agent(
            node_id, "POST", "/workspace/git/commit", json_body=req.model_dump()
        )
        return GitActionResponse.model_validate(data)

    @router.post("/git/generate-commit-message", response_model=GitCommitMessageResponse)
    async def generate_commit_message(
        node_id: str, req: GitCommitMessageRequest
    ) -> GitCommitMessageResponse:
        status_data = await _proxy_agent(
            node_id,
            "GET",
            "/workspace/git/status",
            params={"subpath": req.subpath},
        )
        status = GitStatusResponse.model_validate(status_data)
        if not status.is_git_repo:
            raise HTTPException(status_code=400, detail="Not a git repository")

        staged_changes = [c for c in status.changes if c.staged]
        target_changes = staged_changes or status.changes
        if not target_changes:
            raise HTTPException(status_code=400, detail="No changes available to summarize")

        diff_chunks: list[str] = []
        total_chars = 0
        for ch in target_changes[:12]:
            diff_data = await _proxy_agent(
                node_id,
                "GET",
                "/workspace/git/diff",
                params={"subpath": req.subpath, "path": ch.path, "staged": ch.staged},
            )
            diff_text = str(diff_data.get("diff") or "").strip()
            if not diff_text:
                diff_text = f"File: {ch.path} (status: {ch.index_status}{ch.worktree_status})"
            snippet = diff_text[:3000]
            diff_chunks.append(snippet)
            total_chars += len(snippet)
            if total_chars >= 11_000:
                break

        combined_diff = "\n\n".join(diff_chunks)[:12_000]
        target_model = req.model.strip() or "fast"

        transport = httpx.ASGITransport(app=app_ref)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://splitch.internal",
                headers=internal_auth_headers_getter(),
                timeout=httpx.Timeout(45.0, connect=5.0),
            ) as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": target_model,
                        "stream": False,
                        "temperature": 0.2,
                        "max_tokens": 120,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Write a concise, imperative Conventional Commit message "
                                    "(e.g. `feat: ...`, `fix: ...`, `refactor: ...`) for the diff. "
                                    "Output ONLY the commit message text with no quotes, markdown fences, or explanation."
                                ),
                            },
                            {"role": "user", "content": combined_diff},
                        ],
                    },
                )
                if resp.status_code >= 400:
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail=f"Commit message generation failed: {resp.text[:200]}",
                    )
                body = resp.json()
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        choices = body.get("choices") or []
        raw_msg = ""
        if choices and isinstance(choices[0], dict):
            msg_obj = choices[0].get("message") or {}
            raw_msg = str(msg_obj.get("content") or "").strip()
        cleaned = raw_msg.strip("`\"' \n")
        return GitCommitMessageResponse(message=cleaned or "chore: update workspace files")

    return router
