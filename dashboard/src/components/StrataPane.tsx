import { useEffect, useId, useState } from "react";
import {
  Cpu,
  Download,
  Eye,
  Gauge,
  Loader2,
  Play,
  RefreshCw,
  Square,
  Tag,
  Terminal,
  Zap
} from "lucide-react";
import type { ConfirmState } from "./ConfirmDialog";
import type { NodeStatus, NodeStrataStatus } from "../types";

interface Props {
  nodes: NodeStatus[];
  targetNode: string;
  selectedModelPath?: string;
  onAssignAlias: (nodeId: string, modelId: string) => void;
  onStatusRefresh: () => Promise<void>;
  onConfirm: (state: ConfirmState) => void;
}

export function StrataPane({
  nodes,
  targetNode,
  selectedModelPath = "",
  onAssignAlias,
  onStatusRefresh,
  onConfirm
}: Props) {
  const [statuses, setStatuses] = useState<NodeStrataStatus[]>([]);
  const [contextLen, setContextLen] = useState("64k");
  const [kvFormat, setKvFormat] = useState<"int8" | "q4_0" | "k8v4" | "fp16">("int8");
  const [visionMode, setVisionMode] = useState<"auto" | "on" | "off" | "gpu" | "cpu">("auto");
  const [draftingMode, setDraftingMode] = useState<"auto" | "mtp" | "dflash2" | "off">("auto");
  const [evictPrimaryVram, setEvictPrimaryVram] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [planPreview, setPlanPreview] = useState<Record<string, unknown> | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const evictSwitchId = useId();

  const nodeStatus = statuses.find((s) => s.node_id === targetNode);
  const nodeObj = nodes.find((n) => n.node_id === targetNode);
  const isJobRunning = nodeStatus?.job?.status === "running";

  const fetchStatuses = async () => {
    try {
      const sRes = await fetch("/api/strata/status");
      if (sRes.ok) {
        setStatuses(await sRes.json());
      }
      setErrorMsg(null);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to load Strata status");
    }
  };

  useEffect(() => {
    void fetchStatuses();
  }, []);

  useEffect(() => {
    if (!isJobRunning) return;
    const timer = window.setInterval(() => {
      void fetchStatuses();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [isJobRunning, targetNode]);

  const postNodeAction = async (path: string, body?: unknown) => {
    setBusyAction(path);
    setErrorMsg(null);
    try {
      const res = await fetch(`/api/strata/nodes/${encodeURIComponent(targetNode)}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body !== undefined ? JSON.stringify(body) : "{}"
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      await fetchStatuses();
      await onStatusRefresh();
      return data;
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Strata action failed");
      return null;
    } finally {
      setBusyAction(null);
    }
  };

  const launchPayload = (modelPathOverride?: string) => ({
    model_path: modelPathOverride ?? selectedModelPath,
    context: contextLen,
    kv: kvFormat,
    vision: visionMode,
    drafting: draftingMode,
    evict_primary_vram: evictPrimaryVram
  });

  const handlePlan = async () => {
    const data = await postNodeAction("/plan", launchPayload());
    if (data) setPlanPreview(data);
  };

  const handleQuickTune = () => {
    onConfirm({
      title: "Run Strata Quick Tune?",
      message:
        "Quick Tune benchmarks expert placement and KV settings on this node (takes ~20 minutes). Continue?",
      confirmLabel: "Start Quick Tune",
      onConfirm: async () => {
        await postNodeAction("/launch", {
          ...launchPayload(),
          mode: "quick"
        });
      }
    });
  };

  const vramGb = nodeObj?.telemetry ? (nodeObj.telemetry.vram_total_mb / 1024).toFixed(1) : "0";
  const ramGb = nodeObj?.telemetry ? (nodeObj.telemetry.ram_total_mb / 1024).toFixed(1) : "0";
  const ssdGb = nodeObj?.telemetry ? nodeObj.telemetry.storage_free_gb.toFixed(1) : "0";

  return (
    <div className="settings-stack" style={{ marginTop: "0.65rem", marginBottom: "0.75rem" }}>
      <div className="panel-head" style={{ flexWrap: "wrap", gap: "0.5rem" }}>
        <div className="row-actions" style={{ flexWrap: "wrap" }}>
          <span className="pill">
            <Cpu size={12} aria-hidden="true" /> {vramGb} GB VRAM · {ramGb} GB RAM · {ssdGb} GB SSD
          </span>
          {nodeStatus?.installed ? (
            <span className={`pill ${nodeStatus.healthy ? "pill--ok" : ""}`}>
              Strata {nodeStatus.version ? `v${nodeStatus.version}` : "Ready"}
              {nodeStatus.healthy
                ? ` · Serving ${nodeStatus.active_model || ":" + nodeStatus.port}`
                : " · Idle"}
            </span>
          ) : (
            <span className="pill pill--warn">Strata CLI Not Installed on {targetNode}</span>
          )}
        </div>
        <div className="row-actions">
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void fetchStatuses()}
            disabled={busyAction !== null}
          >
            <RefreshCw size={12} aria-hidden="true" /> Refresh
          </button>
          <button
            type="button"
            className="btn btn--sm btn--accent"
            disabled={isJobRunning || busyAction !== null || !targetNode}
            onClick={() => void postNodeAction("/install", { force: Boolean(nodeStatus?.installed) })}
          >
            <Download size={12} aria-hidden="true" />{" "}
            {nodeStatus?.installed ? "Update Strata CLI" : "Install Strata CLI"}
          </button>
          {(nodeStatus?.running || isJobRunning) && (
            <button
              type="button"
              className="btn btn--sm btn--danger"
              disabled={busyAction !== null}
              onClick={() => void postNodeAction("/stop")}
            >
              <Square size={11} aria-hidden="true" /> Stop Strata
            </button>
          )}
        </div>
      </div>

      {errorMsg && (
        <div className="alert alert--error" role="alert">
          {errorMsg}
        </div>
      )}

      {nodeStatus?.job && nodeStatus.job.status !== "idle" && (
        <div className="panel" style={{ padding: "0.75rem", background: "var(--surface-2)" }}>
          <div className="row-actions" style={{ justifyContent: "space-between" }}>
            <strong>
              {isJobRunning && <Loader2 size={13} className="spin" aria-hidden="true" />}{" "}
              {nodeStatus.job.action.toUpperCase()}: {nodeStatus.job.step || nodeStatus.job.status}
            </strong>
            <span
              className={`pill ${
                nodeStatus.job.status === "completed"
                  ? "pill--ok"
                  : nodeStatus.job.status === "failed"
                    ? "pill--warn"
                    : "pill--accent"
              }`}
            >
              {nodeStatus.job.status}
            </span>
          </div>
          {nodeStatus.job.log_tail.length > 0 && (
            <pre
              style={{
                marginTop: "0.5rem",
                maxHeight: "140px",
                overflow: "auto",
                fontSize: "0.72rem",
                padding: "0.5rem",
                background: "var(--bg)",
                borderRadius: "6px"
              }}
            >
              {nodeStatus.job.log_tail.slice(-14).join("\n")}
            </pre>
          )}
        </div>
      )}

      <div className="two-col" style={{ gap: "0.6rem" }}>
        <label className="media-field">
          <span>Context Length</span>
          <select value={contextLen} onChange={(e) => setContextLen(e.target.value)}>
            <option value="32k">32K</option>
            <option value="64k">64K</option>
            <option value="128k">128K</option>
            <option value="256k">256K</option>
          </select>
        </label>
        <label className="media-field">
          <span>KV Cache Format</span>
          <select
            value={kvFormat}
            onChange={(e) => setKvFormat(e.target.value as "int8" | "q4_0" | "k8v4" | "fp16")}
          >
            <option value="int8">int8 (Default)</option>
            <option value="q4_0">q4_0 (Half Memory)</option>
            <option value="k8v4">k8v4</option>
            <option value="fp16">fp16</option>
          </select>
        </label>
        <label className="media-field">
          <span>
            <Eye size={12} aria-hidden="true" /> Vision Reader
          </span>
          <select
            value={visionMode}
            onChange={(e) =>
              setVisionMode(e.target.value as "auto" | "on" | "off" | "gpu" | "cpu")
            }
          >
            <option value="auto">Auto</option>
            <option value="cpu">CPU (Save VRAM)</option>
            <option value="gpu">GPU (24+ GB)</option>
            <option value="off">Off</option>
          </select>
        </label>
        <label className="media-field">
          <span>
            <Zap size={12} aria-hidden="true" /> Speculative Drafting
          </span>
          <select
            value={draftingMode}
            onChange={(e) =>
              setDraftingMode(e.target.value as "auto" | "mtp" | "dflash2" | "off")
            }
          >
            <option value="auto">Auto</option>
            <option value="dflash2">DFlash2</option>
            <option value="mtp">MTP</option>
            <option value="off">Off</option>
          </select>
        </label>
      </div>

      {selectedModelPath && (
        <div className="panel-head" style={{ flexWrap: "wrap", gap: "0.5rem" }}>
          <label className="fit-switch" htmlFor={evictSwitchId}>
            <input
              id={evictSwitchId}
              type="checkbox"
              {...({ switch: "" } as Record<string, string>)}
              checked={evictPrimaryVram}
              onChange={(e) => setEvictPrimaryVram(e.target.checked)}
            />
            <span>Unload primary engine models first (free VRAM)</span>
          </label>

          <div className="row-actions" style={{ flexWrap: "wrap" }}>
            <button
              type="button"
              className="btn btn--sm"
              disabled={!nodeStatus?.installed || isJobRunning || busyAction !== null}
              onClick={() => void handlePlan()}
            >
              <Terminal size={12} aria-hidden="true" /> Preview Plan
            </button>
            <button
              type="button"
              className="btn btn--sm"
              disabled={!nodeStatus?.installed || isJobRunning || busyAction !== null}
              onClick={handleQuickTune}
            >
              <Gauge size={12} aria-hidden="true" /> Quick Tune (~20m)
            </button>
            <button
              type="button"
              className="btn btn--sm btn--accent"
              disabled={!nodeStatus?.installed || isJobRunning || busyAction !== null}
              onClick={() => void postNodeAction("/launch", launchPayload())}
            >
              <Play size={12} aria-hidden="true" /> Launch Selected
            </button>
          </div>
        </div>
      )}

      {planPreview && (
        <pre
          style={{
            fontSize: "0.72rem",
            padding: "0.6rem",
            background: "var(--surface-2)",
            borderRadius: "6px",
            maxHeight: "180px",
            overflow: "auto"
          }}
        >
          {JSON.stringify(planPreview, null, 2)}
        </pre>
      )}

      {nodeStatus && (nodeStatus.installed_models.length > 0 || nodeStatus.presets.length > 0) && (
        <div className="table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Installed / Preset</th>
                <th scope="col">Status</th>
                <th scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {nodeStatus.installed_models.map((im) => (
                <tr key={`im:${im.id}`}>
                  <td>
                    <code>{im.id}</code>
                  </td>
                  <td>
                    <span className={`pill ${im.is_active ? "pill--ok" : ""}`}>
                      {im.is_active ? "Serving" : im.component_status || "Installed"}
                    </span>
                  </td>
                  <td>
                    <div className="row-actions">
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() => onAssignAlias(targetNode, im.id)}
                      >
                        <Tag size={11} aria-hidden="true" /> Aliases
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {nodeStatus.presets.map((preset) => (
                <tr key={`preset:${preset}`}>
                  <td>
                    <code>preset:{preset}</code>
                  </td>
                  <td>
                    <span className="pill">Preset</span>
                  </td>
                  <td>
                    <div className="row-actions">
                      <button
                        type="button"
                        className="btn btn--sm btn--accent"
                        disabled={isJobRunning || busyAction !== null}
                        onClick={() =>
                          void postNodeAction("/launch", {
                            preset,
                            context: contextLen,
                            kv: kvFormat,
                            evict_primary_vram: evictPrimaryVram
                          })
                        }
                      >
                        <Play size={11} aria-hidden="true" /> Start Preset
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
