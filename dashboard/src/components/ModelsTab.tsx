import { useEffect, useId, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowDownWideNarrow,
  ArrowUpNarrowWide,
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Clock,
  Download,
  ExternalLink,
  Flame,
  HardDrive,
  Heart,
  Loader2,
  Lock,
  Play,
  Plus,
  RefreshCw,
  Search,
  Square,
  Tag,
  Trash2,
  X
} from "lucide-react";
import type { ConfirmState } from "./ConfirmDialog";
import { StrataPane } from "./StrataPane";
import type {
  AliasConfig,
  DownloadJob,
  HfGgufFile,
  HfRepoDetails,
  HfRepoResult,
  NodeModelInventory,
  NodeStatus
} from "../types";

type EngineModel = NonNullable<NodeStatus["telemetry"]>["models"][number];

type SortKey =
  | "downloads"
  | "trendingScore"
  | "likes"
  | "lastModified"
  | "createdAt"
  | "gguf_count";

const SORT_LABELS: Record<SortKey, string> = {
  downloads: "Popularity (Downloads)",
  trendingScore: "Trending",
  likes: "Most Liked",
  lastModified: "Recently Updated",
  createdAt: "Newest Released",
  gguf_count: "Most GGUF Quants"
};

function fmtCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function fmtContextLen(ctx: number | null | undefined): string | null {
  if (!ctx || ctx <= 0) return null;
  if (ctx >= 1_048_576) return `${Math.round(ctx / 1_048_576)}M ctx`;
  if (ctx >= 1024) return `${Math.round(ctx / 1024)}K ctx`;
  return `${ctx} ctx`;
}

function nodeBudgetGb(node: NodeStatus | undefined, gpuSelection: string = "all"): number {
  const t = node?.telemetry;
  if (!t) return 0;
  const gpus = t.gpus ?? [];
  if (gpuSelection !== "all" && gpus.length > 0) {
    const idx = Number(gpuSelection);
    const gpu = gpus.find((g) => g.index === idx);
    if (gpu && gpu.vram_total_mb > 0) {
      return Number(((gpu.vram_total_mb / 1024) * 0.88).toFixed(2));
    }
  }
  if (t.vram_total_mb > 0) {
    return Number(((t.vram_total_mb / 1024) * 0.88).toFixed(2));
  }
  if (t.ram_total_mb > 0) {
    return Number(((t.ram_total_mb / 1024) * 0.60).toFixed(2));
  }
  return 0;
}

function nodeStrataBudgetGb(node: NodeStatus | undefined): number {
  const t = node?.telemetry;
  if (!t) return 0;
  const vramGb = t.vram_total_mb / 1024;
  const ramGb = t.ram_total_mb / 1024;
  const ssdFreeGb = t.storage_free_gb;
  const isUnified = node?.role === "unified" || Boolean(t.gpus?.some((g) => g.is_unified));
  if (isUnified) {
    const pool = Math.max(vramGb, ramGb) * 0.85;
    return Number((ssdFreeGb > 0 ? Math.min(pool, Math.max(0, ssdFreeGb - 4)) : pool).toFixed(2));
  }
  if (vramGb >= 16 && ramGb >= 28 && ssdFreeGb > 0) {
    return Number(Math.max(0, ssdFreeGb - 4).toFixed(2));
  }
  const memPool = vramGb + ramGb;
  return Number((ssdFreeGb > 0 ? Math.min(memPool, Math.max(0, ssdFreeGb - 4)) : memPool).toFixed(2));
}

function fmtRelativeDate(iso: string | null): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "—";
  const diffDays = Math.floor((Date.now() - ts) / 86_400_000);
  if (diffDays <= 0) return "today";
  if (diffDays === 1) return "1d ago";
  if (diffDays < 30) return `${diffDays}d ago`;
  const months = Math.floor(diffDays / 30);
  if (months < 12) return `${months}mo ago`;
  return new Date(ts).toLocaleDateString([], { year: "numeric", month: "short" });
}

function splitRepoId(repoId: string): { owner: string; name: string } {
  const idx = repoId.indexOf("/");
  if (idx === -1) return { owner: "", name: repoId };
  return { owner: repoId.slice(0, idx), name: repoId.slice(idx + 1) };
}

function splitExtension(filename: string): { stem: string; ext: string } {
  const idx = filename.lastIndexOf(".");
  if (idx <= 0) return { stem: filename, ext: "" };
  return { stem: filename.slice(0, idx), ext: filename.slice(idx) };
}

function safeExternalHref(href: string | undefined): string | undefined {
  if (!href) return undefined;
  const trimmed = href.trim();
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  return undefined;
}

export function useDialogLightDismiss(ref: React.RefObject<HTMLDialogElement | null>) {
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog || "closedBy" in HTMLDialogElement.prototype) return;
    const outside = (e: PointerEvent) => {
      const r = dialog.getBoundingClientRect();
      return (
        e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom
      );
    };
    let pressedOutside = false;
    const onDown = (e: PointerEvent) => {
      pressedOutside = outside(e);
    };
    const onUp = (e: PointerEvent) => {
      if (pressedOutside && outside(e)) dialog.close("");
    };
    dialog.addEventListener("pointerdown", onDown);
    dialog.addEventListener("pointerup", onUp);
    return () => {
      dialog.removeEventListener("pointerdown", onDown);
      dialog.removeEventListener("pointerup", onUp);
    };
  }, [ref]);
}

function runWithViewTransition(
  container: HTMLElement | null,
  update: () => void
) {
  const scoped = container as (HTMLElement & { startViewTransition?: (cb: () => void) => void }) | null;
  if (scoped?.startViewTransition) {
    scoped.startViewTransition(update);
  } else if (document.startViewTransition) {
    document.startViewTransition(update);
  } else {
    update();
  }
}

