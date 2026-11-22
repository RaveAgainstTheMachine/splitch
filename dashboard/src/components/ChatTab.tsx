import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertTriangle,
  ArchiveRestore,
  ArrowDown,
  ArrowUp,
  BookOpen,
  Bot,
  Brain,
  Check,
  CheckSquare,
  Copy,
  Download,
  Folder,
  FolderOpen,
  Globe,
  ImagePlus,
  MessageSquarePlus,
  Minimize2,
  Pencil,
  Pin,
  RotateCcw,
  Search,
  Send,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  Square,
  Terminal,
  Trash2,
  User,
  Wrench,
  X
} from "lucide-react";
import { ArtifactPreview, isRunnableArtifact } from "./ArtifactPreview";
import type { ConfirmState } from "./ConfirmDialog";
import { ContextRing } from "./ContextRing";
import { classifyClientMcpEgress } from "./McpTab";
import { useDialogLightDismiss } from "./ModelsTab";
import { SplitchLogo } from "./SplitchLogo";
import type {
  ChatMessageRecord,
  ConversationSummary,
  DashboardSnapshot,
  McpServerConfig,
  PendingApproval,
  ScopedExtensionsConfig,
  SkillItem,
  ToolCallRecord,
  WorkspaceDirListing
} from "../types";

interface Props {
  snapshot: DashboardSnapshot | null;
  onError: (msg: string | null) => void;
  onConfirm: (state: ConfirmState) => void;
}

interface DraftChatConfig {
  model: string;
  exec_node: string;
  workspace_subpath: string;
  tools_enabled: boolean;
  web_search_enabled: boolean;
  auto_approve: boolean;
  auto_compact: boolean;
  enabled_skills: string[];
  enabled_mcps: string[];
  system_prompt: string;
}

interface ParsedSseEvent {
  event: string;
  data: Record<string, unknown>;
}

const DEFAULT_DRAFT: DraftChatConfig = {
  model: "auto",
  exec_node: "",
  workspace_subpath: "",
  tools_enabled: true,
  web_search_enabled: false,
  auto_approve: false,
  auto_compact: true,
  enabled_skills: [],
  enabled_mcps: [],
  system_prompt: ""
};

function parseSseChunk(rawBlock: string): ParsedSseEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of rawBlock.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (dataLines.length === 0) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) as Record<string, unknown> };
  } catch {
    return null;
  }
}

function fmtShortDate(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("Failed to read image"));
    reader.readAsDataURL(file);
  });
}

