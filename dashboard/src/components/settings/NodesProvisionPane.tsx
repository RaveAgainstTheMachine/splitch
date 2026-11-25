import { useEffect, useRef, useState } from "react";
import {
  Check,
  Copy,
  HardDrive,
  KeyRound,
  Pencil,
  Plus,
  Save,
  Server,
  Sliders,
  Terminal,
  Trash2
} from "lucide-react";
import type {
  DashboardSnapshot,
  EngineRuntimeConfig,
  JoinTokenInfo,
  JoinTokenResponse,
  NodeRole,
  NodeSshTestResponse,
  NodeStatus,
  RuntimeSettings,
  StorageUsageResponse
} from "../../types";

export function NodesProvisionPane({
  settings,
  snapshot,
  copiedKey,
  copyText,
  onStatusRefresh,
  onSavedNotice,
  onLocalError,
  onParentError
}: {
  settings: RuntimeSettings | null;
  snapshot?: DashboardSnapshot | null;
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
    engine: "auto" as "auto" | "llama.cpp" | "ollama" | "mlx-lm" | "strata" | "none",
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
    engine: "auto" as "auto" | "llama.cpp" | "ollama" | "mlx-lm" | "strata" | "none",
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
  const [editingNodeId, setEditingNodeId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({
    display_name: "",
    role: "gpu" as NodeRole,
    yield_on_gaming: false,
    yield_gpus: "",
    agent_url: "",
    engine_url: "",
    media_url: ""
  });
  const [editSaving, setEditSaving] = useState(false);
  const [engineNodeId, setEngineNodeId] = useState<string | null>(null);
  const [engineCfg, setEngineCfg] = useState<EngineRuntimeConfig | null>(null);
  const [engineBusy, setEngineBusy] = useState(false);
  const [storageMap, setStorageMap] = useState<Record<string, StorageUsageResponse>>({});
  const [storageBusyNode, setStorageBusyNode] = useState<string | null>(null);

  const startEditNode = (n: NodeStatus) => {
    if (editingNodeId === n.node_id) {
      setEditingNodeId(null);
      return;
    }
    setEditingNodeId(n.node_id);
    setEditForm({
      display_name: n.display_name,
      role: n.role,
      yield_on_gaming: n.yield_on_gaming,
      yield_gpus: (n.yield_gpu_indices ?? []).join(", "),
      agent_url: n.agent_url,
      engine_url: n.engine_url,
      media_url: n.media_url ?? ""
    });
  };

  const saveEditNode = async (e: React.FormEvent, nodeId: string) => {
    e.preventDefault();
    setEditSaving(true);
    onLocalError(null);
    const yield_gpu_indices = editForm.yield_gpus
      .split(",")
      .map((s) => s.trim())
      .filter((s) => /^\d+$/.test(s))
      .map((s) => Number(s));
    try {
      const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          display_name: editForm.display_name,
          role: editForm.role,
          yield_on_gaming: editForm.yield_on_gaming,
          yield_gpu_indices,
          agent_url: editForm.agent_url,
          engine_url: editForm.engine_url,
          media_url: editForm.media_url
        })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      setEditingNodeId(null);
      onSavedNotice(`Updated ${nodeId}`);
      await onStatusRefresh();
    } catch (err) {
      onLocalError(err instanceof Error ? err.message : "Failed to update node");
    } finally {
      setEditSaving(false);
    }
  };

  const toggleEngineTuning = async (nodeId: string) => {
    if (engineNodeId === nodeId) {
      setEngineNodeId(null);
      return;
    }
    setEngineNodeId(nodeId);
    setEngineBusy(true);
    onLocalError(null);
    try {
      const [cfgRes, storRes] = await Promise.all([
        fetch(`/api/nodes/${encodeURIComponent(nodeId)}/engine-config`),
        fetch(`/api/nodes/${encodeURIComponent(nodeId)}/storage/usage`)
      ]);
      if (cfgRes.ok) setEngineCfg((await cfgRes.json()) as EngineRuntimeConfig);
      if (storRes.ok) {
        const usage = (await storRes.json()) as StorageUsageResponse;
        setStorageMap((prev) => ({ ...prev, [nodeId]: usage }));
      }
    } catch (err) {
      onLocalError(err instanceof Error ? err.message : "Failed to inspect node engine config");
    } finally {
      setEngineBusy(false);
    }
  };

  const saveEngineConfig = async (nodeId: string, applyRestart: boolean) => {
    if (!engineCfg) return;
    setEngineBusy(true);
    onLocalError(null);
    try {
      const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/engine-config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...engineCfg, apply_restart: applyRestart })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      setEngineCfg(body as EngineRuntimeConfig);
      onSavedNotice(
        applyRestart ? `Saved & restarted engine on ${nodeId}` : `Saved engine config on ${nodeId}`
      );
      await onStatusRefresh();
    } catch (err) {
      onLocalError(err instanceof Error ? err.message : "Failed to save engine config");
    } finally {
      setEngineBusy(false);
    }
  };

  const cleanNodeCache = async (nodeId: string) => {
    setStorageBusyNode(nodeId);
    onLocalError(null);
    try {
      const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/storage/clean-cache`, {
        method: "POST"
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      onSavedNotice(`Cleaned ${body.removed_files} file(s), freed ${body.freed_gb} GB on ${nodeId}`);
      const storRes = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/storage/usage`);
      if (storRes.ok) {
        const usage = (await storRes.json()) as StorageUsageResponse;
        setStorageMap((prev) => ({ ...prev, [nodeId]: usage }));
      }
      await onStatusRefresh();
    } catch (err) {
      onLocalError(err instanceof Error ? err.message : "Cache cleanup failed");
    } finally {
      setStorageBusyNode(null);
    }
  };

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
      {(snapshot?.nodes ?? []).length > 0 && (
        <div className="settings-subcard settings-stack">
          <strong>Cluster Nodes — Config, Engine Runtime &amp; Storage</strong>
          {(snapshot?.nodes ?? []).map((n) => {
            const isEditing = editingNodeId === n.node_id;
            const isTuning = engineNodeId === n.node_id;
            const usage = storageMap[n.node_id];
            return (
              <div
                key={n.node_id}
                className="panel settings-stack"
                style={{ padding: "0.65rem", background: "var(--surface-2)" }}
              >
                <div className="panel-head" style={{ flexWrap: "wrap", gap: "0.45rem" }}>
                  <div className="row-actions" style={{ flexWrap: "wrap" }}>
                    <strong>{n.display_name}</strong>
                    <code>{n.node_id}</code>
                    <span className={`pill ${n.online ? "pill--ok" : "pill--warn"}`}>
                      {n.online ? "Online" : "Offline"}
                    </span>
                    <span className="pill">{n.role}</span>
                    {n.telemetry && (
                      <span className="pill">
                        {n.telemetry.storage_free_gb.toFixed(1)} GB free
                      </span>
                    )}
                  </div>
                  <div className="row-actions">
                    <button
                      type="button"
                      className={`btn btn--sm ${isEditing ? "btn--accent" : ""}`}
                      onClick={() => startEditNode(n)}
                    >
                      <Pencil size={11} aria-hidden="true" /> Edit
                    </button>
                    <button
                      type="button"
                      className={`btn btn--sm ${isTuning ? "btn--accent" : ""}`}
                      disabled={!n.online}
                      onClick={() => void toggleEngineTuning(n.node_id)}
                    >
                      <Sliders size={11} aria-hidden="true" /> Engine &amp; Cache
                    </button>
                  </div>
                </div>

                {isEditing && (
                  <form className="settings-stack" onSubmit={(e) => void saveEditNode(e, n.node_id)}>
                    <div className="two-col">
                      <label className="media-field">
                        <span>Display Name</span>
                        <input
                          required
                          value={editForm.display_name}
                          onChange={(e) =>
                            setEditForm({ ...editForm, display_name: e.target.value })
                          }
                        />
                      </label>
                      <label className="media-field">
                        <span>Role</span>
                        <select
                          value={editForm.role}
                          onChange={(e) =>
                            setEditForm({ ...editForm, role: e.target.value as NodeRole })
                          }
                        >
                          <option value="gpu">Discrete GPU</option>
                          <option value="unified">Unified Memory</option>
                          <option value="cpu">CPU</option>
                        </select>
                      </label>
                      <label className="media-field">
                        <span>Agent URL</span>
                        <input
                          required
                          type="url"
                          value={editForm.agent_url}
                          onChange={(e) =>
                            setEditForm({ ...editForm, agent_url: e.target.value })
                          }
                        />
                      </label>
                      <label className="media-field">
                        <span>Engine URL</span>
                        <input
                          required
                          type="url"
                          value={editForm.engine_url}
                          onChange={(e) =>
                            setEditForm({ ...editForm, engine_url: e.target.value })
                          }
                        />
                      </label>
                      <label className="media-field">
                        <span>Media URL (optional)</span>
                        <input
                          type="url"
                          value={editForm.media_url}
                          onChange={(e) =>
                            setEditForm({ ...editForm, media_url: e.target.value })
                          }
                        />
                      </label>
                      <label className="media-field">
                        <span>Watch GPU Indices (empty = all)</span>
                        <input
                          value={editForm.yield_gpus}
                          onChange={(e) =>
                            setEditForm({ ...editForm, yield_gpus: e.target.value })
                          }
                          placeholder="e.g. 0, 1"
                        />
                      </label>
                    </div>
                    <div className="row-actions">
                      <label className="gaming-toggle">
                        <input
                          type="checkbox"
                          {...({ switch: "" } as Record<string, string>)}
                          checked={editForm.yield_on_gaming}
                          onChange={(e) =>
                            setEditForm({ ...editForm, yield_on_gaming: e.target.checked })
                          }
                        />
                        <span>GPU Auto-Yield</span>
                      </label>
                      <button
                        type="submit"
                        className="btn btn--sm btn--accent"
                        style={{ marginInlineStart: "auto" }}
                        disabled={editSaving}
                      >
                        <Save size={11} aria-hidden="true" />{" "}
                        {editSaving ? "Saving…" : "Save Node"}
                      </button>
                    </div>
                  </form>
                )}

                {isTuning && engineCfg && (
                  <div className="settings-stack">
                    {!engineCfg.supported && (
                      <div className="muted" style={{ fontSize: "0.76rem" }}>
                        No managed systemd/launchd <code>llama-server</code> unit detected — values
                        saved to <code>engine_runtime.json</code> only.
                      </div>
                    )}
                    <div className="two-col">
                      <label className="media-field">
                        <span>Context Size (--ctx-size)</span>
                        <input
                          type="number"
                          min={1024}
                          max={1048576}
                          step={1024}
                          value={engineCfg.ctx_size}
                          onChange={(e) =>
                            setEngineCfg({ ...engineCfg, ctx_size: Number(e.target.value) })
                          }
                        />
                      </label>
                      <label className="media-field">
                        <span>Parallel Slots (--parallel)</span>
                        <input
                          type="number"
                          min={1}
                          max={32}
                          value={engineCfg.parallel}
                          onChange={(e) =>
                            setEngineCfg({ ...engineCfg, parallel: Number(e.target.value) })
                          }
                        />
                      </label>
                      <label className="media-field">
                        <span>KV Cache K (--cache-type-k)</span>
                        <select
                          value={engineCfg.cache_type_k}
                          onChange={(e) =>
                            setEngineCfg({
                              ...engineCfg,
                              cache_type_k: e.target.value as EngineRuntimeConfig["cache_type_k"]
                            })
                          }
                        >
                          <option value="q8_0">q8_0</option>
                          <option value="q4_0">q4_0</option>
                          <option value="f16">f16</option>
                        </select>
                      </label>
                      <label className="media-field">
                        <span>KV Cache V (--cache-type-v)</span>
                        <select
                          value={engineCfg.cache_type_v}
                          onChange={(e) =>
                            setEngineCfg({
                              ...engineCfg,
                              cache_type_v: e.target.value as EngineRuntimeConfig["cache_type_v"]
                            })
                          }
                        >
                          <option value="q8_0">q8_0</option>
                          <option value="q4_0">q4_0</option>
                          <option value="f16">f16</option>
                        </select>
                      </label>
                      <label className="media-field">
                        <span>Models Max (--models-max)</span>
                        <input
                          type="number"
                          min={1}
                          max={16}
                          value={engineCfg.models_max}
                          onChange={(e) =>
                            setEngineCfg({ ...engineCfg, models_max: Number(e.target.value) })
                          }
                        />
                      </label>
                      <label className="media-field">
                        <span>Idle Sleep (seconds, 0 = off)</span>
                        <input
                          type="number"
                          min={0}
                          max={86400}
                          value={engineCfg.sleep_idle_seconds}
                          onChange={(e) =>
                            setEngineCfg({
                              ...engineCfg,
                              sleep_idle_seconds: Number(e.target.value)
                            })
                          }
                        />
                      </label>
                    </div>

                    <div className="row-actions" style={{ flexWrap: "wrap" }}>
                      <label className="gaming-toggle">
                        <input
                          type="checkbox"
                          {...({ switch: "" } as Record<string, string>)}
                          checked={engineCfg.flash_attn}
                          onChange={(e) =>
                            setEngineCfg({ ...engineCfg, flash_attn: e.target.checked })
                          }
                        />
                        <span>Flash Attention (--flash-attn)</span>
                      </label>
                      <button
                        type="button"
                        className="btn btn--sm"
                        style={{ marginInlineStart: "auto" }}
                        disabled={engineBusy}
                        onClick={() => void saveEngineConfig(n.node_id, false)}
                      >
                        Save Drop-In
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm btn--accent"
                        disabled={engineBusy || !engineCfg.supported}
                        onClick={() => void saveEngineConfig(n.node_id, true)}
                      >
                        <Save size={11} aria-hidden="true" /> Save &amp; Restart Engine
                      </button>
                    </div>

                    {usage && (
                      <div className="panel-head" style={{ flexWrap: "wrap", gap: "0.45rem" }}>
                        <div className="row-actions" style={{ flexWrap: "wrap" }}>
                          <span className="pill">
                            <HardDrive size={11} aria-hidden="true" /> {usage.storage_path}
                          </span>
                          <span className="pill">Models: {usage.models_gb} GB</span>
                          <span className="pill">Cache: {usage.cache_gb} GB</span>
                          {usage.partial_count > 0 && (
                            <span className="pill pill--warn">
                              Partials ({usage.partial_count}): {usage.partial_gb} GB
                            </span>
                          )}
                        </div>
                        <button
                          type="button"
                          className="btn btn--sm btn--danger"
                          disabled={
                            storageBusyNode === n.node_id ||
                            (usage.cache_entries === 0 && usage.partial_count === 0)
                          }
                          onClick={() => void cleanNodeCache(n.node_id)}
                        >
                          <Trash2 size={11} aria-hidden="true" />{" "}
                          {storageBusyNode === n.node_id ? "Cleaning…" : "Clean Cache & Partials"}
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

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
                  <option value="mlx-lm">mlx-lm (Apple Silicon)</option>
                  <option value="strata">strata (Sparse MoE)</option>
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
                  <option value="mlx-lm">mlx-lm (Apple Silicon)</option>
                  <option value="strata">strata (Sparse MoE)</option>
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