export function ModelsTab({
  nodes,
  aliases,
  busyAction,
  onModelAction,
  onStatusRefresh,
  onError,
  onConfirm
}: {
  nodes: NodeStatus[];
  aliases: AliasConfig[];
  busyAction: string | null;
  onModelAction: (nodeId: string, model: string, action: "load" | "unload") => Promise<void>;
  onStatusRefresh: () => Promise<void>;
  onError: (msg: string | null) => void;
  onConfirm: (state: ConfirmState) => void;
}) {
  const [inventory, setInventory] = useState<NodeModelInventory[]>([]);
  const [query, setQuery] = useState("Qwen3-Coder");
  const [searchFormat, setSearchFormat] = useState<"gguf" | "mlx" | "strata">("gguf");
  const [sortBy, setSortBy] = useState<SortKey>("downloads");
  const [sortDesc, setSortDesc] = useState(true);
  const [sortStatusText, setSortStatusText] = useState("");
  const [repos, setRepos] = useState<HfRepoResult[]>([]);
  const [selectedRepo, setSelectedRepo] = useState<string | null>(null);
  const [repoDetailsMap, setRepoDetailsMap] = useState<Record<string, HfRepoDetails>>({});
  const [descExpanded, setDescExpanded] = useState(false);
  const [targetNode, setTargetNode] = useState<string>("");
  const [targetGpu, setTargetGpu] = useState<string>("all");
  const [onlyFitting, setOnlyFitting] = useState<boolean>(false);
  const [fitFilter, setFitFilter] = useState<string>("all");
  const [searching, setSearching] = useState(false);
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [startingPulls, setStartingPulls] = useState<Record<string, boolean>>({});
  const [downloadsOpen, setDownloadsOpen] = useState(true);
  const [dismissedDownloads, setDismissedDownloads] = useState<Record<string, boolean>>({});
  const [downloadNotice, setDownloadNotice] = useState("");
  const prevActiveIdsRef = useRef<Set<string>>(new Set());
  const [assignTarget, setAssignTarget] = useState<{ nodeId: string; modelId: string } | null>(null);
  const [assignSelected, setAssignSelected] = useState<Record<string, boolean>>({});
  const [assignSaving, setAssignSaving] = useState(false);

  const listRef = useRef<HTMLDivElement>(null);
  const aliasDialogRef = useRef<HTMLDialogElement>(null);
  const assignDialogRef = useRef<HTMLDialogElement>(null);
  useDialogLightDismiss(aliasDialogRef);
  useDialogLightDismiss(assignDialogRef);
  const aliasDialogTitleId = useId();
  const assignDialogTitleId = useId();
  const invCaptionId = useId();
  const aliasCaptionId = useId();
  const fitSwitchId = useId();

  useEffect(() => {
    if (typeof HTMLInputElement !== "undefined" && !("switch" in HTMLInputElement.prototype)) {
      void import("input-switch-polyfill");
    }
  }, []);

  const [editAlias, setEditAlias] = useState<AliasConfig>({
    alias: "",
    task_kind: "code",
    primary_node: "",
    primary_model: "",
    fallback_node: null,
    fallback_model: null
  });

  useEffect(() => {
    if (!targetNode && nodes.length > 0) {
      const firstOnline = nodes.find((n) => n.online) ?? nodes[0];
      setTargetNode(firstOnline.node_id);
    }
  }, [nodes, targetNode]);

  const selectedNodeObj = useMemo(
    () => nodes.find((n) => n.node_id === targetNode),
    [nodes, targetNode]
  );
  const selectedNodeGpus = useMemo(
    () => selectedNodeObj?.telemetry?.gpus ?? [],
    [selectedNodeObj]
  );

  useEffect(() => {
    if (
      targetGpu !== "all" &&
      !selectedNodeGpus.some((g) => String(g.index) === targetGpu)
    ) {
      setTargetGpu("all");
    }
  }, [selectedNodeGpus, targetGpu]);

  const [selectedStrataPath, setSelectedStrataPath] = useState("");
  const selectedBudgetGb = useMemo(
    () =>
      searchFormat === "strata"
        ? nodeStrataBudgetGb(selectedNodeObj)
        : nodeBudgetGb(selectedNodeObj, targetGpu),
    [searchFormat, selectedNodeObj, targetGpu]
  );

  const selectedTargetLabel = useMemo(() => {
    if (!selectedNodeObj) return "selected node";
    if (targetGpu !== "all") {
      const g = selectedNodeGpus.find((gpu) => String(gpu.index) === targetGpu);
      if (g) return `${selectedNodeObj.display_name} · GPU ${g.index}`;
    }
    return selectedNodeObj.display_name;
  }, [selectedNodeObj, selectedNodeGpus, targetGpu]);

  const fetchInventory = async () => {
    try {
      const res = await fetch("/api/models/inventory");
      if (res.ok) setInventory(await res.json());
    } catch {
      // ignore transient poll errors
    }
  };

  const hasActiveDownloads = useMemo(
    () => inventory.some((inv) => inv.downloads.some((d) => d.status === "downloading")),
    [inventory]
  );

  useEffect(() => {
    void fetchInventory();
    const timer = window.setInterval(fetchInventory, hasActiveDownloads ? 1500 : 4000);
    return () => window.clearInterval(timer);
  }, [hasActiveDownloads]);

  useEffect(() => {
    const currentActive = new Set<string>();
    const newlyCompleted: DownloadJob[] = [];
    for (const inv of inventory) {
      for (const d of inv.downloads) {
        if (d.status === "downloading") {
          currentActive.add(d.id);
        } else if (d.status === "completed" && prevActiveIdsRef.current.has(d.id)) {
          newlyCompleted.push(d);
        }
      }
    }
    prevActiveIdsRef.current = currentActive;
    if (newlyCompleted.length > 0) {
      const last = newlyCompleted[newlyCompleted.length - 1];
      setDownloadNotice(`Download complete: ${last.filename} on ${last.node_id}`);
      void onStatusRefresh();
    }
  }, [inventory, onStatusRefresh]);

  const executeHfSearch = async (
    q: string,
    sortKey: SortKey,
    desc: boolean,
    fmt: "gguf" | "mlx" | "strata" = searchFormat
  ) => {
    const trimmed = q.trim();
    if (fmt !== "strata" && trimmed.length < 2) return;
    setSearching(true);
    try {
      const serverSort = sortKey === "gguf_count" ? "downloads" : sortKey;
      const dir = desc ? -1 : 1;
      const res = await fetch(
        `/api/models/search?q=${encodeURIComponent(trimmed)}&sort=${encodeURIComponent(serverSort)}&direction=${dir}&limit=18&format=${encodeURIComponent(fmt)}`
      );
      if (!res.ok) throw new Error(`Search failed: HTTP ${res.status}`);
      const items: HfRepoResult[] = await res.json();
      runWithViewTransition(listRef.current, () => setRepos(items));
      setSortStatusText(
        `Sorted by ${SORT_LABELS[sortKey]}, ${desc ? "descending" : "ascending"}, ${items.length} repositories`
      );
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "HF search failed");
    } finally {
      setSearching(false);
    }
  };

  const searchHf = async (e: React.FormEvent) => {
    e.preventDefault();
    setSelectedRepo(null);
    await executeHfSearch(query, sortBy, sortDesc);
  };

  const handleSortChange = (nextSort: SortKey) => {
    setSortBy(nextSort);
    if (repos.length === 0) return;
    if (nextSort === "gguf_count") {
      runWithViewTransition(listRef.current, () => setSortBy(nextSort));
      setSortStatusText(
        `Sorted by ${SORT_LABELS[nextSort]}, ${sortDesc ? "descending" : "ascending"}, ${repos.length} repositories`
      );
    } else {
      void executeHfSearch(query, nextSort, sortDesc);
    }
  };

  const handleDirectionToggle = () => {
    const nextDesc = !sortDesc;
    setSortDesc(nextDesc);
    if (repos.length === 0) return;
    if (sortBy === "gguf_count") {
      runWithViewTransition(listRef.current, () => setSortDesc(nextDesc));
      setSortStatusText(
        `Sorted by ${SORT_LABELS[sortBy]}, ${nextDesc ? "descending" : "ascending"}, ${repos.length} repositories`
      );
    } else {
      void executeHfSearch(query, sortBy, nextDesc);
    }
  };

  const repoMinSizeGb = (repo: HfRepoResult): number | null => {
    const cached = repoDetailsMap[repo.repo_id];
    if (cached && cached.files.length > 0) {
      const weights = cached.files.filter(
        (f) => !/mmproj|imatrix/i.test(f.filename) && f.size_gb > 0
      );
      const list = weights.length > 0 ? weights : cached.files;
      const smallest = Math.min(...list.map((f) => f.size_gb));
      if (Number.isFinite(smallest)) return smallest;
    }
    return repo.est_min_gb ?? repo.est_q4_gb ?? null;
  };

  const repoFitsSelected = (repo: HfRepoResult): boolean | null => {
    if (selectedBudgetGb <= 0) return null;
    const minGb = repoMinSizeGb(repo);
    if (minGb === null) return null;
    return minGb <= selectedBudgetGb;
  };

  const sortedRepos = useMemo(() => {
    const copy = [...repos];
    const dir = sortDesc ? -1 : 1;
    copy.sort((a, b) => {
      switch (sortBy) {
        case "downloads":
          return (a.downloads - b.downloads) * dir;
        case "likes":
          return (a.likes - b.likes) * dir;
        case "trendingScore":
          return (a.trending_score - b.trending_score) * dir;
        case "lastModified":
          return ((Date.parse(a.last_modified || "") || 0) - (Date.parse(b.last_modified || "") || 0)) * dir;
        case "createdAt":
          return ((Date.parse(a.created_at || "") || 0) - (Date.parse(b.created_at || "") || 0)) * dir;
        case "gguf_count":
          return (a.gguf_count - b.gguf_count) * dir;
      }
    });
    return copy;
  }, [repos, sortBy, sortDesc]);

  const visibleRepos = useMemo(() => {
    if (!onlyFitting || selectedBudgetGb <= 0) return sortedRepos;
    return sortedRepos.filter((r) => {
      const fit = repoFitsSelected(r);
      return fit === true;
    });
  }, [sortedRepos, onlyFitting, selectedBudgetGb, repoDetailsMap]);

  const inspectRepo = async (repoId: string) => {
    if (selectedRepo === repoId) {
      setSelectedRepo(null);
      return;
    }
    setSelectedRepo(repoId);
    setDescExpanded(false);
    setFitFilter(onlyFitting ? "selected" : "all");

    if (repoDetailsMap[repoId]) {
      return;
    }
    setLoadingFiles(true);
    try {
      const res = await fetch(
        `/api/models/details?repo_id=${encodeURIComponent(repoId)}&format=${encodeURIComponent(searchFormat)}`
      );
      if (!res.ok) throw new Error(`Model details lookup failed: HTTP ${res.status}`);
      const details: HfRepoDetails = await res.json();
      setRepoDetailsMap((prev) => ({ ...prev, [repoId]: details }));
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to load model details");
    } finally {
      setLoadingFiles(false);
    }
  };

  const handleOnlyFittingToggle = (checked: boolean) => {
    setOnlyFitting(checked);
    setFitFilter(checked ? "selected" : "all");
  };

  const pullGguf = async (
    repoId: string,
    filename: string,
    nodeId: string,
    format: "gguf" | "mlx" = "gguf"
  ) => {
    const pullKey = `${nodeId}:${filename}`;
    setStartingPulls((prev) => ({ ...prev, [pullKey]: true }));
    setDownloadsOpen(true);
    try {
      const res = await fetch("/api/models/pull", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node_id: nodeId, repo_id: repoId, filename, format })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const job: DownloadJob = await res.json();
      setDismissedDownloads((prev) => {
        if (!prev[job.id]) return prev;
        const next = { ...prev };
        delete next[job.id];
        return next;
      });
      setInventory((prev) => {
        const hasNode = prev.some((inv) => inv.node_id === nodeId);
        if (!hasNode) {
          return [...prev, { node_id: nodeId, storage_path: "", files: [], downloads: [job] }];
        }
        return prev.map((inv) =>
          inv.node_id === nodeId
            ? {
                ...inv,
                downloads: [job, ...inv.downloads.filter((d) => d.id !== job.id)]
              }
            : inv
        );
      });
      setDownloadNotice(`Started downloading ${filename} to ${nodeNameById[nodeId] ?? nodeId}`);
      onError(null);
      void fetchInventory();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Download failed");
    } finally {
      setStartingPulls((prev) => {
        const next = { ...prev };
        delete next[pullKey];
        return next;
      });
    }
  };

  const deleteGguf = (nodeId: string, filename: string) => {
    onConfirm({
      title: `Delete GGUF file?`,
      message: `Permanently delete "${filename}" from disk on node "${nodeId}"? This cannot be undone.`,
      confirmLabel: "Delete File",
      danger: true,
      onConfirm: async () => {
        try {
          const res = await fetch(
            `/api/models/${encodeURIComponent(nodeId)}/${encodeURIComponent(filename)}`,
            { method: "DELETE" }
          );
          if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.detail || `HTTP ${res.status}`);
          }
          await fetchInventory();
        } catch (err) {
          onError(err instanceof Error ? err.message : "Delete failed");
        }
      }
    });
  };

  const openAssignAliasesDialog = (nodeId: string, modelId: string) => {
    const isEmbed = /embed|bge-|bge_|nomic|e5-|gte-|mxbai|minilm/i.test(modelId);
    const matching = aliases.filter(
      (a) => a.primary_node === nodeId || a.fallback_node === nodeId
    );
    const initial: Record<string, boolean> = {};
    for (const a of matching) {
      const sameRole = isEmbed ? a.task_kind === "embed" : a.task_kind !== "embed";
      const alreadyBound =
        (a.primary_node === nodeId && a.primary_model === modelId) ||
        (a.fallback_node === nodeId && a.fallback_model === modelId);
      initial[a.alias] = sameRole && !alreadyBound;
    }
    setAssignTarget({ nodeId, modelId });
    setAssignSelected(initial);
    assignDialogRef.current?.showModal();
  };

  const applyAssignAliases = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!assignTarget) return;
    const chosen = aliases.filter((a) => assignSelected[a.alias]);
    if (chosen.length === 0) {
      assignDialogRef.current?.close();
      return;
    }
    setAssignSaving(true);
    try {
      for (const a of chosen) {
        const updated: AliasConfig = {
          ...a,
          primary_model: a.primary_node === assignTarget.nodeId ? assignTarget.modelId : a.primary_model,
          fallback_model:
            a.fallback_node === assignTarget.nodeId ? assignTarget.modelId : a.fallback_model
        };
        const res = await fetch(`/api/aliases/${encodeURIComponent(a.alias)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(updated)
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${res.status}`);
        }
      }
      assignDialogRef.current?.close();
      setAssignTarget(null);
      setDownloadNotice(
        `Updated ${chosen.length} alias${chosen.length > 1 ? "es" : ""} to ${assignTarget.modelId}`
      );
      await onStatusRefresh();
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to update aliases");
    } finally {
      setAssignSaving(false);
    }
  };

  const openAliasDialog = (existing?: AliasConfig) => {
    setEditAlias(
      existing ?? {
        alias: "",
        task_kind: "code",
        primary_node: nodes[0]?.node_id ?? "",
        primary_model: "",
        fallback_node: nodes[1]?.node_id ?? null,
        fallback_model: null
      }
    );
    aliasDialogRef.current?.showModal();
  };

  const saveAlias = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const res = await fetch(`/api/aliases/${encodeURIComponent(editAlias.alias)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editAlias)
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      aliasDialogRef.current?.close();
      await onStatusRefresh();
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Alias save failed");
    }
  };

  const deleteAlias = (name: string) => {
    onConfirm({
      title: `Delete alias "${name}"?`,
      message: `Requests targeting "${name}" will no longer resolve through this routing rule.`,
      confirmLabel: "Delete Alias",
      danger: true,
      onConfirm: async () => {
        try {
          const res = await fetch(`/api/aliases/${encodeURIComponent(name)}`, { method: "DELETE" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          await onStatusRefresh();
        } catch (err) {
          onError(err instanceof Error ? err.message : "Alias delete failed");
        }
      }
    });
  };

  const allDownloads = useMemo(
    () =>
      inventory
        .flatMap((inv) => inv.downloads)
        .sort((a, b) => {
          const rank = (s: DownloadJob["status"]) =>
            s === "downloading" ? 0 : s === "failed" ? 1 : 2;
          return rank(a.status) - rank(b.status);
        }),
    [inventory]
  );
  const visibleDownloads = useMemo(
    () =>
      allDownloads.filter((d) => d.status === "downloading" || !dismissedDownloads[d.id]),
    [allDownloads, dismissedDownloads]
  );
  const activeDownloads = useMemo(
    () => visibleDownloads.filter((d) => d.status === "downloading"),
    [visibleDownloads]
  );
  const finishedDownloadCount = visibleDownloads.length - activeDownloads.length;
  const clearFinishedDownloads = () => {
    setDismissedDownloads((prev) => {
      const next = { ...prev };
      for (const d of visibleDownloads) {
        if (d.status !== "downloading") {
          next[d.id] = true;
        }
      }
      return next;
    });
  };

  const nodeNameById = useMemo(
    () => Object.fromEntries(nodes.map((n) => [n.node_id, n.display_name])),
    [nodes]
  );

  const nodeById = useMemo(
    () => Object.fromEntries(nodes.map((n) => [n.node_id, n])),
    [nodes]
  );

  const onDiskByNode = useMemo(() => {
    const map: Record<string, Set<string>> = {};
    for (const inv of inventory) {
      map[inv.node_id] = new Set(inv.files.map((f) => f.filename.toLowerCase()));
    }
    return map;
  }, [inventory]);

  const findEngineModel = (nodeId: string, filename: string): EngineModel | null => {
    const node = nodeById[nodeId];
    const models = node?.telemetry?.models ?? [];
    if (models.length === 0) return null;
    const base = filename.split("/").pop() ?? filename;
    const stem = base.replace(/\.gguf$/i, "").toLowerCase();
    const baseLower = base.toLowerCase();
    return (
      models.find((m) => {
        const id = m.id.toLowerCase();
        return id === stem || id === baseLower || id.endsWith(`/${stem}`);
      }) ?? null
    );
  };

  const filterGgufList = (files: HfGgufFile[]): HfGgufFile[] => {
    if (fitFilter === "all") return files;
    if (fitFilter === "selected") {
      if (selectedBudgetGb <= 0) return files;
      return files.filter((f) => f.size_gb <= selectedBudgetGb);
    }
    return files.filter((f) => f.fits_nodes.includes(fitFilter));
  };

  return (
    <div className="tab-stack">
      <section className="panel" aria-labelledby={invCaptionId}>
        <div className="panel-head">
          <h2 id={invCaptionId}>
            <HardDrive size={14} aria-hidden="true" /> Node Models
          </h2>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => {
              void fetchInventory();
              void onStatusRefresh();
            }}
          >
            <RefreshCw size={12} aria-hidden="true" /> Refresh
          </button>
        </div>

        <div tabIndex={0} role="region" aria-labelledby={invCaptionId} className="table-wrapper">
          <table className="data-table inventory-table">
            <thead>
              <tr>
                <th scope="col">Node</th>
                <th scope="col">Filename</th>
                <th scope="col">Engine</th>
                <th scope="col">Size</th>
                <th scope="col">Status</th>
                <th scope="col">Location</th>
                <th scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {inventory.flatMap((inv) =>
                inv.files.map((f) => {
                  const engineModel = findEngineModel(inv.node_id, f.filename);
                  const isLoaded = engineModel?.status === "loaded";
                  const isLoading =
                    engineModel?.status === "loading" ||
                    (engineModel != null &&
                      busyAction === `${inv.node_id}:${engineModel.id}:load`);
                  const isUnloading =
                    engineModel != null &&
                    busyAction === `${inv.node_id}:${engineModel.id}:unload`;
                  const loName = f.filename.toLowerCase();
                  const invNode = nodeById[inv.node_id];
                  const engineTag =
                    f.format === "mlx"
                      ? "MLX"
                      : loName.endsWith(".strata")
                        ? "Strata"
                        : invNode?.telemetry?.strata_installed
                          ? "llama.cpp / Strata"
                          : "llama.cpp";

                  return (
                    <tr key={`${inv.node_id}:${f.path}`}>
                      <th scope="row">
                        <span className="node-tag">{inv.node_id}</span>
                      </th>
                      <td>
                        <code>{f.filename}</code>
                      </td>
                      <td>
                        <span className="pill">{engineTag}</span>
                      </td>
                      <td>{f.size_gb} GB</td>
                      <td>
                        {engineModel ? (
                          <span
                            className={`pill ${
                              isLoaded ? "pill--ok" : isLoading ? "pill--accent" : ""
                            }`}
                          >
                            {isLoading ? "Loading…" : isLoaded ? "Loaded" : "Unloaded"}
                          </span>
                        ) : (
                          <span
                            className="pill"
                            title="File is on disk; llama-server indexes .gguf models in its models-dir"
                          >
                            On disk
                          </span>
                        )}
                      </td>
                      <td className="muted">
                        {f.managed ? inv.storage_path : "HF Cache (read-only)"}
                      </td>
                      <td>
                        <div className="row-actions">
                          {engineModel && (
                            <>
                              <button
                                type="button"
                                className={`btn btn--sm ${isLoaded ? "" : "btn--accent"}`}
                                disabled={isLoading || isUnloading}
                                onClick={() =>
                                  void onModelAction(
                                    inv.node_id,
                                    engineModel.id,
                                    isLoaded ? "unload" : "load"
                                  )
                                }
                              >
                                {isLoading || isUnloading ? (
                                  <>
                                    <Loader2 size={12} className="spin" aria-hidden="true" />{" "}
                                    {isUnloading ? "Unloading…" : "Loading…"}
                                  </>
                                ) : isLoaded ? (
                                  <>
                                    <Square size={11} aria-hidden="true" /> Unload
                                  </>
                                ) : (
                                  <>
                                    <Play size={11} aria-hidden="true" /> Load
                                  </>
                                )}
                              </button>
                              <button
                                type="button"
                                className="btn btn--sm"
                                title="Update aliases pointing to this node"
                                onClick={() => openAssignAliasesDialog(inv.node_id, engineModel.id)}
                              >
                                <Tag size={11} aria-hidden="true" /> Aliases
                              </button>
                            </>
                          )}
                          {f.managed ? (
                            <button
                              type="button"
                              className="btn btn--sm btn--danger"
                              onClick={() => deleteGguf(inv.node_id, f.filename)}
                            >
                              <Trash2 size={12} aria-hidden="true" /> Delete
                            </button>
                          ) : (
                            !engineModel && <span className="muted">external</span>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
              {inventory.every((inv) => inv.files.length === 0) && (
                <tr>
                  <td colSpan={7} className="muted empty-cell">
                    No models on disk.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Hugging Face</h2>
          <div className="hf-target-controls">
            <label className="inline-field">
              <span className="muted">Format</span>
              <select
                aria-label="Model format"
                value={searchFormat}
                onChange={(e) => {
                  const nextFmt = e.target.value as "gguf" | "mlx" | "strata";
                  setSearchFormat(nextFmt);
                  setSelectedRepo(null);
                  setRepoDetailsMap({});
                  if (nextFmt === "strata" || repos.length > 0 || query.trim().length >= 2) {
                    void executeHfSearch(query, sortBy, sortDesc, nextFmt);
                  }
                }}
              >
                <option value="gguf">GGUF (llama.cpp)</option>
                <option value="mlx">MLX (Apple Silicon)</option>
                <option value="strata">Strata (Sparse MoE)</option>
              </select>
            </label>

            <label className="inline-field">
              <span className="muted">Node</span>
              <select
                value={targetNode}
                onChange={(e) => {
                  const nextId = e.target.value;
                  setTargetNode(nextId);
                  setTargetGpu("all");
                  const nextNode = nodes.find((n) => n.node_id === nextId);
                  if (nextNode?.telemetry?.engine === "mlx-lm") {
                    setSearchFormat("mlx");
                  }
                }}
              >
                {nodes.map((n) => {
                  const budget = nodeBudgetGb(n, "all");
                  return (
                    <option key={n.node_id} value={n.node_id}>
                      {n.display_name}
                      {budget > 0 ? ` — ${budget.toFixed(1)} GB` : ""}
                      {n.online ? "" : " (offline)"}
                    </option>
                  );
                })}
              </select>
            </label>

            {selectedNodeGpus.length > 1 && (
              <label className="inline-field">
                <span className="muted">GPU</span>
                <select
                  aria-label="Target GPU on selected node"
                  value={targetGpu}
                  onChange={(e) => setTargetGpu(e.target.value)}
                >
                  <option value="all">
                    All {selectedNodeGpus.length} GPUs ({nodeBudgetGb(selectedNodeObj, "all").toFixed(1)} GB)
                  </option>
                  {selectedNodeGpus.map((g) => (
                    <option key={g.index} value={String(g.index)}>
                      GPU {g.index}: {g.name} ({((g.vram_total_mb / 1024) * 0.88).toFixed(1)} GB)
                    </option>
                  ))}
                </select>
              </label>
            )}

            <label className="fit-switch" htmlFor={fitSwitchId}>
              <input
                id={fitSwitchId}
                type="checkbox"
                {...({ switch: "" } as Record<string, string>)}
                checked={onlyFitting}
                disabled={selectedBudgetGb <= 0}
                onChange={(e) => handleOnlyFittingToggle(e.target.checked)}
              />
              <span>
                Only show models that fit
                {selectedBudgetGb > 0 ? ` (≤ ${selectedBudgetGb.toFixed(1)} GB)` : ""}
              </span>
            </label>
          </div>
        </div>

        {searchFormat === "strata" && (
          <StrataPane
            nodes={nodes}
            targetNode={targetNode}
            selectedModelPath={selectedStrataPath}
            onAssignAlias={openAssignAliasesDialog}
            onStatusRefresh={onStatusRefresh}
            onConfirm={onConfirm}
          />
        )}

        <form className="hf-controls" onSubmit={searchHf}>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={
              searchFormat === "strata"
                ? "Filter Strata models on Hugging Face (optional)…"
                : searchFormat === "mlx"
                  ? "Search MLX models on Hugging Face…"
                  : "Search GGUF models…"
            }
            aria-label="Search Hugging Face models"
          />
          <select
            aria-label="Sort repositories by"
            value={sortBy}
            onChange={(e) => handleSortChange(e.target.value as SortKey)}
          >
            {(Object.keys(SORT_LABELS) as SortKey[]).map((key) => (
              <option key={key} value={key}>
                Sort: {SORT_LABELS[key]}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn"
            aria-pressed={!sortDesc}
            aria-label={`Sort order: ${sortDesc ? "descending" : "ascending"}`}
            onClick={handleDirectionToggle}
          >
            {sortDesc ? (
              <>
                <ArrowDownWideNarrow size={14} aria-hidden="true" /> Desc
              </>
            ) : (
              <>
                <ArrowUpNarrowWide size={14} aria-hidden="true" /> Asc
              </>
            )}
          </button>
          <button type="submit" className="btn btn--accent" disabled={searching}>
            <Search size={14} aria-hidden="true" />
            {searching ? "Searching..." : "Search HF"}
          </button>
        </form>

        <p role="status" className="visually-hidden">
          {downloadNotice || sortStatusText}
        </p>

        {sortedRepos.length > 0 && (
          <div className="hf-results">
            <div className="hf-results__meta muted">
              {onlyFitting && selectedBudgetGb > 0 ? (
                <>
                  Showing <strong>{visibleRepos.length}</strong> of {sortedRepos.length} repositories
                  fitting <strong>{selectedTargetLabel}</strong> (≤ {selectedBudgetGb.toFixed(1)} GB)
                </>
              ) : (
                <>{sortedRepos.length} repositories</>
              )}
            </div>

            {visibleRepos.length === 0 ? (
              <div className="hf-empty-fit">
                <p className="muted">
                  None of the {sortedRepos.length} search results have a GGUF quant estimated to fit
                  within {selectedBudgetGb.toFixed(1)} GB on {selectedTargetLabel}.
                </p>
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => handleOnlyFittingToggle(false)}
                >
                  Show all {sortedRepos.length} results
                </button>
              </div>
            ) : (
              <div ref={listRef} className="hf-list">
                {visibleRepos.map((r) => {
                  const { owner, name } = splitRepoId(r.repo_id);
                  const isSelected = selectedRepo === r.repo_id;
                  const fitState = repoFitsSelected(r);
                  const minGb = repoMinSizeGb(r);
                  const ctxLabel = fmtContextLen(r.context_length);
                  const details = repoDetailsMap[r.repo_id];
                  const allFiles = details?.files ?? [];
                  const filteredGgufs = filterGgufList(allFiles);
                  const isLongDesc = (details?.description.length ?? 0) > 650;

                  return (
                    <article
                      key={r.repo_id}
                      className={`repo-slice ${isSelected ? "repo-slice--active" : ""}`}
                    >
                      <button
                        type="button"
                        className="repo-slice__row"
                        aria-expanded={isSelected}
                        onClick={() => void inspectRepo(r.repo_id)}
                      >
                        <span className="repo-slice__chevron" aria-hidden="true">
                          {isSelected ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                        </span>

                        <span className="repo-slice__identity">
                          <span className="repo-slice__title-line">
                            <span className="repo-slice__owner">{owner || "huggingface"}/</span>
                            <strong className="repo-slice__name">{name}</strong>
                          </span>

                          <span className="repo-slice__badges">
                            {r.param_size && <span className="pill pill--accent">{r.param_size}</span>}
                            {minGb !== null && (
                              <span
                                className="pill"
                                title={
                                  r.est_q4_gb && r.est_q4_gb !== minGb
                                    ? `Smallest quant ~${minGb.toFixed(1)} GB · Q4 ~${r.est_q4_gb.toFixed(1)} GB`
                                    : `Estimated GGUF size ~${minGb.toFixed(1)} GB`
                                }
                              >
                                ~{minGb.toFixed(1)} GB+
                              </span>
                            )}
                            {fitState === true && (
                              <span className="pill pill--ok">Fits {selectedTargetLabel}</span>
                            )}
                            {fitState === false && (
                              <span className="pill pill--warn">Too large</span>
                            )}
                            {r.gguf_count > 0 && <span className="pill">{r.gguf_count} GGUFs</span>}
                            {ctxLabel && <span className="pill">{ctxLabel}</span>}
                            {r.license && <span className="pill">{r.license}</span>}
                            {r.capabilities.slice(0, 3).map((cap) => (
                              <span key={cap} className="pill pill--ok">
                                {cap}
                              </span>
                            ))}
                            {r.gated && (
                              <span className="pill pill--warn" title="Gated repo (HF_TOKEN required)">
                                <Lock size={10} aria-hidden="true" /> gated
                              </span>
                            )}
                          </span>
                        </span>

                        <span className="repo-slice__stats">
                          <span className="stat-chip" title="Downloads">
                            <Download size={12} aria-hidden="true" /> {fmtCompact(r.downloads)}
                          </span>
                          <span className="stat-chip" title="Likes">
                            <Heart size={12} aria-hidden="true" /> {fmtCompact(r.likes)}
                          </span>
                          {r.trending_score > 0 && (
                            <span className="stat-chip stat-chip--hot" title="Trending score">
                              <Flame size={12} aria-hidden="true" /> {r.trending_score.toFixed(0)}
                            </span>
                          )}
                          <span className="stat-chip" title={r.last_modified ?? ""}>
                            <Clock size={12} aria-hidden="true" /> {fmtRelativeDate(r.last_modified)}
                          </span>
                        </span>
                      </button>

                      {isSelected && (
                        <div className="repo-slice__drawer">
                          <div className="repo-drawer__bar">
                            <div className="repo-drawer__meta">
                              <a
                                href={`https://huggingface.co/${r.repo_id}`}
                                target="_blank"
                                rel="noreferrer noopener"
                                className="btn btn--sm"
                              >
                                <ExternalLink size={12} aria-hidden="true" /> Hugging Face Card
                              </a>
                              {details?.base_model && (
                                <span className="pill">Base: {details.base_model}</span>
                              )}
                              {(details?.architecture || r.architecture) && (
                                <span className="pill">
                                  Arch: {details?.architecture || r.architecture}
                                </span>
                              )}
                              {fmtContextLen(details?.context_length ?? r.context_length) && (
                                <span className="pill">
                                  {fmtContextLen(details?.context_length ?? r.context_length)}
                                </span>
                              )}
                              {details?.parameters_b && (
                                <span className="pill">{details.parameters_b}B params</span>
                              )}
                            </div>
                            <button
                              type="button"
                              className="btn btn--sm btn--ghost"
                              onClick={() => setSelectedRepo(null)}
                              aria-label="Collapse model details"
                            >
                              <X size={14} aria-hidden="true" />
                            </button>
                          </div>

                          {loadingFiles && !details ? (
                            <p className="muted">
                              Loading description and GGUF files from {r.repo_id}...
                            </p>
                          ) : (
                            <>
                              <div className="repo-desc">
                                <div className="repo-desc__head">
                                  <span className="repo-desc__label">
                                    <BookOpen size={13} aria-hidden="true" /> Model Description
                                  </span>
                                  {isLongDesc && (
                                    <button
                                      type="button"
                                      className="btn btn--sm btn--ghost"
                                      onClick={() => setDescExpanded((v) => !v)}
                                    >
                                      {descExpanded ? "Show less" : "Show more"}
                                    </button>
                                  )}
                                </div>

                                {details?.description ? (
                                  <div
                                    className={`repo-desc__body ${
                                      isLongDesc && !descExpanded ? "repo-desc__body--clamped" : ""
                                    }`}
                                  >
                                    <ReactMarkdown
                                      remarkPlugins={[remarkGfm]}
                                      components={{
                                        a: ({ href, children }) => {
                                          const safe = safeExternalHref(href);
                                          return safe ? (
                                            <a
                                              href={safe}
                                              target="_blank"
                                              rel="noreferrer noopener"
                                            >
                                              {children}
                                            </a>
                                          ) : (
                                            <span>{children}</span>
                                          );
                                        }
                                      }}
                                    >
                                      {details.description}
                                    </ReactMarkdown>
                                    {details.description_truncated && (
                                      <p className="repo-desc__more muted">
                                        Excerpt truncated —{" "}
                                        <a
                                          href={`https://huggingface.co/${r.repo_id}`}
                                          target="_blank"
                                          rel="noreferrer noopener"
                                        >
                                          read full model card on Hugging Face
                                        </a>
                                        .
                                      </p>
                                    )}
                                  </div>
                                ) : (
                                  <p className="muted repo-desc__empty">
                                    No README description available on Hugging Face for this
                                    repository.
                                  </p>
                                )}
                              </div>

                              <div className="gguf-section">
                                <div className="gguf-section__head">
                                  <span className="gguf-section__label">
                                    GGUF Files ({filteredGgufs.length}
                                    {filteredGgufs.length !== allFiles.length
                                      ? ` of ${allFiles.length}`
                                      : ""}
                                    )
                                  </span>
                                  <div
                                    className="filter-pills"
                                    role="group"
                                    aria-label="Filter GGUFs by hardware fit"
                                  >
                                    <button
                                      type="button"
                                      className={`btn btn--sm ${fitFilter === "all" ? "btn--accent" : ""}`}
                                      aria-pressed={fitFilter === "all"}
                                      onClick={() => setFitFilter("all")}
                                    >
                                      All ({allFiles.length})
                                    </button>
                                    {selectedBudgetGb > 0 && (
                                      <button
                                        type="button"
                                        className={`btn btn--sm ${fitFilter === "selected" ? "btn--accent" : ""}`}
                                        aria-pressed={fitFilter === "selected"}
                                        onClick={() => setFitFilter("selected")}
                                      >
                                        Fits {selectedTargetLabel}
                                      </button>
                                    )}
                                    {nodes.map((n) => (
                                      <button
                                        key={n.node_id}
                                        type="button"
                                        className={`btn btn--sm ${fitFilter === n.node_id ? "btn--accent" : ""}`}
                                        aria-pressed={fitFilter === n.node_id}
                                        onClick={() => setFitFilter(n.node_id)}
                                      >
                                        {n.display_name}
                                      </button>
                                    ))}
                                  </div>
                                </div>

                                {filteredGgufs.length === 0 ? (
                                  <p className="muted">
                                    No .gguf files fit the current hardware filter.
                                  </p>
                                ) : (
                                  <ul className="gguf-list">
                                    {filteredGgufs.map((f) => {
                                      const { stem, ext } = splitExtension(f.filename);
                                      const fitsTarget =
                                        selectedBudgetGb > 0 ? f.size_gb <= selectedBudgetGb : null;
                                      return (
                                        <li key={f.filename} className="gguf-slice">
                                          <div className="gguf-slice__main">
                                            <span className="pill gguf-slice__quant">{f.quant}</span>
                                            <code className="filename-ellipsis" title={f.filename}>
                                              <span>{stem}</span>
                                              {ext}
                                            </code>
                                          </div>

                                          <div className="gguf-slice__right">
                                            <span className="pill gguf-slice__size">
                                              {f.size_gb.toFixed(2)} GB
                                            </span>
                                            <div className="gguf-slice__fits">
                                              {f.fits_nodes.map((nid) => {
                                                const isMultiGpu = (
                                                  f.fits_multi_gpu_nodes ?? []
                                                ).includes(nid);
                                                return (
                                                  <span
                                                    key={nid}
                                                    className={`pill ${
                                                      isMultiGpu ? "pill--accent" : "pill--ok"
                                                    }`}
                                                    title={
                                                      isMultiGpu
                                                        ? "Fits across combined multi-GPU VRAM on this node"
                                                        : "Fits on single GPU / unified memory"
                                                    }
                                                  >
                                                    {nodeNameById[nid] ?? nid}
                                                    {isMultiGpu ? " (Multi-GPU)" : ""}
                                                  </span>
                                                );
                                              })}
                                              {fitsTarget === false && (
                                                <span className="pill pill--warn">
                                                  Exceeds {selectedTargetLabel}
                                                </span>
                                              )}
                                            </div>
                                            {(() => {
                                              const baseName = f.filename.split("/").pop() ?? f.filename;
                                              const pullKey = `${targetNode}:${f.filename}`;
                                              const isStarting = Boolean(startingPulls[pullKey]);
                                              const activeJob = allDownloads.find(
                                                (d) =>
                                                  d.node_id === targetNode &&
                                                  (d.filename === f.filename ||
                                                    (d.filename.split("/").pop() ?? d.filename) === baseName) &&
                                                  d.status === "downloading"
                                              );
                                              const isOnDisk = Boolean(
                                                onDiskByNode[targetNode]?.has(baseName.toLowerCase())
                                              );
                                              const engineModel = isOnDisk
                                                ? findEngineModel(targetNode, baseName)
                                                : null;
                                              const isLoaded = engineModel?.status === "loaded";
                                              const actionKey = engineModel
                                                ? `${targetNode}:${engineModel.id}:${isLoaded ? "unload" : "load"}`
                                                : null;
                                              const isActionBusy = Boolean(
                                                actionKey && busyAction === actionKey
                                              );

                                              if (isStarting) {
                                                return (
                                                  <button
                                                    type="button"
                                                    className="btn btn--sm btn--accent"
                                                    disabled
                                                  >
                                                    <Loader2 size={12} className="spin" aria-hidden="true" />{" "}
                                                    Starting…
                                                  </button>
                                                );
                                              }
                                              if (activeJob) {
                                                return (
                                                  <button
                                                    type="button"
                                                    className="btn btn--sm btn--downloading"
                                                    onClick={() => setDownloadsOpen(true)}
                                                    title="View active download in Downloads dock"
                                                  >
                                                    <Loader2 size={12} className="spin" aria-hidden="true" />{" "}
                                                    {activeJob.total_bytes > 0
                                                      ? `${Math.round(activeJob.percent)}%`
                                                      : "Downloading…"}
                                                  </button>
                                                );
                                              }
                                              if (isOnDisk && engineModel) {
                                                return (
                                                  <button
                                                    type="button"
                                                    className={`btn btn--sm ${isLoaded ? "" : "btn--accent"}`}
                                                    disabled={isActionBusy}
                                                    onClick={() =>
                                                      void onModelAction(
                                                        targetNode,
                                                        engineModel.id,
                                                        isLoaded ? "unload" : "load"
                                                      )
                                                    }
                                                  >
                                                    {isActionBusy ? (
                                                      <>
                                                        <Loader2 size={12} className="spin" aria-hidden="true" />{" "}
                                                        {isLoaded ? "Unloading…" : "Loading…"}
                                                      </>
                                                    ) : isLoaded ? (
                                                      <>
                                                        <Square size={11} aria-hidden="true" /> Unload
                                                      </>
                                                    ) : (
                                                      <>
                                                        <Play size={11} aria-hidden="true" /> Load → {targetNode}
                                                      </>
                                                    )}
                                                  </button>
                                                );
                                              }
                                              if (isOnDisk) {
                                                const localFile = (
                                                  inventory.find((inv) => inv.node_id === targetNode)
                                                    ?.files ?? []
                                                ).find(
                                                  (lf) =>
                                                    (lf.filename.split("/").pop() ?? lf.filename).toLowerCase() ===
                                                    baseName.toLowerCase()
                                                );
                                                if (searchFormat === "strata" && localFile) {
                                                  const isSelected =
                                                    selectedStrataPath === localFile.path;
                                                  return (
                                                    <button
                                                      type="button"
                                                      className={`btn btn--sm ${isSelected ? "btn--accent" : ""}`}
                                                      onClick={() =>
                                                        setSelectedStrataPath(
                                                          isSelected ? "" : localFile.path
                                                        )
                                                      }
                                                    >
                                                      <Play size={11} aria-hidden="true" />{" "}
                                                      {isSelected ? "Selected for Strata" : "Use in Strata"}
                                                    </button>
                                                  );
                                                }
                                                return (
                                                  <span
                                                    className="pill pill--ok"
                                                    title={`Already downloaded on ${targetNode}`}
                                                  >
                                                    <Check size={11} aria-hidden="true" /> On {targetNode}
                                                  </span>
                                                );
                                              }
                                              return (
                                                <button
                                                  type="button"
                                                  className="btn btn--sm btn--accent"
                                                  disabled={!targetNode}
                                                  onClick={() =>
                                                    void pullGguf(
                                                      r.repo_id,
                                                      f.filename,
                                                      targetNode,
                                                      f.quant === "MLX" || searchFormat === "mlx"
                                                        ? "mlx"
                                                        : "gguf"
                                                    )
                                                  }
                                                >
                                                  <Download size={12} aria-hidden="true" /> Pull →{" "}
                                                  {targetNode}
                                                </button>
                                              );
                                            })()}
                                          </div>
                                        </li>
                                      );
                                    })}
                                  </ul>
                                )}
                              </div>
                            </>
                          )}
                        </div>
                      )}
                    </article>
                  );
                })}
              </div>
            )}
          </div>
        )}

      </section>

      <section className="panel" aria-labelledby={aliasCaptionId}>
        <div className="panel-head">
          <h2 id={aliasCaptionId}>Aliases</h2>
          <button type="button" className="btn btn--sm btn--accent" onClick={() => openAliasDialog()}>
            <Plus size={12} aria-hidden="true" /> Add Alias
          </button>
        </div>

        <div tabIndex={0} role="region" aria-labelledby={aliasCaptionId} className="table-wrapper">
          <table className="data-table aliases-edit-table">
            <thead>
              <tr>
                <th scope="col">Alias</th>
                <th scope="col">Kind</th>
                <th scope="col">Primary Target</th>
                <th scope="col">Fallback Target</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {aliases.map((a) => (
                <tr key={a.alias}>
                  <th scope="row">
                    <code>{a.alias}</code>
                  </th>
                  <td>{a.task_kind}</td>
                  <td>
                    <span className="node-tag">{a.primary_node}</span> <code>{a.primary_model}</code>
                  </td>
                  <td>
                    {a.fallback_node ? (
                      <>
                        <span className="node-tag">{a.fallback_node}</span>{" "}
                        <code>{a.fallback_model}</code>
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>
                    <div className="row-actions">
                      <button type="button" className="btn btn--sm" onClick={() => openAliasDialog(a)}>
                        Edit
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm btn--danger"
                        onClick={() => deleteAlias(a.alias)}
                      >
                        <Trash2 size={12} aria-hidden="true" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <dialog
        ref={aliasDialogRef}
        aria-labelledby={aliasDialogTitleId}
        {...({ closedby: "any" } as Record<string, string>)}
      >
        <h2 id={aliasDialogTitleId}>Configure Routing Alias</h2>
        <form className="modal-form" onSubmit={saveAlias}>
          <label>
            Alias Name
            <input
              required
              autoFocus
              value={editAlias.alias}
              onChange={(e) => setEditAlias({ ...editAlias, alias: e.target.value })}
              placeholder="e.g. heavy, fast, fim"
            />
          </label>
          <label>
            Task Kind
            <select
              value={editAlias.task_kind}
              onChange={(e) =>
                setEditAlias({
                  ...editAlias,
                  task_kind: e.target.value as AliasConfig["task_kind"]
                })
              }
            >
              <option value="code">code</option>
              <option value="chat">chat</option>
              <option value="fim">fim</option>
              <option value="embed">embed</option>
            </select>
          </label>
          <div className="two-col">
            <label>
              Primary Node
              <select
                value={editAlias.primary_node}
                onChange={(e) => setEditAlias({ ...editAlias, primary_node: e.target.value })}
              >
                {nodes.map((n) => (
                  <option key={n.node_id} value={n.node_id}>
                    {n.display_name} ({n.node_id})
                  </option>
                ))}
              </select>
            </label>
            <label>
              Primary Model ID
              <input
                required
                value={editAlias.primary_model}
                onChange={(e) => setEditAlias({ ...editAlias, primary_model: e.target.value })}
                placeholder="e.g. qwen3-coder:30b"
              />
            </label>
          </div>
          <div className="two-col">
            <label>
              Fallback Node
              <select
                value={editAlias.fallback_node ?? ""}
                onChange={(e) =>
                  setEditAlias({ ...editAlias, fallback_node: e.target.value || null })
                }
              >
                <option value="">None</option>
                {nodes.map((n) => (
                  <option key={n.node_id} value={n.node_id}>
                    {n.display_name} ({n.node_id})
                  </option>
                ))}
              </select>
            </label>
            <label>
              Fallback Model ID
              <input
                value={editAlias.fallback_model ?? ""}
                onChange={(e) =>
                  setEditAlias({ ...editAlias, fallback_model: e.target.value || null })
                }
                placeholder="e.g. qwen3-8b"
              />
            </label>
          </div>
          <div className="modal-actions">
            <button
              type="button"
              className="btn"
              onClick={() => aliasDialogRef.current?.close()}
            >
              Cancel
            </button>
            <button type="submit" className="btn btn--accent">
              Save Alias
            </button>
          </div>
        </form>
      </dialog>
      {visibleDownloads.length > 0 && (
        <aside
          className={`downloads-dock ${downloadsOpen ? "downloads-dock--open" : "downloads-dock--collapsed"}`}
          aria-label="Model downloads"
        >
          <div className="downloads-dock__header">
            <button
              type="button"
              className="downloads-dock__toggle"
              aria-expanded={downloadsOpen}
              onClick={() => setDownloadsOpen((v) => !v)}
            >
              {activeDownloads.length > 0 ? (
                <Loader2 size={14} className="spin" aria-hidden="true" />
              ) : (
                <Download size={14} aria-hidden="true" />
              )}
              <span className="downloads-dock__title">
                {activeDownloads.length > 0
                  ? `Downloading ${activeDownloads.length} model${activeDownloads.length > 1 ? "s" : ""}`
                  : `Downloads (${visibleDownloads.length})`}
              </span>
              {activeDownloads.length > 0 && !downloadsOpen && (
                <span className="pill pill--accent">
                  {Math.round(
                    activeDownloads.reduce((sum, d) => sum + d.percent, 0) / activeDownloads.length
                  )}
                  %
                  {(() => {
                    const totalSpeed = activeDownloads.reduce(
                      (sum, d) => sum + (d.speed_bytes_per_sec ?? 0),
                      0
                    );
                    if (totalSpeed <= 0) return "";
                    return totalSpeed >= 1024 * 1024
                      ? ` · ${(totalSpeed / (1024 * 1024)).toFixed(1)} MB/s`
                      : ` · ${Math.round(totalSpeed / 1024)} KB/s`;
                  })()}
                </span>
              )}
              {downloadsOpen ? (
                <ChevronDown size={14} aria-hidden="true" />
              ) : (
                <ChevronUp size={14} aria-hidden="true" />
              )}
            </button>
            {finishedDownloadCount > 0 && downloadsOpen && (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={clearFinishedDownloads}
              >
                Clear done
              </button>
            )}
          </div>

          {downloadsOpen && (
            <div className="downloads-dock__list">
              {visibleDownloads.map((d) => {
                const isRunning = d.status === "downloading";
                const isIndeterminate = isRunning && d.total_bytes <= 0;
                const engineModel =
                  d.status === "completed" ? findEngineModel(d.node_id, d.filename) : null;
                const isLoaded = engineModel?.status === "loaded";
                const isBusy =
                  engineModel != null &&
                  busyAction === `${d.node_id}:${engineModel.id}:${isLoaded ? "unload" : "load"}`;

                return (
                  <div key={d.id} className="downloads-dock__row">
                    <div className="downloads-dock__row-top">
                      <span className="downloads-dock__file" title={d.filename}>
                        <span className="node-tag">{d.node_id}</span>
                        <code>{d.filename.split("/").pop() ?? d.filename}</code>
                      </span>
                      <span className="downloads-dock__status">
                        {isRunning ? (
                          d.total_bytes > 0 ? (
                            <>
                              <strong>{d.percent.toFixed(1)}%</strong>{" "}
                              <span className="muted">
                                ({(d.downloaded_bytes / 1024 ** 3).toFixed(2)} /{" "}
                                {(d.total_bytes / 1024 ** 3).toFixed(2)} GB)
                                {(d.speed_bytes_per_sec ?? 0) > 0 && (
                                  <>
                                    {" · "}
                                    {(d.speed_bytes_per_sec ?? 0) >= 1024 * 1024
                                      ? `${((d.speed_bytes_per_sec ?? 0) / (1024 * 1024)).toFixed(1)} MB/s`
                                      : `${Math.round((d.speed_bytes_per_sec ?? 0) / 1024)} KB/s`}
                                  </>
                                )}
                                {d.protocol && ` · ${d.protocol.toUpperCase()}`}
                              </span>
                            </>
                          ) : (
                            <span className="muted">Starting…</span>
                          )
                        ) : d.status === "completed" ? (
                          <span className="pill pill--ok">
                            <Check size={10} aria-hidden="true" /> Ready
                          </span>
                        ) : (
                          <span className="pill pill--warn">Failed</span>
                        )}
                      </span>
                    </div>

                    <progress
                      className="downloads-dock__progress"
                      max={100}
                      value={isIndeterminate ? undefined : d.percent}
                      aria-label={`Download progress for ${d.filename}`}
                    />

                    {d.error && <small className="text-err">{d.error}</small>}

                    {d.status === "completed" && engineModel && (
                      <div className="downloads-dock__actions">
                        <button
                          type="button"
                          className="btn btn--sm"
                          onClick={() => openAssignAliasesDialog(d.node_id, engineModel.id)}
                        >
                          <Tag size={11} aria-hidden="true" /> Update Aliases…
                        </button>
                        <button
                          type="button"
                          className={`btn btn--sm ${isLoaded ? "" : "btn--accent"}`}
                          disabled={isBusy}
                          onClick={() =>
                            void onModelAction(
                              d.node_id,
                              engineModel.id,
                              isLoaded ? "unload" : "load"
                            )
                          }
                        >
                          {isBusy ? (
                            <>
                              <Loader2 size={12} className="spin" aria-hidden="true" />{" "}
                              {isLoaded ? "Unloading…" : "Loading…"}
                            </>
                          ) : isLoaded ? (
                            <>
                              <Square size={11} aria-hidden="true" /> Unload
                            </>
                          ) : (
                            <>
                              <Play size={11} aria-hidden="true" /> Load on {d.node_id}
                            </>
                          )}
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </aside>
      )}
      <dialog
        ref={assignDialogRef}
        aria-labelledby={assignDialogTitleId}
        {...({ closedby: "any" } as Record<string, string>)}
      >
        <h2 id={assignDialogTitleId}>Update Aliases for {assignTarget?.nodeId}</h2>
        {assignTarget && (
          <form className="modal-form" onSubmit={applyAssignAliases}>
            <p className="muted" style={{ margin: 0 }}>
              Choose which aliases targeting <strong>{assignTarget.nodeId}</strong> should point to{" "}
              <code>{assignTarget.modelId}</code>:
            </p>
            <div className="assign-alias-list">
              {aliases
                .filter(
                  (a) =>
                    a.primary_node === assignTarget.nodeId ||
                    a.fallback_node === assignTarget.nodeId
                )
                .map((a) => {
                  const isPrimary = a.primary_node === assignTarget.nodeId;
                  const currentModel = isPrimary ? a.primary_model : a.fallback_model;
                  return (
                    <label key={a.alias} className="assign-alias-row">
                      <input
                        type="checkbox"
                        checked={Boolean(assignSelected[a.alias])}
                        onChange={(e) =>
                          setAssignSelected((prev) => ({ ...prev, [a.alias]: e.target.checked }))
                        }
                      />
                      <span className="assign-alias-row__info">
                        <span>
                          <code>{a.alias}</code>{" "}
                          <span className="pill">{a.task_kind}</span>{" "}
                          <span className="muted">({isPrimary ? "primary" : "fallback"})</span>
                        </span>
                        <span className="muted" style={{ fontSize: "0.75rem" }}>
                          Currently: <code>{currentModel || "none"}</code>
                        </span>
                      </span>
                    </label>
                  );
                })}
              {aliases.every(
                (a) =>
                  a.primary_node !== assignTarget.nodeId &&
                  a.fallback_node !== assignTarget.nodeId
              ) && (
                <p className="muted" style={{ margin: 0 }}>
                  No existing aliases target {assignTarget.nodeId}. Use{" "}
                  <strong>Add Alias</strong> to create one.
                </p>
              )}
            </div>
            <div className="modal-actions">
              <button
                type="button"
                className="btn"
                onClick={() => assignDialogRef.current?.close()}
              >
                Cancel
              </button>
              <button type="submit" className="btn btn--accent" disabled={assignSaving}>
                {assignSaving ? "Updating…" : "Update Selected"}
              </button>
            </div>
          </form>
        )}
      </dialog>
    </div>
  );
}
