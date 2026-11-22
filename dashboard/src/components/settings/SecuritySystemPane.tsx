import { useEffect, useState } from "react";
import { Cpu, Download, RefreshCw, Save } from "lucide-react";
import type { DoctorReport, EngineStatus, RuntimeSettings, UpdateStatus } from "../../types";

export function SecuritySystemPane({
  settings,
  setSettings,
  originsText,
  setOriginsText,
  saving,
  saveSettings
}: {
  settings: RuntimeSettings;
  setSettings: React.Dispatch<React.SetStateAction<RuntimeSettings | null>>;
  originsText: string;
  setOriginsText: (val: string) => void;
  saving: boolean;
  saveSettings: (e: React.FormEvent) => Promise<void>;
}) {
  const [doctor, setDoctor] = useState<DoctorReport | null>(null);
  const [engineStatus, setEngineStatus] = useState<EngineStatus | null>(null);
  const [updateStatus, setUpdateStatus] = useState<UpdateStatus | null>(null);
  const [loadingSys, setLoadingSys] = useState(false);
  const [checkingUpdates, setCheckingUpdates] = useState(false);

  const loadSystemDiagnostics = async (checkRemote = false) => {
    if (checkRemote) setCheckingUpdates(true);
    else setLoadingSys(true);
    try {
      const [docRes, engRes, updRes] = await Promise.all([
        fetch(`/api/system/doctor?check_updates=${checkRemote ? "true" : "false"}`),
        fetch(`/api/system/engine?check_remote=${checkRemote ? "true" : "false"}`),
        fetch(`/api/system/update-status?fetch=${checkRemote ? "true" : "false"}`)
      ]);
      if (docRes.ok) setDoctor((await docRes.json()) as DoctorReport);
      if (engRes.ok) setEngineStatus((await engRes.json()) as EngineStatus);
      if (updRes.ok) setUpdateStatus((await updRes.json()) as UpdateStatus);
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
                    ? `${updateStatus.behind_count} commit(s) available`
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
