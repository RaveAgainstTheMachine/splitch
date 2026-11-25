import { useEffect, useState } from "react";
import type React from "react";
import {
  AlertTriangle,
  ArrowUp,
  BookOpen,
  FileText,
  Folder,
  Minimize2,
  Save,
  ShieldAlert,
  ShieldCheck,
  Wrench
} from "lucide-react";
import { ContextRing } from "../ContextRing";
import { classifyClientMcpEgress } from "../McpTab";
import type {
  AgentsMdPayload,
  ChatRulesConfig,
  ConversationSummary,
  McpServerConfig,
  NodeStatus,
  PromptPersona,
  ScopedExtensionsConfig,
  SkillItem,
  WorkspaceDirListing
} from "../../types";
import type { DraftChatConfig } from "./chatHelpers";

export interface ContextMemoryDialogProps {
  dialogRef: React.RefObject<HTMLDialogElement | null>;
  effectiveConfig: DraftChatConfig;
  streaming: boolean;
  updateConfig: (patch: Partial<DraftChatConfig>) => void;
  activeConv: ConversationSummary | null;
  compacting: boolean;
  messagesCount: number;
  compactNow: () => Promise<void>;
  contextStats: { used: number; max: number; estimated: boolean };
  saveContextSettings: (e: React.FormEvent) => Promise<void>;
  settingsPrompt: string;
  setSettingsPrompt: (v: string) => void;
  contextScopeTarget: "chat" | "workspace" | "global";
  selectScopeTarget: (target: "chat" | "workspace" | "global") => void;
  activeWorkspaceKey: string;
  execNode?: string;
  workspaceSubpath?: string;
  skills: SkillItem[];
  scopedConfig: ScopedExtensionsConfig;
  settingsSkills: string[];
  setSettingsSkills: (v: string[]) => void;
  mcps: McpServerConfig[];
  settingsMcps: string[];
  setSettingsMcps: (v: string[]) => void;
  toggleSelection: (list: string[], id: string, setter: (v: string[]) => void) => void;
}

