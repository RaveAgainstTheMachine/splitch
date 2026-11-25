import { useState } from "react";
import { ChevronDown, ChevronRight, Eye, Network, ShieldAlert, Square } from "lucide-react";
import type { PendingApproval, SwarmAgentRecord, SwarmRunRecord } from "../../types";

export interface SwarmCardProps {
  run: SwarmRunRecord;
  pendingApprovals: Record<string, PendingApproval>;
  inspectedAgentId: string | null;
  onInspectAgent: (runId: string, agentId: string) => void;
  onCancelAgent: (agentId: string) => Promise<void>;
  onResolveApproval: (approval: PendingApproval, approved: boolean) => Promise<void>;
}

function statusBadgeClass(status: SwarmAgentRecord["status"] | SwarmRunRecord["status"]): string {
  if (status === "done") return "pill pill--ok";
  if (status === "error" || status === "cancelled") return "pill pill--err";
  if (status === "waiting_approval") return "pill pill--warn";
  return "pill";
}

export function SwarmCard({
  run,
  pendingApprovals,
  inspectedAgentId,
  onInspectAgent,
  onCancelAgent,
  onResolveApproval
}: SwarmCardProps) {
  const [collapsed, setCollapsed] = useState(false);
  const doneCount = run.agents.filter((a) => a.status === "done").length;
  const activeCount = run.agents.filter(
    (a) => a.status === "running" || a.status === "queued" || a.status === "waiting_approval"
  ).length;
  const approvalsList = Object.values(pendingApprovals).filter((p) =>
    run.agents.some((a) => a.id === p.swarm_agent_id)
  );

  return (
    <section className="swarm-card" aria-label="Swarm Run">
      <div className="swarm-card__head">
        <button
          type="button"
          className="btn btn--sm btn--ghost swarm-card__toggle"
          onClick={() => setCollapsed((v) => !v)}
        >
          {collapsed ? (
            <ChevronRight size={13} aria-hidden="true" />
          ) : (
            <ChevronDown size={13} aria-hidden="true" />
          )}
          <Network size={13} aria-hidden="true" />
          <strong>Swarm Run</strong>
          <span className="muted">
            {doneCount}/{run.agents.length} done
            {activeCount > 0 ? ` · ${activeCount} active` : ""}
          </span>
        </button>
        <span className={statusBadgeClass(run.status)}>{run.status.toUpperCase()}</span>
      </div>

      {!collapsed && (
        <div className="swarm-card__body">
          {run.agents.length === 0 ? (
            <p className="muted" style={{ margin: 0, fontSize: "0.78rem" }}>
              Spawning workers…
            </p>
          ) : (
            run.agents.map((ag) => {
              const isActive =
                ag.status === "running" || ag.status === "queued" || ag.status === "waiting_approval";
              const isSelected = inspectedAgentId === ag.id;
              return (
                <div
                  key={ag.id}
                  className={`swarm-worker-row ${isSelected ? "swarm-worker-row--active" : ""}`}
                >
                  <div className="swarm-worker-row__main">
                    <span className="swarm-worker-row__role">{ag.role}</span>
                    <span className={statusBadgeClass(ag.status)}>
                      {ag.status.replace("_", " ").toUpperCase()}
                    </span>
                    {ag.node_id && <span className="pill">{ag.node_id}</span>}
                    {ag.max_rounds > 0 && ag.round > 0 && (
                      <span className="muted swarm-worker-row__step">
                        Step {ag.round}/{ag.max_rounds}
                      </span>
                    )}
                    <span className="muted swarm-worker-row__phase">
                      {ag.active_tool ? `Running ${ag.active_tool}…` : ag.phase}
                    </span>
                    {ag.tokens_per_sec ? (
                      <span className="muted swarm-worker-row__tps">{ag.tokens_per_sec} t/s</span>
                    ) : null}
                  </div>
                  <div className="swarm-worker-row__actions">
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      onClick={() => onInspectAgent(run.id, ag.id)}
                      title="Inspect worker transcript"
                    >
                      <Eye size={12} aria-hidden="true" />
                      Inspect
                    </button>
                    {isActive && (
                      <button
                        type="button"
                        className="btn btn--sm btn--danger"
                        onClick={() => void onCancelAgent(ag.id)}
                        title="Cancel worker"
                      >
                        <Square size={11} aria-hidden="true" />
                      </button>
                    )}
                  </div>
                </div>
              );
            })
          )}

          {approvalsList.map((appr) => (
            <div key={appr.approval_id} className="chat-approval-card">
              <div className="chat-approval-card__head">
                <ShieldAlert size={14} aria-hidden="true" />
                <strong>
                  [{appr.swarm_role || "Worker"}] Approval required: <code>{appr.name}</code>
                </strong>
                <span className="pill">{appr.exec_node || "auto"}</span>
              </div>
              <pre className="chat-approval-card__args">
                {JSON.stringify(appr.arguments, null, 2)}
              </pre>
              <div className="chat-approval-card__actions">
                <button
                  type="button"
                  className="btn btn--sm btn--accent"
                  onClick={() => void onResolveApproval(appr, true)}
                >
                  Approve &amp; Run
                </button>
                <button
                  type="button"
                  className="btn btn--sm btn--danger"
                  onClick={() => void onResolveApproval(appr, false)}
                >
                  Deny
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
