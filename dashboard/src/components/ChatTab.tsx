import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Components } from "react-markdown";
import {
  Cpu,
  Download,
  Folder,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightOpen,
  Zap
} from "lucide-react";
import { ArtifactSplitPane, type InspectedArtifact } from "./ArtifactPreview";
import type { ConfirmState } from "./ConfirmDialog";
import { classifyClientMcpEgress } from "./McpTab";
import { useDialogLightDismiss } from "./ModelsTab";
import { CodeBlock } from "./chat/ChatCodeBlocks";
import { ChatComposer, DEFAULT_SAMPLING, type SamplingConfig } from "./chat/ChatComposer";
import { ContextMemoryDialog, WorkspaceBrowserDialog } from "./chat/ChatDialogs";
import { ChatMessageList } from "./chat/ChatMessageList";
import { ChatSidebar, type SidebarMode } from "./chat/ChatSidebar";
import { SwarmInspectorPane } from "./chat/SwarmInspectorPane";
import { WorkspaceViewerPane, type WorkspaceInspectTarget } from "./chat/WorkspaceViewerPane";
import {
  DEFAULT_DRAFT,
  type DraftChatConfig,
  parseSseChunk,
  readFileAsDataUrl
} from "./chat/chatHelpers";
import type {
  AgentStatusState,
  ChatMessageRecord,
  ConversationSummary,
  DashboardSnapshot,
  GitDiffResponse,
  GitStatusResponse,
  McpServerConfig,
  PendingApproval,
  ScopedExtensionsConfig,
  SkillItem,
  SwarmAgentRecord,
  SwarmRunRecord,
  SwarmWorkerMessage,
  ToolCallRecord,
  WorkspaceDirListing,
  WorkspaceFileContent
} from "../types";

interface Props {
  snapshot: DashboardSnapshot | null;
  onError: (msg: string | null) => void;
  onConfirm: (state: ConfirmState) => void;
}

