import { Network, Square, X } from "lucide-react";
import type { SwarmRunRecord, SwarmWorkerMessage } from "../../types";

export interface SwarmInspectorPaneProps {
  runs: SwarmRunRecord[];
  selectedRunId: string | null;
  selectedAgentId: string | null;
  workerMessages: SwarmWorkerMessage[];
  liveStreamText: string;
  liveReasoningText: string;
  onSelectAgent: (runId: string, agentId: string) => void;
  onCancelAgent: (agentId: string) => Promise<void>;
  onClose: () => void;
}

export function SwarmInspectorPane({
  runs,
  selectedRunId,
  selectedAgentId,
  workerMessages,
  liveStreamText,
  liveReasoningText,
  onSelectAgent,
  onCancelAgent,
  onClose
}: SwarmInspectorPaneProps) {
  const activeRun = runs.find((r) => r.id === selectedRunId) ?? runs[runs.length - 1] ?? null;
  const activeAgent =
    activeRun?.agents.find((a) => a.id === selectedAgentId) ?? activeRun?.agents[0] ?? null;

  return (
    <aside className="artifact-pane swarm-inspector" aria-label="Swarm Inspector">
      <div className="artifact-pane__head">
        <div className="artifact-pane__title">
          <Network size={14} aria-hidden="true" />
          <strong>Swarm Inspector</strong>
          {activeRun && (
            <span className="muted">
              {activeRun.agents.length} worker{activeRun.agents.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          onClick={onClose}
          aria-label="Close swarm inspector"
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>

      {!activeRun ? (
        <div className="swarm-inspector__empty muted">
          No swarm runs in this conversation yet.
        </div>
      ) : (
        <div className="swarm-inspector__body">
          <div className="swarm-roster">
            {activeRun.agents.map((ag) => {
              const isSelected = activeAgent?.id === ag.id;
              const isActive =
                ag.status === "running" || ag.status === "queued" || ag.status === "waiting_approval";
              return (
                <div
                  key={ag.id}
                  className={`swarm-roster__card ${isSelected ? "swarm-roster__card--selected" : ""}`}
                >
                  <button
                    type="button"
                    className="swarm-roster__select"
                    onClick={() => onSelectAgent(activeRun.id, ag.id)}
                  >
                    <div className="swarm-roster__top">
                      <strong>{ag.role}</strong>
                      <span className="pill">{ag.status}</span>
                    </div>
                    <div className="swarm-roster__meta muted">
                      {[
                        ag.node_id || "auto",
                        ag.model,
                        ag.round > 0 ? `Step ${ag.round}/${ag.max_rounds}` : "",
                        ag.tokens_per_sec ? `${ag.tokens_per_sec} t/s` : ""
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </div>
                  </button>
                  {isActive && (
                    <button
                      type="button"
                      className="btn btn--sm btn--danger"
                      onClick={() => void onCancelAgent(ag.id)}
                      title="Cancel worker"
                    >
                      <Square size={10} aria-hidden="true" />
                    </button>
                  )}
                </div>
              );
            })}
          </div>

          {activeAgent && (
            <div className="swarm-transcript">
              <div className="swarm-transcript__task">
                <div className="muted" style={{ fontSize: "0.72rem", marginBottom: "0.2rem" }}>
                  TASK ({activeAgent.readonly ? "Read-Only" : "Read-Write"})
                </div>
                <div>{activeAgent.task}</div>
              </div>

              {workerMessages.map((m) => (
                <div key={m.id} className={`swarm-msg swarm-msg--${m.role}`}>
                  <div className="swarm-msg__head muted">
                    {m.role.toUpperCase()}
                    {m.tool_name ? ` · ${m.tool_name}` : ""}
                  </div>
                  {m.reasoning && (
                    <details className="chat-disclosure">
                      <summary>Thinking ({m.reasoning.length} chars)</summary>
                      <pre className="swarm-msg__pre">{m.reasoning}</pre>
                    </details>
                  )}
                  {m.content && <pre className="swarm-msg__pre">{m.content}</pre>}
                </div>
              ))}

              {(liveReasoningText || liveStreamText) && (
                <div className="swarm-msg swarm-msg--assistant">
                  <div className="swarm-msg__head muted">STREAMING…</div>
                  {liveReasoningText && (
                    <pre className="swarm-msg__pre muted">{liveReasoningText}</pre>
                  )}
                  {liveStreamText && <pre className="swarm-msg__pre">{liveStreamText}</pre>}
                </div>
              )}

              {activeAgent.error && (
                <div className="banner banner--error">{activeAgent.error}</div>
              )}
            </div>
          )}
        </div>
      )}
    </aside>
  );
}
