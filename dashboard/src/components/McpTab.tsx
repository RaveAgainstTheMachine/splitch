import { useEffect, useId, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Code2,
  Download,
  ExternalLink,
  FileJson,
  Hammer,
  Play,
  Plus,
  RefreshCw,
  Search,
  Send,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Trash2,
  Wrench,
  XCircle
} from "lucide-react";
import type {
  McpRegistryResult,
  McpServerConfig,
  McpTestResult,
  McpToolSpec,
  NodeStatus,
  ScopedExtensionsConfig
} from "../types";
import type { ConfirmState } from "./ConfirmDialog";
import { useDialogLightDismiss } from "./ModelsTab";

type DialogMode = "preset" | "json" | "build" | "manual";

interface McpPreset {
  id: string;
  name: string;
  description: string;
  transport: "stdio" | "http" | "sse";
  scope: string;
  command?: string;
  args: string[];
  url?: string;
  fields?: Array<{
    key: string;
    label: string;
    placeholder: string;
    target: "arg" | "env" | "url";
    argPrefix?: string;
    required?: boolean;
  }>;
}

const EXTERNAL_PKG_TOKENS = [
  "context7",
  "server-github",
  "server-slack",
  "server-brave",
  "tavily",
  "firecrawl",
  "exa-mcp"
];

const EXTERNAL_ENV_PREFIXES = [
  "GITHUB_",
  "BRAVE_",
  "TAVILY_",
  "OPENAI_",
  "ANTHROPIC_",
  "SLACK_",
  "NOTION_",
  "LINEAR_",
  "SENTRY_",
  "SUPABASE_",
  "CLOUDFLARE_",
  "UPSTASH_",
  "EXA_",
  "FIRECRAWL_"
];

export function classifyClientMcpEgress(opts: {
  transport: "stdio" | "http" | "sse";
  command?: string | null;
  args?: string[];
  url?: string | null;
  envKeys?: string[];
}): { kind: "local" | "external_api" | "remote_mcp"; target: string | null } {
  if (opts.transport !== "stdio") {
    const rawUrl = (opts.url ?? "").trim();
    if (!rawUrl) return { kind: "remote_mcp", target: "remote HTTP/SSE endpoint" };
    try {
      const host = new URL(rawUrl).hostname.toLowerCase();
      const isLocal =
        host === "localhost" ||
        host === "127.0.0.1" ||
        host === "::1" ||
        host.startsWith("10.") ||
        host.startsWith("192.168.") ||
        host.startsWith("100.") ||
        /^172\.(1[6-9]|2\d|3[0-1])\./.test(host) ||
        host.endsWith(".local") ||
        host.endsWith(".lan") ||
        host.endsWith(".internal");
      return isLocal ? { kind: "local", target: host } : { kind: "remote_mcp", target: host };
    } catch {
      return { kind: "remote_mcp", target: rawUrl };
    }
  }
  const cmdLine = `${opts.command ?? ""} ${(opts.args ?? []).join(" ")}`.toLowerCase();
  for (const tok of EXTERNAL_PKG_TOKENS) {
    if (cmdLine.includes(tok)) return { kind: "external_api", target: tok };
  }
  for (const k of opts.envKeys ?? []) {
    const ku = k.toUpperCase();
    if (EXTERNAL_ENV_PREFIXES.some((p) => ku.startsWith(p))) {
      return { kind: "external_api", target: ku };
    }
  }
  return { kind: "local", target: null };
}

