import type React from "react";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowDown,
  Brain,
  Check,
  Copy,
  Minimize2,
  Pencil,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  Terminal,
  User,
  X
} from "lucide-react";
import {
  inferArtifactTitle,
  type InspectedArtifact,
  isRunnableArtifact
} from "../ArtifactPreview";
import { SplitchLogo } from "../SplitchLogo";
import { DiffViewer } from "./ChatCodeBlocks";
import { SwarmCard } from "./SwarmCard";
import type {
  ChatMessageRecord,
  ConversationSummary,
  NodeStatus,
  PendingApproval,
  SwarmRunRecord
} from "../../types";
import type { ThinkingDisplay } from "../../types";

export interface ChatMessageListProps {
  activeId: string | null;
  activeConv: ConversationSummary | null;
  visibleMessages: ChatMessageRecord[];
  messages: ChatMessageRecord[];
  nodes: NodeStatus[];
  streaming: boolean;
  copiedMsgId: string | null;
  toolResultsByCallId: Record<string, ChatMessageRecord>;
  pendingApprovals: Record<string, PendingApproval>;
  thinkingDisplay?: ThinkingDisplay;
  inspectedArtifact: InspectedArtifact | null;
  markdownComponents: Components;
  threadRef: React.RefObject<HTMLDivElement | null>;
  latestMsgRef: React.RefObject<HTMLElement | null>;
  bottomSentinelRef: React.RefObject<HTMLDivElement | null>;
  handleThreadScroll: () => void;
  jumpToBottom: () => void;
  openArtifactInPane: (art: InspectedArtifact) => void;
  resolveApproval: (approval: PendingApproval, approved: boolean) => Promise<void>;
  editAndBranch: (msg: ChatMessageRecord) => Promise<void>;
  copyMessage: (msg: ChatMessageRecord) => Promise<void>;
  regenerateFromAssistant: (idx: number) => Promise<void>;
  clearCompaction: () => Promise<void>;
  onContinueTurn?: () => void;
  swarmRuns?: SwarmRunRecord[];
  inspectedSwarmAgentId?: string | null;
  onInspectSwarmAgent?: (runId: string, agentId: string) => void;
  onCancelSwarmAgent?: (agentId: string) => Promise<void>;
}

function renderApprovalSandboxBadge(approval: PendingApproval) {
  if (approval.name !== "run_command" && !approval.sandbox) return null;
  const sb = (approval.sandbox || "").trim().toLowerCase();
  if (sb === "bwrap" || sb === "seatbelt") {
    return (
      <span
        className="pill pill--ok"
        title={`Command runs inside OS sandbox (${sb}) with read-only root, masked $HOME/secrets, and no network`}
      >
        <ShieldCheck size={11} aria-hidden="true" /> Sandbox: {sb} (no net)
      </span>
    );
  }
  if (sb === "bwrap+net" || sb === "seatbelt+net") {
    return (
      <span
        className="pill pill--warn"
        title={`Command runs inside OS sandbox (${sb}) with outbound network enabled`}
      >
        <ShieldAlert size={11} aria-hidden="true" /> Sandbox: {sb}
      </span>
    );
  }
  if (sb === "unavailable") {
    return (
      <span
        className="pill pill--err"
        title="Required OS sandbox is unavailable on this node — command will be refused"
      >
        <ShieldAlert size={11} aria-hidden="true" /> Sandbox Unavailable (Blocked)
      </span>
    );
  }
  return (
    <span
      className="pill pill--err"
      title="WARNING: No OS sandbox active — command runs directly on the host shell"
    >
      <ShieldAlert size={11} aria-hidden="true" /> UNSANDBOXED HOST SHELL
    </span>
  );
}