export function ContextMemoryDialog({
  dialogRef,
  effectiveConfig,
  streaming,
  updateConfig,
  activeConv,
  compacting,
  messagesCount,
  compactNow,
  contextStats,
  saveContextSettings,
  settingsPrompt,
  setSettingsPrompt,
  contextScopeTarget,
  selectScopeTarget,
  activeWorkspaceKey,
  execNode = "",
  workspaceSubpath = "",
  skills,
  scopedConfig,
  settingsSkills,
  setSettingsSkills,
  mcps,
  settingsMcps,
  setSettingsMcps,
  toggleSelection
}: ContextMemoryDialogProps) {
  const [personas, setPersonas] = useState<PromptPersona[]>([]);
  const [agentsOpen, setAgentsOpen] = useState(false);
  const [agentsMd, setAgentsMd] = useState("");
  const [agentsExists, setAgentsExists] = useState(false);
  const [agentsBusy, setAgentsBusy] = useState(false);
  const [agentsNotice, setAgentsNotice] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const res = await fetch("/api/chat/rules");
        if (res.ok) {
          const data = (await res.json()) as ChatRulesConfig;
          setPersonas(data.personas ?? []);
        }
      } catch {
        // ignore
      }
    })();
  }, []);

  const loadAgentsMd = async () => {
    if (!execNode) return;
    setAgentsBusy(true);
    setAgentsNotice(null);
    try {
      const res = await fetch(
        `/api/nodes/${encodeURIComponent(execNode)}/workspace/agents-md?subpath=${encodeURIComponent(workspaceSubpath)}`
      );
      if (res.ok) {
        const data = (await res.json()) as AgentsMdPayload;
        setAgentsMd(data.content);
        setAgentsExists(data.exists);
      }
    } finally {
      setAgentsBusy(false);
    }
  };

  const saveAgentsMd = async () => {
    if (!execNode) return;
    setAgentsBusy(true);
    setAgentsNotice(null);
    try {
      const res = await fetch(`/api/nodes/${encodeURIComponent(execNode)}/workspace/agents-md`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subpath: workspaceSubpath, content: agentsMd })
      });
      if (res.ok) {
        setAgentsExists(true);
        setAgentsNotice("Saved AGENTS.md");
      }
    } finally {
      setAgentsBusy(false);
    }
  };
  return (
    <dialog
      ref={dialogRef}
      {...({ closedby: "any" } as Record<string, string>)}
      aria-label="Chat Context and System Prompt"
    >
      <div className="panel-head">
        <h2>Context & Memory</h2>
        <div className="row-actions">
          <label
            className="toolbar-switch"
            title="Automatically summarize older messages when context reaches 80%"
          >
            <span>Auto-Compact</span>
            <input
              type="checkbox"
              {...({ switch: "" } as Record<string, string>)}
              checked={effectiveConfig.auto_compact}
              disabled={streaming}
              onChange={(e) => updateConfig({ auto_compact: e.target.checked })}
            />
          </label>
          <button
            type="button"
            className="btn btn--sm"
            disabled={!activeConv || streaming || compacting || messagesCount < 4}
            onClick={() => void compactNow()}
            title="Summarize older turns now without deleting chat history"
          >
            <Minimize2 size={12} aria-hidden="true" />
            {compacting ? "Compacting…" : "Compact Now"}
          </button>
          <ContextRing
            usedTokens={contextStats.used}
            maxTokens={contextStats.max}
            estimated={contextStats.estimated}
          />
        </div>
      </div>
      <form className="modal-form" onSubmit={saveContextSettings}>
        {personas.length > 0 && (
          <label>
            Prompt Persona Preset
            <select
              value=""
              onChange={(e) => {
                const found = personas.find((p) => p.id === e.target.value);
                if (found) setSettingsPrompt(found.prompt);
              }}
            >
              <option value="">Apply saved persona…</option>
              {personas.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          System Prompt
          <textarea
            rows={3}
            value={settingsPrompt}
            onChange={(e) => setSettingsPrompt(e.target.value)}
            placeholder="Optional system prompt…"
          />
        </label>

        {execNode && (
          <div className="context-section">
            <div className="panel-head">
              <span className="muted" style={{ fontSize: "0.78rem" }}>
                <FileText size={12} aria-hidden="true" /> Project <code>AGENTS.md</code>
                {agentsExists ? "" : " (optional)"}
              </span>
              <div className="row-actions">
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => {
                    const next = !agentsOpen;
                    setAgentsOpen(next);
                    if (next) void loadAgentsMd();
                  }}
                >
                  {agentsOpen ? "Hide AGENTS.md" : "Edit AGENTS.md"}
                </button>
              </div>
            </div>
            {agentsOpen && (
              <div className="settings-stack" style={{ marginTop: "0.45rem" }}>
                <textarea
                  rows={5}
                  value={agentsMd}
                  onChange={(e) => setAgentsMd(e.target.value)}
                  placeholder="# Workspace Instructions&#10;..."
                />
                <div className="row-actions">
                  {agentsNotice && <span className="pill pill--ok">{agentsNotice}</span>}
                  <button
                    type="button"
                    className="btn btn--sm btn--accent"
                    style={{ marginInlineStart: "auto" }}
                    disabled={agentsBusy}
                    onClick={() => void saveAgentsMd()}
                  >
                    <Save size={11} aria-hidden="true" />{" "}
                    {agentsBusy ? "Saving…" : "Save AGENTS.md"}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        <div className="context-section">
          <div className="panel-head">
            <span className="muted" style={{ fontSize: "0.78rem" }}>
              Scope
            </span>
            <div className="dialog-modes" role="group" aria-label="Extension scope target">
              <button
                type="button"
                className={`btn btn--sm ${contextScopeTarget === "chat" ? "btn--accent" : ""}`}
                aria-pressed={contextScopeTarget === "chat"}
                onClick={() => selectScopeTarget("chat")}
              >
                This Chat
              </button>
              <button
                type="button"
                className={`btn btn--sm ${contextScopeTarget === "workspace" ? "btn--accent" : ""}`}
                aria-pressed={contextScopeTarget === "workspace"}
                onClick={() => selectScopeTarget("workspace")}
              >
                Project ({activeWorkspaceKey})
              </button>
              <button
                type="button"
                className={`btn btn--sm ${contextScopeTarget === "global" ? "btn--accent" : ""}`}
                aria-pressed={contextScopeTarget === "global"}
                onClick={() => selectScopeTarget("global")}
              >
                Global
              </button>
            </div>
          </div>
        </div>

        <div className="context-section">
          <div className="context-section__title">
            <BookOpen size={13} aria-hidden="true" />
            <span>Skills ({settingsSkills.length})</span>
          </div>
          {skills.length === 0 ? (
            <p className="muted">No skills.</p>
          ) : (
            <div className="context-check-grid">
              {skills.map((s) => {
                const inGlobal = (scopedConfig.global_scope.skills ?? []).includes(s.id);
                const inWs = (scopedConfig.workspaces[activeWorkspaceKey]?.skills ?? []).includes(
                  s.id
                );
                return (
                  <label key={s.id} className="choice-card">
                    <input
                      type="checkbox"
                      checked={settingsSkills.includes(s.id)}
                      onChange={() => toggleSelection(settingsSkills, s.id, setSettingsSkills)}
                    />
                    <span>
                      {s.name}{" "}
                      {contextScopeTarget !== "global" && inGlobal && (
                        <span className="pill">Global</span>
                      )}
                      {contextScopeTarget === "chat" && inWs && (
                        <span className="pill">Project</span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          )}
        </div>

        <div className="context-section">
          <div className="context-section__title">
            <Wrench size={13} aria-hidden="true" />
            <span>MCP Servers ({settingsMcps.length})</span>
          </div>
          {mcps.length === 0 ? (
            <p className="muted">No MCP servers.</p>
          ) : (
            <div className="context-check-grid">
              {[...mcps]
                .sort((a, b) => (a.transport === "stdio" ? 0 : 1) - (b.transport === "stdio" ? 0 : 1))
                .map((m) => {
                  const inGlobal = (scopedConfig.global_scope.mcps ?? []).includes(m.id);
                  const inWs = (scopedConfig.workspaces[activeWorkspaceKey]?.mcps ?? []).includes(
                    m.id
                  );
                  const eg =
                    m.egress ??
                    classifyClientMcpEgress({
                      transport: m.transport,
                      command: m.command,
                      args: m.args,
                      url: m.url,
                      envKeys: Object.keys(m.env)
                    }).kind;
                  return (
                    <label key={m.id} className="choice-card">
                      <input
                        type="checkbox"
                        checked={settingsMcps.includes(m.id)}
                        onChange={() => toggleSelection(settingsMcps, m.id, setSettingsMcps)}
                      />
                      <span>
                        {m.name}{" "}
                        {eg === "local" ? (
                          <span className="pill pill--ok" title="Runs locally via stdio/LAN">
                            <ShieldCheck size={10} aria-hidden="true" /> local
                          </span>
                        ) : (
                          <span
                            className="pill pill--warn"
                            title="Advised: sends tool context to an external endpoint"
                          >
                            <ShieldAlert size={10} aria-hidden="true" /> ext
                          </span>
                        )}
                        {contextScopeTarget !== "global" && inGlobal && (
                          <span className="pill">Global</span>
                        )}
                        {contextScopeTarget === "chat" && inWs && (
                          <span className="pill">Project</span>
                        )}
                      </span>
                    </label>
                  );
                })}
            </div>
          )}
          {mcps.some((m) => {
            if (!settingsMcps.includes(m.id)) return false;
            const eg =
              m.egress ??
              classifyClientMcpEgress({
                transport: m.transport,
                command: m.command,
                args: m.args,
                url: m.url,
                envKeys: Object.keys(m.env)
              }).kind;
            return eg !== "local";
          }) && (
            <div role="alert" className="banner banner--warn" style={{ fontSize: "0.76rem" }}>
              <AlertTriangle size={13} aria-hidden="true" /> External MCP active — tool calls
              require approval.
            </div>
          )}
        </div>

        <div className="modal-actions">
          <button type="button" className="btn" onClick={() => dialogRef.current?.close()}>
            Cancel
          </button>
          <button type="submit" className="btn btn--accent">
            Save{" "}
            {contextScopeTarget === "global"
              ? "Global Defaults"
              : contextScopeTarget === "workspace"
                ? `Project (${activeWorkspaceKey})`
                : "Chat Context"}
          </button>
        </div>
      </form>
    </dialog>
  );
}

export interface WorkspaceBrowserDialogProps {
  dialogRef: React.RefObject<HTMLDialogElement | null>;
  wsTitleId: string;
  workspaceId: string;
  nodes: NodeStatus[];
  wsBrowseNode: string;
  setWsBrowseNode: (nid: string) => void;
  fetchWorkspaceDirs: (nodeId: string, subpath: string) => Promise<void>;
  workspaceSubpath: string;
  onWorkspaceSubpathChange: (val: string) => void;
  onWorkspaceSubpathBlur: (val: string) => void;
  wsListing: WorkspaceDirListing | null;
  wsError: string | null;
  wsLoading: boolean;
  onApplyWorkspace: (nodeId: string, subpath: string) => void;
}

export function WorkspaceBrowserDialog({
  dialogRef,
  wsTitleId,
  workspaceId,
  nodes,
  wsBrowseNode,
  setWsBrowseNode,
  fetchWorkspaceDirs,
  workspaceSubpath,
  onWorkspaceSubpathChange,
  onWorkspaceSubpathBlur,
  wsListing,
  wsError,
  wsLoading,
  onApplyWorkspace
}: WorkspaceBrowserDialogProps) {
  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={wsTitleId}
      {...({ closedby: "any" } as Record<string, string>)}
    >
      <div className="panel-head">
        <h2 id={wsTitleId}>Select Workspace Folder</h2>
        <select
          aria-label="Node to browse"
          value={wsBrowseNode}
          onChange={(e) => {
            const nid = e.target.value;
            setWsBrowseNode(nid);
            if (nid) void fetchWorkspaceDirs(nid, "");
          }}
        >
          {nodes.map((n) => (
            <option key={n.node_id} value={n.node_id}>
              {n.display_name} ({n.node_id})
            </option>
          ))}
        </select>
      </div>

      <div className="workspace-browser">
        <label className="modal-subpath-field">
          <span className="muted">Workspace Subpath</span>
          <input
            id={workspaceId}
            type="text"
            value={workspaceSubpath}
            placeholder="e.g. my-project (or leave empty for root)"
            onChange={(e) => onWorkspaceSubpathChange(e.target.value)}
            onBlur={(e) => onWorkspaceSubpathBlur(e.target.value)}
          />
        </label>
        {wsListing && (
          <div className="workspace-browser__path">
            <span className="muted">Sandbox Root:</span> <code>{wsListing.root}</code>
            <div>
              <span className="muted">Selected:</span> <code>{wsListing.subpath || "."}</code>
            </div>
          </div>
        )}

        {wsError && <p className="banner--error">{wsError}</p>}

        {wsLoading ? (
          <p className="muted">Loading folders…</p>
        ) : wsListing ? (
          <ul className="workspace-dir-list">
            {wsListing.parent !== null && (
              <li>
                <button
                  type="button"
                  className="btn btn--sm workspace-dir-btn"
                  onClick={() => void fetchWorkspaceDirs(wsBrowseNode, wsListing.parent ?? "")}
                >
                  <ArrowUp size={13} aria-hidden="true" /> ..
                </button>
              </li>
            )}
            {wsListing.dirs.length === 0 ? (
              <li className="muted" style={{ padding: "0.5rem" }}>
                No subfolders here.
              </li>
            ) : (
              wsListing.dirs.map((d) => {
                const nextSub = wsListing.subpath ? `${wsListing.subpath}/${d}` : d;
                return (
                  <li key={d}>
                    <button
                      type="button"
                      className="btn btn--sm workspace-dir-btn"
                      onClick={() => void fetchWorkspaceDirs(wsBrowseNode, nextSub)}
                    >
                      <Folder size={13} aria-hidden="true" /> {d}/
                    </button>
                  </li>
                );
              })
            )}
          </ul>
        ) : null}

        <div className="modal-actions">
          <button type="button" className="btn" onClick={() => dialogRef.current?.close()}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--accent"
            disabled={!wsListing}
            onClick={() => {
              if (!wsListing) return;
              onApplyWorkspace(wsBrowseNode, wsListing.subpath);
              dialogRef.current?.close();
            }}
          >
            Use This Workspace
          </button>
        </div>
      </div>
    </dialog>
  );
}