const MCP_PRESETS: McpPreset[] = [
  {
    id: "graphify",
    name: "Graphify AST Graph",
    description: "Local tree-sitter AST graph",
    transport: "stdio",
    scope: "shared",
    command: "sh",
    args: [
      "-c",
      'W="${GRAPHIFY_WORKSPACE:-${1:-.}}"; G="$W/graphify-out/graph.json"; [ -f "$G" ] || uvx --from "graphifyy[mcp]" graphify update "$W" >&2; exec uvx --from "graphifyy[mcp]" python -m graphify.serve "$G"',
      "--"
    ],
    fields: [
      {
        key: "workspace",
        label: "Repository Path",
        placeholder: "/path/to/repo",
        target: "arg",
        required: false
      }
    ]
  },
  {
    id: "fetch",
    name: "Web Fetch",
    description: "Fetch web pages & convert HTML to markdown",
    transport: "stdio",
    scope: "shared",
    command: "uvx",
    args: ["mcp-server-fetch"]
  },
  {
    id: "filesystem",
    name: "Filesystem",
    description: "Read/write files scoped to a directory",
    transport: "stdio",
    scope: "shared",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-filesystem"],
    fields: [
      {
        key: "dir",
        label: "Allowed Directory Path",
        placeholder: "/path/to/workspace",
        target: "arg",
        required: true
      }
    ]
  },
  {
    id: "git",
    name: "Git Repo",
    description: "Inspect status, diffs, logs & branches",
    transport: "stdio",
    scope: "shared",
    command: "uvx",
    args: ["mcp-server-git"],
    fields: [
      {
        key: "repo",
        label: "Repository Path",
        placeholder: "/path/to/git/repo",
        target: "arg",
        argPrefix: "--repository",
        required: true
      }
    ]
  },
  {
    id: "github",
    name: "GitHub",
    description: "Issues, PRs, code search & repo ops",
    transport: "stdio",
    scope: "shared",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-github"],
    fields: [
      {
        key: "GITHUB_PERSONAL_ACCESS_TOKEN",
        label: "GitHub Personal Access Token",
        placeholder: "ghp_...",
        target: "env",
        required: true
      }
    ]
  },
  {
    id: "context7",
    name: "Context7 Docs",
    description: "Up-to-date library & framework docs",
    transport: "stdio",
    scope: "shared",
    command: "npx",
    args: ["-y", "@upstash/context7-mcp@latest"]
  },
  {
    id: "playwright",
    name: "Playwright Browser",
    description: "Headless/live browser automation & inspection",
    transport: "stdio",
    scope: "shared",
    command: "npx",
    args: ["-y", "@playwright/mcp@latest"]
  },
  {
    id: "memory",
    name: "Memory Graph",
    description: "Persistent entity/relation memory across chats",
    transport: "stdio",
    scope: "shared",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-memory"]
  },
  {
    id: "sqlite",
    name: "SQLite DB",
    description: "Query & inspect a local SQLite database",
    transport: "stdio",
    scope: "shared",
    command: "uvx",
    args: ["mcp-server-sqlite"],
    fields: [
      {
        key: "db",
        label: "SQLite File Path",
        placeholder: "/path/to/database.db",
        target: "arg",
        argPrefix: "--db-path",
        required: true
      }
    ]
  },
  {
    id: "remote-http",
    name: "Remote HTTP MCP",
    description: "Connect to any Streamable HTTP / LAN MCP endpoint",
    transport: "http",
    scope: "shared",
    args: [],
    fields: [
      {
        key: "url",
        label: "MCP Endpoint URL",
        placeholder: "http://localhost:8900/mcp",
        target: "url",
        required: true
      }
    ]
  }
];

