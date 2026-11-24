import { useEffect, useRef, useState } from "react";
import { Cpu, Download, RefreshCw, RotateCcw, Save } from "lucide-react";
import type {
  ClusterUpdateJob,
  DashboardSnapshot,
  DoctorReport,
  EngineStatus,
  NodeUpdateStepStatus,
  RuntimeSettings,
  UpdateStatus
} from "../../types";

function stepPillClass(status: NodeUpdateStepStatus): string {
  if (status === "done") return "pill--ok";
  if (status === "updating" || status === "restarting") return "pill--accent";
  if (status === "rolled_back") return "pill--warn";
  if (status === "failed") return "pill--err";
  return "";
}

function stepLabel(status: NodeUpdateStepStatus): string {
  switch (status) {
    case "pending":
      return "Pending";
    case "updating":
      return "Updating…";
    case "restarting":
      return "Restarting…";
    case "done":
      return "Done";
    case "rolled_back":
      return "Rolled back";
    case "failed":
      return "Failed";
  }
}

export function SecuritySystemPane({
  settings,
  setSettings,
  originsText,
  setOriginsText,
  saving,
  saveSettings,
  snapshot,
  onLocalError
}: {
  settings: RuntimeSettings;
  setSettings: React.Dispatch<React.SetStateAction<RuntimeSettings | null>>;
  originsText: string;
  setOriginsText: (val: string) => void;
  saving: boolean;
  saveSettings: (e: React.FormEvent) => Promise<void>;
  snapshot: DashboardSnapshot | null;
  onLocalError?: (msg: string | null) => void;
}) {
  const [doctor, setDoctor] = useState<DoctorReport | null>(null);
  const [engineStatus, setEngineStatus] = useState<EngineStatus | null>(null);
  const [updateStatus, setUpdateStatus] = useState<UpdateStatus | null>(null);
  const [loadingSys, setLoadingSys] = useState(false);
  const [checkingUpdates, setCheckingUpdates] = useState(false);
  const [withEngine, setWithEngine] = useState(false);
  const [confirmAction, setConfirmAction] = useState<"update" | "rollback" | null>(null);
  const [clusterJob, setClusterJob] = useState<ClusterUpdateJob | null>(null);
  const [startingJob, setStartingJob] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const pollTimerRef = useRef<number | null>(null);
  const healthTimerRef = useRef<number | null>(null);

  const clearTimers = () => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (healthTimerRef.current !== null) {
      window.clearInterval(healthTimerRef.current);
      healthTimerRef.current = null;
    }
  };

  useEffect(() => clearTimers, []);

  const loadSystemDiagnostics = async (checkRemote = false) => {
    if (checkRemote) setCheckingUpdates(true);
    else setLoadingSys(true);
    try {
      const [docRes, engRes, updRes, jobRes] = await Promise.all([
        fetch(`/api/system/doctor?check_updates=${checkRemote ? "true" : "false"}`),
        fetch(`/api/system/engine?check_remote=${checkRemote ? "true" : "false"}`),
        fetch(`/api/system/update-status?fetch=${checkRemote ? "true" : "false"}`),
        fetch("/api/system/update-cluster/status")
      ]);
      if (docRes.ok) setDoctor((await docRes.json()) as DoctorReport);
      if (engRes.ok) setEngineStatus((await engRes.json()) as EngineStatus);
      if (updRes.ok) setUpdateStatus((await updRes.json()) as UpdateStatus);
      if (jobRes.ok) {
        const job = (await jobRes.json()) as ClusterUpdateJob;
        if (job.status !== "idle") setClusterJob(job);
      }
    } catch {
      // ignore
    } finally {
      setLoadingSys(false);
      setCheckingUpdates(false);
    }
  };

  useEffect(() => {
    void loadSystemDiagnostics(false);
  }, []);

  const startHealthReconnect = () => {
    setReconnecting(true);
    if (healthTimerRef.current !== null) return;
    window.setTimeout(() => {
      healthTimerRef.current = window.setInterval(async () => {
        try {
          const res = await fetch("/health", { cache: "no-store" });
          if (res.ok) {
            clearTimers();
            window.location.reload();
          }
        } catch {
          // still restarting
        }
      }, 1500);
    }, 2000);
  };

  const startPollingJob = () => {
    if (pollTimerRef.current !== null) window.clearInterval(pollTimerRef.current);
    pollTimerRef.current = window.setInterval(async () => {
      try {
        const res = await fetch("/api/system/update-cluster/status", { cache: "no-store" });
        if (!res.ok) return;
        const job = (await res.json()) as ClusterUpdateJob;
        setClusterJob(job);
        if (job.status !== "running") {
          if (pollTimerRef.current !== null) {
            window.clearInterval(pollTimerRef.current);
            pollTimerRef.current = null;
          }
          if (job.router_restarting || job.status === "restarting") {
            startHealthReconnect();
          } else {
            void loadSystemDiagnostics(false);
          }
        }
      } catch {
        // Router may already be restarting
        if (pollTimerRef.current !== null) {
          window.clearInterval(pollTimerRef.current);
          pollTimerRef.current = null;
        }
        startHealthReconnect();
      }
    }, 1200);
  };

  const triggerClusterAction = async (action: "update" | "rollback") => {
    setStartingJob(true);
    setConfirmAction(null);
    onLocalError?.(null);
    try {
      const endpoint =
        action === "update" ? "/api/system/update-cluster" : "/api/system/rollback-cluster";
      const payload =
        action === "update" ? { with_engine: withEngine, rollback_on_failure: true } : {};
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setClusterJob(body as ClusterUpdateJob);
      startPollingJob();
    } catch (err) {
      onLocalError?.(err instanceof Error ? err.message : `Cluster ${action} failed`);
    } finally {
      setStartingJob(false);
    }
  };

  const routerVersion = snapshot?.version || updateStatus?.version || "";
  const isJobRunning = clusterJob?.status === "running" || startingJob;

  return (
    <div className="settings-stack">
      <div className="row-actions" style={{ flexWrap: "wrap" }}>
        <span className={`pill ${settings.auth_enabled ? "pill--ok" : "pill--warn"}`}>
          API Auth: {settings.auth_enabled ? "Enabled" : "Open LAN"}
        </span>
        <span className={`pill ${settings.agent_token_enabled ? "pill--ok" : ""}`}>
          Agent Token: {settings.agent_token_enabled ? "Enabled" : "Unset"}
        </span>
        <span className={`pill ${settings.tls_enabled ? "pill--ok" : ""}`}>
          Native TLS: {settings.tls_enabled ? "Active" : "HTTP"}
        </span>
        <span className={`pill ${settings.remote_provision_enabled ? "pill--ok" : ""}`}>
          SSH Push: {settings.remote_provision_enabled ? "Allowed" : "Locked"}
        </span>
        <span className="pill pill--ok">Tool Approval Boundary: Enforced</span>
        <span className="pill pill--ok">SSRF Guard: Active</span>
      </div>

      <form className="settings-stack" onSubmit={saveSettings}>
        <label className="gaming-toggle">
          <input
            type="checkbox"
            {...({ switch: "" } as Record<string, string>)}
            checked={settings.hsts_enabled}
            onChange={(e) => setSettings({ ...settings, hsts_enabled: e.target.checked })}
          />
          <span>Send HSTS header on HTTPS / proxied TLS requests</span>
        </label>
        <div className="two-col">
          <label className="media-field">
            <span>Trusted Reverse Proxy IPs / CIDRs</span>
            <input
              value={settings.trusted_proxies}
              onChange={(e) => setSettings({ ...settings, trusted_proxies: e.target.value })}
              placeholder="127.0.0.1,::1"
            />
          </label>
          <label className="media-field">
            <span>Allowed CORS Origins (comma-separated)</span>
            <input
              value={originsText}
              onChange={(e) => setOriginsText(e.target.value)}
              placeholder="https://ai.example.com"
            />
          </label>
          <label className="media-field">
            <span>Max Failed Login Attempts</span>
            <input
              type="number"
              min={1}
              max={100}
              value={settings.login_max_attempts}
              onChange={(e) =>
                setSettings({ ...settings, login_max_attempts: Number(e.target.value) })
              }
            />
          </label>
          <label className="media-field">
            <span>Login Rate-Limit Window (seconds)</span>
            <input
              type="number"
              min={5}
              max={3600}
              value={settings.login_window_s}
              onChange={(e) => setSettings({ ...settings, login_window_s: Number(e.target.value) })}
            />
          </label>
        </div>
        <div className="modal-actions">
          <button type="submit" className="btn btn--accent" disabled={saving}>
            <Save size={13} aria-hidden="true" />
            {saving ? "Saving…" : "Save Security Settings"}
          </button>
        </div>
      </form>

      <div className="settings-subcard">
        <div className="panel-head">
          <strong>
            <Download size={13} aria-hidden="true" /> Software &amp; Engine Updates
          </strong>
          <button
            type="button"
            className="btn btn--sm"
            disabled={checkingUpdates}
            onClick={() => void loadSystemDiagnostics(true)}
          >
            <RefreshCw size={12} aria-hidden="true" />
            {checkingUpdates ? "Checking GitHub…" : "Check for Updates"}
          </button>
        </div>
        {updateStatus && (
          <div className="probe-list">
            <div className="probe-row">
              <span className="pill">Splitch v{updateStatus.version}</span>
              {updateStatus.local_sha && (
                <code>
                  {updateStatus.branch}@{updateStatus.local_sha}
                </code>
              )}
              <span
                className={`pill ${
                  updateStatus.error
                    ? "pill--warn"
                    : updateStatus.update_available
                      ? "pill--accent"
                      : "pill--ok"
                }`}
              >
                {updateStatus.error
                  ? updateStatus.error
                  : updateStatus.update_available
                    ? `${
                        updateStatus.remote_version ? `→ v${updateStatus.remote_version} · ` : ""
                      }${updateStatus.behind_count} commit(s) available`
                    : "Up to date"}
              </span>
            </div>
            {engineStatus?.latest_llama_tag && (
              <div className="probe-row">
                <span className="muted">llama.cpp upstream</span>
                <code>{engineStatus.latest_llama_tag}</code>
                <span
                  className={`pill ${engineStatus.update_available ? "pill--warn" : "pill--ok"}`}
                >
                  {engineStatus.update_available ? "Update available" : "Current"}
                </span>
              </div>
            )}
            {updateStatus.recent_remote_commits.length > 0 && (
              <pre className="provision-log">{updateStatus.recent_remote_commits.join("\n")}</pre>
            )}
          </div>
        )}

        <div className="probe-list" style={{ marginTop: "0.75rem" }}>
          <div className="probe-row">
            <strong style={{ fontSize: "0.82rem" }}>Cluster Version Parity</strong>
          </div>
          <div className="probe-row">
            <span>Router</span>
            <code>{routerVersion ? `v${routerVersion}` : "—"}</code>
            <span className="pill pill--ok">Online</span>
          </div>
          {(snapshot?.nodes ?? []).map((n) => {
            const nodeVer = n.telemetry?.version || "";
            const isMatch = Boolean(nodeVer && routerVersion && nodeVer === routerVersion);
            return (
              <div key={n.node_id} className="probe-row">
                <span>{n.display_name}</span>
                <code>{nodeVer ? `v${nodeVer}` : "unknown"}</code>
                <span
                  className={`pill ${
                    !n.online ? "pill--err" : isMatch ? "pill--ok" : "pill--warn"
                  }`}
                >
                  {!n.online ? "Offline" : isMatch ? "In sync" : "Mismatch"}
                </span>
              </div>
            );
          })}
        </div>

        <div className="settings-stack" style={{ marginTop: "0.75rem", gap: "0.5rem" }}>
          <label className="gaming-toggle">
            <input
              type="checkbox"
              checked={withEngine}
              disabled={isJobRunning}
              onChange={(e) => setWithEngine(e.target.checked)}
            />
            <span>Also update llama.cpp / sd.cpp binaries</span>
          </label>

          <div className="row-actions" style={{ flexWrap: "wrap" }}>
            <button
              type="button"
              className="btn btn--sm btn--accent"
              disabled={isJobRunning || reconnecting}
              onClick={() => setConfirmAction("update")}
            >
              <Download size={12} aria-hidden="true" />
              {isJobRunning && clusterJob?.action === "update"
                ? "Updating Cluster…"
                : "Update All Nodes"}
            </button>
            <button
              type="button"
              className="btn btn--sm"
              disabled={isJobRunning || reconnecting}
              onClick={() => setConfirmAction("rollback")}
            >
              <RotateCcw size={12} aria-hidden="true" />
              {isJobRunning && clusterJob?.action === "rollback"
                ? "Rolling Back…"
                : "Rollback Cluster"}
            </button>
          </div>

          {confirmAction && (
            <div className="probe-list" style={{ padding: "0.6rem", border: "1px solid var(--border)" }}>
              <div className="probe-row">
                <span>
                  {confirmAction === "update"
                    ? `Update all online worker nodes first, then the router${withEngine ? " (including inference engines)" : ""}? Services will briefly restart.`
                    : "Roll back all online worker nodes and the router to their previous git revision? Services will briefly restart."}
                </span>
              </div>
              <div className="row-actions">
                <button
                  type="button"
                  className="btn btn--sm btn--accent"
                  disabled={startingJob}
                  onClick={() => void triggerClusterAction(confirmAction)}
                >
                  {confirmAction === "update" ? "Confirm Update" : "Confirm Rollback"}
                </button>
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => setConfirmAction(null)}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {reconnecting && (
            <div className="probe-row">
              <span className="pill pill--accent">
                Router restarting — polling /health and reloading once online…
              </span>
            </div>
          )}

          {clusterJob && clusterJob.status !== "idle" && (
            <div className="probe-list" style={{ marginTop: "0.25rem" }}>
              <div className="probe-row">
                <strong style={{ fontSize: "0.82rem" }}>
                  Last Cluster {clusterJob.action === "update" ? "Update" : "Rollback"}
                </strong>
                <span
                  className={`pill ${
                    clusterJob.status === "done"
                      ? "pill--ok"
                      : clusterJob.status === "running" || clusterJob.status === "restarting"
                        ? "pill--accent"
                        : clusterJob.status === "aborted"
                          ? "pill--warn"
                          : "pill--err"
                  }`}
                >
                  {clusterJob.status}
                </span>
                {clusterJob.rolled_back && <span className="pill pill--warn">Rolled back</span>}
              </div>
              {clusterJob.error && <div className="muted">{clusterJob.error}</div>}
              {clusterJob.nodes.map((nodeRes) => (
                <div key={nodeRes.node_id} className="probe-row">
                  <span>{nodeRes.display_name}</span>
                  {nodeRes.current_sha && <code>{nodeRes.current_sha}</code>}
                  <span className={`pill ${stepPillClass(nodeRes.status)}`}>
                    {stepLabel(nodeRes.status)}
                  </span>
                  {nodeRes.error && <span className="muted">{nodeRes.error}</span>}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="settings-subcard">
        <div className="panel-head">
          <strong>
            <Cpu size={13} aria-hidden="true" /> Router Host Diagnostics &amp; GPUs
          </strong>
          <button
            type="button"
            className="btn btn--sm"
            disabled={loadingSys}
            onClick={() => void loadSystemDiagnostics(false)}
          >
            <RefreshCw size={12} aria-hidden="true" />
            {loadingSys ? "Checking…" : "Refresh"}
          </button>
        </div>

        {engineStatus && (
          <div className="probe-list">
            <div className="probe-row">
              <span className="muted">Recommended Backend</span>
              <span className="pill pill--accent">{engineStatus.recommended_backend}</span>
              {engineStatus.cuda_version && (
                <span className="pill">CUDA {engineStatus.cuda_version}</span>
              )}
            </div>
            {(engineStatus.detected_gpus ?? []).map((g) => (
              <div key={`${g.index}-${g.bus_id ?? g.name}`} className="probe-row">
                <span className="pill">{g.vendor.toUpperCase()}</span>
                <code>
                  GPU {g.index}: {g.name}
                </code>
                {g.vram_total_mb > 0 && (
                  <span className="muted">{(g.vram_total_mb / 1024).toFixed(1)} GB</span>
                )}
              </div>
            ))}
          </div>
        )}

        {doctor && (
          <div className="probe-list" style={{ marginTop: "0.5rem" }}>
            {doctor.checks.map((c) => (
              <div key={c.name} className="probe-row">
                <span
                  className={`pill ${
                    c.status === "ok"
                      ? "pill--ok"
                      : c.status === "warn"
                        ? "pill--warn"
                        : c.status === "error"
                          ? "pill--err"
                          : ""
                  }`}
                >
                  {c.name}
                </span>
                <span className="muted">{c.summary}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
