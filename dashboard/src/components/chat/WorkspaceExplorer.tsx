import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AtSign,
  ChevronDown,
  ChevronRight,
  FileCode2,
  Folder,
  FolderOpen,
  GitBranch,
  GitCommitHorizontal,
  Minus,
  Plus,
  RefreshCw,
  RotateCcw,
  Sparkles
} from "lucide-react";
import type { ConfirmState } from "../ConfirmDialog";
import type {
  GitFileChange,
  GitStatusResponse,
  WorkspaceTreeEntry,
  WorkspaceTreeListing
} from "../../types";

export interface WorkspaceExplorerProps {
  mode: "files" | "changes";
  nodeId: string;
  subpath: string;
  model: string;
  toolsEnabled: boolean;
  gitStatus: GitStatusResponse | null;
  gitLoading: boolean;
  onRefreshGit: () => Promise<void>;
  onOpenFile: (path: string) => void;
  onOpenDiff: (path: string, staged: boolean) => void;
  onAttachPath: (path: string) => void;
  onOpenWorkspaceDialog: () => void;
  onConfirm: (state: ConfirmState) => void;
  onError: (msg: string | null) => void;
}

function statusBadge(code: string): { label: string; cls: string } | null {
  const c = code.trim();
  if (!c) return null;
  if (c === "?") return { label: "U", cls: "git-badge--U" };
  if (c === "A") return { label: "A", cls: "git-badge--A" };
  if (c === "D") return { label: "D", cls: "git-badge--D" };
  if (c === "M" || c === "R") return { label: c, cls: "git-badge--M" };
  return { label: c, cls: "git-badge--M" };
}