export function ChatMessageList({
  activeId,
  activeConv,
  visibleMessages,
  messages,
  nodes,
  streaming,
  copiedMsgId,
  toolResultsByCallId,
  pendingApprovals,
  thinkingDisplay = "collapsed",
  inspectedArtifact,
  markdownComponents,
  threadRef,
  latestMsgRef,
  bottomSentinelRef,
  handleThreadScroll,
  jumpToBottom,
  openArtifactInPane,
  resolveApproval,
  editAndBranch,
  copyMessage,
  regenerateFromAssistant,
  clearCompaction,
  onContinueTurn,
  swarmRuns,
  inspectedSwarmAgentId,
  onInspectSwarmAgent,
  onCancelSwarmAgent
}: ChatMessageListProps) {
  return (
    <div
      key={activeId ?? "empty"}
      ref={threadRef}
      tabIndex={-1}
      className="chat-thread"
      aria-live="polite"
      onScroll={handleThreadScroll}
    >
      {visibleMessages.length === 0 ? (
        <div className="chat-empty">
          <SplitchLogo size={44} />
        </div>
      ) : (
        visibleMessages.map((msg, idx) => {
          const isLatest = idx === visibleMessages.length - 1;
          const isAssistant = msg.role === "assistant";
          const watermark = activeConv?.compacted_through_ts ?? 0;
          const isCompacted = watermark > 0 && msg.created_at <= watermark;
          const nextMsg = visibleMessages[idx + 1];
          const showCompactedDivider =
            Boolean(activeConv?.compacted_summary) &&
            isCompacted &&
            (!nextMsg || nextMsg.created_at > watermark);

          const metaParts: string[] = [];
          if (isAssistant) {
            if (msg.meta.node) {
              const nObj = nodes.find((n) => n.node_id === msg.meta.node);
              metaParts.push(nObj?.display_name ?? msg.meta.node);
            }
            if (msg.meta.ttft_ms != null) metaParts.push(`${msg.meta.ttft_ms}ms TTFT`);
            if (msg.meta.tokens_per_sec != null) metaParts.push(`${msg.meta.tokens_per_sec} t/s`);
            if (msg.meta.model) metaParts.push(msg.meta.model);
          }

          return (
            <div key={msg.id} className="chat-msg-slot">
              <article
                ref={isLatest ? latestMsgRef : null}
                className={`chat-msg chat-msg--${msg.role} ${
                  isLatest ? "chat-msg--latest" : ""
                } ${isCompacted ? "chat-msg--compacted" : ""}`}
              >
                <div className="chat-msg__avatar" aria-hidden="true">
                  {isAssistant ? (
                    <SplitchLogo size={22} />
                  ) : (
                    <span className="chat-user-avatar">
                      <User size={13} />
                    </span>
                  )}
                </div>

                <div className="chat-msg__body">
                  {msg.images && msg.images.length > 0 && (
                    <div className="chat-msg__images">
                      {msg.images.map((src, i) => (
                        <img key={i} src={src} alt={`Attachment ${i + 1}`} />
                      ))}
                    </div>
                  )}

                  {isAssistant && msg.reasoning && thinkingDisplay !== "hidden" && (
                    <details
                      className="chat-disclosure chat-disclosure--pill"
                      open={
                        thinkingDisplay === "expanded" ||
                        (streaming && isLatest && !msg.content)
                      }
                    >
                      <summary>
                        <Brain size={12} aria-hidden="true" />
                        <span>Thinking ({msg.reasoning.length} chars)</span>
                      </summary>
                      <div className="chat-disclosure__body">
                        <pre className="chat-reasoning">{msg.reasoning}</pre>
                      </div>
                    </details>
                  )}

                  {isAssistant && msg.tool_calls && msg.tool_calls.length > 0 && (
                    <div className="chat-tools-stack">
                      {msg.tool_calls.map((tc) => {
                        const result = toolResultsByCallId[tc.id];
                        const approval = pendingApprovals[tc.id];
                        return (
                          <div key={tc.id} className="chat-tool-card">
                            {approval && (
                              <div className="approval-card" role="alert">
                                <div className="approval-card__head">
                                  <ShieldAlert size={15} aria-hidden="true" />
                                  <strong>
                                    Approval Required: <code>{approval.name}</code> on{" "}
                                    <span className="node-tag">{approval.exec_node}</span>
                                  </strong>
                                  {renderApprovalSandboxBadge(approval)}
                                </div>
                                <pre className="code-preview">
                                  {JSON.stringify(approval.arguments, null, 2)}
                                </pre>
                                <div className="row-actions">
                                  <button
                                    type="button"
                                    className="btn btn--sm btn--accent"
                                    onClick={() => void resolveApproval(approval, true)}
                                  >
                                    <Check size={12} aria-hidden="true" /> Allow
                                  </button>
                                  <button
                                    type="button"
                                    className="btn btn--sm btn--danger"
                                    onClick={() => void resolveApproval(approval, false)}
                                  >
                                    <X size={12} aria-hidden="true" /> Deny
                                  </button>
                                </div>
                              </div>
                            )}

                            <details
                              className="chat-disclosure"
                              open={Boolean(result?.meta?.diff)}
                            >
                              <summary>
                                <Terminal size={13} aria-hidden="true" />
                                <span>
                                  Tool: <code>{tc.function.name}</code>
                                </span>
                                {result ? (
                                  <>
                                    <span
                                      className={`pill ${
                                        result.meta.ok ? "pill--ok" : "pill--err"
                                      }`}
                                    >
                                      {result.meta.ok ? "OK" : "ERR"}
                                    </span>
                                    {result.meta.sandbox && (
                                      <span
                                        className={`pill ${
                                          result.meta.sandbox.startsWith("bwrap") ||
                                          result.meta.sandbox.startsWith("seatbelt")
                                            ? "pill--ok"
                                            : "pill--warn"
                                        }`}
                                        title={`Command execution sandbox: ${result.meta.sandbox}`}
                                      >
                                        {result.meta.sandbox}
                                      </span>
                                    )}
                                    {result.meta.verification?.checked && (
                                      <span
                                        className={`pill ${
                                          result.meta.verification.ok
                                            ? "pill--ok"
                                            : "pill--warn"
                                        }`}
                                        title={result.meta.verification.output}
                                      >
                                        {result.meta.verification.ok ? "✓" : "✗"}{" "}
                                        {result.meta.verification.checker}
                                      </span>
                                    )}
                                  </>
                                ) : approval ? (
                                  <span className="pill pill--warn">WAITING APPROVAL</span>
                                ) : (
                                  <span className="pill">RUNNING…</span>
                                )}
                              </summary>
                              <div className="chat-disclosure__body">
                                <div className="muted">Arguments</div>
                                <pre className="code-preview">{tc.function.arguments}</pre>
                                {result?.meta?.diff && (
                                  <>
                                    <div className="muted">Unified Diff</div>
                                    <DiffViewer diff={result.meta.diff} />
                                  </>
                                )}
                                {result && (
                                  <>
                                    <div className="muted">Output</div>
                                    <pre className="code-preview">{result.content}</pre>
                                  </>
                                )}
                              </div>
                            </details>

                            {tc.function.name === "write_file" &&
                              (() => {
                                try {
                                  const a = JSON.parse(tc.function.arguments || "{}") as {
                                    path?: string;
                                    content?: string;
                                  };
                                  const p = String(a.path || "");
                                  const c = String(a.content || "");
                                  const ext = p.split(".").pop()?.toLowerCase() || "";
                                  if (c && isRunnableArtifact(ext, c)) {
                                    const isInsp = inspectedArtifact?.code === c;
                                    return (
                                      <button
                                        type="button"
                                        className={`artifact-chip ${
                                          isInsp ? "artifact-chip--active" : ""
                                        }`}
                                        onClick={() =>
                                          openArtifactInPane({
                                            id: `file-${p}`,
                                            lang: ext || "html",
                                            code: c,
                                            label: p || inferArtifactTitle(ext || "html", c)
                                          })
                                        }
                                      >
                                        <span className="artifact-chip__badge">
                                          {(ext || "HTML").toUpperCase()}
                                        </span>
                                        <strong className="artifact-chip__name">
                                          {p || "artifact.html"}
                                        </strong>
                                        <span className="artifact-chip__hint">
                                          •{" "}
                                          {isInsp
                                            ? "Inspecting in split pane"
                                            : "Click to inspect"}
                                        </span>
                                      </button>
                                    );
                                  }
                                } catch {
                                  // ignore invalid json
                                }
                                return null;
                              })()}

                            {result?.meta?.media_url && (
                              <div className="chat-media-embed">
                                {result.meta.media_kind === "video" ? (
                                  <video
                                    controls
                                    playsInline
                                    preload="metadata"
                                    width={768}
                                    height={512}
                                    src={result.meta.media_url}
                                    className="chat-media-embed__media"
                                  />
                                ) : (
                                  <a
                                    href={result.meta.media_url}
                                    target="_blank"
                                    rel="noreferrer"
                                  >
                                    <img
                                      src={result.meta.media_url}
                                      alt="Generated media"
                                      className="chat-media-embed__media"
                                    />
                                  </a>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {msg.content && (
                    <div className="chat-prose">
                      {isAssistant ? (
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={markdownComponents}
                        >
                          {msg.content}
                        </ReactMarkdown>
                      ) : (
                        <p className="chat-user-text">{msg.content}</p>
                      )}
                    </div>
                  )}

                  <footer className="chat-msg__foot">
                    {isAssistant && metaParts.length > 0 && (
                      <span className="chat-msg__telemetry">{metaParts.join(" • ")}</span>
                    )}
                    {isAssistant && msg.meta.fallback_reason && (
                      <span className="pill pill--warn">{msg.meta.fallback_reason}</span>
                    )}
                    {isAssistant && msg.meta.stopped && (
                      <span className="pill pill--warn">STOPPED</span>
                    )}
                    {isAssistant && msg.meta.max_rounds_reached && (
                      <span className="chat-max-rounds-notice">
                        <span
                          className="pill pill--warn"
                          title="Increase Max Agent Tool Steps / Turn in Settings -> Agent & Chat"
                        >
                          Max steps reached ({msg.meta.steps_used ?? msg.meta.max_steps ?? "?"}/
                          {msg.meta.max_steps ?? "?"})
                        </span>
                        {isLatest && !streaming && onContinueTurn && (
                          <button
                            type="button"
                            className="btn btn--sm btn--accent"
                            onClick={onContinueTurn}
                          >
                            Continue
                          </button>
                        )}
                      </span>
                    )}

                    <div className="chat-msg__actions">
                      {msg.role === "user" && !streaming && !msg.id.startsWith("temp-") && (
                        <button
                          type="button"
                          className="btn btn--sm btn--ghost"
                          onClick={() => void editAndBranch(msg)}
                          title="Edit and branch from this message"
                        >
                          <Pencil size={11} aria-hidden="true" />
                          Edit
                        </button>
                      )}
                      {isAssistant && !streaming && !msg.id.startsWith("stream-") && (
                        <>
                          <button
                            type="button"
                            className="btn btn--sm btn--ghost"
                            onClick={() => void copyMessage(msg)}
                            title="Copy response"
                          >
                            {copiedMsgId === msg.id ? (
                              <Check size={11} aria-hidden="true" />
                            ) : (
                              <Copy size={11} aria-hidden="true" />
                            )}
                          </button>
                          <button
                            type="button"
                            className="btn btn--sm btn--ghost"
                            onClick={() => {
                              const realIdx = messages.findIndex((x) => x.id === msg.id);
                              if (realIdx >= 0) void regenerateFromAssistant(realIdx);
                            }}
                            title="Regenerate response"
                          >
                            <RotateCcw size={11} aria-hidden="true" />
                          </button>
                        </>
                      )}
                    </div>
                  </footer>
                </div>
              </article>
              {showCompactedDivider && activeConv?.compacted_summary && (
                <details className="chat-disclosure chat-compacted-divider">
                  <summary>
                    <Minimize2 size={13} aria-hidden="true" />
                    <span>Messages above compacted into active context summary</span>
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      style={{ marginInlineStart: "auto" }}
                      onClick={(e) => {
                        e.preventDefault();
                        void clearCompaction();
                      }}
                    >
                      Clear Summary
                    </button>
                  </summary>
                  <div className="chat-disclosure__body chat-prose">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                      {activeConv.compacted_summary}
                    </ReactMarkdown>
                  </div>
                </details>
              )}
            </div>
          );
        })
      )}
      {swarmRuns &&
        swarmRuns.length > 0 &&
        onInspectSwarmAgent &&
        onCancelSwarmAgent &&
        swarmRuns.map((run) => (
          <SwarmCard
            key={run.id}
            run={run}
            pendingApprovals={pendingApprovals}
            inspectedAgentId={inspectedSwarmAgentId ?? null}
            onInspectAgent={onInspectSwarmAgent}
            onCancelAgent={onCancelSwarmAgent}
            onResolveApproval={resolveApproval}
          />
        ))}
      <div ref={bottomSentinelRef} className="scroll-sentinel" aria-hidden="true" />
      {visibleMessages.length > 0 && (
        <button
          type="button"
          className="btn btn--sm jump-to-bottom"
          onClick={jumpToBottom}
          onBlur={(e) => {
            if (!e.relatedTarget && threadRef.current) {
              threadRef.current.focus({ preventScroll: true });
            }
          }}
          aria-label="Scroll to latest message"
        >
          <ArrowDown size={13} aria-hidden="true" />
          Latest
        </button>
      )}
    </div>
  );
}
