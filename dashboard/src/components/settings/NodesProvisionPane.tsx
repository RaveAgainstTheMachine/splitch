import { useEffect, useRef, useState } from "react";
import { Check, Copy, KeyRound, Plus, Server, Terminal, Trash2 } from "lucide-react";
import type {
  JoinTokenInfo,
  JoinTokenResponse,
  NodeRole,
  NodeSshTestResponse,
  RuntimeSettings
} from "../../types";

export function NodesProvisionPane({
  settings,
  copiedKey,
  copyText,
  onStatusRefresh,
  onSavedNotice,
  onLocalError,
  onParentError
}: {
  settings: RuntimeSettings | null;
  copiedKey: string | null;
  copyText: (key: string, text: string) => Promise<void>;
  onStatusRefresh: () => Promise<void>;
  onSavedNotice: (msg: string) => void;
  onLocalError: (msg: string | null) => void;
  onParentError: (msg: string | null) => void;
}) {
  const [nodeMode, setNodeMode] = useState<"ssh" | "token" | "manual">("ssh");
  const [sshForm, setSshForm] = useState({
    ssh_target: "",
    ssh_port: 22,
    node_id: "",
    display_name: "",
    role: "gpu" as NodeRole,
    yield_on_gaming: true,
    yield_gpus: "",
    engine: "auto" as "auto" | "llama.cpp" | "ollama" | "none",
    backend: "auto" as "auto" | "cuda" | "rocm" | "vulkan" | "metal" | "cpu",
    with_media: false,
    enable_tools: false,
    install_service: true,
    router_url: ""
  });
  const [sshTest, setSshTest] = useState<NodeSshTestResponse | null>(null);
  const [sshTesting, setSshTesting] = useState(false);
  const [sshInstalling, setSshInstalling] = useState(false);
  const [sshProgress, setSshProgress] = useState<{ step: number; total: number; label: string } | null>(
    null
  );
  const [sshLogs, setSshLogs] = useState<string[]>([]);
  const [sshDoneMsg, setSshDoneMsg] = useState<string | null>(null);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  const [tokForm, setTokForm] = useState({
    node_id: "",
    role: "gpu" as NodeRole,
    yield_on_gaming: true,
    engine: "auto" as "auto" | "llama.cpp" | "ollama" | "none",
    backend: "auto" as "auto" | "cuda" | "rocm" | "vulkan" | "metal" | "cpu",
    with_media: false,
    enable_tools: false,
    ttl_seconds: 900,
    router_url: ""
  });
  const [mintedToken, setMintedToken] = useState<JoinTokenResponse | null>(null);
  const [activeTokens, setActiveTokens] = useState<JoinTokenInfo[]>([]);
  const [minting, setMinting] = useState(false);

  const [manualNode, setManualNode] = useState({
    id: "",
    display_name: "",
    role: "gpu" as NodeRole,
    yield_on_gaming: true,
    yield_gpus: "",
    agent_url: "http://192.168.1.50:8090",
    engine_url: "http://192.168.1.50:8081",
    media_url: ""
  });
  const [registering, setRegistering] = useState(false);

  const loadJoinTokens = async () => {
    try {
      const res = await fetch("/api/provision/join-tokens");
      if (res.ok) setActiveTokens((await res.json()) as JoinTokenInfo[]);
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    void loadJoinTokens();
  }, []);

  useEffect(() => {
    if (logEndRef.current) {
      logEndRef.current.scrollIntoView({ block: "end" });
    }
  }, [sshLogs]);

  const runSshTest = async () => {
    setSshTesting(true);
    setSshTest(null);
    onLocalError(null);
    try {
      const res = await fetch("/api/provision/ssh/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ssh_target: sshForm.ssh_target, ssh_port: sshForm.ssh_port })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      setSshTest(body as NodeSshTestResponse);
    } catch (err) {
      onLocalError(err instanceof Error ? err.message : "SSH test failed");
    } finally {
      setSshTesting(false);
    }
  };

  const runSshProvision = async (e: React.FormEvent) => {
    e.preventDefault();
    setSshInstalling(true);
    setSshLogs([]);
    setSshProgress({ step: 0, total: 7, label: "Starting remote install…" });
    setSshDoneMsg(null);
    onLocalError(null);

    const yieldIndices = sshForm.yield_gpus
      .split(",")
      .map((s) => s.trim())
      .filter((s) => /^\d+$/.test(s))
      .map((s) => Number(s));

    try {
      const res = await fetch("/api/provision/ssh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ssh_target: sshForm.ssh_target,
          ssh_port: sshForm.ssh_port,
          node_id: sshForm.node_id,
          display_name: sshForm.display_name,
          role: sshForm.role,
          yield_on_gaming: sshForm.yield_on_gaming,
          yield_gpu_indices: yieldIndices,
          engine: sshForm.engine,
          backend: sshForm.backend,
          with_media: sshForm.with_media,
          enable_tools: sshForm.enable_tools,
          install_service: sshForm.install_service,
          router_url: sshForm.router_url || settings?.public_url || ""
        })
      });
      if (!res.ok || !res.body) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() ?? "";
        for (const chunk of parts) {
          let ev = "message";
          let dataStr = "";
          for (const line of chunk.split("\n")) {
            if (line.startsWith("event:")) ev = line.slice(6).trim();
            else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
          }
          if (!dataStr) continue;
          try {
            const payload = JSON.parse(dataStr);
            if (ev === "step") {
              setSshProgress({ step: payload.step, total: payload.total, label: payload.label });
            } else if (ev === "log" && payload.line) {
              setSshLogs((prev) => [...prev.slice(-240), payload.line]);
            } else if (ev === "error") {
              onLocalError(payload.detail || "Remote install failed");
            } else if (ev === "done") {
              setSshProgress({ step: 7, total: 7, label: "Node installed and joined" });
              setSshDoneMsg(`Installed and joined node "${payload.node_id}"`);
              await onStatusRefresh();
            }
          } catch {
            // ignore malformed SSE chunk
          }
        }
      }
    } catch (err) {
      onLocalError(err instanceof Error ? err.message : "SSH install failed");
    } finally {
      setSshInstalling(false);
    }
  };

  const mintJoinToken = async (e: React.FormEvent) => {
    e.preventDefault();
    setMinting(true);
    onLocalError(null);
    try {
      const res = await fetch("/api/provision/join-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...tokForm,
          router_url: tokForm.router_url || settings?.public_url || ""
        })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      setMintedToken(body as JoinTokenResponse);
      await loadJoinTokens();
    } catch (err) {
      onLocalError(err instanceof Error ? err.message : "Failed to mint join token");
    } finally {
      setMinting(false);
    }
  };

  const revokeToken = async (id: string) => {
    try {
      await fetch(`/api/provision/join-tokens/${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadJoinTokens();
      if (mintedToken?.id === id) setMintedToken(null);
    } catch {
      // ignore
    }
  };

  const registerManualNode = async (e: React.FormEvent) => {
    e.preventDefault();
    setRegistering(true);
    onLocalError(null);
    try {
      const yield_gpu_indices = manualNode.yield_gpus
        .split(",")
        .map((s) => s.trim())
        .filter((s) => /^\d+$/.test(s))
        .map((s) => Number(s));
      const res = await fetch("/api/nodes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: manualNode.id,
          display_name: manualNode.display_name,
          role: manualNode.role,
          yield_on_gaming: manualNode.yield_on_gaming,
          yield_gpu_indices,
          agent_url: manualNode.agent_url,
          engine_url: manualNode.engine_url,
          media_url: manualNode.media_url
        })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      setManualNode({
        id: "",
        display_name: "",
        role: "gpu",
        yield_on_gaming: true,
        yield_gpus: "",
        agent_url: "http://192.168.1.50:8090",
        engine_url: "http://192.168.1.50:8081",
        media_url: ""
      });
      onSavedNotice("Node registered");
      await onStatusRefresh();
      onParentError(null);
    } catch (err) {
      onLocalError(err instanceof Error ? err.message : "Failed to register node");
    } finally {
      setRegistering(false);
    }
  };

  return (
    <div className="settings-stack">
      <div className="client-pill-row" role="group" aria-label="Node provisioning method">
        <button
          type="button"
          className={`btn btn--sm ${nodeMode === "ssh" ? "btn--accent" : ""}`}
          aria-pressed={nodeMode === "ssh"}
          onClick={() => setNodeMode("ssh")}
        >
          <Terminal size={12} aria-hidden="true" /> SSH Push Install
        </button>
        <button
          type="button"
          className={`btn btn--sm ${nodeMode === "token" ? "btn--accent" : ""}`}
          aria-pressed={nodeMode === "token"}
          onClick={() => setNodeMode("token")}
        >
          <KeyRound size={12} aria-hidden="true" /> Join Token One-Liner
        </button>
        <button
          type="button"
          className={`btn btn--sm ${nodeMode === "manual" ? "btn--accent" : ""}`}
          aria-pressed={nodeMode === "manual"}
          onClick={() => setNodeMode("manual")}
        >
          <Plus size={12} aria-hidden="true" /> Register Existing Agent
        </button>
      </div>

      {nodeMode === "ssh" && (
        <form className="settings-stack" onSubmit={runSshProvision}>
          {settings && !settings.remote_provision_enabled && (
            <div className="banner banner--error">
              SSH Push requires <code>SPLITCH_API_KEY</code> authentication or{" "}
              <code>SPLITCH_ENABLE_REMOTE_PROVISION=1</code>. Use{" "}
              <strong>Join Token One-Liner</strong> for token-based setup.
            </div>
          )}
          <div className="two-col">
            <label className="media-field">
              <span>SSH Target (user@host)</span>
              <input
                required
                value={sshForm.ssh_target}
                onChange={(e) => setSshForm({ ...sshForm, ssh_target: e.target.value })}
                placeholder="user@192.168.1.60"
              />
            </label>
            <label className="media-field">
              <span>SSH Port</span>
              <input
                type="number"
                min={1}
                max={65535}
                value={sshForm.ssh_port}
                onChange={(e) => setSshForm({ ...sshForm, ssh_port: Number(e.target.value) })}
              />
            </label>
            <label className="media-field">
              <span>Node ID (optional)</span>
              <input
                value={sshForm.node_id}
                onChange={(e) => setSshForm({ ...sshForm, node_id: e.target.value })}
                placeholder="gpu-node-2"
              />
            </label>
            <label className="media-field">
              <span>Display Name (optional)</span>
              <input
                value={sshForm.display_name}
                onChange={(e) => setSshForm({ ...sshForm, display_name: e.target.value })}
                placeholder="Dual-GPU Rig"
              />
            </label>
            <label className="media-field">
              <span>Role</span>
              <select
                value={sshForm.role}
                onChange={(e) => setSshForm({ ...sshForm, role: e.target.value as NodeRole })}
              >
                <option value="gpu">Discrete GPU</option>
                <option value="unified">Unified Memory</option>
                <option value="cpu">CPU</option>
              </select>
            </label>
            <label className="media-field">
              <span>Engine &amp; Backend</span>
              <div className="row-actions">
                <select
                  value={sshForm.engine}
                  onChange={(e) =>
                    setSshForm({ ...sshForm, engine: e.target.value as typeof sshForm.engine })
                  }
                >
                  <option value="auto">auto</option>
                  <option value="llama.cpp">llama.cpp</option>
                  <option value="ollama">ollama</option>
                  <option value="none">none</option>
                </select>
                <select
                  value={sshForm.backend}
                  onChange={(e) =>
                    setSshForm({ ...sshForm, backend: e.target.value as typeof sshForm.backend })
                  }
                >
                  <option value="auto">auto</option>
                  <option value="cuda">cuda</option>
                  <option value="rocm">rocm</option>
                  <option value="vulkan">vulkan (multi-vendor)</option>
                  <option value="metal">metal</option>
                  <option value="cpu">cpu</option>
                </select>
              </div>
            </label>
            <label className="media-field">
              <span>Watch GPU Indices for Yield (empty = all)</span>
              <input
                value={sshForm.yield_gpus}
                onChange={(e) => setSshForm({ ...sshForm, yield_gpus: e.target.value })}
                placeholder="e.g. 1"
              />
            </label>
            <label className="media-field">
              <span>Router URL Override (optional)</span>
              <input
                type="url"
                value={sshForm.router_url}
                onChange={(e) => setSshForm({ ...sshForm, router_url: e.target.value })}
                placeholder={settings?.public_url || window.location.origin}
              />
            </label>
          </div>

          <div className="checkbox-row">
            <label className="gaming-toggle">
              <input
                type="checkbox"
                {...({ switch: "" } as Record<string, string>)}
                checked={sshForm.yield_on_gaming}
                onChange={(e) => setSshForm({ ...sshForm, yield_on_gaming: e.target.checked })}
              />
              <span>GPU Auto-Yield</span>
            </label>
            <label className="gaming-toggle">
              <input
                type="checkbox"
                {...({ switch: "" } as Record<string, string>)}
                checked={sshForm.with_media}
                onChange={(e) => setSshForm({ ...sshForm, with_media: e.target.checked })}
              />
              <span>Install sd.cpp</span>
            </label>
            <label className="gaming-toggle">
              <input
                type="checkbox"
                {...({ switch: "" } as Record<string, string>)}
                checked={sshForm.enable_tools}
                onChange={(e) => setSshForm({ ...sshForm, enable_tools: e.target.checked })}
              />
              <span>Workspace Tools</span>
            </label>
            <label className="gaming-toggle">
              <input
                type="checkbox"
                {...({ switch: "" } as Record<string, string>)}
                checked={sshForm.install_service}
                onChange={(e) => setSshForm({ ...sshForm, install_service: e.target.checked })}
              />
              <span>System Service</span>
            </label>
          </div>

          <div className="row-actions">
            <button
              type="button"
              className="btn"
              disabled={!sshForm.ssh_target || sshTesting || sshInstalling}
              onClick={() => void runSshTest()}
            >
              {sshTesting ? "Testing SSH…" : "Test SSH"}
            </button>
            <button
              type="submit"
              className="btn btn--accent"
              disabled={
                !sshForm.ssh_target ||
                sshInstalling ||
                (settings !== null && !settings.remote_provision_enabled)
              }
            >
              <Server size={13} aria-hidden="true" />
              {sshInstalling ? "Installing…" : "Install & Join Node"}
            </button>
          </div>

          {sshTest && (
            <div className="settings-subcard">
              <span className={`pill ${sshTest.ok ? "pill--ok" : "pill--err"}`}>
                {sshTest.ok ? "SSH OK" : "SSH FAILED"}
              </span>{" "}
              {sshTest.ok ? (
                <span className="muted">
                  {sshTest.os_info} · {sshTest.gpu_summary} ·{" "}
                  {sshTest.splitch_installed ? "Splitch installed (will update)" : "Fresh host"}
                </span>
              ) : (
                <span className="text-err">{sshTest.error}</span>
              )}
            </div>
          )}

          {sshProgress && (
            <div className="settings-subcard">
              <div className="panel-head">
                <strong>
                  [{sshProgress.step}/{sshProgress.total}] {sshProgress.label}
                </strong>
                {sshDoneMsg && <span className="pill pill--ok">{sshDoneMsg}</span>}
              </div>
              <progress max={sshProgress.total} value={sshProgress.step} />
              {sshLogs.length > 0 && (
                <pre className="provision-log">
                  {sshLogs.join("\n")}
                  <div ref={logEndRef} />
                </pre>
              )}
            </div>
          )}
        </form>
      )}

      {nodeMode === "token" && (
        <div className="settings-stack">
          <form className="settings-stack" onSubmit={mintJoinToken}>
            <div className="two-col">
              <label className="media-field">
                <span>Node ID (optional)</span>
                <input
                  value={tokForm.node_id}
                  onChange={(e) => setTokForm({ ...tokForm, node_id: e.target.value })}
                  placeholder="win-gpu"
                />
              </label>
              <label className="media-field">
                <span>Role</span>
                <select
                  value={tokForm.role}
                  onChange={(e) => setTokForm({ ...tokForm, role: e.target.value as NodeRole })}
                >
                  <option value="gpu">Discrete GPU</option>
                  <option value="unified">Unified Memory</option>
                  <option value="cpu">CPU</option>
                </select>
              </label>
              <label className="media-field">
                <span>Engine</span>
                <select
                  value={tokForm.engine}
                  onChange={(e) =>
                    setTokForm({ ...tokForm, engine: e.target.value as typeof tokForm.engine })
                  }
                >
                  <option value="auto">auto</option>
                  <option value="llama.cpp">llama.cpp</option>
                  <option value="ollama">ollama</option>
                  <option value="none">none</option>
                </select>
              </label>
              <label className="media-field">
                <span>Backend</span>
                <select
                  value={tokForm.backend}
                  onChange={(e) =>
                    setTokForm({ ...tokForm, backend: e.target.value as typeof tokForm.backend })
                  }
                >
                  <option value="auto">auto</option>
                  <option value="cuda">cuda</option>
                  <option value="rocm">rocm</option>
                  <option value="vulkan">vulkan (multi-vendor)</option>
                  <option value="metal">metal</option>
                  <option value="cpu">cpu</option>
                </select>
              </label>
            </div>
            <div className="checkbox-row">
              <label className="gaming-toggle">
                <input
                  type="checkbox"
                  {...({ switch: "" } as Record<string, string>)}
                  checked={tokForm.yield_on_gaming}
                  onChange={(e) => setTokForm({ ...tokForm, yield_on_gaming: e.target.checked })}
                />
                <span>GPU Auto-Yield</span>
              </label>
              <label className="gaming-toggle">
                <input
                  type="checkbox"
                  {...({ switch: "" } as Record<string, string>)}
                  checked={tokForm.with_media}
                  onChange={(e) => setTokForm({ ...tokForm, with_media: e.target.checked })}
                />
                <span>Install sd.cpp</span>
              </label>
              <label className="gaming-toggle">
                <input
                  type="checkbox"
                  {...({ switch: "" } as Record<string, string>)}
                  checked={tokForm.enable_tools}
                  onChange={(e) => setTokForm({ ...tokForm, enable_tools: e.target.checked })}
                />
                <span>Workspace Tools</span>
              </label>
              <button
                type="submit"
                className="btn btn--accent"
                style={{ marginInlineStart: "auto" }}
                disabled={minting}
              >
                <KeyRound size={12} aria-hidden="true" />
                {minting ? "Minting…" : "Generate Join Command"}
              </button>
            </div>
          </form>

          {mintedToken && (
            <div className="settings-stack">
              <div className="snippet-card">
                <div className="snippet-card__head">
                  <span className="muted">Linux / macOS · Single-use (15 min TTL)</span>
                  <button
                    type="button"
                    className="btn btn--sm"
                    onClick={() => void copyText("sh", mintedToken.linux_macos_cmd)}
                  >
                    {copiedKey === "sh" ? <Check size={12} /> : <Copy size={12} />}
                    {copiedKey === "sh" ? "Copied" : "Copy"}
                  </button>
                </div>
                <pre className="snippet-pre">
                  <code>{mintedToken.linux_macos_cmd}</code>
                </pre>
              </div>

              <div className="snippet-card">
                <div className="snippet-card__head">
                  <span className="muted">Windows PowerShell</span>
                  <button
                    type="button"
                    className="btn btn--sm"
                    onClick={() => void copyText("ps", mintedToken.windows_ps_cmd)}
                  >
                    {copiedKey === "ps" ? <Check size={12} /> : <Copy size={12} />}
                    {copiedKey === "ps" ? "Copied" : "Copy"}
                  </button>
                </div>
                <pre className="snippet-pre">
                  <code>{mintedToken.windows_ps_cmd}</code>
                </pre>
              </div>
            </div>
          )}

          {activeTokens.length > 0 && (
            <div className="settings-subcard">
              <strong>Active Join Tokens ({activeTokens.length})</strong>
              <div className="probe-list">
                {activeTokens.map((t) => (
                  <div key={t.id} className="probe-row">
                    <code>{t.id}</code>
                    <span className="muted">
                      {t.node_hint || "any node"} · expires{" "}
                      {new Date(t.expires_at * 1000).toLocaleTimeString()}
                    </span>
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost btn--danger"
                      onClick={() => void revokeToken(t.id)}
                      aria-label={`Revoke token ${t.id}`}
                    >
                      <Trash2 size={12} aria-hidden="true" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {nodeMode === "manual" && (
        <form className="modal-form" onSubmit={registerManualNode}>
          <div className="two-col">
            <label className="media-field">
              <span>Node ID</span>
              <input
                required
                value={manualNode.id}
                onChange={(e) => setManualNode({ ...manualNode, id: e.target.value })}
                placeholder="gpu-rig"
              />
            </label>
            <label className="media-field">
              <span>Display Name</span>
              <input
                required
                value={manualNode.display_name}
                onChange={(e) => setManualNode({ ...manualNode, display_name: e.target.value })}
                placeholder="Workstation GPU"
              />
            </label>
            <label className="media-field">
              <span>Role</span>
              <select
                value={manualNode.role}
                onChange={(e) => setManualNode({ ...manualNode, role: e.target.value as NodeRole })}
              >
                <option value="gpu">Discrete GPU</option>
                <option value="unified">Unified Memory</option>
                <option value="cpu">CPU</option>
              </select>
            </label>
            <label className="media-field">
              <span>Watch GPU Indices (empty = all)</span>
              <input
                value={manualNode.yield_gpus}
                onChange={(e) => setManualNode({ ...manualNode, yield_gpus: e.target.value })}
                placeholder="e.g. 1"
              />
            </label>
          </div>
          <label className="gaming-toggle">
            <input
              type="checkbox"
              {...({ switch: "" } as Record<string, string>)}
              checked={manualNode.yield_on_gaming}
              onChange={(e) => setManualNode({ ...manualNode, yield_on_gaming: e.target.checked })}
            />
            <span>GPU Auto-Yield</span>
          </label>
          <label className="media-field">
            <span>Agent URL</span>
            <input
              required
              type="url"
              value={manualNode.agent_url}
              onChange={(e) => setManualNode({ ...manualNode, agent_url: e.target.value })}
            />
          </label>
          <label className="media-field">
            <span>Engine URL</span>
            <input
              required
              type="url"
              value={manualNode.engine_url}
              onChange={(e) => setManualNode({ ...manualNode, engine_url: e.target.value })}
            />
          </label>
          <label className="media-field">
            <span>Media URL (optional)</span>
            <input
              type="url"
              value={manualNode.media_url}
              onChange={(e) => setManualNode({ ...manualNode, media_url: e.target.value })}
            />
          </label>
          <div className="modal-actions">
            <button type="submit" className="btn btn--accent" disabled={registering}>
              {registering ? "Registering…" : "Register Node"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
