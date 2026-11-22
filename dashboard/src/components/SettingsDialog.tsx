import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Check,
  Copy,
  Cpu,
  KeyRound,
  Palette,
  Play,
  Plus,
  RefreshCw,
  Save,
  Send,
  Server,
  Shield,
  Sliders,
  Terminal,
  Trash2,
  X
} from "lucide-react";
import { useDialogLightDismiss } from "./ModelsTab";
import { AppearancePanel } from "./ThemeDialog";
import type {
  ClientProbeResponse,
  DashboardSnapshot,
  DoctorReport,
  EngineStatus,
  JoinTokenInfo,
  JoinTokenResponse,
  NodeRole,
  NodeSshTestResponse,
  RuntimeSettings
} from "../types";

export type SettingsTabId = "clients" | "routing" | "nodes" | "security" | "appearance";

const SETTINGS_TABS: Array<{ id: SettingsTabId; label: string; icon: typeof Terminal }> = [
  { id: "clients", label: "Connect Clients", icon: Terminal },
  { id: "routing", label: "Routing & Guards", icon: Sliders },
  { id: "nodes", label: "Install Nodes", icon: Server },
  { id: "security", label: "Security & System", icon: Shield },
  { id: "appearance", label: "Appearance", icon: Palette }
];

type ClientId =
  | "hermes"
  | "claude_code"
  | "codex"
  | "opencode"
  | "aider"
  | "goose"
  | "zed"
  | "cline"
  | "python"
  | "curl";

const PUSHABLE_CLIENTS = new Set<ClientId>([
  "hermes",
  "claude_code",
  "codex",
  "opencode",
  "aider",
  "zed"
]);

const CLIENTS: Array<{ id: ClientId; label: string; proto: string }> = [
  { id: "hermes", label: "Hermes", proto: "OpenAI /v1" },
  { id: "claude_code", label: "Claude Code", proto: "Anthropic /v1/messages" },
  { id: "codex", label: "Codex CLI", proto: "Responses /v1/responses" },
  { id: "opencode", label: "OpenCode", proto: "OpenAI /v1 + MCP" },
  { id: "aider", label: "Aider", proto: "OpenAI /v1" },
  { id: "goose", label: "Goose", proto: "OpenAI /v1" },
  { id: "zed", label: "Zed", proto: "OpenAI /v1 + MCP" },
  { id: "cline", label: "Cline / Roo", proto: "OpenAI /v1" },
  { id: "python", label: "Python SDK", proto: "OpenAI /v1" },
  { id: "curl", label: "curl", proto: "HTTP" }
];

