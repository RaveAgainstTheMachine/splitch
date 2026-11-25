import { useEffect, useId, useRef, useState } from "react";
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
import type {
  NodeStatus,
  NodeStrataStatus,
  StrataCatalogEntry,
  StrataFitTier
} from "../types";

interface StrataPaneProps {
  nodes: NodeStatus[];
  targetNode: string;
  onAssignAlias: (nodeId: string, modelId: string) => void;
  onStatusRefresh: () => Promise<void> | void;
  onConfirm: (next: ConfirmState) => void;
}

const TIER_PILL: Record<StrataFitTier, { cls: string; label: string }> = {
  optimal: { cls: "pill--ok", label: "Optimal Fit" },
  low_ram: { cls: "pill--accent", label: "Low-RAM Split" },
  tight: { cls: "pill--warn", label: "Tight Fit" },
  insufficient: { cls: "", label: "Insufficient" }
};

export function StrataPane({
  nodes,
  targetNode,
  onAssignAlias,
  onStatusRefresh,
  onConfirm
}: StrataPaneProps) {
  const [catalog, setCatalog] = useState<StrataCatalogEntry[]>([]);
  const [statuses, setStatuses] = useState<Record<string, NodeStrataStatus>>({});
  const [selectedFamily, setSelectedFamily] = useState("qwen");
  const [selectedModel, setSelectedModel] = useState("iq2_xs");
  const [contextLen, setContextLen] = useState("64k");
  const [kvFormat, setKvFormat] = useState<"int8" | "q4_0" | "k8v4" | "fp16">("int8");
  const [visionMode, setVisionMode] = useState<"auto" | "on" | "off" | "gpu" | "cpu">("auto");
  const [draftingMode, setDraftingMode] = useState<"auto" | "mtp" | "dflash2" | "off">("auto");
  const [evictPrimaryVram, setEvictPrimaryVram] = useState(true);
  const [planPreview, setPlanPreview] = useState<Record<string, unknown> | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);
  const evictSwitchId = useId();

  const fetchAll = async () => {
    try {
      const [catRes, stRes] = await Promise.all([
        fetch("/api/strata/catalog", { cache: "no-store" }),
        fetch("/api/strata/status", { cache: "no-store" })
      ]);
      if (catRes.ok) {
        setCatalog((await catRes.json()) as StrataCatalogEntry[]);
      }
      if (stRes.ok) {
        const list = (await stRes.json()) as NodeStrataStatus[];
        const byId: Record<string, NodeStrataStatus> = {};
        for (const item of list) byId[item.node_id] = item;
        setStatuses(byId);
      }
    } catch {
      // ignore transient network errors
    }
  };

  useEffect(() => {
    void fetchAll();
  }, []);

  const nodeStatus = statuses[targetNode];
  const nodeObj = nodes.find((n) => n.node_id === targetNode);
  const isJobRunning = nodeStatus?.job?.status === "running";

  useEffect(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (isJobRunning) {
      pollRef.current = window.setInterval(() => {
        void fetchAll();
      }, 1500);
    }
    return () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [isJobRunning]);

  const postNodeAction = async (path: string, body: Record<string, unknown> = {}) => {
    if (!targetNode) return null;
    setErrorMsg(null);
    setBusyAction(path);
    try {
      const res = await fetch(`/api/strata/nodes/${encodeURIComponent(targetNode)}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      await fetchAll();
      await onStatusRefresh();
      return data;
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Strata action failed");
      return null;
    } finally {
      setBusyAction(null);
    }
  };

  const launchPayload = (overrides: Record<string, unknown> = {}) => ({
    family: selectedFamily,
    model: selectedModel,
    context: contextLen,
    kv: kvFormat,
    vision: visionMode,
    drafting: draftingMode,
    evict_primary_vram: evictPrimaryVram,
    ...overrides
  });

  const handlePlan = async () => {
    const res = await postNodeAction("/plan", launchPayload({ plan_only: true }));
    if (res && typeof res === "object") {
      setPlanPreview(res as Record<string, unknown>);
    }
  };

  const handleQuickTune = () => {
    onConfirm({
      title: `Run Quick Tune on ${nodeObj?.display_name ?? targetNode}?`,
      message:
        `Quick Tune benchmarks ${selectedFamily}/${selectedModel} (${contextLen}) for ~20 minutes ` +
        "using exclusive GPU access. Primary engine VRAM and any active Strata server will be stopped first.",
      confirmLabel: "Start Quick Tune",
      onConfirm: async () => {
        await postNodeAction("/calibrate", {
          family: selectedFamily,
          model: selectedModel,
          context: contextLen,
          kv: kvFormat,
          mode: "quick",
          evict_primary_vram: evictPrimaryVram
        });
      }
    });
  };

  const vramGb = nodeObj?.telemetry ? (nodeObj.telemetry.vram_total_mb / 1024).toFixed(1) : "0";
  const ramGb = nodeObj?.telemetry ? (nodeObj.telemetry.ram_total_mb / 1024).toFixed(1) : "0";
  const ssdGb = nodeObj?.telemetry ? nodeObj.telemetry.storage_free_gb.toFixed(1) : "0";

  return (
    <div className="settings-stack" style={{ marginTop: "0.75rem" }}>
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
            onClick={() => void fetchAll()}
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
            <span className={`pill ${nodeStatus.job.status === "completed" ? "pill--ok" : nodeStatus.job.status === "failed" ? "pill--warn" : "pill--accent"}`}>
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

      <div className="hf-results">
        <div className="hf-list">
          {catalog.map((entry) => {
            const { spec, fits } = entry;
            const fit = fits[targetNode];
            const pill = fit ? TIER_PILL[fit.tier] : TIER_PILL.insufficient;
            const active = selectedFamily === spec.family && selectedModel === spec.model;
            return (
              <article
                key={`${spec.family}:${spec.model}`}
                className={`repo-slice ${active ? "repo-slice--active" : ""}`}
              >
                <button
                  type="button"
                  className="repo-slice__row strata-pack__row"
                  onClick={() => {
                    setSelectedFamily(spec.family);
                    setSelectedModel(spec.model);
                    setPlanPreview(null);
                  }}
                >
                  <div className="repo-slice__identity">
                    <div className="strata-pack__title">
                      <strong className="repo-slice__name">{spec.label}</strong>
                      <span className="repo-slice__badges">
                        <span className={`pill ${pill.cls}`} title={fit?.reason || ""}>
                          {pill.label}
                        </span>
                        {spec.supports_vision && <span className="pill">Vision</span>}
                        {spec.supports_dflash2 && <span className="pill">DFlash2</span>}
                      </span>
                    </div>
                    <div className="strata-pack__notes muted">
                      {spec.notes}
                    </div>
                  </div>
                  <div className="strata-pack__stats muted">
                    <span>{spec.download_gb} GB dl</span>
                    <span>{spec.experts_ram_gb} GB RAM</span>
                    <span>{spec.ssd_shard_gb > 0 ? `+${spec.ssd_shard_gb} GB SSD` : ""}</span>
                  </div>
                </button>
              </article>
            );
          })}
        </div>
      </div>

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
            <Play size={12} aria-hidden="true" /> Launch / Switch Model
          </button>
        </div>
      </div>

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