export function ChatTab({ snapshot, onError, onConfirm }: Props) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [trash, setTrash] = useState<ConversationSummary[]>([]);
  const [sidebarView, setSidebarView] = useState<"chats" | "trash">("chats");
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessageRecord[]>([]);
  const [sidebarQuery, setSidebarQuery] = useState("");
  const [draftConfig, setDraftConfig] = useState<DraftChatConfig>(DEFAULT_DRAFT);

  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [mcps, setMcps] = useState<McpServerConfig[]>([]);
  const [scopedConfig, setScopedConfig] = useState<ScopedExtensionsConfig>({
    global_scope: { skills: [], mcps: [] },
    workspaces: {}
  });
  const [contextScopeTarget, setContextScopeTarget] = useState<"chat" | "workspace" | "global">(
    "chat"
  );

  const [composerText, setComposerText] = useState("");
  const [attachedImages, setAttachedImages] = useState<string[]>([]);
  const [sampling, setSampling] = useState<SamplingConfig>(DEFAULT_SAMPLING);
  const [dragActive, setDragActive] = useState(false);

  const [streaming, setStreaming] = useState(false);
  const [agentStatus, setAgentStatus] = useState<AgentStatusState | null>(null);
  const [streamingDraft, setStreamingDraft] = useState<ChatMessageRecord | null>(null);
  const [streamingTools, setStreamingTools] = useState<ChatMessageRecord[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState<Record<string, PendingApproval>>({});
  const [copiedMsgId, setCopiedMsgId] = useState<string | null>(null);
  const [compacting, setCompacting] = useState(false);
  const [compactNotice, setCompactNotice] = useState<string | null>(null);
  const [thinkingDisplay, setThinkingDisplay] = useState<"collapsed" | "expanded" | "hidden">(
    "collapsed"
  );
  const [completionNotifications, setCompletionNotifications] = useState(false);
  const [swarmRuns, setSwarmRuns] = useState<SwarmRunRecord[]>([]);
  const [inspectedSwarm, setInspectedSwarm] = useState<{ runId: string; agentId: string } | null>(
    null
  );
  const [swarmWorkerMsgs, setSwarmWorkerMsgs] = useState<SwarmWorkerMessage[]>([]);
  const [swarmLiveText, setSwarmLiveText] = useState<Record<string, string>>({});
  const [swarmLiveReasoning, setSwarmLiveReasoning] = useState<Record<string, string>>({});
  const swarmDefaultAppliedRef = useRef(false);

  useEffect(() => {
    void (async () => {
      try {
        const res = await fetch("/api/settings");
        if (res.ok) {
          const s = (await res.json()) as {
            thinking_display?: "collapsed" | "expanded" | "hidden";
            completion_notifications?: boolean;
          };
          if (s.thinking_display) setThinkingDisplay(s.thinking_display);
          if (typeof s.completion_notifications === "boolean") {
            setCompletionNotifications(s.completion_notifications);
          }
        }
      } catch {
        // ignore
      }
    })();
  }, []);

  // Context & Skills/MCP settings modal state
  const [settingsPrompt, setSettingsPrompt] = useState("");
  const [settingsSkills, setSettingsSkills] = useState<string[]>([]);
  const [settingsMcps, setSettingsMcps] = useState<string[]>([]);

  // Workspace folder browser modal state
  const [wsBrowseNode, setWsBrowseNode] = useState<string>("");
  const [wsListing, setWsListing] = useState<WorkspaceDirListing | null>(null);
  const [wsLoading, setWsLoading] = useState(false);
  const [wsError, setWsError] = useState<string | null>(null);

  const [messagesLoadedTick, setMessagesLoadedTick] = useState(0);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sidebarMode, setSidebarMode] = useState<SidebarMode>("chats");
  const [searchOpen, setSearchOpen] = useState(false);
  const [inspectedArtifact, setInspectedArtifact] = useState<InspectedArtifact | null>(null);
  const [lastArtifact, setLastArtifact] = useState<InspectedArtifact | null>(null);
  const [workspaceInspect, setWorkspaceInspect] = useState<WorkspaceInspectTarget | null>(null);
  const [gitStatus, setGitStatus] = useState<GitStatusResponse | null>(null);
  const [gitLoading, setGitLoading] = useState(false);
  const [paneWidth, setPaneWidth] = useState<number>(470);
  const [isResizingPane, setIsResizingPane] = useState(false);

  const layoutRef = useRef<HTMLDivElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  const openArtifactInPane = (art: InspectedArtifact) => {
    setWorkspaceInspect(null);
    setInspectedArtifact(art);
    setLastArtifact(art);
    if (typeof window !== "undefined" && window.innerWidth < 1380) {
      setSidebarCollapsed(true);
    }
  };

  const clampPaneWidth = (rawW: number) => {
    const rect = layoutRef.current?.getBoundingClientRect();
    const totalWidth = rect ? rect.width : window.innerWidth;
    const sidebarW = sidebarCollapsed ? 0 : 236;
    const minChatW = 340;
    const maxW = Math.max(
      300,
      Math.min(Math.floor(totalWidth * 0.68), totalWidth - sidebarW - minChatW)
    );
    return Math.max(280, Math.min(maxW, Math.round(rawW)));
  };

  useEffect(() => {
    if (!swarmDefaultAppliedRef.current && snapshot?.nodes) {
      swarmDefaultAppliedRef.current = true;
      if (snapshot.nodes.length > 1) {
        setDraftConfig((prev) => ({ ...prev, swarm_enabled: true }));
      }
    }
  }, [snapshot?.nodes]);

  const loadSwarmRuns = async (convId: string) => {
    try {
      const res = await fetch(`/api/chat/conversations/${encodeURIComponent(convId)}/swarms`);
      if (!res.ok) return;
      setSwarmRuns((await res.json()) as SwarmRunRecord[]);
    } catch {
      // non-fatal
    }
  };

  const loadSwarmWorkerMessages = async (agentId: string) => {
    try {
      const res = await fetch(`/api/chat/swarms/agents/${encodeURIComponent(agentId)}/messages`);
      if (!res.ok) return;
      setSwarmWorkerMsgs((await res.json()) as SwarmWorkerMessage[]);
    } catch {
      // non-fatal
    }
  };

  useEffect(() => {
    if (!activeId) {
      setSwarmRuns([]);
      setInspectedSwarm(null);
      setSwarmWorkerMsgs([]);
      return;
    }
    void loadSwarmRuns(activeId);
  }, [activeId]);

  useEffect(() => {
    if (!inspectedSwarm?.agentId) {
      setSwarmWorkerMsgs([]);
      return;
    }
    void loadSwarmWorkerMessages(inspectedSwarm.agentId);
  }, [inspectedSwarm?.agentId]);

  const handleInspectSwarmAgent = (runId: string, agentId: string) => {
    setWorkspaceInspect(null);
    setInspectedArtifact(null);
    setInspectedSwarm({ runId, agentId });
  };

  const handleCancelSwarmAgent = async (agentId: string) => {
    try {
      await fetch(`/api/chat/swarms/agents/${encodeURIComponent(agentId)}/cancel`, {
        method: "POST"
      });
      if (activeId) void loadSwarmRuns(activeId);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to cancel swarm worker");
    }
  };

  const startPaneResize = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const splitter = e.currentTarget;
    const pointerId = e.pointerId;
    try {
      splitter.setPointerCapture(pointerId);
    } catch {
      // Synthetic pointer events in tests may not have an active pointerId
    }
    setIsResizingPane(true);

    const onMove = (ev: PointerEvent) => {
      const rect = layoutRef.current?.getBoundingClientRect();
      const rightEdge = rect ? rect.right : window.innerWidth;
      setPaneWidth(clampPaneWidth(rightEdge - ev.clientX));
    };
    const onEnd = () => {
      setIsResizingPane(false);
      try {
        if (splitter.hasPointerCapture(pointerId)) {
          splitter.releasePointerCapture(pointerId);
        }
      } catch {
        // ignore
      }
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
  };

  const markdownComponents = useMemo<Components>(
    () => ({
      a({ href, children, ...props }) {
        return (
          <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
            {children}
          </a>
        );
      },
      code({ className, children }) {
        return (
          <CodeBlock
            className={className}
            onInspect={openArtifactInPane}
            activeArtifactCode={inspectedArtifact?.code ?? null}
          >
            {children}
          </CodeBlock>
        );
      }
    }),
    [inspectedArtifact?.code]
  );

  const abortRef = useRef<AbortController | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const threadRef = useRef<HTMLDivElement | null>(null);
  const latestMsgRef = useRef<HTMLElement | null>(null);
  const bottomSentinelRef = useRef<HTMLDivElement | null>(null);
  const userScrolledUpRef = useRef(false);
  const settingsDialogRef = useRef<HTMLDialogElement | null>(null);
  const wsDialogRef = useRef<HTMLDialogElement | null>(null);
  const selectAllRef = useRef<HTMLInputElement | null>(null);
  const lastClickedIdxRef = useRef<number | null>(null);
  const shiftHeldRef = useRef(false);
  useDialogLightDismiss(settingsDialogRef);
  useDialogLightDismiss(wsDialogRef);

  const composerId = useId();
  const modelSelectId = useId();
  const execNodeId = useId();
  const workspaceId = useId();
  const wsTitleId = useId();
  const selectAllId = useId();
  const selectRowPrefix = useId();
  const supportsHasSelector = useMemo(
    () => typeof CSS !== "undefined" && typeof CSS.supports === "function" && CSS.supports("selector(:has(*))"),
    []
  );

  const activeConv = useMemo(
    () => conversations.find((c) => c.id === activeId) ?? null,
    [conversations, activeId]
  );

  // Always-interactive effective settings (backed by active conversation OR pre-prompt draft)
  const effectiveConfig: DraftChatConfig = activeConv ?? draftConfig;
  const activeWorkspaceKey = useMemo(
    () => (effectiveConfig.workspace_subpath || "").trim().replace(/^\/+|\/+$/g, "") || ".",
    [effectiveConfig.workspace_subpath]
  );

  const effectiveSkillIds = useMemo(() => {
    const wsRule = scopedConfig.workspaces[activeWorkspaceKey];
    return Array.from(
      new Set([
        ...(scopedConfig.global_scope.skills ?? []),
        ...(wsRule?.skills ?? []),
        ...(effectiveConfig.enabled_skills ?? [])
      ])
    );
  }, [scopedConfig, activeWorkspaceKey, effectiveConfig.enabled_skills]);

  const effectiveMcpIds = useMemo(() => {
    const wsRule = scopedConfig.workspaces[activeWorkspaceKey];
    return Array.from(
      new Set([
        ...(scopedConfig.global_scope.mcps ?? []),
        ...(wsRule?.mcps ?? []),
        ...(effectiveConfig.enabled_mcps ?? [])
      ])
    );
  }, [scopedConfig, activeWorkspaceKey, effectiveConfig.enabled_mcps]);

  const activeExternalMcps = useMemo(() => {
    return mcps.filter((m) => {
      if (!effectiveMcpIds.includes(m.id)) return false;
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
    });
  }, [mcps, effectiveMcpIds]);

  const modelOptions = useMemo(() => {
    const opts: Array<{ value: string; label: string; group: string }> = [
      { value: "auto", label: "auto", group: "Routing" }
    ];
    for (const a of snapshot?.aliases ?? []) {
      opts.push({
        value: a.alias,
        label: `${a.alias} → ${a.primary_node}/${a.primary_model}`,
        group: "Aliases"
      });
    }
    for (const n of snapshot?.nodes ?? []) {
      for (const m of n.telemetry?.models ?? []) {
        opts.push({
          value: `${n.node_id}/${m.id}`,
          label: `${n.node_id}/${m.id} (${m.status})`,
          group: n.display_name
        });
      }
    }
    return opts;
  }, [snapshot]);

  const filteredConversations = useMemo(() => {
    const src = sidebarView === "chats" ? conversations : trash;
    const q = sidebarQuery.trim().toLowerCase();
    if (!q) return src;
    return src.filter(
      (c) => c.title.toLowerCase().includes(q) || c.model.toLowerCase().includes(q)
    );
  }, [conversations, trash, sidebarView, sidebarQuery]);

  const visibleSelectedCount = useMemo(
    () => filteredConversations.reduce((acc, c) => acc + (selectedIds.has(c.id) ? 1 : 0), 0),
    [filteredConversations, selectedIds]
  );
  const allVisibleSelected =
    filteredConversations.length > 0 && visibleSelectedCount === filteredConversations.length;
  const someVisibleSelected = visibleSelectedCount > 0 && !allVisibleSelected;

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someVisibleSelected;
    }
  }, [someVisibleSelected, selectMode]);

  useEffect(() => {
    if (!selectMode) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !document.querySelector("dialog[open]")) {
        setSelectMode(false);
        setSelectedIds(new Set());
        lastClickedIdxRef.current = null;
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selectMode]);

  // Live context window progress metrics (accounts for non-destructive compaction watermark)
  const contextStats = useMemo(() => {
    const watermark = activeConv?.compacted_through_ts ?? 0;
    const summaryText = activeConv?.compacted_summary ?? "";
    const allMsgs = streamingDraft ? [...messages, streamingDraft, ...streamingTools] : messages;
    const activeMsgs =
      watermark > 0 ? allMsgs.filter((m) => m.created_at > watermark) : allMsgs;

    let lastRealIdx = -1;
    let lastRealTotal = 0;
    let maxTokens = 0;

    for (let i = activeMsgs.length - 1; i >= 0; i -= 1) {
      const meta = activeMsgs[i].meta;
      if (meta?.context_window && !maxTokens) {
        maxTokens = meta.context_window;
      }
      if (meta?.total_tokens && lastRealIdx === -1) {
        lastRealIdx = i;
        lastRealTotal = meta.total_tokens;
      }
    }

    if (!maxTokens) {
      const nodes = snapshot?.nodes ?? [];
      const targetNode =
        nodes.find((n) => n.node_id === effectiveConfig.exec_node && n.telemetry?.context_length) ??
        nodes.find((n) => n.online && n.telemetry?.context_length);
      maxTokens = targetNode?.telemetry?.context_length ?? 32768;
    }

    let deltaChars = composerText.length;
    if (lastRealIdx >= 0 && watermark === 0) {
      for (let i = lastRealIdx + 1; i < activeMsgs.length; i += 1) {
        deltaChars += (activeMsgs[i].content?.length ?? 0) + (activeMsgs[i].reasoning?.length ?? 0);
      }
      return {
        used: lastRealTotal + Math.ceil(deltaChars / 4),
        max: maxTokens,
        estimated: deltaChars > 0
      };
    }

    let baseChars = 180 + (effectiveConfig.system_prompt?.length ?? 0) + summaryText.length;
    for (const sid of effectiveSkillIds) {
      const sk = skills.find((s) => s.id === sid);
      if (sk) baseChars += Math.min(sk.content.length, 8000);
    }
    for (const m of activeMsgs) {
      baseChars += (m.content?.length ?? 0) + (m.reasoning?.length ?? 0);
    }
    return {
      used: Math.ceil((baseChars + deltaChars) / 4),
      max: maxTokens,
      estimated: true
    };
  }, [activeConv, messages, streamingDraft, streamingTools, composerText, effectiveConfig, effectiveSkillIds, skills, snapshot]);

  const loadConversations = async (selectFirst = false) => {
    try {
      const [cRes, tRes] = await Promise.all([
        fetch("/api/chat/conversations"),
        fetch("/api/chat/trash")
      ]);
      if (!cRes.ok) throw new Error(`HTTP ${cRes.status}`);
      const list = (await cRes.json()) as ConversationSummary[];
      setConversations(list);
      if (tRes.ok) {
        setTrash((await tRes.json()) as ConversationSummary[]);
      }
      if (selectFirst && list.length > 0) {
        setActiveId((prev) => prev ?? list[0].id);
      }
      return list;
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to load conversations");
      return [];
    }
  };

  const loadMessages = async (convId: string) => {
    try {
      const res = await fetch(`/api/chat/conversations/${encodeURIComponent(convId)}/messages`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const list = (await res.json()) as ChatMessageRecord[];
      setMessages(list);
      setMessagesLoadedTick((t) => t + 1);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to load messages");
    }
  };

  const loadContextCatalog = async () => {
    try {
      const [sRes, mRes, scRes] = await Promise.all([
        fetch("/api/skills"),
        fetch("/api/mcp"),
        fetch("/api/chat/scopes")
      ]);
      if (sRes.ok) setSkills((await sRes.json()) as SkillItem[]);
      if (mRes.ok) setMcps((await mRes.json()) as McpServerConfig[]);
      if (scRes.ok) setScopedConfig((await scRes.json()) as ScopedExtensionsConfig);
    } catch {
      // non-fatal
    }
  };

  useEffect(() => {
    void loadConversations(true);
    void loadContextCatalog();
  }, []);

  useEffect(() => {
    userScrolledUpRef.current = false;
    if (!activeId) {
      setMessages([]);
      return;
    }
    void loadMessages(activeId);
  }, [activeId]);

  // Open chats scrolled to the bottom by default unless user has manually scrolled up
  useLayoutEffect(() => {
    const el = threadRef.current;
    if (!el || userScrolledUpRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [activeId, messagesLoadedTick, messages.length]);

  // Keep viewport pinned to bottom during streaming unless user scrolled up
  useLayoutEffect(() => {
    const el = threadRef.current;
    if (!el || !streaming || userScrolledUpRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [streaming, streamingDraft?.content, streamingDraft?.reasoning, streamingTools.length]);

  // IntersectionObserver fallback for floating Jump-to-Bottom when scroll-state queries unsupported
  useEffect(() => {
    if (CSS.supports("container-type", "scroll-state")) return;
    const scroller = threadRef.current;
    const sentinel = bottomSentinelRef.current;
    if (!scroller || !sentinel) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          scroller.classList.toggle("is-scrolled-up", !entry.isIntersecting);
        }
      },
      { root: scroller, threshold: 0.1 }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [activeId, messages.length]);

  const handleThreadScroll = () => {
    const el = threadRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    userScrolledUpRef.current = distFromBottom > 64;
  };

  const jumpToBottom = () => {
    userScrolledUpRef.current = false;
    const el = threadRef.current;
    if (!el) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollTo({ top: el.scrollHeight, behavior: reduced ? "instant" : "smooth" });
  };

  const createConversation = async (): Promise<ConversationSummary | null> => {
    try {
      const res = await fetch("/api/chat/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: "New Chat",
          model: effectiveConfig.model,
          exec_node: effectiveConfig.exec_node,
          workspace_subpath: effectiveConfig.workspace_subpath,
          tools_enabled: effectiveConfig.tools_enabled,
          web_search_enabled: effectiveConfig.web_search_enabled,
          auto_approve: effectiveConfig.auto_approve,
          auto_compact: effectiveConfig.auto_compact,
          enabled_skills: effectiveConfig.enabled_skills,
          enabled_mcps: effectiveConfig.enabled_mcps,
          system_prompt: effectiveConfig.system_prompt
        })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const created = (await res.json()) as ConversationSummary;
      setConversations((prev) =>
        [created, ...prev].sort(
          (a, b) => Number(b.pinned) - Number(a.pinned) || b.updated_at - a.updated_at
        )
      );
      setSidebarView("chats");
      setActiveId(created.id);
      setMessages([]);
      setStreamingDraft(null);
      setStreamingTools([]);
      return created;
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to create conversation");
      return null;
    }
  };

  const patchConversation = async (
    convId: string,
    patch: Partial<
      Pick<
        ConversationSummary,
        | "title"
        | "model"
        | "exec_node"
        | "workspace_subpath"
        | "tools_enabled"
        | "web_search_enabled"
        | "auto_approve"
        | "auto_compact"
        | "pinned"
        | "enabled_skills"
        | "enabled_mcps"
        | "system_prompt"
      >
    >
  ) => {
    try {
      const res = await fetch(`/api/chat/conversations/${encodeURIComponent(convId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch)
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated = (await res.json()) as ConversationSummary;
      setConversations((prev) =>
        prev
          .map((c) => (c.id === updated.id ? updated : c))
          .sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.updated_at - a.updated_at)
      );
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to update conversation");
    }
  };

  const updateConfig = (patch: Partial<DraftChatConfig>) => {
    if (activeConv) {
      void patchConversation(activeConv.id, patch);
    } else {
      setDraftConfig((prev) => ({ ...prev, ...patch }));
    }
  };

  const requestTrashConversation = (conv: ConversationSummary) => {
    onConfirm({
      title: `Move "${conv.title}" to Trash?`,
      message: "You can recover this chat anytime from the Trash bin, or delete it permanently there.",
      confirmLabel: "Move to Trash",
      danger: true,
      onConfirm: async () => {
        try {
          const res = await fetch(`/api/chat/conversations/${encodeURIComponent(conv.id)}`, {
            method: "DELETE"
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          if (activeId === conv.id) {
            setActiveId(null);
            setMessages([]);
          }
          await loadConversations(true);
        } catch (err) {
          onError(err instanceof Error ? err.message : "Failed to move conversation to trash");
        }
      }
    });
  };

  const restoreConversation = async (conv: ConversationSummary) => {
    try {
      const res = await fetch(`/api/chat/conversations/${encodeURIComponent(conv.id)}/restore`, {
        method: "POST"
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadConversations(false);
      setSidebarView("chats");
      setActiveId(conv.id);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to restore conversation");
    }
  };

  const requestPermanentDelete = (conv: ConversationSummary) => {
    onConfirm({
      title: `Permanently delete "${conv.title}"?`,
      message: "This chat and all its messages will be erased permanently and cannot be recovered.",
      confirmLabel: "Delete Forever",
      danger: true,
      onConfirm: async () => {
        try {
          const res = await fetch(
            `/api/chat/conversations/${encodeURIComponent(conv.id)}/permanent`,
            { method: "DELETE" }
          );
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          await loadConversations(false);
        } catch (err) {
          onError(err instanceof Error ? err.message : "Permanent delete failed");
        }
      }
    });
  };

  const requestEmptyTrash = () => {
    if (trash.length === 0) return;
    onConfirm({
      title: `Empty Trash (${trash.length} ${trash.length === 1 ? "chat" : "chats"})?`,
      message: "All conversations in Trash will be permanently erased and cannot be recovered.",
      confirmLabel: "Empty Trash",
      danger: true,
      onConfirm: async () => {
        try {
          const res = await fetch("/api/chat/trash", { method: "DELETE" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          setSelectedIds(new Set());
          setSelectMode(false);
          await loadConversations(false);
        } catch (err) {
          onError(err instanceof Error ? err.message : "Empty trash failed");
        }
      }
    });
  };

  const exitSelectMode = () => {
    setSelectMode(false);
    setSelectedIds(new Set());
    lastClickedIdxRef.current = null;
  };

  const switchSidebarView = (view: "chats" | "trash") => {
    if (view !== sidebarView) {
      exitSelectMode();
      setSidebarView(view);
    }
  };

  const handleSelectChange = (idx: number, convId: string, checked: boolean) => {
    const useRange = shiftHeldRef.current && lastClickedIdxRef.current !== null;
    shiftHeldRef.current = false;
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (useRange && lastClickedIdxRef.current !== null) {
        const start = Math.min(lastClickedIdxRef.current, idx);
        const end = Math.max(lastClickedIdxRef.current, idx);
        for (let i = start; i <= end; i += 1) {
          const item = filteredConversations[i];
          if (!item) continue;
          if (checked) next.add(item.id);
          else next.delete(item.id);
        }
      } else if (checked) {
        next.add(convId);
      } else {
        next.delete(convId);
      }
      return next;
    });
    lastClickedIdxRef.current = idx;
  };

  const toggleSelectAllVisible = () => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) {
        for (const c of filteredConversations) next.delete(c.id);
      } else {
        for (const c of filteredConversations) next.add(c.id);
      }
      return next;
    });
  };

  const requestBulkTrash = () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    const noun = ids.length === 1 ? "chat" : "chats";
    onConfirm({
      title: `Move ${ids.length} ${noun} to Trash?`,
      message: "You can recover them anytime from the Trash bin, or delete them permanently there.",
      confirmLabel: `Move ${ids.length} to Trash`,
      danger: true,
      onConfirm: async () => {
        try {
          const res = await fetch("/api/chat/conversations/bulk-delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids })
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          if (activeId && selectedIds.has(activeId)) {
            setActiveId(null);
            setMessages([]);
          }
          exitSelectMode();
          await loadConversations(true);
        } catch (err) {
          onError(err instanceof Error ? err.message : "Bulk delete failed");
        }
      }
    });
  };

  const bulkRestoreSelected = async () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    try {
      const res = await fetch("/api/chat/conversations/bulk-restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      exitSelectMode();
      await loadConversations(false);
      setSidebarView("chats");
    } catch (err) {
      onError(err instanceof Error ? err.message : "Bulk restore failed");
    }
  };

  const requestBulkPermanentDelete = () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    const noun = ids.length === 1 ? "chat" : "chats";
    onConfirm({
      title: `Permanently delete ${ids.length} ${noun}?`,
      message: "These chats and all their messages will be erased permanently and cannot be recovered.",
      confirmLabel: `Delete ${ids.length} Forever`,
      danger: true,
      onConfirm: async () => {
        try {
          const res = await fetch("/api/chat/conversations/bulk-permanent-delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids })
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          exitSelectMode();
          await loadConversations(false);
        } catch (err) {
          onError(err instanceof Error ? err.message : "Bulk permanent delete failed");
        }
      }
    });
  };

  const compactNow = async () => {
    if (!activeConv || streaming || compacting) return;
    setCompacting(true);
    setCompactNotice(null);
    onError(null);
    try {
      const res = await fetch(
        `/api/chat/conversations/${encodeURIComponent(activeConv.id)}/compact`,
        { method: "POST" }
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      if (data.compacted) {
        setCompactNotice(
          `Compacted ${data.compacted_messages} messages (~${data.estimated_tokens_before} → ~${data.estimated_tokens_after} tok)`
        );
        await loadConversations(false);
      } else {
        setCompactNotice(data.detail || "Nothing older to compact yet.");
      }
      window.setTimeout(() => setCompactNotice(null), 5000);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Compaction failed");
    } finally {
      setCompacting(false);
    }
  };

  const clearCompaction = async () => {
    if (!activeConv || streaming) return;
    try {
      const res = await fetch(
        `/api/chat/conversations/${encodeURIComponent(activeConv.id)}/compact`,
        { method: "DELETE" }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadConversations(false);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to clear compaction");
    }
  };

  const selectScopeTarget = (target: "chat" | "workspace" | "global", sc = scopedConfig) => {
    setContextScopeTarget(target);
    if (target === "global") {
      setSettingsSkills(sc.global_scope.skills ?? []);
      setSettingsMcps(sc.global_scope.mcps ?? []);
    } else if (target === "workspace") {
      const rule = sc.workspaces[activeWorkspaceKey];
      setSettingsSkills(rule?.skills ?? []);
      setSettingsMcps(rule?.mcps ?? []);
    } else {
      setSettingsSkills(effectiveConfig.enabled_skills || []);
      setSettingsMcps(effectiveConfig.enabled_mcps || []);
    }
  };

  const openContextDialog = () => {
    void loadContextCatalog();
    setSettingsPrompt(effectiveConfig.system_prompt || "");
    selectScopeTarget("chat");
    settingsDialogRef.current?.showModal();
  };

  const saveContextSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      if (contextScopeTarget === "global") {
        const res = await fetch("/api/chat/scopes/global", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skills: settingsSkills, mcps: settingsMcps })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        setScopedConfig((await res.json()) as ScopedExtensionsConfig);
        if (settingsPrompt !== (effectiveConfig.system_prompt || "")) {
          updateConfig({ system_prompt: settingsPrompt });
        }
      } else if (contextScopeTarget === "workspace") {
        const res = await fetch("/api/chat/scopes/workspace", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            workspace: activeWorkspaceKey,
            skills: settingsSkills,
            mcps: settingsMcps
          })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        setScopedConfig((await res.json()) as ScopedExtensionsConfig);
        if (settingsPrompt !== (effectiveConfig.system_prompt || "")) {
          updateConfig({ system_prompt: settingsPrompt });
        }
      } else {
        updateConfig({
          system_prompt: settingsPrompt,
          enabled_skills: settingsSkills,
          enabled_mcps: settingsMcps
        });
      }
      settingsDialogRef.current?.close();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to save scoped context settings");
    }
  };

  const fetchWorkspaceDirs = async (nodeId: string, subpath: string) => {
    setWsLoading(true);
    setWsError(null);
    try {
      const res = await fetch(
        `/api/nodes/${encodeURIComponent(nodeId)}/dirs?subpath=${encodeURIComponent(subpath)}`
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setWsListing((await res.json()) as WorkspaceDirListing);
    } catch (err) {
      setWsListing(null);
      setWsError(err instanceof Error ? err.message : "Failed to browse workspace");
    } finally {
      setWsLoading(false);
    }
  };

  const openWorkspaceBrowser = () => {
    const nodes = snapshot?.nodes ?? [];
    const target =
      effectiveConfig.exec_node ||
      nodes.find((n) => n.online && n.telemetry?.tools_enabled)?.node_id ||
      nodes.find((n) => n.online)?.node_id ||
      "";
    setWsBrowseNode(target);
    wsDialogRef.current?.showModal();
    if (target) {
      void fetchWorkspaceDirs(target, effectiveConfig.workspace_subpath || "");
    } else {
      setWsListing(null);
      setWsError("No online node available to browse.");
    }
  };

  const handleFiles = async (files: FileList | File[]) => {
    const imgs = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (imgs.length === 0) return;
    try {
      const urls = await Promise.all(imgs.slice(0, 4).map(readFileAsDataUrl));
      setAttachedImages((prev) => [...prev, ...urls].slice(0, 6));
    } catch (err) {
      onError(err instanceof Error ? err.message : "Image read failed");
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = Array.from(e.clipboardData.items);
    const imgFiles = items
      .filter((it) => it.type.startsWith("image/"))
      .map((it) => it.getAsFile())
      .filter((f): f is File => f !== null);
    if (imgFiles.length > 0) {
      e.preventDefault();
      void handleFiles(imgFiles);
    }
  };

  const sendPrompt = async (promptText: string, promptImages: string[], convOverride?: string) => {
    const trimmed = promptText.trim();
    if (!trimmed && promptImages.length === 0) return;
    if (streaming) return;

    let targetId = convOverride ?? activeId;
    if (!targetId) {
      const created = await createConversation();
      if (!created) return;
      targetId = created.id;
    }

    onError(null);
    setStreaming(true);
    setAgentStatus({ round: 1, maxRounds: 0, phase: "routing" });
    setPendingApprovals({});
    setStreamingTools([]);

    const tempUserId = `temp-user-${Date.now()}`;
    const optimisticUser: ChatMessageRecord = {
      id: tempUserId,
      conversation_id: targetId,
      role: "user",
      content: trimmed,
      reasoning: "",
      images: promptImages,
      tool_calls: [],
      tool_call_id: null,
      tool_name: null,
      meta: {},
      created_at: Date.now() / 1000
    };
    setMessages((prev) => [...prev, optimisticUser]);

    const controller = new AbortController();
    abortRef.current = controller;

    let activeDraft: ChatMessageRecord | null = null;
    let activeRoundTools: ChatMessageRecord[] = [];
    let roundIdx = 0;

    try {
      const bodyPayload: Record<string, unknown> = {
        content: trimmed,
        images: promptImages
      };
      const optFloat = (v: string) => {
        const n = v.trim() !== "" ? Number(v) : Number.NaN;
        return Number.isFinite(n) ? n : undefined;
      };
      const optInt = (v: string) => {
        const n = v.trim() !== "" ? Number.parseInt(v, 10) : Number.NaN;
        return Number.isFinite(n) ? n : undefined;
      };
      const t = optFloat(sampling.temperature);
      const tp = optFloat(sampling.topP);
      const tk = optInt(sampling.topK);
      const mp = optFloat(sampling.minP);
      const rp = optFloat(sampling.repeatPenalty);
      const mt = optInt(sampling.maxTokens);
      const sd = optInt(sampling.seed);
      const stopSeqs = sampling.stop
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      if (t !== undefined) bodyPayload.temperature = t;
      if (tp !== undefined) bodyPayload.top_p = tp;
      if (tk !== undefined) bodyPayload.top_k = tk;
      if (mp !== undefined) bodyPayload.min_p = mp;
      if (rp !== undefined) bodyPayload.repeat_penalty = rp;
      if (mt !== undefined) bodyPayload.max_tokens = mt;
      if (sd !== undefined) bodyPayload.seed = sd;
      if (stopSeqs.length > 0) bodyPayload.stop = stopSeqs;
      if (sampling.reasoningEffort) bodyPayload.reasoning_effort = sampling.reasoningEffort;
      if (sampling.enableThinking) {
        bodyPayload.enable_thinking = sampling.enableThinking === "on";
      }

      const res = await fetch(`/api/chat/conversations/${encodeURIComponent(targetId)}/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(bodyPayload),
        signal: controller.signal
      });
      if (!res.ok || !res.body) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody.detail || `HTTP ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let splitIdx = buffer.indexOf("\n\n");
        while (splitIdx !== -1) {
          const rawBlock = buffer.slice(0, splitIdx);
          buffer = buffer.slice(splitIdx + 2);
          splitIdx = buffer.indexOf("\n\n");

          const parsed = parseSseChunk(rawBlock);
          if (!parsed) continue;
          const { event, data } = parsed;

          if (event === "user_saved") {
            const saved = data as unknown as ChatMessageRecord;
            setMessages((prev) => prev.map((m) => (m.id === tempUserId ? saved : m)));
          } else if (event === "compacting") {
            setCompacting(true);
            setCompactNotice("Auto-compacting earlier messages…");
          } else if (event === "compacted") {
            setCompacting(false);
            setCompactNotice(
              `Auto-compacted ${String(data.compacted_messages ?? 0)} older messages`
            );
            setConversations((prev) =>
              prev.map((c) =>
                c.id === targetId
                  ? {
                      ...c,
                      compacted_summary: String(data.summary ?? ""),
                      compacted_through_ts: Number(data.compacted_through_ts ?? 0)
                    }
                  : c
              )
            );
            window.setTimeout(() => setCompactNotice(null), 5000);
          } else if (event === "meta") {
            if (activeDraft) {
              const completedDraft = activeDraft;
              const completedTools = activeRoundTools;
              setMessages((prev) => [...prev, completedDraft, ...completedTools]);
              activeRoundTools = [];
              setStreamingTools([]);
            }
            roundIdx += 1;
            activeDraft = {
              id: `stream-asst-${roundIdx}-${Date.now()}`,
              conversation_id: targetId,
              role: "assistant",
              content: "",
              reasoning: "",
              images: [],
              tool_calls: [],
              tool_call_id: null,
              tool_name: null,
              meta: {
                node: String(data.node ?? ""),
                model: String(data.model ?? ""),
                fallback_reason: (data.fallback_reason as string | null) ?? null
              },
              created_at: Date.now() / 1000
            };
            setStreamingDraft({ ...activeDraft });
            setAgentStatus({
              round: Number(data.round ?? roundIdx),
              maxRounds: Number(data.max_rounds ?? 0),
              phase: "thinking",
              node: String(data.node ?? ""),
              model: String(data.model ?? "")
            });
          } else if (event === "reasoning" && activeDraft) {
            activeDraft.reasoning += String(data.text ?? "");
            setStreamingDraft({ ...activeDraft });
            setAgentStatus((prev) => (prev ? { ...prev, phase: "thinking" } : prev));
          } else if (event === "token" && activeDraft) {
            activeDraft.content += String(data.text ?? "");
            setStreamingDraft({ ...activeDraft });
            setAgentStatus((prev) => (prev ? { ...prev, phase: "generating" } : prev));
          } else if (event === "tool_call" && activeDraft) {
            const tc: ToolCallRecord = {
              id: String(data.id ?? ""),
              type: "function",
              function: {
                name: String(data.name ?? ""),
                arguments: JSON.stringify(data.arguments ?? {}, null, 2)
              }
            };
            activeDraft.tool_calls = [...activeDraft.tool_calls, tc];
            setStreamingDraft({ ...activeDraft });
            setAgentStatus((prev) =>
              prev ? { ...prev, phase: "tool", toolName: tc.function.name } : prev
            );
          } else if (event === "approval_required") {
            const appr: PendingApproval = {
              approval_id: String(data.approval_id ?? ""),
              tool_call_id: String(data.tool_call_id ?? ""),
              name: String(data.name ?? ""),
              arguments: (data.arguments as Record<string, unknown>) ?? {},
              exec_node: String(data.exec_node ?? ""),
              sandbox: (data.sandbox as string | null) ?? null,
              swarm_agent_id: (data.swarm_agent_id as string | null) ?? null,
              swarm_role: (data.swarm_role as string | null) ?? null
            };
            setPendingApprovals((prev) => ({ ...prev, [appr.tool_call_id]: appr }));
            setAgentStatus((prev) =>
              prev ? { ...prev, phase: "approval", toolName: appr.name } : prev
            );
          } else if (event === "swarm_run_started") {
            const run = data as unknown as SwarmRunRecord;
            setSwarmRuns((prev) => [...prev.filter((r) => r.id !== run.id), run]);
          } else if (event === "swarm_agent_update") {
            const ag = data as unknown as SwarmAgentRecord;
            setSwarmRuns((prev) =>
              prev.map((r) =>
                r.id === ag.run_id
                  ? {
                      ...r,
                      agents: r.agents.some((x) => x.id === ag.id)
                        ? r.agents.map((x) => (x.id === ag.id ? ag : x))
                        : [...r.agents, ag]
                    }
                  : r
              )
            );
            if (ag.status === "done" || ag.status === "error" || ag.status === "cancelled") {
              setSwarmLiveText((prev) => {
                const next = { ...prev };
                delete next[ag.id];
                return next;
              });
              setSwarmLiveReasoning((prev) => {
                const next = { ...prev };
                delete next[ag.id];
                return next;
              });
              if (inspectedSwarm?.agentId === ag.id) {
                void loadSwarmWorkerMessages(ag.id);
              }
            }
          } else if (event === "swarm_agent_token") {
            const aid = String(data.agent_id ?? "");
            const kind = String(data.kind ?? "token");
            const txt = String(data.text ?? "");
            if (kind === "reasoning") {
              setSwarmLiveReasoning((prev) => ({ ...prev, [aid]: (prev[aid] ?? "") + txt }));
            } else {
              setSwarmLiveText((prev) => ({ ...prev, [aid]: (prev[aid] ?? "") + txt }));
            }
          } else if (event === "swarm_agent_tool") {
            const aid = String(data.agent_id ?? "");
            if (inspectedSwarm?.agentId === aid) {
              void loadSwarmWorkerMessages(aid);
            }
          } else if (event === "swarm_run_done") {
            const rid = String(data.run_id ?? "");
            const st = String(data.status ?? "done") as SwarmRunRecord["status"];
            const ags = (data.agents as SwarmAgentRecord[] | undefined) ?? undefined;
            setSwarmRuns((prev) =>
              prev.map((r) =>
                r.id === rid ? { ...r, status: st, agents: ags ?? r.agents } : r
              )
            );
          } else if (event === "tool_result") {
            const tcId = String(data.id ?? "");
            setPendingApprovals((prev) => {
              const copy = { ...prev };
              delete copy[tcId];
              return copy;
            });
            setAgentStatus((prev) =>
              prev ? { ...prev, phase: "routing", toolName: undefined } : prev
            );
            const toolRec: ChatMessageRecord = {
              id: `stream-tool-${tcId}-${Date.now()}`,
              conversation_id: targetId,
              role: "tool",
              content: String(data.output ?? ""),
              reasoning: "",
              images: [],
              tool_calls: [],
              tool_call_id: tcId,
              tool_name: String(data.name ?? ""),
              meta: {
                ok: Boolean(data.ok),
                diff: (data.diff as string | null) ?? null,
                verification:
                  (data.verification as ChatMessageRecord["meta"]["verification"]) ?? null,
                sandbox: (data.sandbox as string | null) ?? null,
                media_url: (data.media_url as string | null) ?? null,
                media_kind: (data.media_kind as "image" | "video" | null) ?? null
              },
              created_at: Date.now() / 1000
            };
            activeRoundTools = [...activeRoundTools, toolRec];
            setStreamingTools([...activeRoundTools]);
          } else if (event === "error") {
            onError(String(data.message ?? "Chat stream error"));
          } else if (event === "done") {
            activeDraft = null;
            activeRoundTools = [];
            setStreamingDraft(null);
            setStreamingTools([]);
          }
        }
      }

      await loadMessages(targetId);
      await loadSwarmRuns(targetId);
      await loadConversations(false);
      if (
        completionNotifications &&
        typeof document !== "undefined" &&
        document.hidden &&
        typeof Notification !== "undefined" &&
        Notification.permission === "granted"
      ) {
        new Notification("Splitch", { body: "Agent response finished" });
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        if (activeDraft) {
          const stoppedDraft: ChatMessageRecord = {
            ...activeDraft,
            meta: { ...activeDraft.meta, stopped: true }
          };
          setMessages((prev) => [...prev, stoppedDraft, ...activeRoundTools]);
        }
      } else {
        onError(err instanceof Error ? err.message : "Chat request failed");
      }
    } finally {
      abortRef.current = null;
      setStreaming(false);
      setAgentStatus(null);
      setStreamingDraft(null);
      setStreamingTools([]);
      setPendingApprovals({});
    }
  };

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (streaming) return;
    const text = composerText;
    const imgs = attachedImages;
    if (!text.trim() && imgs.length === 0) return;
    setComposerText("");
    setAttachedImages([]);
    void sendPrompt(text, imgs);
  };

  const handleComposerKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (e.nativeEvent.isComposing || e.keyCode === 229) return;
      formRef.current?.requestSubmit();
    }
  };

  const stopGeneration = () => {
    abortRef.current?.abort();
  };

  const resolveApproval = async (appr: PendingApproval, approved: boolean) => {
    try {
      const res = await fetch(`/api/chat/approvals/${encodeURIComponent(appr.approval_id)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setPendingApprovals((prev) => {
        const copy = { ...prev };
        delete copy[appr.tool_call_id];
        return copy;
      });
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to submit approval");
    }
  };

  const editAndBranch = async (msg: ChatMessageRecord) => {
    if (streaming) return;
    try {
      const res = await fetch(`/api/chat/messages/${encodeURIComponent(msg.id)}`, {
        method: "DELETE"
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      if (activeId) await loadMessages(activeId);
      setComposerText(msg.content);
      setAttachedImages(msg.images || []);
      textareaRef.current?.focus();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to branch message");
    }
  };

  const regenerateFromAssistant = async (asstIndex: number) => {
    if (streaming || !activeId) return;
    let prevUser: ChatMessageRecord | null = null;
    for (let i = asstIndex - 1; i >= 0; i -= 1) {
      if (messages[i].role === "user") {
        prevUser = messages[i];
        break;
      }
    }
    if (!prevUser) return;
    try {
      const res = await fetch(`/api/chat/messages/${encodeURIComponent(prevUser.id)}`, {
        method: "DELETE"
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadMessages(activeId);
      await sendPrompt(prevUser.content, prevUser.images || [], activeId);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Regenerate failed");
    }
  };

  const copyMessage = async (msg: ChatMessageRecord) => {
    try {
      await navigator.clipboard.writeText(msg.content);
      setCopiedMsgId(msg.id);
      window.setTimeout(() => setCopiedMsgId(null), 1500);
    } catch {
      // clipboard unavailable
    }
  };

  const exportMarkdown = () => {
    if (!activeConv) return;
    const lines: string[] = [
      `# ${activeConv.title}`,
      "",
      `- **Model:** \`${activeConv.model}\``,
      `- **Exec Node:** \`${activeConv.exec_node || "auto"}\``,
      `- **Workspace:** \`${activeConv.workspace_subpath || "."}\``,
      ""
    ];
    for (const m of messages) {
      if (m.role === "user") {
        lines.push(`## User`, "", m.content, "");
      } else if (m.role === "assistant") {
        const tag = m.meta.node ? ` (${m.meta.node}/${m.meta.model})` : "";
        lines.push(`## Assistant${tag}`, "");
        if (m.reasoning) {
          lines.push("<details><summary>Thinking</summary>", "", m.reasoning, "", "</details>", "");
        }
        if (m.content) lines.push(m.content, "");
      } else if (m.role === "tool") {
        lines.push(`### Tool: \`${m.tool_name || "tool"}\``, "", "```", m.content, "```", "");
      }
    }
    const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const slug = activeConv.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    a.download = `${slug || "splitch-chat"}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const displayMessages = useMemo(() => {
    const combined = [...messages];
    if (streamingDraft) combined.push(streamingDraft);
    if (streamingTools.length > 0) combined.push(...streamingTools);
    return combined;
  }, [messages, streamingDraft, streamingTools]);

  const toolResultsByCallId = useMemo(() => {
    const map: Record<string, ChatMessageRecord> = {};
    for (const m of displayMessages) {
      if (m.role === "tool" && m.tool_call_id) {
        map[m.tool_call_id] = m;
      }
    }
    return map;
  }, [displayMessages]);

  const visibleMessages = useMemo(
    () => displayMessages.filter((m) => m.role !== "tool"),
    [displayMessages]
  );

  const toggleSelection = (list: string[], id: string, setter: (v: string[]) => void) => {
    setter(list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);
  };

  const activeWorkspaceNode = useMemo(() => {
    const nodes = snapshot?.nodes ?? [];
    if (effectiveConfig.exec_node) {
      return nodes.find((n) => n.node_id === effectiveConfig.exec_node) ?? null;
    }
    return nodes.find((n) => n.online && n.telemetry?.tools_enabled) ?? null;
  }, [snapshot?.nodes, effectiveConfig.exec_node]);

  const wsNodeId = activeWorkspaceNode?.node_id ?? "";
  const wsToolsEnabled = Boolean(
    activeWorkspaceNode?.online && activeWorkspaceNode?.telemetry?.tools_enabled
  );

  const refreshGitStatus = async () => {
    if (!wsNodeId || !wsToolsEnabled) {
      setGitStatus(null);
      return;
    }
    setGitLoading(true);
    try {
      const res = await fetch(
        `/api/nodes/${encodeURIComponent(wsNodeId)}/git/status?subpath=${encodeURIComponent(
          effectiveConfig.workspace_subpath
        )}`
      );
      if (res.ok) {
        setGitStatus((await res.json()) as GitStatusResponse);
      }
    } catch {
      // ignore transient poll errors
    } finally {
      setGitLoading(false);
    }
  };

  useEffect(() => {
    void refreshGitStatus();
  }, [wsNodeId, wsToolsEnabled, effectiveConfig.workspace_subpath]);

  useEffect(() => {
    if (!streaming && wsNodeId && wsToolsEnabled) {
      void refreshGitStatus();
    }
  }, [streaming]);

  const openWorkspaceFile = async (relPath: string) => {
    if (!wsNodeId) return;
    setInspectedArtifact(null);
    setWorkspaceInspect({ kind: "file", path: relPath, data: null, loading: true });
    if (typeof window !== "undefined" && window.innerWidth < 1380) {
      setSidebarCollapsed(true);
    }
    try {
      const res = await fetch(
        `/api/nodes/${encodeURIComponent(wsNodeId)}/workspace/file?subpath=${encodeURIComponent(
          effectiveConfig.workspace_subpath
        )}&path=${encodeURIComponent(relPath)}`
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const data = (await res.json()) as WorkspaceFileContent;
      setWorkspaceInspect({ kind: "file", path: relPath, data, loading: false });
    } catch (err) {
      setWorkspaceInspect({ kind: "file", path: relPath, data: null, loading: false });
      onError(err instanceof Error ? err.message : "Failed to read workspace file");
    }
  };

  const openWorkspaceDiff = async (relPath: string, staged: boolean) => {
    if (!wsNodeId) return;
    setInspectedArtifact(null);
    setWorkspaceInspect({ kind: "diff", path: relPath, staged, data: null, loading: true });
    if (typeof window !== "undefined" && window.innerWidth < 1380) {
      setSidebarCollapsed(true);
    }
    try {
      const res = await fetch(
        `/api/nodes/${encodeURIComponent(wsNodeId)}/git/diff?subpath=${encodeURIComponent(
          effectiveConfig.workspace_subpath
        )}&path=${encodeURIComponent(relPath)}&staged=${staged ? "true" : "false"}`
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const data = (await res.json()) as GitDiffResponse;
      setWorkspaceInspect({ kind: "diff", path: relPath, staged, data, loading: false });
    } catch (err) {
      setWorkspaceInspect({ kind: "diff", path: relPath, staged, data: null, loading: false });
      onError(err instanceof Error ? err.message : "Failed to load git diff");
    }
  };

  const attachWorkspacePath = (relPath: string) => {
    const token = `@${relPath}`;
    setComposerText((prev) => {
      if (!prev.trim()) return `${token} `;
      return prev.endsWith(" ") || prev.endsWith("\n") ? `${prev}${token} ` : `${prev} ${token} `;
    });
    textareaRef.current?.focus();
  };

  const rightPaneOpen = Boolean(inspectedArtifact || workspaceInspect || inspectedSwarm);

  return (
    <div
      ref={layoutRef}
      className={`chat-layout ${sidebarCollapsed ? "chat-layout--sidebar-collapsed" : ""} ${
        rightPaneOpen ? "chat-layout--with-artifact" : ""
      } ${isResizingPane ? "chat-layout--resizing" : ""}`}
      style={
        rightPaneOpen
          ? ({ "--artifact-pane-width": `${paneWidth}px` } as React.CSSProperties)
          : undefined
      }
    >
      <ChatSidebar
        sidebarCollapsed={sidebarCollapsed}
        sidebarMode={sidebarMode}
        setSidebarMode={setSidebarMode}
        dirtyCount={gitStatus?.changes?.length ?? 0}
        workspaceProps={{
          nodeId: wsNodeId,
          subpath: effectiveConfig.workspace_subpath,
          model: effectiveConfig.model,
          toolsEnabled: wsToolsEnabled,
          gitStatus,
          gitLoading,
          onRefreshGit: refreshGitStatus,
          onOpenFile: (p) => void openWorkspaceFile(p),
          onOpenDiff: (p, st) => void openWorkspaceDiff(p, st),
          onAttachPath: attachWorkspacePath,
          onOpenWorkspaceDialog: openWorkspaceBrowser,
          onConfirm,
          onError
        }}
        streaming={streaming}
        searchOpen={searchOpen}
        setSearchOpen={setSearchOpen}
        sidebarQuery={sidebarQuery}
        setSidebarQuery={setSidebarQuery}
        searchInputRef={searchInputRef}
        selectMode={selectMode}
        setSelectMode={setSelectMode}
        exitSelectMode={exitSelectMode}
        sidebarView={sidebarView}
        switchSidebarView={switchSidebarView}
        conversations={conversations}
        trash={trash}
        filteredConversations={filteredConversations}
        activeId={activeId}
        setActiveId={setActiveId}
        selectAllId={selectAllId}
        selectAllRef={selectAllRef}
        selectRowPrefix={selectRowPrefix}
        supportsHasSelector={supportsHasSelector}
        shiftHeldRef={shiftHeldRef}
        allVisibleSelected={allVisibleSelected}
        visibleSelectedCount={visibleSelectedCount}
        selectedIds={selectedIds}
        toggleSelectAllVisible={toggleSelectAllVisible}
        handleSelectChange={handleSelectChange}
        createConversation={createConversation}
        patchConversation={patchConversation}
        requestTrashConversation={requestTrashConversation}
        restoreConversation={restoreConversation}
        requestPermanentDelete={requestPermanentDelete}
        requestBulkTrash={requestBulkTrash}
        bulkRestoreSelected={bulkRestoreSelected}
        requestBulkPermanentDelete={requestBulkPermanentDelete}
        requestEmptyTrash={requestEmptyTrash}
      />

      <section className="chat-main" aria-label="Agent Chat Workspace">
        <header className="chat-toolbar">
          <div className="chat-toolbar__group">
            <button
              type="button"
              className="btn btn--sm btn--ghost chat-sidebar-toggle"
              onClick={() => setSidebarCollapsed((v) => !v)}
              aria-label={sidebarCollapsed ? "Expand chat sidebar" : "Collapse chat sidebar"}
              title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            >
              {sidebarCollapsed ? (
                <PanelLeftOpen size={15} aria-hidden="true" />
              ) : (
                <PanelLeftClose size={15} aria-hidden="true" />
              )}
            </button>

            <label htmlFor={modelSelectId} className="toolbar-pill">
              <Zap size={12} aria-hidden="true" className="toolbar-pill__icon" />
              <span className="visually-hidden">Model</span>
              <select
                id={modelSelectId}
                value={effectiveConfig.model}
                disabled={streaming}
                onChange={(e) => updateConfig({ model: e.target.value })}
              >
                {modelOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>

            <label htmlFor={execNodeId} className="toolbar-pill">
              <Cpu size={12} aria-hidden="true" className="toolbar-pill__icon" />
              <span className="visually-hidden">Tools Node</span>
              <select
                id={execNodeId}
                value={effectiveConfig.exec_node}
                disabled={streaming}
                onChange={(e) => updateConfig({ exec_node: e.target.value })}
              >
                <option value="">Auto Node</option>
                {(snapshot?.nodes ?? []).map((n) => {
                  const vramGb = n.telemetry?.vram_total_mb
                    ? `${(n.telemetry.vram_total_mb / 1024).toFixed(0)}GB`
                    : "";
                  return (
                    <option key={n.node_id} value={n.node_id}>
                      {n.display_name}
                      {vramGb ? ` (${vramGb})` : ""}
                      {n.telemetry?.tools_enabled ? "" : " — tools off"}
                    </option>
                  );
                })}
              </select>
            </label>

            <button
              type="button"
              className="toolbar-pill toolbar-pill--btn"
              disabled={streaming}
              onClick={openWorkspaceBrowser}
              title={`Workspace directory: ${effectiveConfig.workspace_subpath || "."}`}
            >
              <Folder size={12} aria-hidden="true" className="toolbar-pill__icon" />
              <span>{effectiveConfig.workspace_subpath || "workspace"}</span>
            </button>
          </div>

          <div className="chat-toolbar__group chat-toolbar__toggles">
            <label className="toolbar-switch">
              <span>Tools</span>
              <input
                type="checkbox"
                {...({ switch: "" } as Record<string, string>)}
                checked={effectiveConfig.tools_enabled}
                disabled={streaming}
                onChange={(e) => updateConfig({ tools_enabled: e.target.checked })}
              />
            </label>

            <label
              className="toolbar-switch"
              title="Enable multi-agent swarm delegation across cluster nodes"
            >
              <span>Swarm</span>
              <input
                type="checkbox"
                {...({ switch: "" } as Record<string, string>)}
                checked={effectiveConfig.swarm_enabled}
                disabled={streaming}
                onChange={(e) => updateConfig({ swarm_enabled: e.target.checked })}
              />
            </label>

            {swarmRuns.length > 0 && (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => {
                  const lastRun = swarmRuns[swarmRuns.length - 1];
                  const firstAgent = lastRun?.agents[0];
                  if (lastRun) {
                    handleInspectSwarmAgent(lastRun.id, firstAgent?.id ?? "");
                  }
                }}
                title="Open Swarm Inspector"
                aria-label="Open Swarm Inspector"
              >
                <Network size={13} aria-hidden="true" />
              </button>
            )}

            <label
              className="toolbar-switch"
              title="Skip approval prompts for write_file & run_command"
            >
              <span>Auto-Approve</span>
              <input
                type="checkbox"
                {...({ switch: "" } as Record<string, string>)}
                checked={effectiveConfig.auto_approve}
                disabled={streaming}
                onChange={(e) => updateConfig({ auto_approve: e.target.checked })}
              />
            </label>

            {activeConv && messages.length > 0 && (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={exportMarkdown}
                title="Export conversation to Markdown (.md)"
                aria-label="Export conversation to Markdown"
              >
                <Download size={13} aria-hidden="true" />
              </button>
            )}

            {!inspectedArtifact && lastArtifact && (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => openArtifactInPane(lastArtifact)}
                title={`Open ${lastArtifact.label} in split pane`}
                aria-label="Open artifact split pane"
              >
                <PanelRightOpen size={14} aria-hidden="true" />
              </button>
            )}
          </div>
        </header>

        <ChatMessageList
          activeId={activeId}
          activeConv={activeConv}
          visibleMessages={visibleMessages}
          messages={messages}
          nodes={snapshot?.nodes ?? []}
          streaming={streaming}
          copiedMsgId={copiedMsgId}
          toolResultsByCallId={toolResultsByCallId}
          pendingApprovals={pendingApprovals}
          thinkingDisplay={thinkingDisplay}
          inspectedArtifact={inspectedArtifact}
          markdownComponents={markdownComponents}
          threadRef={threadRef}
          latestMsgRef={latestMsgRef}
          bottomSentinelRef={bottomSentinelRef}
          handleThreadScroll={handleThreadScroll}
          jumpToBottom={jumpToBottom}
          openArtifactInPane={openArtifactInPane}
          resolveApproval={resolveApproval}
          editAndBranch={editAndBranch}
          copyMessage={copyMessage}
          regenerateFromAssistant={regenerateFromAssistant}
          clearCompaction={clearCompaction}
          onContinueTurn={() => void sendPrompt("Continue", [])}
          swarmRuns={swarmRuns}
          inspectedSwarmAgentId={inspectedSwarm?.agentId ?? null}
          onInspectSwarmAgent={handleInspectSwarmAgent}
          onCancelSwarmAgent={handleCancelSwarmAgent}
        />

        <ChatComposer
          formRef={formRef}
          textareaRef={textareaRef}
          fileInputRef={fileInputRef}
          composerId={composerId}
          dragActive={dragActive}
          setDragActive={setDragActive}
          attachedImages={attachedImages}
          setAttachedImages={setAttachedImages}
          composerText={composerText}
          setComposerText={setComposerText}
          streaming={streaming}
          agentStatus={agentStatus}
          effectiveConfig={effectiveConfig}
          updateConfig={updateConfig}
          effectiveSkillCount={effectiveSkillIds.length}
          effectiveMcpCount={effectiveMcpIds.length}
          activeExternalMcps={activeExternalMcps}
          sampling={sampling}
          setSampling={setSampling}
          gamingMode={Boolean(snapshot?.control.gaming_mode)}
          compactNotice={compactNotice}
          contextStats={contextStats}
          handleSubmit={handleSubmit}
          handleFiles={handleFiles}
          handleComposerKeyDown={handleComposerKeyDown}
          handlePaste={handlePaste}
          openContextDialog={openContextDialog}
          stopGeneration={stopGeneration}
        />
      </section>

      {rightPaneOpen && (
        <>
          <div
            className="chat-splitter"
            role="separator"
            tabIndex={0}
            aria-orientation="vertical"
            aria-label="Resize inspector pane"
            aria-valuemin={280}
            aria-valuenow={paneWidth}
            title="Drag to resize • Double-click to reset width"
            onPointerDown={startPaneResize}
            onDoubleClick={() => setPaneWidth(470)}
            onKeyDown={(e) => {
              if (e.key === "ArrowLeft") {
                e.preventDefault();
                setPaneWidth((w) => clampPaneWidth(w + 24));
              } else if (e.key === "ArrowRight") {
                e.preventDefault();
                setPaneWidth((w) => clampPaneWidth(w - 24));
              }
            }}
          />
          {inspectedSwarm ? (
            <SwarmInspectorPane
              runs={swarmRuns}
              selectedRunId={inspectedSwarm.runId}
              selectedAgentId={inspectedSwarm.agentId}
              workerMessages={swarmWorkerMsgs}
              liveStreamText={
                inspectedSwarm.agentId ? (swarmLiveText[inspectedSwarm.agentId] ?? "") : ""
              }
              liveReasoningText={
                inspectedSwarm.agentId ? (swarmLiveReasoning[inspectedSwarm.agentId] ?? "") : ""
              }
              onSelectAgent={(rid, aid) => setInspectedSwarm({ runId: rid, agentId: aid })}
              onCancelAgent={handleCancelSwarmAgent}
              onClose={() => setInspectedSwarm(null)}
            />
          ) : workspaceInspect ? (
            <WorkspaceViewerPane
              target={workspaceInspect}
              onAttachPath={attachWorkspacePath}
              onToggleDiffStaged={(p, nextStaged) => void openWorkspaceDiff(p, nextStaged)}
              onClose={() => setWorkspaceInspect(null)}
            />
          ) : (
            inspectedArtifact && (
              <ArtifactSplitPane
                artifact={inspectedArtifact}
                onClose={() => setInspectedArtifact(null)}
              />
            )
          )}
        </>
      )}

      <ContextMemoryDialog
        dialogRef={settingsDialogRef}
        effectiveConfig={effectiveConfig}
        streaming={streaming}
        updateConfig={updateConfig}
        activeConv={activeConv}
        compacting={compacting}
        messagesCount={messages.length}
        compactNow={compactNow}
        contextStats={contextStats}
        saveContextSettings={saveContextSettings}
        settingsPrompt={settingsPrompt}
        setSettingsPrompt={setSettingsPrompt}
        contextScopeTarget={contextScopeTarget}
        selectScopeTarget={selectScopeTarget}
        activeWorkspaceKey={activeWorkspaceKey}
        execNode={effectiveConfig.exec_node || snapshot?.nodes.find((n) => n.online)?.node_id || ""}
        workspaceSubpath={effectiveConfig.workspace_subpath}
        skills={skills}
        scopedConfig={scopedConfig}
        settingsSkills={settingsSkills}
        setSettingsSkills={setSettingsSkills}
        mcps={mcps}
        settingsMcps={settingsMcps}
        setSettingsMcps={setSettingsMcps}
        toggleSelection={toggleSelection}
      />

      <WorkspaceBrowserDialog
        dialogRef={wsDialogRef}
        wsTitleId={wsTitleId}
        workspaceId={workspaceId}
        nodes={snapshot?.nodes ?? []}
        wsBrowseNode={wsBrowseNode}
        setWsBrowseNode={setWsBrowseNode}
        fetchWorkspaceDirs={fetchWorkspaceDirs}
        workspaceSubpath={effectiveConfig.workspace_subpath}
        onWorkspaceSubpathChange={(val) => {
          if (activeConv) {
            setConversations((prev) =>
              prev.map((c) => (c.id === activeConv.id ? { ...c, workspace_subpath: val } : c))
            );
          } else {
            setDraftConfig((prev) => ({ ...prev, workspace_subpath: val }));
          }
        }}
        onWorkspaceSubpathBlur={(val) => {
          if (activeConv) {
            void patchConversation(activeConv.id, { workspace_subpath: val });
          }
        }}
        wsListing={wsListing}
        wsError={wsError}
        wsLoading={wsLoading}
        onApplyWorkspace={(nodeId, subpath) =>
          updateConfig({ exec_node: nodeId, workspace_subpath: subpath })
        }
      />
    </div>
  );
}