function buildClientSnippets(
  client: ClientId,
  baseOrigin: string,
  model: string,
  apiKey: string
): { runCmd: string; configTitle?: string; configBody?: string } {
  const origin = baseOrigin.replace(/\/+$/, "");
  const v1 = `${origin}/v1`;
  switch (client) {
    case "hermes":
      return {
        runCmd: `hermes chat --model ${model}`,
        configTitle: "~/.hermes/config.yaml",
        configBody: `model:\n  provider: custom\n  default: ${model}\n  base_url: ${v1}\n  api_key: ${apiKey}\n  context_length: 65536`
      };
    case "claude_code":
      return {
        runCmd: `ANTHROPIC_BASE_URL="${origin}" ANTHROPIC_AUTH_TOKEN="${apiKey}" ANTHROPIC_MODEL="${model}" claude`,
        configTitle: "~/.claude/settings.json",
        configBody: JSON.stringify(
          {
            env: {
              ANTHROPIC_BASE_URL: origin,
              ANTHROPIC_AUTH_TOKEN: apiKey,
              ANTHROPIC_MODEL: model
            }
          },
          null,
          2
        )
      };
    case "codex":
      return {
        runCmd: `SPLITCH_API_KEY="${apiKey}" codex -m ${model}`,
        configTitle: "~/.codex/config.toml",
        configBody: `model = "${model}"\nmodel_provider = "splitch"\n\n[model_providers.splitch]\nname = "Splitch"\nbase_url = "${v1}"\nwire_api = "responses"\nenv_key = "SPLITCH_API_KEY"`
      };
    case "opencode":
      return {
        runCmd: `opencode -m splitch/${model}`,
        configTitle: "~/.config/opencode/opencode.json",
        configBody: JSON.stringify(
          {
            $schema: "https://opencode.ai/config.json",
            provider: {
              splitch: {
                npm: "@ai-sdk/openai-compatible",
                name: "Splitch Local Cluster",
                options: { baseURL: v1, apiKey },
                models: { [model]: { name: model } }
              }
            },
            model: `splitch/${model}`
          },
          null,
          2
        )
      };
    case "aider":
      return {
        runCmd: `OPENAI_API_BASE="${v1}" OPENAI_API_KEY="${apiKey}" aider --model openai/${model} --weak-model openai/fast`,
        configTitle: "~/.aider.conf.yml",
        configBody: `openai-api-base: ${v1}\nopenai-api-key: ${apiKey}\nmodel: openai/${model}\nweak-model: openai/fast`
      };
    case "goose":
      return {
        runCmd: `OPENAI_HOST="${origin}" OPENAI_API_KEY="${apiKey}" GOOSE_PROVIDER=openai GOOSE_MODEL="${model}" goose session`
      };
    case "zed":
      return {
        runCmd: `zed .`,
        configTitle: "~/.config/zed/settings.json",
        configBody: JSON.stringify(
          {
            language_models: {
              openai_compatible: {
                Splitch: {
                  api_url: v1,
                  available_models: [
                    { name: model, display_name: `Splitch ${model}`, max_tokens: 65536 }
                  ]
                }
              }
            }
          },
          null,
          2
        )
      };
    case "cline":
      return {
        runCmd: `Provider: OpenAI Compatible  |  Base URL: ${v1}  |  API Key: ${apiKey}  |  Model: ${model}`
      };
    case "python":
      return {
        runCmd: `from openai import OpenAI\n\nclient = OpenAI(base_url="${v1}", api_key="${apiKey}")\nresp = client.chat.completions.create(\n    model="${model}",\n    messages=[{"role": "user", "content": "Hello from Splitch"}],\n)\nprint(resp.choices[0].message.content)`
      };
    case "curl":
      return {
        runCmd: `curl ${v1}/chat/completions \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${apiKey}" \\\n  -d '{"model":"${model}","messages":[{"role":"user","content":"Hello"}]}'`
      };
  }
}

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

  // Install Nodes state
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
  const [sshProgress, setSshProgress] = useState<{ step: number; total: number; label: string } | null>(null);
  const [sshLogs, setSshLogs] = useState<string[]>([]);
  const [sshDoneMsg, setSshDoneMsg] = useState<string | null>(null);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  // Join token bootstrap state
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

  // Manual node registration state
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

  // Security & System state
  const [doctor, setDoctor] = useState<DoctorReport | null>(null);
  const [engineStatus, setEngineStatus] = useState<EngineStatus | null>(null);
  const [loadingSys, setLoadingSys] = useState(false);
  const [originsText, setOriginsText] = useState("");

  const onlineNodes = useMemo(
    () => (snapshot?.nodes ?? []).filter((n) => n.online),
    [snapshot?.nodes]
  );

  useEffect(() => {
    if (!pushNodeId && onlineNodes.length > 0) {
      setPushNodeId(onlineNodes[0].node_id);
    }
  }, [onlineNodes, pushNodeId]);

  useEffect(() => {
    if (logEndRef.current) {
      logEndRef.current.scrollIntoView({ block: "end" });
    }
  }, [sshLogs]);

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

  const loadJoinTokens = async () => {
    try {
      const res = await fetch("/api/provision/join-tokens");
      if (res.ok) setActiveTokens((await res.json()) as JoinTokenInfo[]);
    } catch {
      // ignore
    }
  };

  const loadSystemDiagnostics = async () => {
    setLoadingSys(true);
    try {
      const [docRes, engRes] = await Promise.all([
        fetch("/api/system/doctor"),
        fetch("/api/system/engine")
      ]);
      if (docRes.ok) setDoctor((await docRes.json()) as DoctorReport);
      if (engRes.ok) setEngineStatus((await engRes.json()) as EngineStatus);
    } catch {
      // ignore
    } finally {
      setLoadingSys(false);
    }
  };

  useEffect(() => {
    void loadSettings();
    void loadJoinTokens();
  }, [activeTab]);

  useEffect(() => {
    if (activeTab === "security" && !doctor && !loadingSys) {
      void loadSystemDiagnostics();
    }
  }, [activeTab, doctor, loadingSys]);

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
          hsts_enabled: settings.hsts_enabled,
          trusted_proxies: settings.trusted_proxies,
          allowed_origins
        })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const updated = (await res.json()) as RuntimeSettings;
      setSettings(updated);
      setOriginsText((updated.allowed_origins ?? []).join(", "));
      setSavedNotice("Saved and applied live");
      window.setTimeout(() => setSavedNotice(null), 2500);
      await onStatusRefresh();
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "Failed to save settings");
    } finally {
      setSaving(false);
    }
  };

  const runProbe = async (protocol: "models" | "openai_chat" | "openai_responses" | "anthropic_messages") => {
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

  const runSshTest = async () => {
    setSshTesting(true);
    setSshTest(null);
    setLocalError(null);
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
      setLocalError(err instanceof Error ? err.message : "SSH test failed");
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
    setLocalError(null);

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
              setLocalError(payload.detail || "Remote install failed");
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
      setLocalError(err instanceof Error ? err.message : "SSH install failed");
    } finally {
      setSshInstalling(false);
    }
  };

  const mintJoinToken = async (e: React.FormEvent) => {
    e.preventDefault();
    setMinting(true);
    setLocalError(null);
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
      setLocalError(err instanceof Error ? err.message : "Failed to mint join token");
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
    setLocalError(null);
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
      setSavedNotice("Node registered");
      window.setTimeout(() => setSavedNotice(null), 2500);
      await onStatusRefresh();
      onError(null);
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "Failed to register node");
    } finally {
      setRegistering(false);
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

                <div className="settings-subcard">
                  <div className="panel-head">
                    <strong>Live Protocol Probe</strong>
                    <div className="row-actions" style={{ flexWrap: "wrap" }}>
                      {(
                        [
                          { id: "models", label: "/v1/models" },
                          { id: "openai_chat", label: "/v1/chat/completions" },
                          { id: "openai_responses", label: "/v1/responses" },
                          { id: "anthropic_messages", label: "/v1/messages" }
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
                            {pr.ok ? `${pr.resolved_model}${pr.snippet ? ` — ${pr.snippet}` : ""}` : pr.error}
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

                <label className="media-field">
                  <span>SearXNG URL (Web Search)</span>
                  <input
                    type="url"
                    value={settings.searxng_url}
                    onChange={(e) => setSettings({ ...settings, searxng_url: e.target.value })}
                    placeholder="http://localhost:8888"
                  />
                </label>

                <div className="row-actions">
                  <span className={`pill ${settings.brave_api_key_set ? "pill--ok" : ""}`}>
                    Brave Key: {settings.brave_api_key_set ? "Set" : "Unset"}
                  </span>
                  <span className={`pill ${settings.tavily_api_key_set ? "pill--ok" : ""}`}>
                    Tavily Key: {settings.tavily_api_key_set ? "Set" : "Unset"}
                  </span>
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

            {activeTab === "nodes" && (
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
                          onChange={(e) =>
                            setSshForm({ ...sshForm, ssh_port: Number(e.target.value) })
                          }
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
                          onChange={(e) =>
                            setSshForm({ ...sshForm, role: e.target.value as NodeRole })
                          }
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
                              setSshForm({
                                ...sshForm,
                                engine: e.target.value as typeof sshForm.engine
                              })
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
                              setSshForm({
                                ...sshForm,
                                backend: e.target.value as typeof sshForm.backend
                              })
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
                          checked={sshForm.yield_on_gaming}
                          onChange={(e) =>
                            setSshForm({ ...sshForm, yield_on_gaming: e.target.checked })
                          }
                        />
                        <span>GPU Auto-Yield</span>
                      </label>
                      <label className="gaming-toggle">
                        <input
                          type="checkbox"
                          checked={sshForm.with_media}
                          onChange={(e) => setSshForm({ ...sshForm, with_media: e.target.checked })}
                        />
                        <span>Install sd.cpp</span>
                      </label>
                      <label className="gaming-toggle">
                        <input
                          type="checkbox"
                          checked={sshForm.enable_tools}
                          onChange={(e) =>
                            setSshForm({ ...sshForm, enable_tools: e.target.checked })
                          }
                        />
                        <span>Workspace Tools</span>
                      </label>
                      <label className="gaming-toggle">
                        <input
                          type="checkbox"
                          checked={sshForm.install_service}
                          onChange={(e) =>
                            setSshForm({ ...sshForm, install_service: e.target.checked })
                          }
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
                            onChange={(e) =>
                              setTokForm({ ...tokForm, role: e.target.value as NodeRole })
                            }
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
                              setTokForm({
                                ...tokForm,
                                engine: e.target.value as typeof tokForm.engine
                              })
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
                              setTokForm({
                                ...tokForm,
                                backend: e.target.value as typeof tokForm.backend
                              })
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
                            checked={tokForm.yield_on_gaming}
                            onChange={(e) =>
                              setTokForm({ ...tokForm, yield_on_gaming: e.target.checked })
                            }
                          />
                          <span>GPU Auto-Yield</span>
                        </label>
                        <label className="gaming-toggle">
                          <input
                            type="checkbox"
                            checked={tokForm.with_media}
                            onChange={(e) =>
                              setTokForm({ ...tokForm, with_media: e.target.checked })
                            }
                          />
                          <span>Install sd.cpp</span>
                        </label>
                        <label className="gaming-toggle">
                          <input
                            type="checkbox"
                            checked={tokForm.enable_tools}
                            onChange={(e) =>
                              setTokForm({ ...tokForm, enable_tools: e.target.checked })
                            }
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
                          onChange={(e) =>
                            setManualNode({ ...manualNode, display_name: e.target.value })
                          }
                          placeholder="Workstation GPU"
                        />
                      </label>
                      <label className="media-field">
                        <span>Role</span>
                        <select
                          value={manualNode.role}
                          onChange={(e) =>
                            setManualNode({ ...manualNode, role: e.target.value as NodeRole })
                          }
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
                          onChange={(e) =>
                            setManualNode({ ...manualNode, yield_gpus: e.target.value })
                          }
                          placeholder="e.g. 1"
                        />
                      </label>
                    </div>
                    <label className="gaming-toggle">
                      <input
                        type="checkbox"
                        checked={manualNode.yield_on_gaming}
                        onChange={(e) =>
                          setManualNode({ ...manualNode, yield_on_gaming: e.target.checked })
                        }
                      />
                      <span>GPU Auto-Yield</span>
                    </label>
                    <label className="media-field">
                      <span>Agent URL</span>
                      <input
                        required
                        type="url"
                        value={manualNode.agent_url}
                        onChange={(e) =>
                          setManualNode({ ...manualNode, agent_url: e.target.value })
                        }
                      />
                    </label>
                    <label className="media-field">
                      <span>Engine URL</span>
                      <input
                        required
                        type="url"
                        value={manualNode.engine_url}
                        onChange={(e) =>
                          setManualNode({ ...manualNode, engine_url: e.target.value })
                        }
                      />
                    </label>
                    <label className="media-field">
                      <span>Media URL (optional)</span>
                      <input
                        type="url"
                        value={manualNode.media_url}
                        onChange={(e) =>
                          setManualNode({ ...manualNode, media_url: e.target.value })
                        }
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
            )}

            {activeTab === "security" && settings && (
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
                </div>

                <form className="settings-stack" onSubmit={saveSettings}>
                  <label className="gaming-toggle">
                    <input
                      type="checkbox"
                      checked={settings.hsts_enabled}
                      onChange={(e) => setSettings({ ...settings, hsts_enabled: e.target.checked })}
                    />
                    <span>Send HSTS header on HTTPS / proxied TLS requests</span>
                  </label>
                  <label className="media-field">
                    <span>Trusted Reverse Proxy IPs / CIDRs</span>
                    <input
                      value={settings.trusted_proxies}
                      onChange={(e) =>
                        setSettings({ ...settings, trusted_proxies: e.target.value })
                      }
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
                      <Cpu size={13} aria-hidden="true" /> Router Host Diagnostics &amp; GPUs
                    </strong>
                    <button
                      type="button"
                      className="btn btn--sm"
                      disabled={loadingSys}
                      onClick={() => void loadSystemDiagnostics()}
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
                            <span className="muted">
                              {(g.vram_total_mb / 1024).toFixed(1)} GB
                            </span>
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
            )}

            {activeTab === "appearance" && <AppearancePanel />}
          </div>
        </div>
      </div>
    </dialog>
  );
}