export function WorkspaceExplorer({
  mode,
  nodeId,
  subpath,
  model,
  toolsEnabled,
  gitStatus,
  gitLoading,
  onRefreshGit,
  onOpenFile,
  onOpenDiff,
  onAttachPath,
  onOpenWorkspaceDialog,
  onConfirm,
  onError
}: WorkspaceExplorerProps) {
  const [treeCache, setTreeCache] = useState<Record<string, WorkspaceTreeEntry[]>>({});
  const [expandedDirs, setExpandedDirs] = useState<Record<string, boolean>>({});
  const [loadingDir, setLoadingDir] = useState<string | null>(null);
  const [commitMsg, setCommitMsg] = useState("");
  const [generatingMsg, setGeneratingMsg] = useState(false);
  const [gitBusy, setGitBusy] = useState(false);

  const loadDir = useCallback(
    async (dirPath: string) => {
      if (!nodeId || !toolsEnabled) return;
      setLoadingDir(dirPath);
      try {
        const res = await fetch(
          `/api/nodes/${encodeURIComponent(nodeId)}/workspace/tree?subpath=${encodeURIComponent(
            subpath
          )}&dir_path=${encodeURIComponent(dirPath)}`
        );
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${res.status}`);
        }
        const listing = (await res.json()) as WorkspaceTreeListing;
        setTreeCache((prev) => ({ ...prev, [dirPath]: listing.entries }));
      } catch (err) {
        onError(err instanceof Error ? err.message : "Failed to load directory");
      } finally {
        setLoadingDir((prev) => (prev === dirPath ? null : prev));
      }
    },
    [nodeId, subpath, toolsEnabled, onError]
  );

  useEffect(() => {
    setTreeCache({});
    setExpandedDirs({});
    if (mode === "files" && nodeId && toolsEnabled) {
      void loadDir("");
    }
  }, [mode, nodeId, subpath, toolsEnabled, loadDir]);

  const changeByPath = useMemo(() => {
    const map: Record<string, string> = {};
    for (const ch of gitStatus?.changes ?? []) {
      const code = ch.untracked
        ? "?"
        : ch.worktree_status !== " "
          ? ch.worktree_status
          : ch.index_status;
      map[ch.path] = code;
    }
    return map;
  }, [gitStatus]);

  const dirHasChanges = useCallback(
    (dirPath: string): boolean => {
      const prefix = dirPath ? `${dirPath}/` : "";
      return (gitStatus?.changes ?? []).some((ch) => ch.path.startsWith(prefix));
    },
    [gitStatus]
  );

  const toggleDir = (dirPath: string) => {
    const nextOpen = !expandedDirs[dirPath];
    setExpandedDirs((prev) => ({ ...prev, [dirPath]: nextOpen }));
    if (nextOpen && !treeCache[dirPath]) {
      void loadDir(dirPath);
    }
  };

  const runGitPost = async (endpoint: string, body: Record<string, unknown>) => {
    if (!nodeId) return;
    setGitBusy(true);
    try {
      const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/git/${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subpath, ...body })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || data.detail || `Git ${endpoint} failed`);
      }
      await onRefreshGit();
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : `Git ${endpoint} failed`);
    } finally {
      setGitBusy(false);
    }
  };

  const handleGenerateCommitMsg = async () => {
    if (!nodeId) return;
    setGeneratingMsg(true);
    try {
      const res = await fetch(
        `/api/nodes/${encodeURIComponent(nodeId)}/git/generate-commit-message`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ subpath, model: model || "fast" })
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      if (data.message) setCommitMsg(String(data.message));
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to generate commit message");
    } finally {
      setGeneratingMsg(false);
    }
  };

  const handleCommit = async () => {
    if (!commitMsg.trim()) return;
    await runGitPost("commit", { message: commitMsg.trim() });
    setCommitMsg("");
  };

  const requestDiscard = (path: string) => {
    onConfirm({
      title: `Discard changes to "${path}"?`,
      message: "Uncommitted changes to this file will be permanently lost.",
      confirmLabel: "Discard",
      danger: true,
      onConfirm: async () => {
        await runGitPost("discard", { paths: [path] });
      }
    });
  };

  if (!nodeId || !toolsEnabled) {
    return (
      <div className="ws-empty">
        <p className="muted">
          Select a node with <code>--enable-tools</code> to browse files and Git changes.
        </p>
        <button type="button" className="btn btn--sm" onClick={onOpenWorkspaceDialog}>
          Select Workspace
        </button>
      </div>
    );
  }

  const renderTreeEntries = (dirPath: string, depth: number) => {
    const entries = treeCache[dirPath] ?? [];
    if (entries.length === 0 && loadingDir === dirPath) {
      return (
        <div className="ws-tree__empty muted" style={{ paddingLeft: `${12 + depth * 14}px` }}>
          Loading…
        </div>
      );
    }
    return entries.map((entry) => {
      const isOpen = Boolean(expandedDirs[entry.path]);
      const badge = entry.is_dir
        ? dirHasChanges(entry.path)
          ? { label: "•", cls: "git-badge--M" }
          : null
        : statusBadge(changeByPath[entry.path] ?? entry.git_status);

      return (
        <div key={entry.path}>
          <div
            className="ws-tree__row"
            style={{ paddingLeft: `${8 + depth * 14}px` }}
          >
            <button
              type="button"
              className="ws-tree__btn"
              onClick={() => (entry.is_dir ? toggleDir(entry.path) : onOpenFile(entry.path))}
              title={entry.path}
            >
              {entry.is_dir ? (
                <>
                  {isOpen ? (
                    <ChevronDown size={12} aria-hidden="true" />
                  ) : (
                    <ChevronRight size={12} aria-hidden="true" />
                  )}
                  {isOpen ? (
                    <FolderOpen size={13} aria-hidden="true" className="ws-tree__icon-dir" />
                  ) : (
                    <Folder size={13} aria-hidden="true" className="ws-tree__icon-dir" />
                  )}
                </>
              ) : (
                <>
                  <span className="ws-tree__spacer" aria-hidden="true" />
                  <FileCode2 size={13} aria-hidden="true" className="ws-tree__icon-file" />
                </>
              )}
              <span className="ws-tree__name">{entry.name}</span>
            </button>
            {badge && <span className={`git-badge ${badge.cls}`}>{badge.label}</span>}
            {!entry.is_dir && (
              <button
                type="button"
                className="btn btn--sm btn--ghost ws-tree__attach"
                title={`Attach @${entry.path} to chat`}
                aria-label={`Attach @${entry.path}`}
                onClick={() => onAttachPath(entry.path)}
              >
                <AtSign size={11} aria-hidden="true" />
              </button>
            )}
          </div>
          {entry.is_dir && isOpen && renderTreeEntries(entry.path, depth + 1)}
        </div>
      );
    });
  };

  if (mode === "files") {
    return (
      <div className="ws-panel">
        <div className="ws-panel__head">
          <button
            type="button"
            className="ws-panel__ws-btn"
            onClick={onOpenWorkspaceDialog}
            title="Change node or workspace folder"
          >
            <code>{nodeId}</code>
            <span>/{subpath || "."}</span>
          </button>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => {
              void loadDir("");
              void onRefreshGit();
            }}
            disabled={loadingDir === ""}
            title="Refresh files"
            aria-label="Refresh files"
          >
            <RefreshCw size={12} className={loadingDir === "" ? "spin" : ""} aria-hidden="true" />
          </button>
        </div>
        <div className="ws-tree" role="tree" aria-label="Workspace files">
          {(treeCache[""] ?? []).length === 0 && loadingDir !== "" ? (
            <p className="muted ws-tree__empty">Workspace folder is empty.</p>
          ) : (
            renderTreeEntries("", 0)
          )}
        </div>
      </div>
    );
  }

  const stagedChanges: GitFileChange[] = (gitStatus?.changes ?? []).filter((c) => c.staged);
  const unstagedChanges: GitFileChange[] = (gitStatus?.changes ?? []).filter(
    (c) => c.untracked || c.worktree_status !== " "
  );

  return (
    <div className="ws-panel">
      <div className="ws-panel__head">
        <span className="ws-git-branch">
          <GitBranch size={12} aria-hidden="true" />
          <strong>{gitStatus?.is_git_repo ? gitStatus.branch || "HEAD" : "No Git repo"}</strong>
          {gitStatus && gitStatus.ahead > 0 && (
            <span className="pill">↑{gitStatus.ahead}</span>
          )}
          {gitStatus && gitStatus.behind > 0 && (
            <span className="pill">↓{gitStatus.behind}</span>
          )}
        </span>
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          onClick={() => void onRefreshGit()}
          disabled={gitLoading}
          title="Refresh Git status"
          aria-label="Refresh Git status"
        >
          <RefreshCw size={12} className={gitLoading ? "spin" : ""} aria-hidden="true" />
        </button>
      </div>

      {!gitStatus?.is_git_repo ? (
        <p className="muted ws-tree__empty">Current workspace is not a Git repository.</p>
      ) : (
        <div className="ws-git-body">
          <div className="ws-commit-box">
            <textarea
              rows={2}
              value={commitMsg}
              onChange={(e) => setCommitMsg(e.target.value)}
              placeholder="Commit message…"
              aria-label="Commit message"
              disabled={gitBusy || generatingMsg}
            />
            <div className="row-actions">
              <button
                type="button"
                className="btn btn--sm"
                disabled={gitBusy || generatingMsg || (gitStatus.changes?.length ?? 0) === 0}
                onClick={() => void handleGenerateCommitMsg()}
                title="Generate Conventional Commit message from diff"
              >
                <Sparkles size={11} aria-hidden="true" />
                {generatingMsg ? "Drafting…" : "Generate"}
              </button>
              <button
                type="button"
                className="btn btn--sm btn--accent"
                style={{ marginInlineStart: "auto" }}
                disabled={gitBusy || stagedChanges.length === 0 || !commitMsg.trim()}
                onClick={() => void handleCommit()}
              >
                <GitCommitHorizontal size={12} aria-hidden="true" />
                Commit ({stagedChanges.length})
              </button>
            </div>
          </div>

          {stagedChanges.length > 0 && (
            <div className="ws-git-section">
              <div className="ws-git-section__head">
                <span>Staged ({stagedChanges.length})</span>
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  disabled={gitBusy}
                  onClick={() =>
                    void runGitPost("unstage", { paths: stagedChanges.map((c) => c.path) })
                  }
                  title="Unstage all"
                >
                  <Minus size={11} aria-hidden="true" />
                </button>
              </div>
              {stagedChanges.map((ch) => {
                const badge = statusBadge(ch.index_status);
                return (
                  <div key={`staged:${ch.path}`} className="ws-tree__row">
                    <button
                      type="button"
                      className="ws-tree__btn"
                      onClick={() => onOpenDiff(ch.path, true)}
                      title={ch.path}
                    >
                      <span className="ws-tree__name">{ch.path}</span>
                    </button>
                    {badge && <span className={`git-badge ${badge.cls}`}>{badge.label}</span>}
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost ws-tree__attach"
                      disabled={gitBusy}
                      title="Unstage file"
                      aria-label={`Unstage ${ch.path}`}
                      onClick={() => void runGitPost("unstage", { paths: [ch.path] })}
                    >
                      <Minus size={11} aria-hidden="true" />
                    </button>
                  </div>
                );
              })}
            </div>
          )}

          <div className="ws-git-section">
            <div className="ws-git-section__head">
              <span>Changes ({unstagedChanges.length})</span>
              {unstagedChanges.length > 0 && (
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  disabled={gitBusy}
                  onClick={() =>
                    void runGitPost("stage", { paths: unstagedChanges.map((c) => c.path) })
                  }
                  title="Stage all"
                >
                  <Plus size={11} aria-hidden="true" />
                </button>
              )}
            </div>
            {unstagedChanges.length === 0 ? (
              <p className="muted ws-tree__empty">Working tree clean.</p>
            ) : (
              unstagedChanges.map((ch) => {
                const badge = statusBadge(ch.untracked ? "?" : ch.worktree_status);
                return (
                  <div key={`wt:${ch.path}`} className="ws-tree__row">
                    <button
                      type="button"
                      className="ws-tree__btn"
                      onClick={() => onOpenDiff(ch.path, false)}
                      title={ch.path}
                    >
                      <span className="ws-tree__name">{ch.path}</span>
                    </button>
                    {badge && <span className={`git-badge ${badge.cls}`}>{badge.label}</span>}
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost ws-tree__attach"
                      disabled={gitBusy}
                      title="Discard changes"
                      aria-label={`Discard ${ch.path}`}
                      onClick={() => requestDiscard(ch.path)}
                    >
                      <RotateCcw size={11} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost ws-tree__attach"
                      disabled={gitBusy}
                      title="Stage file"
                      aria-label={`Stage ${ch.path}`}
                      onClick={() => void runGitPost("stage", { paths: [ch.path] })}
                    >
                      <Plus size={11} aria-hidden="true" />
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