function CodeBlock({
  className,
  children
}: {
  className?: string;
  children?: React.ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const langMatch = /language-([\w-]+)/.exec(className || "");
  const codeText = String(children ?? "").replace(/\n$/, "");
  const isBlock = Boolean(langMatch) || codeText.includes("\n");

  if (!isBlock) {
    return <code className={className}>{children}</code>;
  }

  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(codeText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // clipboard unavailable
    }
  };

  const lang = langMatch?.[1] || "text";
  const runnable = isRunnableArtifact(lang, codeText);

  return (
    <div className="md-codeblock">
      <div className="md-codeblock__bar">
        <span>{lang}</span>
        <button type="button" className="btn btn--sm btn--ghost" onClick={copyCode}>
          {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      {runnable && <ArtifactPreview lang={lang} code={codeText} />}
      <pre>
        <code className={className}>{codeText}</code>
      </pre>
    </div>
  );
}

const MARKDOWN_COMPONENTS: Components = {
  code({ className, children }) {
    return <CodeBlock className={className}>{children}</CodeBlock>;
  }
};

function DiffViewer({ diff }: { diff: string }) {
  return (
    <pre className="diff-block">
      {diff.split("\n").map((line, i) => {
        const cls =
          line.startsWith("+") && !line.startsWith("+++")
            ? "diff-line diff-line--add"
            : line.startsWith("-") && !line.startsWith("---")
              ? "diff-line diff-line--del"
              : line.startsWith("@@")
                ? "diff-line diff-line--hunk"
                : "diff-line";
        return (
          <span key={i} className={cls}>
            {line}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
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
  const [temperature, setTemperature] = useState<string>("");
  const [dragActive, setDragActive] = useState(false);

  const [streaming, setStreaming] = useState(false);
  const [streamingDraft, setStreamingDraft] = useState<ChatMessageRecord | null>(null);
  const [streamingTools, setStreamingTools] = useState<ChatMessageRecord[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState<Record<string, PendingApproval>>({});
  const [copiedMsgId, setCopiedMsgId] = useState<string | null>(null);
  const [compacting, setCompacting] = useState(false);
  const [compactNotice, setCompactNotice] = useState<string | null>(null);

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
      const tempNum = temperature.trim() !== "" ? Number(temperature) : null;
      const bodyPayload: Record<string, unknown> = {
        content: trimmed,
        images: promptImages
      };
      if (tempNum !== null && !Number.isNaN(tempNum)) {
        bodyPayload.temperature = tempNum;
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
          } else if (event === "reasoning" && activeDraft) {
            activeDraft.reasoning += String(data.text ?? "");
            setStreamingDraft({ ...activeDraft });
          } else if (event === "token" && activeDraft) {
            activeDraft.content += String(data.text ?? "");
            setStreamingDraft({ ...activeDraft });
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
          } else if (event === "approval_required") {
            const appr: PendingApproval = {
              approval_id: String(data.approval_id ?? ""),
              tool_call_id: String(data.tool_call_id ?? ""),
              name: String(data.name ?? ""),
              arguments: (data.arguments as Record<string, unknown>) ?? {},
              exec_node: String(data.exec_node ?? "")
            };
            setPendingApprovals((prev) => ({ ...prev, [appr.tool_call_id]: appr }));
          } else if (event === "tool_result") {
            const tcId = String(data.id ?? "");
            setPendingApprovals((prev) => {
              const copy = { ...prev };
              delete copy[tcId];
              return copy;
            });
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
      await loadConversations(false);
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

  return (
    <div className="chat-layout">
      <aside className="chat-sidebar" aria-label="Conversations">
        <div className="chat-sidebar__head">
          <button
            type="button"
            className="btn btn--accent chat-new-btn"
            onClick={() => void createConversation()}
            disabled={streaming}
          >
            <MessageSquarePlus size={14} aria-hidden="true" />
            New Chat
          </button>
          <button
            type="button"
            className={`btn btn--sm ${selectMode ? "btn--accent" : ""}`}
            aria-pressed={selectMode}
            title={selectMode ? "Exit multi-select (Esc)" : "Select multiple chats"}
            disabled={
              streaming ||
              (!selectMode && (sidebarView === "chats" ? conversations.length === 0 : trash.length === 0))
            }
            onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
          >
            <CheckSquare size={13} aria-hidden="true" />
            {selectMode ? "Done" : "Select"}
          </button>
        </div>

        <div className="chat-sidebar__tabs" role="group" aria-label="Conversation views">
          <button
            type="button"
            className="chat-sidebar__tab"
            aria-pressed={sidebarView === "chats"}
            onClick={() => switchSidebarView("chats")}
          >
            Chats ({conversations.length})
          </button>
          <button
            type="button"
            className="chat-sidebar__tab"
            aria-pressed={sidebarView === "trash"}
            onClick={() => switchSidebarView("trash")}
          >
            <Trash2 size={12} aria-hidden="true" />
            Trash ({trash.length})
          </button>
        </div>

        <div className="chat-sidebar__search">
          <Search size={13} aria-hidden="true" className="muted" />
          <input
            type="search"
            value={sidebarQuery}
            onChange={(e) => setSidebarQuery(e.target.value)}
            placeholder={sidebarView === "chats" ? "Filter chats…" : "Filter trash…"}
            aria-label="Filter conversations"
          />
        </div>

        {selectMode && (
          <div className="chat-bulk-bar" role="group" aria-label="Bulk actions">
            <div className="chat-bulk-bar__top">
              <div className="chat-bulk-select-all">
                <input
                  id={selectAllId}
                  ref={selectAllRef}
                  type="checkbox"
                  className="chat-select-box"
                  checked={allVisibleSelected}
                  disabled={filteredConversations.length === 0}
                  onChange={toggleSelectAllVisible}
                />
                <label htmlFor={selectAllId} className="chat-bulk-select-all__label">
                  All ({visibleSelectedCount}/{filteredConversations.length})
                </label>
              </div>
              <span className="muted" style={{ fontSize: "0.68rem" }}>
                Shift+click range
              </span>
            </div>
            <div className="row-actions">
              {sidebarView === "chats" ? (
                <button
                  type="button"
                  className="btn btn--sm btn--danger"
                  style={{ flex: 1, justifyContent: "center" }}
                  disabled={streaming || selectedIds.size === 0}
                  onClick={requestBulkTrash}
                >
                  <Trash2 size={12} aria-hidden="true" />
                  Delete ({selectedIds.size})
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    className="btn btn--sm"
                    style={{ flex: 1, justifyContent: "center" }}
                    disabled={streaming || selectedIds.size === 0}
                    onClick={() => void bulkRestoreSelected()}
                  >
                    <ArchiveRestore size={12} aria-hidden="true" />
                    Restore ({selectedIds.size})
                  </button>
                  <button
                    type="button"
                    className="btn btn--sm btn--danger"
                    style={{ flex: 1, justifyContent: "center" }}
                    disabled={streaming || selectedIds.size === 0}
                    onClick={requestBulkPermanentDelete}
                  >
                    <Trash2 size={12} aria-hidden="true" />
                    Erase ({selectedIds.size})
                  </button>
                </>
              )}
            </div>
          </div>
        )}

        {sidebarView === "trash" && !selectMode && trash.length > 0 && (
          <button
            type="button"
            className="btn btn--sm btn--danger"
            disabled={streaming}
            onClick={requestEmptyTrash}
          >
            <Trash2 size={12} aria-hidden="true" />
            Empty Trash ({trash.length})
          </button>
        )}

        {selectMode ? (
          <fieldset className="chat-conv-list chat-conv-fieldset">
            <legend className="visually-hidden">
              Select {sidebarView === "chats" ? "conversations" : "trashed conversations"}
            </legend>
            {filteredConversations.length === 0 ? (
              <p className="muted chat-conv-empty">
                {sidebarView === "chats" ? "No matching chats." : "Trash bin is empty."}
              </p>
            ) : (
              filteredConversations.map((c, idx) => {
                const cbId = `${selectRowPrefix}-${c.id}`;
                const checked = selectedIds.has(c.id);
                return (
                  <div
                    key={c.id}
                    className={`chat-conv-item chat-conv-item--selectable ${
                      sidebarView === "trash" ? "chat-conv-item--trash" : ""
                    } ${!supportsHasSelector && checked ? "chat-conv-item--checked" : ""}`}
                    onMouseDown={(e) => {
                      shiftHeldRef.current = e.shiftKey;
                    }}
                  >
                    <input
                      id={cbId}
                      type="checkbox"
                      className="chat-select-box"
                      checked={checked}
                      disabled={streaming}
                      onKeyDown={(e) => {
                        shiftHeldRef.current = e.shiftKey;
                      }}
                      onChange={(e) => handleSelectChange(idx, c.id, e.target.checked)}
                    />
                    <label htmlFor={cbId} className="chat-conv-item__label">
                      <span className="chat-conv-item__title">
                        {c.pinned && sidebarView === "chats" && (
                          <Pin size={11} aria-hidden="true" className="chat-pin-badge" />
                        )}
                        {c.title}
                      </span>
                      <span className="chat-conv-item__meta">
                        <code>{c.model}</code>
                        <span>
                          {sidebarView === "trash"
                            ? c.deleted_at
                              ? fmtShortDate(c.deleted_at)
                              : ""
                            : fmtShortDate(c.updated_at)}
                        </span>
                      </span>
                    </label>
                  </div>
                );
              })
            )}
          </fieldset>
        ) : (
          <ul className="chat-conv-list">
            {filteredConversations.length === 0 ? (
              <li className="muted chat-conv-empty">
                {sidebarView === "chats" ? "No conversations." : "Trash is empty."}
              </li>
            ) : sidebarView === "chats" ? (
              filteredConversations.map((c) => {
                const isSelected = c.id === activeId;
                return (
                  <li
                    key={c.id}
                    className={`chat-conv-item ${isSelected ? "chat-conv-item--active" : ""} ${
                      c.pinned ? "chat-conv-item--pinned" : ""
                    }`}
                  >
                    <button
                      type="button"
                      className="chat-conv-item__btn"
                      onClick={() => !streaming && setActiveId(c.id)}
                    >
                      <span className="chat-conv-item__title">{c.title}</span>
                      <span className="chat-conv-item__meta">
                        <code>{c.model}</code>
                        <span>{fmtShortDate(c.updated_at)}</span>
                      </span>
                    </button>
                    <div className="row-actions chat-conv-item__actions">
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost chat-pin-btn"
                        aria-label={`Pin ${c.title}`}
                        aria-pressed={c.pinned}
                        title={c.pinned ? "Unpin chat" : "Pin chat to top"}
                        disabled={streaming}
                        onClick={() => void patchConversation(c.id, { pinned: !c.pinned })}
                      >
                        <Pin size={12} aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost btn--danger"
                        aria-label={`Move ${c.title} to trash`}
                        disabled={streaming}
                        onClick={() => requestTrashConversation(c)}
                      >
                        <Trash2 size={12} aria-hidden="true" />
                      </button>
                    </div>
                  </li>
                );
              })
            ) : (
              filteredConversations.map((c) => (
                <li key={c.id} className="chat-conv-item chat-conv-item--trash">
                  <div className="chat-conv-item__btn">
                    <span className="chat-conv-item__title">{c.title}</span>
                    <span className="chat-conv-item__meta">
                      <code>{c.model}</code>
                      <span>{c.deleted_at ? fmtShortDate(c.deleted_at) : ""}</span>
                    </span>
                  </div>
                  <div className="row-actions">
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      aria-label={`Restore ${c.title}`}
                      title="Restore chat"
                      disabled={streaming}
                      onClick={() => void restoreConversation(c)}
                    >
                      <ArchiveRestore size={12} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost btn--danger"
                      aria-label={`Permanently delete ${c.title}`}
                      title="Delete forever"
                      disabled={streaming}
                      onClick={() => requestPermanentDelete(c)}
                    >
                      <Trash2 size={12} aria-hidden="true" />
                    </button>
                  </div>
                </li>
              ))
            )}
          </ul>
        )}
      </aside>

      <section className="chat-main" aria-label="Agent Chat Workspace">
        <header className="chat-toolbar">
          <div className="chat-toolbar__group">
            <label htmlFor={modelSelectId} className="chat-field-label">
              <span>Model</span>
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

            <label htmlFor={execNodeId} className="chat-field-label">
              <span>Tools Node</span>
              <select
                id={execNodeId}
                value={effectiveConfig.exec_node}
                disabled={streaming}
                onChange={(e) => updateConfig({ exec_node: e.target.value })}
              >
                <option value="">Auto</option>
                {(snapshot?.nodes ?? []).map((n) => (
                  <option key={n.node_id} value={n.node_id}>
                    {n.display_name} ({n.node_id})
                    {n.telemetry?.tools_enabled ? "" : " — tools off"}
                  </option>
                ))}
              </select>
            </label>

            <div className="chat-field-label chat-field-label--grow">
              <label htmlFor={workspaceId}>
                <FolderOpen size={12} aria-hidden="true" /> Workspace
              </label>
              <div className="ws-input-row">
                <input
                  id={workspaceId}
                  type="text"
                  value={effectiveConfig.workspace_subpath}
                  disabled={streaming}
                  placeholder="subpath"
                  onChange={(e) => {
                    const val = e.target.value;
                    if (activeConv) {
                      setConversations((prev) =>
                        prev.map((c) =>
                          c.id === activeConv.id ? { ...c, workspace_subpath: val } : c
                        )
                      );
                    } else {
                      setDraftConfig((prev) => ({ ...prev, workspace_subpath: val }));
                    }
                  }}
                  onBlur={(e) =>
                    activeConv &&
                    void patchConversation(activeConv.id, { workspace_subpath: e.target.value })
                  }
                />
                <button
                  type="button"
                  className="btn btn--sm"
                  disabled={streaming}
                  onClick={openWorkspaceBrowser}
                  title="Browse & select workspace directory on node"
                >
                  <Folder size={13} aria-hidden="true" />
                  Browse
                </button>
              </div>
            </div>
          </div>

          <div className="chat-toolbar__group chat-toolbar__toggles">
            <label className="chat-check">
              <input
                type="checkbox"
                checked={effectiveConfig.tools_enabled}
                disabled={streaming}
                onChange={(e) => updateConfig({ tools_enabled: e.target.checked })}
              />
              <span>Tools</span>
            </label>

            <label className="chat-check" title="Skip approval prompts for write_file & run_command">
              <input
                type="checkbox"
                checked={effectiveConfig.auto_approve}
                disabled={streaming}
                onChange={(e) => updateConfig({ auto_approve: e.target.checked })}
              />
              <span>Auto-Approve</span>
            </label>

            <button
              type="button"
              className="btn btn--sm"
              disabled={!activeConv || messages.length === 0}
              onClick={exportMarkdown}
              title="Export conversation to Markdown"
            >
              <Download size={13} aria-hidden="true" />
              .md
            </button>
          </div>
        </header>

        <div
          key={activeId ?? "empty"}
          ref={threadRef}
          tabIndex={-1}
          className="chat-thread"
          aria-live="polite"
          onScroll={handleThreadScroll}
        >
          {visibleMessages.length === 0 ? (
            <div className="chat-empty">
              <SplitchLogo size={44} />
            </div>
          ) : (
            visibleMessages.map((msg, idx) => {
              const isLatest = idx === visibleMessages.length - 1;
              const isAssistant = msg.role === "assistant";
              const watermark = activeConv?.compacted_through_ts ?? 0;
              const isCompacted = watermark > 0 && msg.created_at <= watermark;
              const nextMsg = visibleMessages[idx + 1];
              const showCompactedDivider =
                Boolean(activeConv?.compacted_summary) &&
                isCompacted &&
                (!nextMsg || nextMsg.created_at > watermark);

              return (
                <div key={msg.id} className="chat-msg-slot">
                  <article
                    ref={isLatest ? latestMsgRef : null}
                    className={`chat-msg chat-msg--${msg.role} ${isLatest ? "chat-msg--latest" : ""} ${isCompacted ? "chat-msg--compacted" : ""}`}
                  >
                  <header className="chat-msg__head">
                    <div className="chat-msg__who">
                      {isAssistant ? (
                        <Bot size={14} aria-hidden="true" className="brand__icon" />
                      ) : (
                        <User size={14} aria-hidden="true" />
                      )}
                      <strong>{isAssistant ? "Splitch" : "You"}</strong>
                      {isAssistant && msg.meta.node && (
                        <span className="pill">
                          <span className="node-tag">{msg.meta.node}</span> {msg.meta.model}
                        </span>
                      )}
                      {isAssistant && msg.meta.fallback_reason && (
                        <span className="pill pill--warn">{msg.meta.fallback_reason}</span>
                      )}
                      {isAssistant && msg.meta.ttft_ms != null && (
                        <span className="muted">{msg.meta.ttft_ms}ms TTFT</span>
                      )}
                      {isAssistant && msg.meta.tokens_per_sec != null && (
                        <span className="pill pill--ok">{msg.meta.tokens_per_sec} t/s</span>
                      )}
                      {isAssistant && msg.meta.stopped && (
                        <span className="pill pill--warn">STOPPED</span>
                      )}
                    </div>

                    <div className="chat-msg__actions">
                      {msg.role === "user" && !streaming && !msg.id.startsWith("temp-") && (
                        <button
                          type="button"
                          className="btn btn--sm btn--ghost"
                          onClick={() => void editAndBranch(msg)}
                          title="Edit and branch from this message"
                        >
                          <Pencil size={12} aria-hidden="true" />
                          Edit
                        </button>
                      )}
                      {isAssistant && !streaming && !msg.id.startsWith("stream-") && (
                        <>
                          <button
                            type="button"
                            className="btn btn--sm btn--ghost"
                            onClick={() => void copyMessage(msg)}
                          >
                            {copiedMsgId === msg.id ? (
                              <Check size={12} aria-hidden="true" />
                            ) : (
                              <Copy size={12} aria-hidden="true" />
                            )}
                          </button>
                          <button
                            type="button"
                            className="btn btn--sm btn--ghost"
                            onClick={() => {
                              const realIdx = messages.findIndex((x) => x.id === msg.id);
                              if (realIdx >= 0) void regenerateFromAssistant(realIdx);
                            }}
                            title="Regenerate response"
                          >
                            <RotateCcw size={12} aria-hidden="true" />
                          </button>
                        </>
                      )}
                    </div>
                  </header>

                  {msg.images && msg.images.length > 0 && (
                    <div className="chat-msg__images">
                      {msg.images.map((src, i) => (
                        <img key={i} src={src} alt={`Attachment ${i + 1}`} />
                      ))}
                    </div>
                  )}

                  {isAssistant && msg.reasoning && (
                    <details className="chat-disclosure" open={streaming && isLatest && !msg.content}>
                      <summary>
                        <Brain size={13} aria-hidden="true" />
                        <span>Thinking ({msg.reasoning.length} chars)</span>
                      </summary>
                      <div className="chat-disclosure__body">
                        <pre className="chat-reasoning">{msg.reasoning}</pre>
                      </div>
                    </details>
                  )}

                  {isAssistant && msg.tool_calls && msg.tool_calls.length > 0 && (
                    <div className="chat-tools-stack">
                      {msg.tool_calls.map((tc) => {
                        const result = toolResultsByCallId[tc.id];
                        const approval = pendingApprovals[tc.id];
                        return (
                          <div key={tc.id} className="chat-tool-card">
                            {approval && (
                              <div className="approval-card" role="alert">
                                <div className="approval-card__head">
                                  <ShieldAlert size={15} aria-hidden="true" />
                                  <strong>
                                    Approval Required: <code>{approval.name}</code> on{" "}
                                    <span className="node-tag">{approval.exec_node}</span>
                                  </strong>
                                </div>
                                <pre className="code-preview">
                                  {JSON.stringify(approval.arguments, null, 2)}
                                </pre>
                                <div className="row-actions">
                                  <button
                                    type="button"
                                    className="btn btn--sm btn--accent"
                                    onClick={() => void resolveApproval(approval, true)}
                                  >
                                    <Check size={12} aria-hidden="true" /> Allow
                                  </button>
                                  <button
                                    type="button"
                                    className="btn btn--sm btn--danger"
                                    onClick={() => void resolveApproval(approval, false)}
                                  >
                                    <X size={12} aria-hidden="true" /> Deny
                                  </button>
                                </div>
                              </div>
                            )}

                            <details className="chat-disclosure" open={Boolean(result?.meta?.diff)}>
                              <summary>
                                <Terminal size={13} aria-hidden="true" />
                                <span>
                                  Tool: <code>{tc.function.name}</code>
                                </span>
                                {result ? (
                                  <span
                                    className={`pill ${result.meta.ok ? "pill--ok" : "pill--err"}`}
                                  >
                                    {result.meta.ok ? "OK" : "ERR"}
                                  </span>
                                ) : approval ? (
                                  <span className="pill pill--warn">WAITING APPROVAL</span>
                                ) : (
                                  <span className="pill">RUNNING…</span>
                                )}
                              </summary>
                              <div className="chat-disclosure__body">
                                <div className="muted">Arguments</div>
                                <pre className="code-preview">{tc.function.arguments}</pre>
                                {result?.meta?.diff && (
                                  <>
                                    <div className="muted">Unified Diff</div>
                                    <DiffViewer diff={result.meta.diff} />
                                  </>
                                )}
                                {tc.function.name === "write_file" &&
                                  (() => {
                                    try {
                                      const a = JSON.parse(tc.function.arguments || "{}") as {
                                        path?: string;
                                        content?: string;
                                      };
                                      const p = String(a.path || "");
                                      const c = String(a.content || "");
                                      const ext = p.split(".").pop()?.toLowerCase() || "";
                                      if (c && isRunnableArtifact(ext, c)) {
                                        return (
                                          <ArtifactPreview
                                            lang={ext || "html"}
                                            code={c}
                                            label={p}
                                          />
                                        );
                                      }
                                    } catch {
                                      // ignore invalid json
                                    }
                                    return null;
                                  })()}
                                {result && (
                                  <>
                                    <div className="muted">Output</div>
                                    <pre className="code-preview">{result.content}</pre>
                                  </>
                                )}
                              </div>
                            </details>
                            {result?.meta?.media_url && (
                              <div className="chat-media-embed">
                                {result.meta.media_kind === "video" ? (
                                  <video
                                    controls
                                    playsInline
                                    preload="metadata"
                                    width={768}
                                    height={512}
                                    src={result.meta.media_url}
                                    className="chat-media-embed__media"
                                  />
                                ) : (
                                  <a
                                    href={result.meta.media_url}
                                    target="_blank"
                                    rel="noreferrer"
                                  >
                                    <img
                                      src={result.meta.media_url}
                                      alt="Generated media"
                                      className="chat-media-embed__media"
                                    />
                                  </a>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {msg.content && (
                    <div className="chat-prose">
                      {isAssistant ? (
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={MARKDOWN_COMPONENTS}
                        >
                          {msg.content}
                        </ReactMarkdown>
                      ) : (
                        <p className="chat-user-text">{msg.content}</p>
                      )}
                    </div>
                  )}
                </article>
                {showCompactedDivider && activeConv?.compacted_summary && (
                  <details className="chat-disclosure chat-compacted-divider">
                    <summary>
                      <Minimize2 size={13} aria-hidden="true" />
                      <span>Messages above compacted into active context summary</span>
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        style={{ marginInlineStart: "auto" }}
                        onClick={(e) => {
                          e.preventDefault();
                          void clearCompaction();
                        }}
                      >
                        Clear Summary
                      </button>
                    </summary>
                    <div className="chat-disclosure__body chat-prose">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {activeConv.compacted_summary}
                      </ReactMarkdown>
                    </div>
                  </details>
                )}
              </div>
              );
            })
          )}
          <div ref={bottomSentinelRef} className="scroll-sentinel" aria-hidden="true" />
          {visibleMessages.length > 0 && (
            <button
              type="button"
              className="btn btn--sm jump-to-bottom"
              onClick={jumpToBottom}
              onBlur={(e) => {
                if (!e.relatedTarget && threadRef.current) {
                  threadRef.current.focus({ preventScroll: true });
                }
              }}
              aria-label="Scroll to latest message"
            >
              <ArrowDown size={13} aria-hidden="true" />
              Latest
            </button>
          )}
        </div>

        <form
          ref={formRef}
          className={`chat-composer ${dragActive ? "chat-composer--drag" : ""}`}
          onSubmit={handleSubmit}
          onDragOver={(e) => {
            e.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={() => setDragActive(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragActive(false);
            if (e.dataTransfer.files.length > 0) {
              void handleFiles(e.dataTransfer.files);
            }
          }}
        >
          {attachedImages.length > 0 && (
            <div className="chat-attachments">
              {attachedImages.map((src, i) => (
                <div key={i} className="chat-attachment">
                  <img src={src} alt={`Upload ${i + 1}`} />
                  <button
                    type="button"
                    className="btn btn--sm btn--ghost"
                    aria-label="Remove attachment"
                    onClick={() => setAttachedImages((prev) => prev.filter((_, idx) => idx !== i))}
                  >
                    <X size={12} aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>
          )}

          <label htmlFor={composerId} className="visually-hidden">
            Message Splitch
          </label>
          <textarea
            ref={textareaRef}
            id={composerId}
            className="chat-composer__input"
            value={composerText}
            onChange={(e) => setComposerText(e.target.value)}
            onKeyDown={handleComposerKeyDown}
            onPaste={handlePaste}
            placeholder="Message…"
            disabled={streaming}
          />

          <div className="chat-composer__bar">
            <div className="row-actions">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                multiple
                hidden
                onChange={(e) => {
                  if (e.target.files) void handleFiles(e.target.files);
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                className="btn btn--sm"
                disabled={streaming}
                onClick={() => fileInputRef.current?.click()}
                title="Attach image for vision models"
              >
                <ImagePlus size={13} aria-hidden="true" />
                Image
              </button>

              <button
                type="button"
                className={`btn btn--sm ${effectiveConfig.web_search_enabled ? "btn--accent" : ""}`}
                aria-pressed={effectiveConfig.web_search_enabled}
                disabled={streaming}
                onClick={() =>
                  updateConfig({ web_search_enabled: !effectiveConfig.web_search_enabled })
                }
                title="Enable web_search and web_fetch tools for live web lookups"
              >
                <Globe size={13} aria-hidden="true" />
                Web Search
              </button>

              <button
                type="button"
                className="btn btn--sm"
                disabled={streaming}
                onClick={openContextDialog}
                title="Configure System Prompt, Skills, and MCP Servers (Per-Chat, Per-Project Workspace, or Global)"
              >
                <SlidersHorizontal size={13} aria-hidden="true" />
                Context
                {effectiveSkillIds.length + effectiveMcpIds.length > 0 && (
                  <span className="pill pill--accent">
                    {effectiveSkillIds.length + effectiveMcpIds.length}
                  </span>
                )}
              </button>

              {activeExternalMcps.length > 0 && (
                <span
                  className="pill pill--warn"
                  title={`External MCP active (${activeExternalMcps.map((m) => m.name).join(", ")}) — tool calls send data outside your local network and pause for approval`}
                >
                  <ShieldAlert size={11} aria-hidden="true" /> Ext MCP ({activeExternalMcps.length})
                </span>
              )}

              <label className="chat-temp-field">
                <span className="muted">Temp</span>
                <input
                  type="number"
                  min={0}
                  max={2}
                  step={0.1}
                  value={temperature}
                  onChange={(e) => setTemperature(e.target.value)}
                  placeholder="auto"
                />
              </label>

              {snapshot?.control.gaming_mode && (
                <span className="pill pill--warn">
                  <AlertTriangle size={11} aria-hidden="true" /> Gaming Mode active
                </span>
              )}
            </div>

            <div className="row-actions">
              {compactNotice && <span className="pill pill--ok">{compactNotice}</span>}
              <label
                className="chat-check"
                title="Automatically summarize older messages when context reaches 80%"
              >
                <input
                  type="checkbox"
                  checked={effectiveConfig.auto_compact}
                  disabled={streaming}
                  onChange={(e) => updateConfig({ auto_compact: e.target.checked })}
                />
                <span>Auto-Compact</span>
              </label>
              <button
                type="button"
                className="btn btn--sm"
                disabled={!activeConv || streaming || compacting || messages.length < 4}
                onClick={() => void compactNow()}
                title="Summarize older turns now without deleting chat history"
              >
                <Minimize2 size={12} aria-hidden="true" />
                {compacting ? "Compacting…" : "Compact"}
              </button>
              <ContextRing
                usedTokens={contextStats.used}
                maxTokens={contextStats.max}
                estimated={contextStats.estimated}
              />
              {streaming ? (
                <button type="button" className="btn btn--danger" onClick={stopGeneration}>
                  <Square size={13} aria-hidden="true" />
                  Stop
                </button>
              ) : (
                <button
                  type="submit"
                  className="btn btn--accent"
                  disabled={!composerText.trim() && attachedImages.length === 0}
                >
                  <Send size={13} aria-hidden="true" />
                  Send
                </button>
              )}
            </div>
          </div>
        </form>
      </section>

      <dialog
        ref={settingsDialogRef}
        {...({ closedby: "any" } as Record<string, string>)}
        aria-label="Chat Context and System Prompt"
      >
        <h2>Context</h2>
        <form className="modal-form" onSubmit={saveContextSettings}>
          <label>
            System Prompt
            <textarea
              rows={3}
              value={settingsPrompt}
              onChange={(e) => setSettingsPrompt(e.target.value)}
              placeholder="Optional system prompt…"
            />
          </label>

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
            <button
              type="button"
              className="btn"
              onClick={() => settingsDialogRef.current?.close()}
            >
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

      <dialog
        ref={wsDialogRef}
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
            {(snapshot?.nodes ?? []).map((n) => (
              <option key={n.node_id} value={n.node_id}>
                {n.display_name} ({n.node_id})
              </option>
            ))}
          </select>
        </div>

        <div className="workspace-browser">
          {wsListing && (
            <div className="workspace-browser__path">
              <span className="muted">Sandbox Root:</span> <code>{wsListing.root}</code>
              <div>
                <span className="muted">Selected:</span>{" "}
                <code>{wsListing.subpath || "."}</code>
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
            <button type="button" className="btn" onClick={() => wsDialogRef.current?.close()}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn--accent"
              disabled={!wsListing}
              onClick={() => {
                if (!wsListing) return;
                updateConfig({
                  exec_node: wsBrowseNode,
                  workspace_subpath: wsListing.subpath
                });
                wsDialogRef.current?.close();
              }}
            >
              Use This Workspace
            </button>
          </div>
        </div>
      </dialog>
    </div>
  );
}