export function McpTab({
  nodes,
  onError,
  onConfirm
}: {
  nodes: NodeStatus[];
  onError: (msg: string | null) => void;
  onConfirm: (state: ConfirmState) => void;
}) {
  const [servers, setServers] = useState<McpServerConfig[]>([]);
  const [globalMcps, setGlobalMcps] = useState<string[]>([]);
  const [globalSkills, setGlobalSkills] = useState<string[]>([]);
  const [testResults, setTestResults] = useState<Record<string, McpTestResult>>({});
  const [testingId, setTestingId] = useState<string | null>(null);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const [syncNodeId, setSyncNodeId] = useState<string>("");
  const [exportClient, setExportClient] = useState<"opencode" | "zed">("opencode");
  const [exportPreview, setExportPreview] = useState<string>("");

  // Live MCP Registry / npm Discovery state
  const [searchQuery, setSearchQuery] = useState("");
  const [searchSource, setSearchSource] = useState<"all" | "official" | "npm">("all");
  const [searching, setSearching] = useState(false);
  const [searchResults, setSearchResults] = useState<McpRegistryResult[]>([]);

  const dialogRef = useRef<HTMLDialogElement>(null);
  useDialogLightDismiss(dialogRef);
  const dialogTitleId = useId();
  const tableCaptionId = useId();

  const [dialogMode, setDialogMode] = useState<DialogMode>("preset");
  const [activePreset, setActivePreset] = useState<McpPreset>(MCP_PRESETS[0]);
  const [presetInputs, setPresetInputs] = useState<Record<string, string>>({});
  const [pasteJson, setPasteJson] = useState("");

  // Builder state
  const [buildForm, setBuildForm] = useState({
    id: "custom-tools",
    name: "Custom Tools",
    description: "Custom local Python MCP server",
    scope: "shared",
    customCode: "",
    useCustomCode: false
  });
  const [buildTools, setBuildTools] = useState<McpToolSpec[]>([
    {
      name: "check_service",
      description: "Check status of a local service",
      parameters: [{ name: "target", type: "string", description: "Host or URL", required: true }]
    }
  ]);

  // Advanced manual form state
  const [form, setForm] = useState({
    id: "",
    name: "",
    description: "",
    transport: "stdio" as McpServerConfig["transport"],
    scope: "shared",
    command: "npx",
    argsText: "-y @modelcontextprotocol/server-fetch",
    url: "",
    envText: ""
  });

  useEffect(() => {
    if (!syncNodeId && nodes.length > 0) {
      const firstOnline = nodes.find((n) => n.online) ?? nodes[0];
      setSyncNodeId(firstOnline.node_id);
    }
  }, [nodes, syncNodeId]);

  const loadServers = async () => {
    try {
      const [mRes, scRes] = await Promise.all([fetch("/api/mcp"), fetch("/api/chat/scopes")]);
      if (mRes.ok) setServers(await mRes.json());
      if (scRes.ok) {
        const sc = (await scRes.json()) as ScopedExtensionsConfig;
        setGlobalMcps(sc.global_scope.mcps ?? []);
        setGlobalSkills(sc.global_scope.skills ?? []);
      }
    } catch {
      // ignore
    }
  };

  const toggleGlobalMcp = async (id: string, checked: boolean) => {
    const next = checked
      ? Array.from(new Set([...globalMcps, id]))
      : globalMcps.filter((x) => x !== id);
    setGlobalMcps(next);
    try {
      const res = await fetch("/api/chat/scopes/global", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skills: globalSkills, mcps: next })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to update global MCP scope");
    }
  };

  const loadExport = async (client: "opencode" | "zed") => {
    try {
      const origin = window.location.origin;
      const res = await fetch(
        `/api/mcp/export/${client}?router_url=${encodeURIComponent(origin)}`
      );
      if (res.ok) {
        setExportPreview(JSON.stringify(await res.json(), null, 2));
      }
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    loadServers();
    loadExport(exportClient);
  }, [exportClient]);

  const applyPresetToManual = (preset: McpPreset, inputs: Record<string, string>) => {
    const args = [...preset.args];
    const envLines: string[] = [];
    let url = preset.url ?? "";

    for (const f of preset.fields ?? []) {
      const val = (inputs[f.key] ?? "").trim();
      if (!val) continue;
      if (f.target === "arg") {
        if (f.argPrefix) args.push(f.argPrefix);
        args.push(val);
      } else if (f.target === "env") {
        envLines.push(`${f.key}=${val}`);
      } else if (f.target === "url") {
        url = val;
      }
    }

    setForm({
      id: preset.id,
      name: preset.name,
      description: preset.description,
      transport: preset.transport,
      scope: preset.scope,
      command: preset.command ?? "",
      argsText: args.join(" "),
      url,
      envText: envLines.join("\n")
    });
  };

  const openPresetDialog = (preset: McpPreset) => {
    setActivePreset(preset);
    setPresetInputs({});
    setDialogMode("preset");
    dialogRef.current?.showModal();
  };

  const runMcpSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (searchQuery.trim().length < 2) return;
    setSearching(true);
    try {
      const res = await fetch(
        `/api/mcp/search?q=${encodeURIComponent(searchQuery.trim())}&source=${searchSource}&limit=18`
      );
      if (!res.ok) throw new Error(`MCP search failed: HTTP ${res.status}`);
      setSearchResults(await res.json());
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "MCP search failed");
    } finally {
      setSearching(false);
    }
  };

  const installRegistryResult = (item: McpRegistryResult) => {
    const preset: McpPreset = {
      id: item.id,
      name: item.registry_name,
      description: item.description || `Installed from ${item.source} registry`,
      transport: item.transport,
      scope: "shared",
      command: item.command ?? undefined,
      args: item.args,
      url: item.url ?? undefined,
      fields: item.env_vars.map((ev) => ({
        key: ev.name,
        label: `${ev.name}${ev.is_required ? " *" : ""}${ev.description ? ` — ${ev.description}` : ""}`,
        placeholder: ev.default ?? (ev.is_secret ? "secret..." : "value..."),
        target: "env" as const,
        required: ev.is_required
      }))
    };
    const defaults: Record<string, string> = {};
    for (const ev of item.env_vars) {
      if (ev.default) defaults[ev.name] = ev.default;
    }
    setActivePreset(preset);
    setPresetInputs(defaults);
    setDialogMode("preset");
    dialogRef.current?.showModal();
  };

  const openEditor = (mode: DialogMode, srv?: McpServerConfig) => {
    setDialogMode(mode);
    if (srv) {
      setForm({
        id: srv.id,
        name: srv.name,
        description: srv.description,
        transport: srv.transport,
        scope: srv.scope,
        command: srv.command ?? "",
        argsText: srv.args.join(" "),
        url: srv.url ?? "",
        envText: Object.entries(srv.env)
          .map(([k, v]) => `${k}=${v}`)
          .join("\n")
      });
    }
    dialogRef.current?.showModal();
  };

  const openCustomScriptEditor = async (srv: McpServerConfig) => {
    try {
      const res = await fetch(`/api/mcp/${encodeURIComponent(srv.id)}/script`);
      if (!res.ok) throw new Error("No custom Python script stored for this MCP server");
      const data = await res.json();
      setBuildForm({
        id: srv.id,
        name: srv.name,
        description: srv.description,
        scope: srv.scope,
        customCode: data.code,
        useCustomCode: true
      });
      setDialogMode("build");
      dialogRef.current?.showModal();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to load script");
    }
  };

  const submitPreset = async (e: React.FormEvent) => {
    e.preventDefault();
    const args = [...activePreset.args];
    const env: Record<string, string> = {};
    let url: string | null = activePreset.url ?? null;

    for (const f of activePreset.fields ?? []) {
      const val = (presetInputs[f.key] ?? "").trim();
      if (!val) continue;
      if (f.target === "arg") {
        if (f.argPrefix) args.push(f.argPrefix);
        args.push(val);
      } else if (f.target === "env") {
        env[f.key] = val;
      } else if (f.target === "url") {
        url = val;
      }
    }

    const payload: McpServerConfig = {
      id: activePreset.id,
      name: activePreset.name,
      description: activePreset.description,
      transport: activePreset.transport,
      scope: activePreset.scope,
      enabled: true,
      command: activePreset.transport === "stdio" ? (activePreset.command ?? null) : null,
      args,
      url,
      env
    };

    try {
      const res = await fetch("/api/mcp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      dialogRef.current?.close();
      await loadServers();
      await loadExport(exportClient);
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to add preset MCP");
    }
  };

  const submitPasteJson = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const res = await fetch("/api/mcp/import-json", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw_json: pasteJson, default_scope: "shared" })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setPasteJson("");
      dialogRef.current?.close();
      await loadServers();
      await loadExport(exportClient);
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "JSON import failed");
    }
  };

  const submitBuildMcp = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const res = await fetch("/api/mcp/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: buildForm.id,
          name: buildForm.name,
          description: buildForm.description,
          scope: buildForm.scope,
          tools: buildTools,
          code: buildForm.useCustomCode && buildForm.customCode.trim() ? buildForm.customCode : null
        })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      dialogRef.current?.close();
      await loadServers();
      await loadExport(exportClient);
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Custom MCP build failed");
    }
  };

  const saveServer = async (e: React.FormEvent) => {
    e.preventDefault();
    const env: Record<string, string> = {};
    for (const line of form.envText.split("\n")) {
      const idx = line.indexOf("=");
      if (idx > 0) env[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    }
    const payload: McpServerConfig = {
      id: form.id.trim().toLowerCase(),
      name: form.name.trim() || form.id.trim(),
      description: form.description.trim(),
      transport: form.transport,
      scope: form.scope,
      enabled: true,
      command: form.transport === "stdio" ? form.command.trim() : null,
      args: form.transport === "stdio" ? form.argsText.trim().split(/\s+/).filter(Boolean) : [],
      url: form.transport !== "stdio" ? form.url.trim() : null,
      env
    };
    try {
      const res = await fetch("/api/mcp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      dialogRef.current?.close();
      await loadServers();
      await loadExport(exportClient);
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to save MCP server");
    }
  };

  const toggleServer = async (id: string, enabled: boolean) => {
    try {
      const res = await fetch(`/api/mcp/${encodeURIComponent(id)}/toggle?enabled=${enabled}`, {
        method: "POST"
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadServers();
      await loadExport(exportClient);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Toggle failed");
    }
  };

  const deleteServer = (id: string) => {
    onConfirm({
      title: `Delete MCP server "${id}"?`,
      message: `Remove "${id}" from Splitch's MCP registry?`,
      confirmLabel: "Delete MCP",
      danger: true,
      onConfirm: async () => {
        try {
          const res = await fetch(`/api/mcp/${encodeURIComponent(id)}`, { method: "DELETE" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          await loadServers();
          await loadExport(exportClient);
        } catch (err) {
          onError(err instanceof Error ? err.message : "Delete failed");
        }
      }
    });
  };

  const testServer = async (id: string) => {
    setTestingId(id);
    try {
      const res = await fetch(`/api/mcp/${encodeURIComponent(id)}/test`, { method: "POST" });
      const data: McpTestResult = await res.json();
      setTestResults((prev) => ({ ...prev, [id]: data }));
    } catch (err) {
      onError(err instanceof Error ? err.message : "MCP probe failed");
    } finally {
      setTestingId(null);
    }
  };

  const syncToNode = async (nodeId: string, client: "opencode" | "zed") => {
    if (!nodeId) return;
    setSyncMsg(null);
    try {
      const origin = window.location.origin;
      const res = await fetch(
        `/api/mcp/sync/${encodeURIComponent(nodeId)}/${client}?router_url=${encodeURIComponent(origin)}`,
        { method: "POST" }
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      setSyncMsg(`Synced ${client} config to ${nodeId}: ${data.path}`);
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : `Sync to ${nodeId} failed`);
    }
  };

  const installedIds = new Set(servers.map((s) => s.id));

  return (
    <div className="tab-stack">
      <section className="panel">
        <div className="panel-head">
          <h2>
            <Sparkles size={14} aria-hidden="true" /> MCP Catalog
          </h2>
          <div className="row-actions">
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => openEditor("json")}
            >
              <FileJson size={12} aria-hidden="true" /> Paste JSON
            </button>
            <button
              type="button"
              className="btn btn--sm btn--accent"
              onClick={() => {
                setBuildForm((f) => ({ ...f, useCustomCode: false }));
                openEditor("build");
              }}
            >
              <Hammer size={12} aria-hidden="true" /> Build Python
            </button>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => openEditor("manual")}
            >
              <Settings2 size={12} aria-hidden="true" /> Manual
            </button>
          </div>
        </div>

        <form className="hf-controls" onSubmit={runMcpSearch}>
          <input
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search MCP registry or npm…"
            aria-label="Search MCP registries"
          />
          <select
            aria-label="MCP search source"
            value={searchSource}
            onChange={(e) => setSearchSource(e.target.value as "all" | "official" | "npm")}
          >
            <option value="all">All (Official + npm)</option>
            <option value="official">Official MCP Registry</option>
            <option value="npm">npm Packages</option>
          </select>
          <button type="submit" className="btn btn--accent" disabled={searching}>
            <Search size={14} aria-hidden="true" />
            {searching ? "Searching..." : "Search MCP"}
          </button>
        </form>

        {searchResults.length > 0 && (
          <div className="hf-results">
            <div className="hf-results__meta muted">
              {searchResults.length} MCP servers from{" "}
              {searchSource === "all"
                ? "Official Registry + npm"
                : searchSource === "official"
                  ? "registry.modelcontextprotocol.io"
                  : "npm"}
            </div>
            <div className="hf-grid">
              {searchResults.map((item) => {
                const added = installedIds.has(item.id);
                const reqEnvCount = item.env_vars.filter((v) => v.is_required).length;
                const egress = classifyClientMcpEgress({
                  transport: item.transport,
                  command: item.command,
                  args: item.args,
                  url: item.url,
                  envKeys: item.env_vars.map((v) => v.name)
                });
                return (
                  <article key={`${item.source}:${item.registry_name}`} className="repo-card">
                    <div className="discovery-card">
                      <div className="repo-card__top">
                        <span className="repo-card__owner">{item.registry_name}</span>
                        <span className="repo-card__top-badges">
                          <span
                            className={`pill ${egress.kind === "local" ? "pill--ok" : "pill--warn"}`}
                            title={
                              egress.kind === "local"
                                ? "Installs & executes locally via stdio"
                                : `External data egress (${egress.target})`
                            }
                          >
                            {egress.kind === "local"
                              ? "LOCAL STDIO"
                              : egress.kind === "external_api"
                                ? "EXTERNAL API"
                                : "REMOTE MCP"}
                          </span>
                          {item.version && <span className="pill">v{item.version}</span>}
                        </span>
                      </div>
                      <p className="discovery-card__desc">
                        {item.description || "No description provided."}
                      </p>
                      <div className="repo-card__tags">
                        <code className="filename-ellipsis">
                          <span>
                            {item.transport === "stdio"
                              ? `${item.command ?? ""} ${item.args.join(" ")}`.trim()
                              : (item.url ?? "")}
                          </span>
                        </code>
                        {reqEnvCount > 0 && (
                          <span className="pill pill--warn">{reqEnvCount} env req</span>
                        )}
                      </div>
                      <div className="discovery-card__foot">
                        <div className="row-actions">
                          {item.downloads_weekly !== null && (
                            <span className="stat-chip muted">
                              <Download size={11} aria-hidden="true" />{" "}
                              {item.downloads_weekly.toLocaleString()}/wk
                            </span>
                          )}
                          {(item.repo_url || item.website_url) && (
                            <a
                              href={item.repo_url || item.website_url || "#"}
                              target="_blank"
                              rel="noreferrer"
                              className="btn btn--sm btn--ghost"
                              aria-label={`Open ${item.registry_name}`}
                            >
                              <ExternalLink size={12} aria-hidden="true" />
                            </a>
                          )}
                        </div>
                        <button
                          type="button"
                          className={`btn btn--sm ${added ? "" : "btn--accent"}`}
                          onClick={() => installRegistryResult(item)}
                        >
                          {added ? "Reconfigure" : "Install"}
                        </button>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          </div>
        )}

        <div className="muted" style={{ fontSize: "0.76rem" }}>
          Presets
        </div>
        <div className="preset-grid">
          {MCP_PRESETS.map((p) => {
            const added = installedIds.has(p.id);
            const egress = classifyClientMcpEgress({
              transport: p.transport,
              command: p.command,
              args: p.args,
              url: p.url,
              envKeys: (p.fields ?? []).filter((f) => f.target === "env").map((f) => f.key)
            });
            return (
              <article key={p.id} className="preset-card">
                <button
                  type="button"
                  className="preset-card__btn"
                  onClick={() => openPresetDialog(p)}
                >
                  <span className="preset-card__head">
                    <strong>{p.name}</strong>
                    <span
                      className={`pill ${
                        added ? "pill--ok" : egress.kind === "local" ? "pill--ok" : "pill--warn"
                      }`}
                    >
                      {added
                        ? "ADDED"
                        : egress.kind === "local"
                          ? "local stdio"
                          : egress.kind === "external_api"
                            ? "ext API"
                            : "remote"}
                    </span>
                  </span>
                  <span className="muted">{p.description}</span>
                </button>
              </article>
            );
          })}
        </div>
      </section>

      <section className="panel" aria-labelledby={tableCaptionId}>
        <div className="panel-head">
          <h2 id={tableCaptionId}>
            <Wrench size={14} aria-hidden="true" /> MCP Servers ({servers.length})
          </h2>
          <button type="button" className="btn btn--sm" onClick={loadServers}>
            <RefreshCw size={12} aria-hidden="true" /> Refresh
          </button>
        </div>

        <div tabIndex={0} role="region" aria-labelledby={tableCaptionId} className="table-wrapper">
          <table className="data-table mcp-table">
            <thead>
              <tr>
                <th scope="col">ID</th>
                <th scope="col">Transport / Target</th>
                <th scope="col">Egress</th>
                <th scope="col">Scope</th>
                <th scope="col">Enabled</th>
                <th scope="col" title="Attach to all chats">
                  Global
                </th>
                <th scope="col">Status</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {servers.length === 0 ? (
                <tr>
                  <td colSpan={8} className="muted empty-cell">
                    No MCP servers.
                  </td>
                </tr>
              ) : (
                servers.map((s) => {
                  const tr = testResults[s.id];
                  const isCustomPy =
                    s.command === "python3" &&
                    s.args.some((a) => a.includes("mcp_servers/") && a.endsWith(".py"));
                  const egress =
                    s.egress ??
                    classifyClientMcpEgress({
                      transport: s.transport,
                      command: s.command,
                      args: s.args,
                      url: s.url,
                      envKeys: Object.keys(s.env)
                    }).kind;
                  const egressTarget = s.egress_target ?? s.url ?? "";
                  return (
                    <tr key={s.id}>
                      <th scope="row">
                        <code>{s.id}</code>
                        {s.description && <div className="muted">{s.description}</div>}
                      </th>
                      <td>
                        <span className="pill">{s.transport}</span>{" "}
                        <code>
                          {s.transport === "stdio"
                            ? `${s.command ?? ""} ${s.args.join(" ")}`.trim()
                            : (s.url ?? "")}
                        </code>
                      </td>
                      <td>
                        {egress === "local" ? (
                          <span className="pill pill--ok" title="Runs locally; zero external data leak">
                            <ShieldCheck size={11} aria-hidden="true" /> Local
                          </span>
                        ) : (
                          <span
                            className="pill pill--warn"
                            title={`Advised: tool calls may send workspace context externally (${egressTarget})`}
                          >
                            <ShieldAlert size={11} aria-hidden="true" />{" "}
                            {egress === "remote_mcp" ? "Remote Egress" : "External API"}
                          </span>
                        )}
                      </td>
                      <td>{s.scope}</td>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`Enable ${s.id}`}
                          checked={s.enabled}
                          onChange={(e) => toggleServer(s.id, e.target.checked)}
                        />
                      </td>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`Use ${s.id} globally in all chats`}
                          checked={globalMcps.includes(s.id)}
                          onChange={(e) => void toggleGlobalMcp(s.id, e.target.checked)}
                        />
                      </td>
                      <td>
                        {tr ? (
                          tr.ok ? (
                            <span className="pill pill--ok" title={tr.tools.join(", ")}>
                              <CheckCircle2 size={12} aria-hidden="true" /> {tr.tools.length} tools (
                              {tr.latency_ms}ms)
                            </span>
                          ) : (
                            <span className="pill pill--err" title={tr.error ?? ""}>
                              <XCircle size={12} aria-hidden="true" /> {tr.error?.slice(0, 36)}
                            </span>
                          )
                        ) : (
                          <span className="muted">untested</span>
                        )}
                      </td>
                      <td>
                        <div className="row-actions">
                          <button
                            type="button"
                            className="btn btn--sm"
                            disabled={testingId === s.id}
                            onClick={() => testServer(s.id)}
                          >
                            <Play size={12} aria-hidden="true" /> Test
                          </button>
                          {isCustomPy && (
                            <button
                              type="button"
                              className="btn btn--sm"
                              onClick={() => openCustomScriptEditor(s)}
                            >
                              <Code2 size={12} aria-hidden="true" /> Code
                            </button>
                          )}
                          <button
                            type="button"
                            className="btn btn--sm"
                            onClick={() => openEditor("manual", s)}
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            className="btn btn--sm btn--danger"
                            onClick={() => deleteServer(s.id)}
                          >
                            <Trash2 size={12} aria-hidden="true" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Client Config</h2>
          <div className="row-actions">
            <select
              aria-label="Target node"
              value={syncNodeId}
              onChange={(e) => setSyncNodeId(e.target.value)}
            >
              {nodes.map((n) => (
                <option key={n.node_id} value={n.node_id}>
                  {n.display_name} ({n.online ? "online" : "offline"})
                </option>
              ))}
            </select>
            <select
              aria-label="Client format"
              value={exportClient}
              onChange={(e) => setExportClient(e.target.value as "opencode" | "zed")}
            >
              <option value="opencode">OpenCode (opencode.json)</option>
              <option value="zed">Zed (settings.json)</option>
            </select>
            <button
              type="button"
              className="btn btn--sm btn--accent"
              disabled={!syncNodeId}
              onClick={() => syncToNode(syncNodeId, exportClient)}
            >
              <Send size={12} aria-hidden="true" /> Sync to {syncNodeId || "Node"} ({exportClient})
            </button>
          </div>
        </div>
        {syncMsg && <p className="pill pill--ok">{syncMsg}</p>}
        <pre className="code-preview">{exportPreview}</pre>
      </section>

      <dialog
        ref={dialogRef}
        aria-labelledby={dialogTitleId}
        {...({ closedby: "any" } as Record<string, string>)}
      >
        <div className="panel-head">
          <h2 id={dialogTitleId}>
            {dialogMode === "preset"
              ? activePreset.name
              : dialogMode === "json"
                ? "Import JSON"
                : dialogMode === "build"
                  ? "Build Python MCP"
                  : "Manual MCP"}
          </h2>
          <div className="dialog-modes" role="group" aria-label="Configuration mode">
            {(
              [
                { id: "preset", label: "Preset" },
                { id: "json", label: "Paste JSON" },
                { id: "build", label: "Build Python" },
                { id: "manual", label: "Manual" }
              ] as const
            ).map((m) => (
              <button
                key={m.id}
                type="button"
                className={`btn btn--sm ${dialogMode === m.id ? "btn--accent" : ""}`}
                aria-pressed={dialogMode === m.id}
                onClick={() => {
                  if (m.id === "manual" && dialogMode === "preset") {
                    applyPresetToManual(activePreset, presetInputs);
                  }
                  setDialogMode(m.id);
                }}
              >
                {m.label}
              </button>
            ))}
          </div>
        </div>

        {dialogMode === "preset" && (
          <form className="modal-form" onSubmit={submitPreset}>
            <p className="muted" style={{ margin: 0 }}>
              {activePreset.description} — runs{" "}
              <code>
                {activePreset.transport === "stdio"
                  ? `${activePreset.command ?? ""} ${activePreset.args.join(" ")}`
                  : "HTTP/SSE endpoint"}
              </code>
            </p>
            {(() => {
              const eg = classifyClientMcpEgress({
                transport: activePreset.transport,
                command: activePreset.command,
                args: activePreset.args,
                url: presetInputs.url || activePreset.url,
                envKeys: (activePreset.fields ?? [])
                  .filter((f) => f.target === "env")
                  .map((f) => f.key)
              });
              if (eg.kind !== "local") {
                return (
                  <div role="alert" className="banner banner--warn" style={{ fontSize: "0.78rem" }}>
                    <AlertTriangle size={14} aria-hidden="true" />{" "}
                    External egress: <code>{eg.target ?? "external endpoint"}</code>
                  </div>
                );
              }
              return null;
            })()}
            {(activePreset.fields ?? []).map((f, idx) => (
              <label key={f.key}>
                {f.label}
                <input
                  autoFocus={idx === 0}
                  required={f.required}
                  value={presetInputs[f.key] ?? ""}
                  onChange={(e) =>
                    setPresetInputs((prev) => ({ ...prev, [f.key]: e.target.value }))
                  }
                  placeholder={f.placeholder}
                />
              </label>
            ))}
            <div className="modal-actions">
              <button
                type="button"
                className="btn"
                onClick={() => {
                  applyPresetToManual(activePreset, presetInputs);
                  setDialogMode("manual");
                }}
              >
                Customize in Manual →
              </button>
              <button type="button" className="btn" onClick={() => dialogRef.current?.close()}>
                Cancel
              </button>
              <button type="submit" className="btn btn--accent">
                Add {activePreset.name}
              </button>
            </div>
          </form>
        )}

        {dialogMode === "json" && (
          <form className="modal-form" onSubmit={submitPasteJson}>
            <label>
              MCP JSON
              <textarea
                required
                autoFocus
                rows={10}
                value={pasteJson}
                onChange={(e) => setPasteJson(e.target.value)}
                placeholder={`{\n  "mcpServers": {\n    "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] }\n  }\n}`}
              />
            </label>
            <div className="modal-actions">
              <button type="button" className="btn" onClick={() => dialogRef.current?.close()}>
                Cancel
              </button>
              <button type="submit" className="btn btn--accent">
                Import JSON
              </button>
            </div>
          </form>
        )}

        {dialogMode === "build" && (
          <form className="modal-form" onSubmit={submitBuildMcp}>
            <div className="two-col">
              <label>
                Server ID
                <input
                  required
                  autoFocus
                  value={buildForm.id}
                  onChange={(e) => setBuildForm({ ...buildForm, id: e.target.value })}
                  placeholder="homelab-tools"
                />
              </label>
              <label>
                Scope
                <select
                  value={buildForm.scope}
                  onChange={(e) =>
                    setBuildForm({
                      ...buildForm,
                      scope: e.target.value
                    })
                  }
                >
                  <option value="shared">shared</option>
                  {nodes.map((n) => (
                    <option key={n.node_id} value={n.node_id}>
                      {n.display_name} ({n.node_id})
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="two-col">
              <label>
                Display Name
                <input
                  value={buildForm.name}
                  onChange={(e) => setBuildForm({ ...buildForm, name: e.target.value })}
                />
              </label>
              <label>
                Description
                <input
                  value={buildForm.description}
                  onChange={(e) => setBuildForm({ ...buildForm, description: e.target.value })}
                />
              </label>
            </div>

            {!buildForm.useCustomCode ? (
              <div className="tool-builder-list">
                <div className="panel-head">
                  <span className="muted">Defined Tools ({buildTools.length})</span>
                  <button
                    type="button"
                    className="btn btn--sm"
                    onClick={() =>
                      setBuildTools((prev) => [
                        ...prev,
                        { name: `tool_${prev.length + 1}`, description: "", parameters: [] }
                      ])
                    }
                  >
                    <Plus size={12} aria-hidden="true" /> Add Tool
                  </button>
                </div>
                {buildTools.map((tool, ti) => (
                  <div key={ti} className="tool-builder-card">
                    <div className="two-col">
                      <input
                        required
                        value={tool.name}
                        placeholder="tool_name"
                        onChange={(e) =>
                          setBuildTools((prev) =>
                            prev.map((t, i) => (i === ti ? { ...t, name: e.target.value } : t))
                          )
                        }
                      />
                      <input
                        value={tool.description}
                        placeholder="What this tool does"
                        onChange={(e) =>
                          setBuildTools((prev) =>
                            prev.map((t, i) =>
                              i === ti ? { ...t, description: e.target.value } : t
                            )
                          )
                        }
                      />
                    </div>
                    <div className="row-actions">
                      <span className="muted">
                        Params:{" "}
                        {tool.parameters.map((p) => `${p.name}:${p.type}`).join(", ") || "none"}
                      </span>
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() =>
                          setBuildTools((prev) =>
                            prev.map((t, i) =>
                              i === ti
                                ? {
                                    ...t,
                                    parameters: [
                                      ...t.parameters,
                                      {
                                        name: `arg_${t.parameters.length + 1}`,
                                        type: "string",
                                        description: "",
                                        required: true
                                      }
                                    ]
                                  }
                                : t
                            )
                          )
                        }
                      >
                        + Param
                      </button>
                      {buildTools.length > 1 && (
                        <button
                          type="button"
                          className="btn btn--sm btn--danger"
                          onClick={() =>
                            setBuildTools((prev) => prev.filter((_, i) => i !== ti))
                          }
                        >
                          Remove
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <label>
                Python Script
                <textarea
                  required
                  rows={14}
                  value={buildForm.customCode}
                  onChange={(e) => setBuildForm({ ...buildForm, customCode: e.target.value })}
                />
              </label>
            )}

            <label className="gaming-toggle">
              <input
                type="checkbox"
                checked={buildForm.useCustomCode}
                onChange={(e) => setBuildForm({ ...buildForm, useCustomCode: e.target.checked })}
              />
              <span>Edit raw Python script</span>
            </label>

            <div className="modal-actions">
              <button type="button" className="btn" onClick={() => dialogRef.current?.close()}>
                Cancel
              </button>
              <button type="submit" className="btn btn--accent">
                <Hammer size={12} aria-hidden="true" /> Build & Register MCP
              </button>
            </div>
          </form>
        )}

        {dialogMode === "manual" && (
          <form className="modal-form" onSubmit={saveServer}>
            <div className="two-col">
              <label>
                Server ID
                <input
                  required
                  autoFocus
                  value={form.id}
                  onChange={(e) => setForm({ ...form, id: e.target.value })}
                  placeholder="fetch"
                />
              </label>
              <label>
                Display Name
                <input
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="Web Fetch"
                />
              </label>
            </div>
            <label>
              Description
              <input
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="Description"
              />
            </label>
            <div className="two-col">
              <label>
                Transport
                <select
                  value={form.transport}
                  onChange={(e) =>
                    setForm({ ...form, transport: e.target.value as McpServerConfig["transport"] })
                  }
                >
                  <option value="stdio">stdio</option>
                  <option value="http">http</option>
                  <option value="sse">sse</option>
                </select>
              </label>
              <label>
                Scope
                <select
                  value={form.scope}
                  onChange={(e) =>
                    setForm({ ...form, scope: e.target.value })
                  }
                >
                  <option value="shared">shared</option>
                  {nodes.map((n) => (
                    <option key={n.node_id} value={n.node_id}>
                      {n.display_name} ({n.node_id})
                    </option>
                  ))}
                </select>
              </label>
            </div>

            {form.transport === "stdio" ? (
              <>
                <label>
                  Command
                  <input
                    required
                    value={form.command}
                    onChange={(e) => setForm({ ...form, command: e.target.value })}
                    placeholder="npx or uvx"
                  />
                </label>
                <label>
                  Arguments
                  <input
                    value={form.argsText}
                    onChange={(e) => setForm({ ...form, argsText: e.target.value })}
                    placeholder="-y @modelcontextprotocol/server-fetch"
                  />
                </label>
              </>
            ) : (
              <>
                <label>
                  Server URL
                  <input
                    required
                    type="url"
                    value={form.url}
                    onChange={(e) => setForm({ ...form, url: e.target.value })}
                    placeholder="http://localhost:8900/mcp"
                  />
                </label>
                {classifyClientMcpEgress({ transport: form.transport, url: form.url }).kind !==
                  "local" && (
                  <div role="alert" className="banner banner--warn" style={{ fontSize: "0.78rem" }}>
                    <AlertTriangle size={14} aria-hidden="true" />{" "}
                    External egress: <code>{form.url || "external endpoint"}</code>
                  </div>
                )}
              </>
            )}

            <label>
              Environment (KEY=VALUE)
              <textarea
                rows={3}
                value={form.envText}
                onChange={(e) => setForm({ ...form, envText: e.target.value })}
                placeholder="API_KEY=..."
              />
            </label>

            <div className="modal-actions">
              <button type="button" className="btn" onClick={() => dialogRef.current?.close()}>
                Cancel
              </button>
              <button type="submit" className="btn btn--accent">
                Save MCP
              </button>
            </div>
          </form>
        )}
      </dialog>
    </div>
  );
}
