import { useEffect, useId, useMemo, useState } from "react";
import { Check, Copy, Play, Save, Send, X } from "lucide-react";
import { useDialogLightDismiss } from "./ModelsTab";
import { AppearancePanel } from "./ThemeDialog";
import { AgentChatSettingsPane } from "./settings/AgentChatSettingsPane";
import { BenchmarksPane } from "./settings/BenchmarksPane";
import {
  buildClientSnippets,
  CLIENTS,
  type ClientId,
  PUSHABLE_CLIENTS,
  SETTINGS_TABS,
  type SettingsTabId
} from "./settings/clientSnippets";
import { NodesProvisionPane } from "./settings/NodesProvisionPane";
import { SecuritySystemPane } from "./settings/SecuritySystemPane";
import type {
  ClientProbeResponse,
  ControlState,
  DashboardSnapshot,
  RuntimeSettings
} from "../types";

export type { SettingsTabId };

export function SettingsDialog({
  dialogRef,
  activeTab,
  onTabChange,
  snapshot,
  onStatusRefresh,
  onError
}: {
  dialogRef: React.RefObject<HTMLDialogElement | null>;
  activeTab: SettingsTabId;
  onTabChange: (tab: SettingsTabId) => void;
  snapshot: DashboardSnapshot | null;
  onStatusRefresh: () => Promise<void>;
  onError: (msg: string | null) => void;
}) {
  useDialogLightDismiss(dialogRef);
  const titleId = useId();
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [originsText, setOriginsText] = useState("");
  const [controlBusy, setControlBusy] = useState(false);
  const [secretInputs, setSecretInputs] = useState<{
    brave_api_key: string;
    tavily_api_key: string;
    hf_token: string;
  }>({ brave_api_key: "", tavily_api_key: "", hf_token: "" });
  const [secretBusy, setSecretBusy] = useState<string | null>(null);

  // Connect Clients state
  const [clientId, setClientId] = useState<ClientId>("hermes");
  const [clientModel, setClientModel] = useState<string>("heavy");
  const [customBaseUrl, setCustomBaseUrl] = useState<string>("");
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [pushNodeId, setPushNodeId] = useState<string>("");
  const [pushResult, setPushResult] = useState<string | null>(null);
  const [pushing, setPushing] = useState(false);
  const [probeBusy, setProbeBusy] = useState<string | null>(null);
  const [probeResults, setProbeResults] = useState<Record<string, ClientProbeResponse>>({});

  const onlineNodes = useMemo(
    () => (snapshot?.nodes ?? []).filter((n) => n.online),
    [snapshot?.nodes]
  );

  useEffect(() => {
    if (!pushNodeId && onlineNodes.length > 0) {
      setPushNodeId(onlineNodes[0].node_id);
    }
  }, [onlineNodes, pushNodeId]);

  const showSavedNotice = (msg: string) => {
    setSavedNotice(msg);
    window.setTimeout(() => setSavedNotice(null), 2500);
  };

  const loadSettings = async () => {
    try {
      const res = await fetch("/api/settings");
      if (res.ok) {
        const data = (await res.json()) as RuntimeSettings;
        setSettings(data);
        setOriginsText((data.allowed_origins ?? []).join(", "));
      }
    } catch {
      // ignore transient errors
    }
  };

  useEffect(() => {
    void loadSettings();
  }, [activeTab]);

  const copyText = async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      window.setTimeout(() => setCopiedKey((prev) => (prev === key ? null : prev)), 1800);
    } catch {
      setLocalError("Clipboard write blocked by browser");
    }
  };

  const saveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settings) return;
    setSaving(true);
    setLocalError(null);
    setSavedNotice(null);
    try {
      const allowed_origins = originsText
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await fetch("/api/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          non_llm_vram_threshold_mb: settings.non_llm_vram_threshold_mb,
          min_vram_free_mb: settings.min_vram_free_mb,
          max_external_gpu_util_percent: settings.max_external_gpu_util_percent,
          yield_cooldown_s: settings.yield_cooldown_s,
          auto_evict_on_yield: settings.auto_evict_on_yield,
          poll_interval_s: settings.poll_interval_s,
          auto_heavy_char_threshold: settings.auto_heavy_char_threshold,
          max_traces: settings.max_traces,
          public_url: settings.public_url,
          searxng_url: settings.searxng_url,
          web_search_provider: settings.web_search_provider,
          hsts_enabled: settings.hsts_enabled,
          trusted_proxies: settings.trusted_proxies,
          allowed_origins,
          login_max_attempts: settings.login_max_attempts,
          login_window_s: settings.login_window_s,
          approval_policy: settings.approval_policy,
          shell_allowlist: settings.shell_allowlist,
          shell_denylist: settings.shell_denylist,
          max_agent_steps: settings.max_agent_steps,
          max_parallel_agents: settings.max_parallel_agents,
          auto_compact_threshold: settings.auto_compact_threshold,
          thinking_display: settings.thinking_display,
          completion_notifications: settings.completion_notifications
        })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const updated = (await res.json()) as RuntimeSettings;
      setSettings(updated);
      setOriginsText((updated.allowed_origins ?? []).join(", "));
      showSavedNotice("Saved and applied live");
      await onStatusRefresh();
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "Failed to save settings");
    } finally {
      setSaving(false);
    }
  };

  const saveSecretKey = async (name: "brave_api_key" | "tavily_api_key" | "hf_token") => {
    const val = secretInputs[name].trim();
    if (!val) return;
    setSecretBusy(name);
    setLocalError(null);
    try {
      const res = await fetch("/api/settings/secrets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, value: val })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      setSettings(body as RuntimeSettings);
      setSecretInputs((prev) => ({ ...prev, [name]: "" }));
      showSavedNotice(`Saved ${name} to encrypted Vault`);
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "Failed to store secret");
    } finally {
      setSecretBusy(null);
    }
  };

  const clearSecretKey = async (name: "brave_api_key" | "tavily_api_key" | "hf_token") => {
    setSecretBusy(name);
    setLocalError(null);
    try {
      const res = await fetch(`/api/settings/secrets/${encodeURIComponent(name)}`, {
        method: "DELETE"
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      setSettings(body as RuntimeSettings);
      showSavedNotice(`Cleared ${name} from Vault`);
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "Failed to remove secret");
    } finally {
      setSecretBusy(null);
    }
  };

  const patchClusterControl = async (patch: Partial<ControlState>) => {
    setControlBusy(true);
    setLocalError(null);
    try {
      const res = await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch)
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await onStatusRefresh();
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "Control update failed");
    } finally {
      setControlBusy(false);
    }
  };

  const runProbe = async (
    protocol:
      | "models"
      | "openai_chat"
      | "openai_responses"
      | "anthropic_messages"
      | "anthropic_count_tokens"
  ) => {
    setProbeBusy(protocol);
    setLocalError(null);
    try {
      const res = await fetch("/api/clients/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ protocol, model: clientModel })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as ClientProbeResponse;
      setProbeResults((prev) => ({ ...prev, [protocol]: data }));
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "Probe failed");
    } finally {
      setProbeBusy(null);
    }
  };

  const pushClientConfig = async () => {
    if (!pushNodeId || !PUSHABLE_CLIENTS.has(clientId)) return;
    setPushing(true);
    setPushResult(null);
    setLocalError(null);
    try {
      const res = await fetch("/api/clients/push", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client: clientId,
          node_id: pushNodeId,
          model: clientModel,
          router_url: effectiveOrigin
        })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      setPushResult(`Written to ${body.path} on ${pushNodeId}`);
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "Push failed");
    } finally {
      setPushing(false);
    }
  };

  const availableModels = useMemo(() => {
    const list: string[] = ["auto"];
    for (const a of snapshot?.aliases ?? []) {
      if (!list.includes(a.alias)) list.push(a.alias);
    }
    for (const n of snapshot?.nodes ?? []) {
      for (const m of n.telemetry?.models ?? []) {
        const key = `${n.node_id}/${m.id}`;
        if (!list.includes(key)) list.push(key);
      }
    }
    return list;
  }, [snapshot]);

  const effectiveOrigin = (customBaseUrl.trim() || settings?.public_url || window.location.origin).replace(
    /\/+$/,
    ""
  );
  const apiKeySnippet = settings?.auth_enabled ? "$SPLITCH_API_KEY" : "splitch-local";
  const snippet = useMemo(
    () => buildClientSnippets(clientId, effectiveOrigin, clientModel, apiKeySnippet),
    [clientId, effectiveOrigin, clientModel, apiKeySnippet]
  );

  const handleTabKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>, idx: number) => {
    let next = -1;
    if (e.key === "ArrowDown" || e.key === "ArrowRight") next = (idx + 1) % SETTINGS_TABS.length;
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft")
      next = (idx - 1 + SETTINGS_TABS.length) % SETTINGS_TABS.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = SETTINGS_TABS.length - 1;
    if (next >= 0) {
      e.preventDefault();
      onTabChange(SETTINGS_TABS[next].id);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="settings-dialog"
      aria-labelledby={titleId}
      {...({ closedby: "any" } as Record<string, string>)}
    >
      <div className="settings-modal">
        <header className="settings-modal__header">
          <h2 id={titleId}>Settings</h2>
          <div className="row-actions">
            {savedNotice && <span className="pill pill--ok">{savedNotice}</span>}
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() => dialogRef.current?.close()}
              aria-label="Close settings"
            >
              <X size={15} aria-hidden="true" />
            </button>
          </div>
        </header>

        {localError && (
          <div role="alert" className="banner banner--error" style={{ margin: "0 1rem" }}>
            {localError}
          </div>
        )}

        <div className="settings-modal__body">
          <nav
            className="settings-nav"
            role="tablist"
            aria-label="Settings sections"
            aria-orientation="vertical"
          >
            {SETTINGS_TABS.map(({ id, label, icon: Icon }, idx) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={activeTab === id}
                tabIndex={activeTab === id ? 0 : -1}
                className="settings-nav__item"
                onClick={() => {
                  setLocalError(null);
                  onTabChange(id);
                }}
                onKeyDown={(e) => handleTabKeyDown(e, idx)}
              >
                <Icon size={14} aria-hidden="true" />
                <span>{label}</span>
              </button>
            ))}
          </nav>

          <div className="settings-pane" role="tabpanel">
            {activeTab === "agent" && settings && (
              <AgentChatSettingsPane
                settings={settings}
                setSettings={setSettings}
                saving={saving}
                saveSettings={saveSettings}
                onSavedNotice={showSavedNotice}
                onLocalError={setLocalError}
              />
            )}

            {activeTab === "clients" && (
              <div className="settings-stack">
                <div className="two-col">
                  <label className="media-field">
                    <span>Model / Alias</span>
                    <select value={clientModel} onChange={(e) => setClientModel(e.target.value)}>
                      {availableModels.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="media-field">
                    <span>Base URL Override</span>
                    <input
                      type="url"
                      value={customBaseUrl}
                      onChange={(e) => setCustomBaseUrl(e.target.value)}
                      placeholder={settings?.public_url || window.location.origin}
                    />
                  </label>
                </div>

                <div className="client-pill-row" role="group" aria-label="CLI and IDE clients">
                  {CLIENTS.map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      className={`btn btn--sm ${clientId === c.id ? "btn--accent" : ""}`}
                      aria-pressed={clientId === c.id}
                      onClick={() => {
                        setClientId(c.id);
                        setPushResult(null);
                      }}
                    >
                      {c.label}
                    </button>
                  ))}
                </div>

                <div className="snippet-card">
                  <div className="snippet-card__head">
                    <span className="muted">
                      Command · {CLIENTS.find((c) => c.id === clientId)?.proto}
                    </span>
                    <button
                      type="button"
                      className="btn btn--sm"
                      onClick={() => void copyText("run", snippet.runCmd)}
                    >
                      {copiedKey === "run" ? <Check size={12} /> : <Copy size={12} />}
                      {copiedKey === "run" ? "Copied" : "Copy"}
                    </button>
                  </div>
                  <pre className="snippet-pre">
                    <code>{snippet.runCmd}</code>
                  </pre>
                </div>

                {snippet.configBody && (
                  <div className="snippet-card">
                    <div className="snippet-card__head">
                      <code>{snippet.configTitle}</code>
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() => void copyText("cfg", snippet.configBody ?? "")}
                      >
                        {copiedKey === "cfg" ? <Check size={12} /> : <Copy size={12} />}
                        {copiedKey === "cfg" ? "Copied" : "Copy"}
                      </button>
                    </div>
                    <pre className="snippet-pre">
                      <code>{snippet.configBody}</code>
                    </pre>
                  </div>
                )}

                {PUSHABLE_CLIENTS.has(clientId) && onlineNodes.length > 0 && (
                  <div className="settings-subcard">
                    <div className="panel-head">
                      <strong>Push Config to Node</strong>
                      <div className="row-actions">
                        <select
                          aria-label="Target node for config push"
                          value={pushNodeId}
                          onChange={(e) => setPushNodeId(e.target.value)}
                        >
                          {onlineNodes.map((n) => (
                            <option key={n.node_id} value={n.node_id}>
                              {n.display_name} ({n.node_id})
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          className="btn btn--sm btn--accent"
                          disabled={pushing}
                          onClick={() => void pushClientConfig()}
                        >
                          <Send size={12} aria-hidden="true" />
                          {pushing ? "Pushing…" : `Write to ${pushNodeId}`}
                        </button>
                      </div>
                    </div>
                    {pushResult && <p className="pill pill--ok">{pushResult}</p>}
                  </div>
                )}

                {clientId === "claude_code" && (
                  <p className="muted" style={{ margin: 0, fontSize: "0.78rem" }}>
                    Native Anthropic <code>/v1/messages</code> &amp;{" "}
                    <code>/v1/messages/count_tokens</code> adapter translates streaming, thinking
                    blocks, and tool calls to any local engine. Standard <code>claude-sonnet-*</code>{" "}
                    / <code>claude-opus-*</code> requests map automatically to <code>heavy</code>,
                    and <code>claude-haiku-*</code> maps to <code>fast</code>.
                  </p>
                )}

                <div className="settings-subcard">
                  <div className="panel-head">
                    <strong>Live Protocol Probe</strong>
                    <div className="row-actions" style={{ flexWrap: "wrap" }}>
                      {(
                        [
                          { id: "models", label: "/v1/models" },
                          { id: "openai_chat", label: "/v1/chat/completions" },
                          { id: "openai_responses", label: "/v1/responses" },
                          { id: "anthropic_messages", label: "/v1/messages" },
                          { id: "anthropic_count_tokens", label: "/v1/messages/count_tokens" }
                        ] as const
                      ).map((p) => (
                        <button
                          key={p.id}
                          type="button"
                          className="btn btn--sm"
                          disabled={probeBusy !== null}
                          onClick={() => void runProbe(p.id)}
                        >
                          <Play size={11} aria-hidden="true" />
                          {probeBusy === p.id ? "Testing…" : p.label}
                        </button>
                      ))}
                    </div>
                  </div>
                  {Object.values(probeResults).length > 0 && (
                    <div className="probe-list">
                      {Object.values(probeResults).map((pr) => (
                        <div key={pr.protocol} className="probe-row">
                          <code>{pr.endpoint}</code>
                          <span className={`pill ${pr.ok ? "pill--ok" : "pill--err"}`}>
                            HTTP {pr.status_code} · {pr.latency_ms}ms
                          </span>
                          <span className="muted probe-row__snippet">
                            {pr.ok
                              ? `${pr.resolved_model}${pr.snippet ? ` — ${pr.snippet}` : ""}`
                              : pr.error}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}

            {activeTab === "routing" && settings && (
              <form className="settings-stack" onSubmit={saveSettings}>
                <div className="settings-subcard">
                  <div className="panel-head">
                    <strong>Live Cluster Overrides</strong>
                    <div className="row-actions">
                      <span className="pill pill--ok">Model-Aware VRAM Fit</span>
                      <span className="pill pill--ok">Pre-Stream Failover</span>
                    </div>
                  </div>
                  <div className="two-col">
                    <label className="gaming-toggle">
                      <input
                        type="checkbox"
                        {...({ switch: "" } as Record<string, string>)}
                        disabled={controlBusy}
                        checked={Boolean(snapshot?.control.gaming_mode)}
                        onChange={(e) => void patchClusterControl({ gaming_mode: e.target.checked })}
                      />
                      <span>Manual Gaming Mode (yield all gaming GPUs)</span>
                    </label>
                    <label className="media-field">
                      <span>Force Routing Target</span>
                      <select
                        disabled={controlBusy}
                        value={snapshot?.control.force_node ?? "auto"}
                        onChange={(e) => void patchClusterControl({ force_node: e.target.value })}
                      >
                        <option value="auto">Auto Split (Hardware-Aware)</option>
                        {(snapshot?.nodes ?? []).map((n) => (
                          <option key={n.node_id} value={n.node_id}>
                            Force {n.display_name} ({n.node_id})
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                </div>

                <h3>GPU Auto-Yield &amp; VRAM Guards</h3>
                <div className="two-col">
                  <label className="media-field">
                    <span>Non-LLM VRAM Yield Threshold (MB)</span>
                    <input
                      type="number"
                      min={256}
                      max={262144}
                      value={settings.non_llm_vram_threshold_mb}
                      onChange={(e) =>
                        setSettings({ ...settings, non_llm_vram_threshold_mb: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label className="media-field">
                    <span>Min Free VRAM Guard (MB)</span>
                    <input
                      type="number"
                      min={0}
                      max={262144}
                      value={settings.min_vram_free_mb}
                      onChange={(e) =>
                        setSettings({ ...settings, min_vram_free_mb: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label className="media-field">
                    <span>Max External GPU Util (%)</span>
                    <input
                      type="number"
                      min={10}
                      max={100}
                      value={settings.max_external_gpu_util_percent}
                      onChange={(e) =>
                        setSettings({
                          ...settings,
                          max_external_gpu_util_percent: Number(e.target.value)
                        })
                      }
                    />
                  </label>
                  <label className="media-field">
                    <span>Yield Cooldown (seconds)</span>
                    <input
                      type="number"
                      min={0}
                      max={3600}
                      value={settings.yield_cooldown_s}
                      onChange={(e) =>
                        setSettings({ ...settings, yield_cooldown_s: Number(e.target.value) })
                      }
                    />
                  </label>
                </div>
                <label className="gaming-toggle">
                  <input
                    type="checkbox"
                    {...({ switch: "" } as Record<string, string>)}
                    checked={settings.auto_evict_on_yield}
                    onChange={(e) =>
                      setSettings({ ...settings, auto_evict_on_yield: e.target.checked })
                    }
                  />
                  <span>Auto-evict loaded LLM models from GPU VRAM when yielding</span>
                </label>

                <h3>Router &amp; Web Search</h3>
                <div className="two-col">
                  <label className="media-field">
                    <span>Poll Interval (seconds)</span>
                    <input
                      type="number"
                      step={0.5}
                      min={1}
                      max={60}
                      value={settings.poll_interval_s}
                      onChange={(e) =>
                        setSettings({ ...settings, poll_interval_s: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label className="media-field">
                    <span>Auto-Heavy Char Threshold</span>
                    <input
                      type="number"
                      min={500}
                      max={500000}
                      value={settings.auto_heavy_char_threshold}
                      onChange={(e) =>
                        setSettings({
                          ...settings,
                          auto_heavy_char_threshold: Number(e.target.value)
                        })
                      }
                    />
                  </label>
                  <label className="media-field">
                    <span>Max Routing Traces</span>
                    <input
                      type="number"
                      min={10}
                      max={1000}
                      value={settings.max_traces}
                      onChange={(e) =>
                        setSettings({ ...settings, max_traces: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label className="media-field">
                    <span>Public / Proxy Base URL</span>
                    <input
                      type="url"
                      value={settings.public_url}
                      onChange={(e) => setSettings({ ...settings, public_url: e.target.value })}
                      placeholder="https://ai.example.com"
                    />
                  </label>
                </div>

                <div className="two-col">
                  <label className="media-field">
                    <span>Web Search Provider</span>
                    <select
                      value={settings.web_search_provider}
                      onChange={(e) =>
                        setSettings({
                          ...settings,
                          web_search_provider: e.target.value as RuntimeSettings["web_search_provider"]
                        })
                      }
                    >
                      <option value="auto">Auto (SearXNG → Brave → Tavily → Open Web)</option>
                      <option value="searxng">SearXNG Only</option>
                      <option value="brave">Brave Search Only</option>
                      <option value="tavily">Tavily AI Only</option>
                      <option value="off">Disabled</option>
                    </select>
                  </label>
                  <label className="media-field">
                    <span>SearXNG URL</span>
                    <input
                      type="url"
                      value={settings.searxng_url}
                      onChange={(e) => setSettings({ ...settings, searxng_url: e.target.value })}
                      placeholder="http://localhost:8888"
                    />
                  </label>
                </div>

                <h3>Encrypted Vault Keys (Write-Only)</h3>
                <div className="settings-subcard settings-stack">
                  {(
                    [
                      {
                        name: "hf_token" as const,
                        label: "Hugging Face Token (HF_TOKEN — gated pulls)",
                        isSet: settings.hf_token_set,
                        placeholder: "hf_..."
                      },
                      {
                        name: "brave_api_key" as const,
                        label: "Brave Search API Key",
                        isSet: settings.brave_api_key_set,
                        placeholder: "BSA..."
                      },
                      {
                        name: "tavily_api_key" as const,
                        label: "Tavily API Key",
                        isSet: settings.tavily_api_key_set,
                        placeholder: "tvly-..."
                      }
                    ] as const
                  ).map((item) => (
                    <div key={item.name} className="probe-row" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
                      <span className={`pill ${item.isSet ? "pill--ok" : ""}`}>
                        {item.isSet ? "Set" : "Unset"}
                      </span>
                      <span style={{ minWidth: "14rem", fontSize: "0.82rem" }}>{item.label}</span>
                      <input
                        type="password"
                        autoComplete="off"
                        value={secretInputs[item.name]}
                        onChange={(e) =>
                          setSecretInputs((prev) => ({ ...prev, [item.name]: e.target.value }))
                        }
                        placeholder={item.isSet ? "•••••••• (replace)" : item.placeholder}
                        style={{ flex: "1 1 12rem" }}
                      />
                      <button
                        type="button"
                        className="btn btn--sm btn--accent"
                        disabled={secretBusy !== null || !secretInputs[item.name].trim()}
                        onClick={() => void saveSecretKey(item.name)}
                      >
                        Save Key
                      </button>
                      {item.isSet && (
                        <button
                          type="button"
                          className="btn btn--sm btn--danger"
                          disabled={secretBusy !== null}
                          onClick={() => void clearSecretKey(item.name)}
                        >
                          Clear
                        </button>
                      )}
                    </div>
                  ))}
                </div>

                <div className="row-actions">
                  <button
                    type="submit"
                    className="btn btn--accent"
                    style={{ marginInlineStart: "auto" }}
                    disabled={saving}
                  >
                    <Save size={13} aria-hidden="true" />
                    {saving ? "Saving…" : "Save Settings"}
                  </button>
                </div>
              </form>
            )}

            {activeTab === "benchmarks" && (
              <BenchmarksPane
                availableModels={availableModels}
                effectiveOrigin={effectiveOrigin}
                copiedKey={copiedKey}
                copyText={copyText}
                onStatusRefresh={onStatusRefresh}
                onLocalError={setLocalError}
              />
            )}

            {activeTab === "nodes" && (
              <NodesProvisionPane
                settings={settings}
                snapshot={snapshot}
                copiedKey={copiedKey}
                copyText={copyText}
                onStatusRefresh={onStatusRefresh}
                onSavedNotice={showSavedNotice}
                onLocalError={setLocalError}
                onParentError={onError}
              />
            )}

            {activeTab === "security" && settings && (
              <SecuritySystemPane
                settings={settings}
                setSettings={setSettings}
                originsText={originsText}
                setOriginsText={setOriginsText}
                saving={saving}
                saveSettings={saveSettings}
                snapshot={snapshot}
                onSavedNotice={showSavedNotice}
                onLocalError={setLocalError}
              />
            )}

            {activeTab === "appearance" && <AppearancePanel />}
          </div>
        </div>
      </div>
    </dialog>
  );
}
