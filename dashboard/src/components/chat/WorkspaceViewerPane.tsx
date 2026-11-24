import { useState } from "react";
import { AtSign, Check, Copy, FileCode2, GitCompare, X } from "lucide-react";
import type { GitDiffResponse, WorkspaceFileContent } from "../../types";

export type WorkspaceInspectTarget =
  | { kind: "file"; path: string; data: WorkspaceFileContent | null; loading: boolean }
  | {
      kind: "diff";
      path: string;
      staged: boolean;
      data: GitDiffResponse | null;
      loading: boolean;
    };

export function WorkspaceViewerPane({
  target,
  onAttachPath,
  onToggleDiffStaged,
  onClose
}: {
  target: WorkspaceInspectTarget;
  onAttachPath: (path: string) => void;
  onToggleDiffStaged: (path: string, nextStaged: boolean) => void;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const copyContent = async () => {
    const text =
      target.kind === "file" ? target.data?.content ?? "" : target.data?.diff ?? "";
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore
    }
  };

  return (
    <aside className="artifact-pane ws-viewer" aria-label="Workspace file and diff viewer">
      <div className="artifact-pane__head">
        <div className="artifact-pane__title">
          {target.kind === "file" ? (
            <FileCode2 size={14} aria-hidden="true" />
          ) : (
            <GitCompare size={14} aria-hidden="true" />
          )}
          <code className="ws-viewer__path" title={target.path}>
            {target.path}
          </code>
          {target.kind === "file" && target.data && (
            <span className="pill">{target.data.language}</span>
          )}
          {target.kind === "diff" && (
            <button
              type="button"
              className={`pill ${target.staged ? "pill--accent" : ""}`}
              onClick={() => onToggleDiffStaged(target.path, !target.staged)}
              title="Toggle between Staged and Working Tree diff"
            >
              {target.staged ? "Staged" : "Working Tree"}
            </button>
          )}
        </div>
        <div className="row-actions">
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => onAttachPath(target.path)}
            title={`Insert @${target.path} into chat`}
          >
            <AtSign size={12} aria-hidden="true" />
            Attach
          </button>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => void copyContent()}
            title="Copy content"
            aria-label="Copy content"
          >
            {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
          </button>
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={onClose}
            aria-label="Close viewer"
            title="Close viewer"
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="ws-viewer__body">
        {target.loading ? (
          <p className="muted" style={{ padding: "1rem" }}>
            Loading…
          </p>
        ) : target.kind === "file" ? (
          !target.data ? (
            <p className="muted" style={{ padding: "1rem" }}>
              Unable to load file.
            </p>
          ) : target.data.is_binary ? (
            <p className="muted" style={{ padding: "1rem" }}>
              Binary file ({target.data.size_bytes} bytes) — preview unavailable.
            </p>
          ) : (
            <div className="ws-code-lines">
              {target.data.truncated && (
                <div className="ws-viewer__notice">
                  Showing first 256 KB of {Math.round(target.data.size_bytes / 1024)} KB.
                </div>
              )}
              {target.data.content.split("\n").map((line, idx) => (
                <div key={idx} className="ws-code-line">
                  <span className="ws-code-line__num">{idx + 1}</span>
                  <span className="ws-code-line__text">{line || " "}</span>
                </div>
              ))}
            </div>
          )
        ) : !target.data || !target.data.diff ? (
          <p className="muted" style={{ padding: "1rem" }}>
            No diff to display for this view.
          </p>
        ) : (
          <div className="ws-code-lines">
            {target.data.diff.split("\n").map((line, idx) => {
              const cls = line.startsWith("+++") || line.startsWith("---")
                ? "ws-diff-line--meta"
                : line.startsWith("@@")
                  ? "ws-diff-line--hunk"
                  : line.startsWith("+")
                    ? "ws-diff-line--add"
                    : line.startsWith("-")
                      ? "ws-diff-line--del"
                      : "";
              return (
                <div key={idx} className={`ws-code-line ${cls}`}>
                  <span className="ws-code-line__num">{idx + 1}</span>
                  <span className="ws-code-line__text">{line || " "}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </aside>
  );
}
