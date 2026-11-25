import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Dices,
  Download,
  ExternalLink,
  Film,
  Image as ImageIcon,
  Loader2,
  Lock,
  Maximize2,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Settings,
  Sparkles,
  Square,
  Trash2,
  Upload,
  X
} from "lucide-react";
import type { ConfirmState } from "./ConfirmDialog";
import { useDialogLightDismiss } from "./ModelsTab";
import type {
  MediaBackendNode,
  MediaHfFile,
  MediaHfRepoResult,
  MediaJob,
  MediaKind,
  NodeMediaEngineStatus
} from "../types";

interface Props {
  onError: (msg: string | null) => void;
  onConfirm: (cfg: ConfirmState) => void;
}

const IMAGE_PRESETS = [
  { label: "1:1", width: 1024, height: 1024 },
  { label: "16:9", width: 1344, height: 768 },
  { label: "9:16", width: 768, height: 1344 },
  { label: "4:3", width: 1152, height: 896 },
  { label: "3:2", width: 1216, height: 832 },
  { label: "512", width: 512, height: 512 }
] as const;

const VIDEO_PRESETS = [
  { label: "16:9", width: 832, height: 480 },
  { label: "3:2", width: 768, height: 512 },
  { label: "1:1", width: 512, height: 512 },
  { label: "9:16", width: 480, height: 832 }
] as const;

interface StarterMediaBundle {
  id: string;
  kind: MediaKind;
  label: string;
  hint: string;
  files: Array<{ repo_id: string; filename: string }>;
}

const STARTER_MEDIA_BUNDLES: readonly StarterMediaBundle[] = [
  {
    id: "z-image-turbo",
    kind: "image",
    label: "Z-Image-Turbo (Q4)",
    hint: "~7.5 GB · 4–8 steps · Apache-2.0",
    files: [
      { repo_id: "unsloth/Z-Image-Turbo-GGUF", filename: "z-image-turbo-Q4_K_M.gguf" },
      { repo_id: "unsloth/Qwen3-4B-GGUF", filename: "Qwen3-4B-Q4_K_M.gguf" },
      { repo_id: "Comfy-Org/z_image_turbo", filename: "split_files/vae/ae.safetensors" }
    ]
  },
  {
    id: "flux-schnell",
    kind: "image",
    label: "FLUX.1-schnell (Q4)",
    hint: "~10 GB · 4 steps · Apache-2.0",
    files: [
      { repo_id: "city96/FLUX.1-schnell-gguf", filename: "flux1-schnell-Q4_K_S.gguf" },
      { repo_id: "city96/t5-v1_1-xxl-encoder-gguf", filename: "t5-v1_1-xxl-encoder-Q4_K_M.gguf" },
      { repo_id: "comfyanonymous/flux_text_encoders", filename: "clip_l.safetensors" },
      { repo_id: "Comfy-Org/z_image_turbo", filename: "split_files/vae/ae.safetensors" }
    ]
  },
  {
    id: "sdxl-turbo",
    kind: "image",
    label: "SDXL Turbo FP16",
    hint: "6.9 GB · single file · 4 steps",
    files: [
      { repo_id: "stabilityai/sdxl-turbo", filename: "sd_xl_turbo_1.0_fp16.safetensors" }
    ]
  },
  {
    id: "wan21-1.3b",
    kind: "video",
    label: "Wan 2.1 T2V 1.3B",
    hint: "~9.8 GB · fast 480p video",
    files: [
      {
        repo_id: "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        filename: "split_files/diffusion_models/wan2.1_t2v_1.3B_bf16.safetensors"
      },
      {
        repo_id: "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        filename: "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
      },
      {
        repo_id: "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        filename: "split_files/vae/wan_2.1_vae.safetensors"
      }
    ]
  },
  {
    id: "wan21-14b",
    kind: "video",
    label: "Wan 2.1 T2V 14B (Q4)",
    hint: "~16.5 GB · 24 GB VRAM",
    files: [
      { repo_id: "city96/Wan2.1-T2V-14B-gguf", filename: "wan2.1-t2v-14b-Q4_K_M.gguf" },
      {
        repo_id: "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        filename: "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
      },
      {
        repo_id: "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        filename: "split_files/vae/wan_2.1_vae.safetensors"
      }
    ]
  }
];

function fileBasename(path: string): string {
  return path.split("/").pop() ?? path;
}

function fmtDuration(ms: number | null): string {
  if (ms === null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

export function MediaTab({ onError, onConfirm }: Props) {
  const [backends, setBackends] = useState<MediaBackendNode[]>([]);
  const [jobs, setJobs] = useState<MediaJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const [setupNodeId, setSetupNodeId] = useState("");
  const [engineStatus, setEngineStatus] = useState<NodeMediaEngineStatus | null>(null);
  const [selectedStartModel, setSelectedStartModel] = useState("");
  const [engineBusy, setEngineBusy] = useState<string | null>(null);

  const [hfQuery, setHfQuery] = useState("");
  const [hfKind, setHfKind] = useState<MediaKind>("image");
  const [hfSearching, setHfSearching] = useState(false);
  const [hfRepos, setHfRepos] = useState<MediaHfRepoResult[]>([]);
  const [hfExpandedRepo, setHfExpandedRepo] = useState<string | null>(null);
  const [hfFilesMap, setHfFilesMap] = useState<Record<string, MediaHfFile[]>>({});
  const [hfLoadingFiles, setHfLoadingFiles] = useState(false);

  const [kind, setKind] = useState<MediaKind>("image");
  const [nodeId, setNodeId] = useState("auto");
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [negativePrompt, setNegativePrompt] = useState("");
  const [width, setWidth] = useState(1024);
  const [height, setHeight] = useState(1024);
  const [steps, setSteps] = useState(20);
  const [cfgScale, setCfgScale] = useState(7.0);
  const [seedText, setSeedText] = useState("");
  const [frames, setFrames] = useState(16);
  const [fps, setFps] = useState(8);
  const [strength, setStrength] = useState(0.7);
  const [initImage, setInitImage] = useState<string | null>(null);
  const [evictLlmVram, setEvictLlmVram] = useState(true);
  const [workflowJson, setWorkflowJson] = useState("");

  const [galleryFilter, setGalleryFilter] = useState<"all" | MediaKind>("all");
  const [viewingJob, setViewingJob] = useState<MediaJob | null>(null);
  const viewerDialogRef = useRef<HTMLDialogElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  useDialogLightDismiss(viewerDialogRef);

  const viewerTitleId = useId();
  const studioHeadingId = useId();

  const refreshEngineStatus = useCallback(async (targetNode: string) => {
    if (!targetNode) return;
    try {
      const res = await fetch(`/api/media/nodes/${encodeURIComponent(targetNode)}/engine`);
      if (res.ok) {
        const st = (await res.json()) as NodeMediaEngineStatus;
        setEngineStatus(st);
        setSelectedStartModel((prev) => {
          if (prev && st.local_media_models.some((m) => m.filename === prev)) return prev;
          if (st.sd_running_model) return st.sd_running_model;
          const preferred =
            st.local_media_models.find((m) => m.filename.endsWith(".safetensors")) ??
            st.local_media_models[0];
          return preferred ? preferred.filename : "";
        });
      }
    } catch {
      // ignore transient poll errors
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [bRes, jRes] = await Promise.all([
        fetch("/api/media/backends"),
        fetch("/api/media/jobs")
      ]);
      if (bRes.ok) {
        const list = (await bRes.json()) as MediaBackendNode[];
        setBackends(list);
        setSetupNodeId((prev) => {
          if (prev && list.some((b) => b.node_id === prev && b.online)) return prev;
          const onlineGpu = list.find((b) => b.online && b.role === "gpu");
          const anyOnline = list.find((b) => b.online);
          return onlineGpu?.node_id ?? anyOnline?.node_id ?? "";
        });
      }
      if (jRes.ok) {
        setJobs((await jRes.json()) as MediaJob[]);
      }
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to load media state");
    }
  }, [onError]);

  useEffect(() => {
    setLoading(true);
    void refresh().finally(() => setLoading(false));
  }, [refresh]);

  useEffect(() => {
    if (setupNodeId) {
      void refreshEngineStatus(setupNodeId);
    }
  }, [setupNodeId, refreshEngineStatus]);

  const activeJobs = useMemo(
    () =>
      jobs.filter(
        (j) => j.status === "queued" || j.status === "evicting_vram" || j.status === "running"
      ),
    [jobs]
  );

  const hasActiveEngineWork = Boolean(
    engineStatus?.install_in_progress ||
      engineStatus?.active_downloads.some((d) => d.status === "downloading")
  );

  useEffect(() => {
    if (activeJobs.length === 0 && !hasActiveEngineWork) return;
    const id = window.setInterval(() => {
      void refresh();
      if (setupNodeId) void refreshEngineStatus(setupNodeId);
    }, 1500);
    return () => window.clearInterval(id);
  }, [activeJobs.length, hasActiveEngineWork, refresh, refreshEngineStatus, setupNodeId]);

  const healthyBackends = useMemo(
    () => backends.filter((b) => b.online && b.media_healthy),
    [backends]
  );

  const availableModels = useMemo(() => {
    const selected =
      nodeId === "auto"
        ? healthyBackends
        : healthyBackends.filter((b) => b.node_id === nodeId);
    const out: string[] = [];
    for (const b of selected) {
      for (const m of b.media_models) {
        if (!out.includes(m)) out.push(m);
      }
    }
    return out;
  }, [healthyBackends, nodeId]);

  const handleInstallSd = async () => {
    if (!setupNodeId) return;
    setEngineBusy("install");
    onError(null);
    try {
      const res = await fetch(`/api/media/nodes/${encodeURIComponent(setupNodeId)}/install`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ backend: "auto", force: false })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setEngineStatus((await res.json()) as NodeMediaEngineStatus);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to install sd-server");
    } finally {
      setEngineBusy(null);
    }
  };

  const handlePullMediaFile = async (repoId: string, filename: string) => {
    if (!setupNodeId) return;
    setEngineBusy(`pull:${filename}`);
    onError(null);
    try {
      const res = await fetch(`/api/media/nodes/${encodeURIComponent(setupNodeId)}/pull`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node_id: setupNodeId, repo_id: repoId, filename })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      await refreshEngineStatus(setupNodeId);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Model pull failed");
    } finally {
      setEngineBusy(null);
    }
  };

  const handlePullBundle = async (bundle: StarterMediaBundle) => {
    if (!setupNodeId) return;
    setEngineBusy(`bundle:${bundle.id}`);
    onError(null);
    try {
      const present = new Set(
        (engineStatus?.local_media_models ?? []).map((f) => f.filename.toLowerCase())
      );
      const pulling = new Set(
        (engineStatus?.active_downloads ?? [])
          .filter((d) => d.status === "downloading")
          .map((d) => fileBasename(d.filename).toLowerCase())
      );
      for (const file of bundle.files) {
        const base = fileBasename(file.filename).toLowerCase();
        if (present.has(base) || pulling.has(base)) continue;
        const res = await fetch(`/api/media/nodes/${encodeURIComponent(setupNodeId)}/pull`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            node_id: setupNodeId,
            repo_id: file.repo_id,
            filename: file.filename
          })
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${res.status}`);
        }
      }
      await refreshEngineStatus(setupNodeId);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Bundle pull failed");
    } finally {
      setEngineBusy(null);
    }
  };

  const searchHfMedia = async (qOverride?: string, kindOverride?: MediaKind) => {
    const q = qOverride ?? hfQuery;
    const k = kindOverride ?? hfKind;
    setHfSearching(true);
    setHfExpandedRepo(null);
    onError(null);
    try {
      const res = await fetch(
        `/api/media/hf/search?q=${encodeURIComponent(q)}&kind=${encodeURIComponent(k)}&limit=14`
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setHfRepos((await res.json()) as MediaHfRepoResult[]);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Hugging Face media search failed");
    } finally {
      setHfSearching(false);
    }
  };

  const toggleHfRepoFiles = async (repoId: string) => {
    if (hfExpandedRepo === repoId) {
      setHfExpandedRepo(null);
      return;
    }
    setHfExpandedRepo(repoId);
    if (hfFilesMap[repoId]) return;
    setHfLoadingFiles(true);
    try {
      const res = await fetch(`/api/media/hf/files?repo_id=${encodeURIComponent(repoId)}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const files = (await res.json()) as MediaHfFile[];
      setHfFilesMap((prev) => ({ ...prev, [repoId]: files }));
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to load repo files");
    } finally {
      setHfLoadingFiles(false);
    }
  };

  const handleStartSd = async () => {
    if (!setupNodeId || !selectedStartModel) return;
    setEngineBusy("start");
    onError(null);
    try {
      const res = await fetch(`/api/media/nodes/${encodeURIComponent(setupNodeId)}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_file: selectedStartModel, port: 8082 })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setEngineStatus((await res.json()) as NodeMediaEngineStatus);
      await refresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to start sd-server");
    } finally {
      setEngineBusy(null);
    }
  };

  const handleStopSd = async () => {
    if (!setupNodeId) return;
    setEngineBusy("stop");
    onError(null);
    try {
      const res = await fetch(`/api/media/nodes/${encodeURIComponent(setupNodeId)}/stop`, {
        method: "POST"
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setEngineStatus((await res.json()) as NodeMediaEngineStatus);
      await refresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to stop sd-server");
    } finally {
      setEngineBusy(null);
    }
  };

  const switchKind = (next: MediaKind) => {
    setKind(next);
    if (next === "video") {
      setWidth(768);
      setHeight(512);
      setCfgScale(6.0);
    } else {
      setWidth(1024);
      setHeight(1024);
      setCfgScale(7.0);
    }
  };

  const handleInitImageFile = (file: File | null) => {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      onError("Reference upload must be an image file (PNG, JPEG, WebP).");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") {
        setInitImage(reader.result);
      }
    };
    reader.readAsDataURL(file);
  };

  const handleGenerate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!prompt.trim()) return;
    setSubmitting(true);
    onError(null);
    try {
      const parsedSeed = seedText.trim() ? Number.parseInt(seedText.trim(), 10) : null;
      const res = await fetch("/api/media/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind,
          prompt: prompt.trim(),
          negative_prompt: negativePrompt.trim(),
          node_id: nodeId,
          model: model.trim(),
          width,
          height,
          steps,
          cfg_scale: cfgScale,
          seed: Number.isFinite(parsedSeed) ? parsedSeed : null,
          frames,
          fps,
          init_image: initImage,
          strength,
          evict_llm_vram: evictLlmVram,
          workflow_json: workflowJson.trim() || null
        })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      await refresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Media generation request failed");
    } finally {
      setSubmitting(false);
    }
  };

  const openViewer = (job: MediaJob) => {
    setViewingJob(job);
    window.setTimeout(() => viewerDialogRef.current?.showModal(), 0);
  };

  const reuseParams = (job: MediaJob) => {
    setKind(job.kind);
    setPrompt(job.prompt);
    setNegativePrompt(job.negative_prompt);
    setWidth(job.width);
    setHeight(job.height);
    setSteps(job.steps);
    setCfgScale(job.cfg_scale);
    setSeedText(job.seed !== null ? String(job.seed) : "");
    setFrames(job.frames);
    setFps(job.fps);
    if (job.model) setModel(job.model);
    if (job.node_id) setNodeId(job.node_id);
  };

  const requestDeleteJob = (job: MediaJob) => {
    onConfirm({
      title: `Delete generated ${job.kind}?`,
      message: `This permanently deletes "${job.filename ?? job.id}" from Splitch's local media storage.`,
      confirmLabel: "Delete",
      danger: true,
      onConfirm: async () => {
        const res = await fetch(`/api/media/jobs/${encodeURIComponent(job.id)}`, {
          method: "DELETE"
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        if (viewingJob?.id === job.id) {
          viewerDialogRef.current?.close();
          setViewingJob(null);
        }
        await refresh();
      }
    });
  };

  const galleryJobs = useMemo(
    () =>
      jobs.filter((j) => {
        if (j.status !== "completed" && j.status !== "error") return false;
        if (galleryFilter !== "all" && j.kind !== galleryFilter) return false;
        return true;
      }),
    [jobs, galleryFilter]
  );

  const presets = kind === "video" ? VIDEO_PRESETS : IMAGE_PRESETS;
  const activeDownloads = (engineStatus?.active_downloads ?? []).filter(
    (d) => d.status === "downloading"
  );

  return (
    <div className="tab-stack">
      <section className="panel" aria-labelledby={studioHeadingId}>
        <div className="panel-head">
          <h2 id={studioHeadingId}>
            <Sparkles size={14} aria-hidden="true" /> Media Studio
          </h2>
          <div className="row-actions">
            {healthyBackends.length > 0 ? (
              healthyBackends.map((b) => (
                <span key={b.node_id} className="pill pill--ok" title={b.media_url}>
                  {b.display_name}: {b.media_engine}
                </span>
              ))
            ) : (
              <span className="pill pill--warn">NO ENGINE</span>
            )}
            <button
              type="button"
              className="btn btn--sm"
              disabled={loading}
              onClick={() => {
                void refresh();
                if (setupNodeId) void refreshEngineStatus(setupNodeId);
              }}
            >
              <RefreshCw size={12} aria-hidden="true" /> Refresh
            </button>
          </div>
        </div>

        <details
          className="chat-disclosure media-engine-manager"
          open={healthyBackends.length === 0}
        >
          <summary>
            <Settings size={13} aria-hidden="true" />
            <span>Media Engine</span>
            {engineStatus?.media_healthy ? (
              <span className="pill pill--ok">
                {engineStatus.media_engine.toUpperCase()}
              </span>
            ) : engineStatus?.install_in_progress ? (
              <span className="pill pill--warn">INSTALLING…</span>
            ) : engineStatus?.sd_installed ? (
              <span className="pill pill--accent">STOPPED</span>
            ) : (
              <span className="pill">NOT INSTALLED</span>
            )}
          </summary>

          <div className="chat-disclosure__body">
            <div className="media-engine-row">
              <label className="inline-field">
                <span className="muted">Node</span>
                <select
                  value={setupNodeId}
                  onChange={(e) => setSetupNodeId(e.target.value)}
                >
                  {backends
                    .filter((b) => b.online)
                    .map((b) => (
                      <option key={b.node_id} value={b.node_id}>
                        {b.display_name} ({b.role.toUpperCase()})
                      </option>
                    ))}
                </select>
              </label>

              {!engineStatus?.sd_installed ? (
                <button
                  type="button"
                  className="btn btn--sm btn--accent"
                  disabled={
                    !setupNodeId ||
                    engineBusy === "install" ||
                    Boolean(engineStatus?.install_in_progress)
                  }
                  onClick={() => void handleInstallSd()}
                >
                  <Download size={12} aria-hidden="true" />
                  {engineStatus?.install_in_progress ? "Installing…" : "Install sd-server"}
                </button>
              ) : (
                <span className="pill pill--ok" title={engineStatus.sd_binary}>
                  sd.cpp {engineStatus.sd_version || "installed"}
                  {engineStatus.sd_backend ? ` (${engineStatus.sd_backend})` : ""}
                </span>
              )}

              {engineStatus?.sd_installed && (
                <>
                  <label className="inline-field">
                    <span className="muted">Model</span>
                    <select
                      value={selectedStartModel}
                      onChange={(e) => setSelectedStartModel(e.target.value)}
                      disabled={engineStatus.local_media_models.length === 0}
                    >
                      {engineStatus.local_media_models.length === 0 ? (
                        <option value="">No local checkpoints</option>
                      ) : (
                        engineStatus.local_media_models.map((m) => (
                          <option key={m.filename} value={m.filename}>
                            {m.filename} ({m.size_gb} GB)
                          </option>
                        ))
                      )}
                    </select>
                  </label>

                  {engineStatus.sd_managed_running ? (
                    <button
                      type="button"
                      className="btn btn--sm btn--danger"
                      disabled={engineBusy === "stop"}
                      onClick={() => void handleStopSd()}
                    >
                      <Square size={12} aria-hidden="true" /> Stop
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="btn btn--sm btn--accent"
                      disabled={!selectedStartModel || engineBusy === "start"}
                      onClick={() => void handleStartSd()}
                    >
                      <Play size={12} aria-hidden="true" />
                      {engineBusy === "start" ? "Starting…" : "Start"}
                    </button>
                  )}
                </>
              )}
            </div>

            {engineStatus?.install_in_progress && (
              <div className="download-row">
                <div className="meter__row">
                  <span className="muted">{engineStatus.install_step}</span>
                </div>
                <progress aria-label="Installing stable-diffusion.cpp" />
              </div>
            )}

            {engineStatus?.sd_error && (
              <div role="alert" className="banner banner--error">
                {engineStatus.sd_error}
              </div>
            )}

            <div className="media-starter-row">
              <span className="muted" style={{ fontSize: "0.76rem" }}>
                Starter Bundles (Image &amp; Video — auto-wires VAE &amp; encoders)
              </span>
              <div className="media-starter-grid">
                {STARTER_MEDIA_BUNDLES.map((b) => {
                  const localSet = new Set(
                    (engineStatus?.local_media_models ?? []).map((f) => f.filename.toLowerCase())
                  );
                  const presentCount = b.files.filter((f) =>
                    localSet.has(fileBasename(f.filename).toLowerCase())
                  ).length;
                  const allPresent = presentCount === b.files.length;
                  const pulling = b.files.some((f) =>
                    activeDownloads.some(
                      (d) =>
                        fileBasename(d.filename).toLowerCase() ===
                        fileBasename(f.filename).toLowerCase()
                    )
                  );
                  return (
                    <div key={b.id} className="media-starter-card">
                      <div>
                        <div className="row-actions" style={{ gap: "0.35rem" }}>
                          <span className={`pill ${b.kind === "video" ? "pill--accent" : ""}`}>
                            {b.kind.toUpperCase()}
                          </span>
                          <strong>{b.label}</strong>
                        </div>
                        <div className="muted" style={{ fontSize: "0.73rem" }}>
                          {b.hint}
                          {b.files.length > 1 && !allPresent
                            ? ` · ${presentCount}/${b.files.length} files`
                            : ""}
                        </div>
                      </div>
                      {allPresent ? (
                        <span className="pill pill--ok">
                          <Check size={11} aria-hidden="true" /> READY
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="btn btn--sm"
                          disabled={!setupNodeId || pulling || engineBusy === `bundle:${b.id}`}
                          onClick={() => void handlePullBundle(b)}
                        >
                          {pulling || engineBusy === `bundle:${b.id}` ? (
                            <>
                              <Loader2 size={12} className="spin" aria-hidden="true" /> Pulling…
                            </>
                          ) : (
                            <>
                              <Download size={12} aria-hidden="true" /> Pull
                              {b.files.length > 1 ? " Bundle" : ""}
                            </>
                          )}
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="media-hf-browser">
              <div className="media-hf-browser__head">
                <span className="muted" style={{ fontSize: "0.76rem" }}>
                  Browse Hugging Face Image &amp; Video Models (.gguf / .safetensors)
                </span>
                <div className="row-actions" style={{ gap: "0.3rem" }}>
                  {(
                    [
                      { label: "Z-Image", q: "Z-Image-Turbo", k: "image" },
                      { label: "FLUX", q: "FLUX.1 gguf", k: "image" },
                      { label: "SDXL", q: "sdxl", k: "image" },
                      { label: "Wan 2.1", q: "Wan2.1 gguf", k: "video" },
                      { label: "LTX-Video", q: "LTX-Video", k: "video" }
                    ] as const
                  ).map((chip) => (
                    <button
                      key={chip.label}
                      type="button"
                      className="btn btn--sm btn--ghost"
                      onClick={() => {
                        setHfQuery(chip.q);
                        setHfKind(chip.k);
                        void searchHfMedia(chip.q, chip.k);
                      }}
                    >
                      {chip.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="media-hf-browser__bar">
                <div className="dialog-modes" role="group" aria-label="Hugging Face media kind">
                  <button
                    type="button"
                    className={`btn btn--sm ${hfKind === "image" ? "btn--accent" : ""}`}
                    aria-pressed={hfKind === "image"}
                    onClick={() => setHfKind("image")}
                  >
                    Image
                  </button>
                  <button
                    type="button"
                    className={`btn btn--sm ${hfKind === "video" ? "btn--accent" : ""}`}
                    aria-pressed={hfKind === "video"}
                    onClick={() => setHfKind("video")}
                  >
                    Video
                  </button>
                </div>
                <input
                  type="search"
                  value={hfQuery}
                  onChange={(e) => setHfQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void searchHfMedia();
                    }
                  }}
                  placeholder={
                    hfKind === "video"
                      ? "Search Wan 2.1, LTX-Video, HunyuanVideo…"
                      : "Search FLUX, Z-Image, SDXL, Pony, Illustrious…"
                  }
                  aria-label="Search Hugging Face media models"
                />
                <button
                  type="button"
                  className="btn btn--sm btn--accent"
                  disabled={hfSearching}
                  onClick={() => void searchHfMedia()}
                >
                  {hfSearching ? (
                    <Loader2 size={12} className="spin" aria-hidden="true" />
                  ) : (
                    <Search size={12} aria-hidden="true" />
                  )}{" "}
                  {hfSearching ? "Searching…" : "Search HF"}
                </button>
              </div>

              {hfRepos.length > 0 && (
                <div className="media-hf-results">
                  {hfRepos.map((repo) => {
                    const expanded = hfExpandedRepo === repo.repo_id;
                    const files = hfFilesMap[repo.repo_id] ?? [];
                    return (
                      <div key={repo.repo_id} className="media-hf-repo">
                        <button
                          type="button"
                          className="media-hf-repo__row"
                          aria-expanded={expanded}
                          onClick={() => void toggleHfRepoFiles(repo.repo_id)}
                        >
                          <span className="row-actions" style={{ gap: "0.35rem", minWidth: 0 }}>
                            {expanded ? (
                              <ChevronDown size={13} aria-hidden="true" />
                            ) : (
                              <ChevronRight size={13} aria-hidden="true" />
                            )}
                            <code style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                              {repo.repo_id}
                            </code>
                            {repo.gated && (
                              <span className="pill pill--warn" title="Gated repo (HF_TOKEN required)">
                                <Lock size={10} aria-hidden="true" /> gated
                              </span>
                            )}
                          </span>
                          <span className="muted" style={{ fontSize: "0.73rem", whiteSpace: "nowrap" }}>
                            {repo.file_count} files · {repo.downloads.toLocaleString()} dl
                          </span>
                        </button>

                        {expanded && (
                          <div className="media-hf-repo__files">
                            <div className="row-actions" style={{ justifyContent: "space-between" }}>
                              <span className="muted" style={{ fontSize: "0.72rem" }}>
                                Select a .gguf or .safetensors file to pull to {setupNodeId || "node"}
                              </span>
                              <a
                                href={`https://huggingface.co/${repo.repo_id}`}
                                target="_blank"
                                rel="noreferrer noopener"
                                className="btn btn--sm btn--ghost"
                              >
                                <ExternalLink size={11} aria-hidden="true" /> HF Card
                              </a>
                            </div>
                            {hfLoadingFiles && files.length === 0 ? (
                              <span className="muted" style={{ fontSize: "0.75rem" }}>
                                Loading files…
                              </span>
                            ) : (
                              files.slice(0, 20).map((f) => {
                                const base = fileBasename(f.filename);
                                const onDisk = Boolean(
                                  engineStatus?.local_media_models.some(
                                    (m) => m.filename.toLowerCase() === base.toLowerCase()
                                  )
                                );
                                const job = activeDownloads.find(
                                  (d) =>
                                    fileBasename(d.filename).toLowerCase() === base.toLowerCase()
                                );
                                return (
                                  <div key={f.filename} className="media-hf-file-row">
                                    <span className="media-hf-file-row__name" title={f.filename}>
                                      <span className="pill">{f.format.toUpperCase()}</span>
                                      <code>{f.filename}</code>
                                    </span>
                                    <span className="row-actions" style={{ flexShrink: 0 }}>
                                      {f.size_gb > 0 && (
                                        <span className="muted" style={{ fontSize: "0.73rem" }}>
                                          {f.size_gb.toFixed(2)} GB
                                        </span>
                                      )}
                                      {onDisk ? (
                                        <span className="pill pill--ok">
                                          <Check size={10} aria-hidden="true" /> On disk
                                        </span>
                                      ) : job ? (
                                        <span className="pill pill--accent">
                                          <Loader2 size={10} className="spin" aria-hidden="true" />{" "}
                                          {Math.round(job.percent)}%
                                        </span>
                                      ) : (
                                        <button
                                          type="button"
                                          className="btn btn--sm btn--accent"
                                          disabled={!setupNodeId || engineBusy === `pull:${f.filename}`}
                                          onClick={() =>
                                            void handlePullMediaFile(repo.repo_id, f.filename)
                                          }
                                        >
                                          <Download size={11} aria-hidden="true" /> Pull →{" "}
                                          {setupNodeId}
                                        </button>
                                      )}
                                    </span>
                                  </div>
                                );
                              })
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {activeDownloads.length > 0 && (
              <div className="downloads-box">
                {activeDownloads.map((d) => (
                  <div key={d.id} className="download-row">
                    <div className="meter__row">
                      <span>
                        <span className="node-tag">{d.node_id}</span> <code>{d.filename}</code>
                      </span>
                      <strong>
                        {d.percent}% ({(d.downloaded_bytes / 1024 ** 3).toFixed(2)} GB)
                        {(d.speed_bytes_per_sec ?? 0) > 0 &&
                          ((d.speed_bytes_per_sec ?? 0) >= 1024 * 1024
                            ? ` · ${((d.speed_bytes_per_sec ?? 0) / (1024 * 1024)).toFixed(1)} MB/s`
                            : ` · ${Math.round((d.speed_bytes_per_sec ?? 0) / 1024)} KB/s`)}
                        {d.protocol ? ` · ${d.protocol.toUpperCase()}` : ""}
                      </strong>
                    </div>
                    <progress
                      max={100}
                      value={d.percent}
                      aria-label={`Download progress for ${d.filename}`}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        </details>

        <form className="media-form" onSubmit={handleGenerate}>
          <div className="media-form__top">
            <div className="dialog-modes" role="group" aria-label="Generation mode">
              <button
                type="button"
                className={`btn btn--sm ${kind === "image" ? "btn--accent" : ""}`}
                aria-pressed={kind === "image"}
                onClick={() => switchKind("image")}
              >
                <ImageIcon size={13} aria-hidden="true" /> Image
              </button>
              <button
                type="button"
                className={`btn btn--sm ${kind === "video" ? "btn--accent" : ""}`}
                aria-pressed={kind === "video"}
                onClick={() => switchKind("video")}
              >
                <Film size={13} aria-hidden="true" /> Video
              </button>
            </div>

            <div className="media-form__selectors">
              <label className="inline-field">
                <span className="muted">Node</span>
                <select value={nodeId} onChange={(e) => setNodeId(e.target.value)}>
                  <option value="auto">Auto</option>
                  {backends.map((b) => (
                    <option key={b.node_id} value={b.node_id}>
                      {b.display_name} ({b.media_healthy ? b.media_engine : "offline"})
                    </option>
                  ))}
                </select>
              </label>

              <label className="inline-field">
                <span className="muted">Model</span>
                {availableModels.length > 0 ? (
                  <select value={model} onChange={(e) => setModel(e.target.value)}>
                    <option value="">Default ({availableModels[0]})</option>
                    {availableModels.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="text"
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    placeholder="Model"
                  />
                )}
              </label>

              <label
                className="chat-check"
                title="Unload LLM from GPU VRAM before generation"
              >
                <input
                  type="checkbox"
                  checked={evictLlmVram}
                  onChange={(e) => setEvictLlmVram(e.target.checked)}
                />
                <span>Evict LLM VRAM</span>
              </label>
            </div>
          </div>

          <div className="media-form__prompts">
            <label className="media-field">
              <span>Prompt</span>
              <textarea
                required
                rows={3}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="Prompt…"
              />
            </label>
            <label className="media-field">
              <span>Negative Prompt</span>
              <textarea
                rows={2}
                value={negativePrompt}
                onChange={(e) => setNegativePrompt(e.target.value)}
                placeholder="Negative prompt…"
              />
            </label>
          </div>

          <div className="media-params-grid">
            <div className="media-field">
              <span>Aspect Ratio</span>
              <div className="dialog-modes" role="group" aria-label="Resolution presets">
                {presets.map((p) => {
                  const active = width === p.width && height === p.height;
                  return (
                    <button
                      key={p.label}
                      type="button"
                      className={`btn btn--sm ${active ? "btn--accent" : ""}`}
                      aria-pressed={active}
                      onClick={() => {
                        setWidth(p.width);
                        setHeight(p.height);
                      }}
                    >
                      {p.label}
                    </button>
                  );
                })}
              </div>
            </div>

            <label className="media-field">
              <span>Width</span>
              <input
                type="number"
                min={128}
                max={2048}
                step={64}
                value={width}
                onChange={(e) => setWidth(Number(e.target.value) || 1024)}
              />
            </label>

            <label className="media-field">
              <span>Height</span>
              <input
                type="number"
                min={128}
                max={2048}
                step={64}
                value={height}
                onChange={(e) => setHeight(Number(e.target.value) || 1024)}
              />
            </label>

            <label className="media-field">
              <span>Steps</span>
              <input
                type="number"
                min={1}
                max={150}
                value={steps}
                onChange={(e) => setSteps(Number(e.target.value) || 20)}
              />
            </label>

            <label className="media-field">
              <span>CFG Scale</span>
              <input
                type="number"
                min={0}
                max={30}
                step={0.5}
                value={cfgScale}
                onChange={(e) => setCfgScale(Number(e.target.value))}
              />
            </label>

            <div className="media-field">
              <span>Seed</span>
              <div className="media-seed-row">
                <input
                  type="text"
                  value={seedText}
                  onChange={(e) => setSeedText(e.target.value.replace(/[^0-9]/g, ""))}
                  placeholder="Random"
                />
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  aria-label="Randomize seed"
                  title="Randomize seed"
                  onClick={() => setSeedText("")}
                >
                  <Dices size={13} aria-hidden="true" />
                </button>
              </div>
            </div>

            {kind === "video" && (
              <>
                <label className="media-field">
                  <span>Frames</span>
                  <input
                    type="number"
                    min={2}
                    max={240}
                    value={frames}
                    onChange={(e) => setFrames(Number(e.target.value) || 16)}
                  />
                </label>
                <label className="media-field">
                  <span>FPS</span>
                  <input
                    type="number"
                    min={1}
                    max={60}
                    value={fps}
                    onChange={(e) => setFps(Number(e.target.value) || 8)}
                  />
                </label>
              </>
            )}
          </div>

          <div className="media-form__foot">
            <div className="row-actions">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                className="visually-hidden"
                onChange={(e) => handleInitImageFile(e.target.files?.[0] ?? null)}
              />
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => fileInputRef.current?.click()}
              >
                <Upload size={12} aria-hidden="true" />{" "}
                {initImage ? "Replace Ref Image" : kind === "video" ? "Img2Video Ref" : "Img2Img Ref"}
              </button>
              {initImage && (
                <>
                  <img src={initImage} alt="Reference preview" className="media-ref-thumb" />
                  <label className="inline-field">
                    <span className="muted">Strength</span>
                    <input
                      type="number"
                      min={0.05}
                      max={1}
                      step={0.05}
                      value={strength}
                      onChange={(e) => setStrength(Number(e.target.value) || 0.7)}
                      style={{ width: "4.5rem" }}
                    />
                  </label>
                  <button
                    type="button"
                    className="btn btn--sm btn--ghost"
                    aria-label="Clear reference image"
                    onClick={() => setInitImage(null)}
                  >
                    <X size={12} aria-hidden="true" />
                  </button>
                </>
              )}
            </div>

            <button
              type="submit"
              className="btn btn--accent"
              disabled={submitting || !prompt.trim()}
            >
              <Sparkles size={13} aria-hidden="true" />
              {submitting
                ? "Queuing…"
                : kind === "video"
                  ? "Generate Video"
                  : "Generate Image"}
            </button>
          </div>

          <details className="chat-disclosure">
            <summary>
              <span>ComfyUI Workflow JSON</span>
            </summary>
            <div className="chat-disclosure__body">
              <span className="muted" style={{ fontSize: "0.75rem" }}>
                <code>{"{{PROMPT}}"}</code> <code>{"{{NEGATIVE_PROMPT}}"}</code>{" "}
                <code>{"{{MODEL}}"}</code> <code>{"{{WIDTH}}"}</code> <code>{"{{HEIGHT}}"}</code>{" "}
                <code>{"{{STEPS}}"}</code> <code>{"{{CFG}}"}</code> <code>{"{{SEED}}"}</code>{" "}
                <code>{"{{FRAMES}}"}</code> <code>{"{{FPS}}"}</code> <code>{"{{IMAGE}}"}</code>
              </span>
              <textarea
                rows={4}
                value={workflowJson}
                onChange={(e) => setWorkflowJson(e.target.value)}
                placeholder='{"1": {"class_type": "CheckpointLoaderSimple", ...}}'
              />
            </div>
          </details>
        </form>

        {activeJobs.length > 0 && (
          <div className="downloads-box">
            <h3>Queue ({activeJobs.length})</h3>
            {activeJobs.map((j) => (
              <div key={j.id} className="download-row">
                <div className="meter__row">
                  <span>
                    <span className="node-tag">{j.node_id}</span>{" "}
                    <span className="pill">{j.kind.toUpperCase()}</span>{" "}
                    <span>{j.stage || j.status}</span>
                  </span>
                  <strong>{j.progress}%</strong>
                </div>
                <progress
                  max={100}
                  value={j.progress}
                  aria-label={`Generation progress for ${j.id}`}
                />
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="panel" aria-label="Gallery">
        <div className="panel-head">
          <h2>
            <ImageIcon size={14} aria-hidden="true" /> Gallery ({galleryJobs.length})
          </h2>
          <div className="dialog-modes" role="group" aria-label="Filter gallery by media type">
            {(["all", "image", "video"] as const).map((f) => (
              <button
                key={f}
                type="button"
                className={`btn btn--sm ${galleryFilter === f ? "btn--accent" : ""}`}
                aria-pressed={galleryFilter === f}
                onClick={() => setGalleryFilter(f)}
              >
                {f === "all" ? "All" : f === "image" ? "Images" : "Videos"}
              </button>
            ))}
          </div>
        </div>

        {galleryJobs.length === 0 ? (
          <p className="muted">No items.</p>
        ) : (
          <div className="media-gallery-grid">
            {galleryJobs.map((j) => {
              const isVideo =
                j.kind === "video" ||
                Boolean(j.mime_type?.startsWith("video/")) ||
                Boolean(j.filename?.endsWith(".mp4") || j.filename?.endsWith(".webm"));
              return (
                <article key={j.id} className="media-card">
                  <div className="media-card__stage">
                    {j.status === "error" ? (
                      <div className="media-card__error">
                        <span className="pill pill--err">FAILED</span>
                        <p className="text-err">{j.error || "Generation failed"}</p>
                      </div>
                    ) : j.file_url ? (
                      isVideo ? (
                        <video
                          controls
                          playsInline
                          preload="metadata"
                          width={j.width}
                          height={j.height}
                          {...({ loading: "lazy" } as Record<string, string>)}
                          src={j.file_url}
                          className="media-card__media"
                        />
                      ) : (
                        <button
                          type="button"
                          className="media-card__img-btn"
                          aria-label={`View generated image: ${j.prompt.slice(0, 60)}`}
                          onClick={() => openViewer(j)}
                        >
                          <img
                            src={j.file_url}
                            alt={j.prompt}
                            width={j.width}
                            height={j.height}
                            loading="lazy"
                            className="media-card__media"
                          />
                        </button>
                      )
                    ) : null}
                  </div>

                  <div className="media-card__body">
                    <div className="media-card__meta">
                      <span className="pill">{j.kind.toUpperCase()}</span>
                      <span className="pill">
                        {j.width}×{j.height}
                      </span>
                      {j.node_id && <span className="node-tag">{j.node_id}</span>}
                      {j.duration_ms !== null && (
                        <span className="muted">{fmtDuration(j.duration_ms)}</span>
                      )}
                    </div>
                    <p className="media-card__prompt" title={j.prompt}>
                      {j.prompt}
                    </p>
                    <div className="media-card__actions">
                      {j.file_url && (
                        <>
                          <button
                            type="button"
                            className="btn btn--sm btn--ghost"
                            aria-label="Inspect full resolution"
                            onClick={() => openViewer(j)}
                          >
                            <Maximize2 size={12} aria-hidden="true" />
                          </button>
                          <a
                            href={j.file_url}
                            download={j.filename ?? `${j.id}.png`}
                            className="btn btn--sm btn--ghost"
                            aria-label="Download generated media"
                          >
                            <Download size={12} aria-hidden="true" />
                          </a>
                        </>
                      )}
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        title="Reuse prompt & settings"
                        aria-label="Reuse prompt and settings"
                        onClick={() => reuseParams(j)}
                      >
                        <RotateCcw size={12} aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost btn--danger"
                        aria-label="Delete media item"
                        onClick={() => requestDeleteJob(j)}
                      >
                        <Trash2 size={12} aria-hidden="true" />
                      </button>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <dialog
        ref={viewerDialogRef}
        aria-labelledby={viewerTitleId}
        className="media-viewer-dialog"
        {...({ closedby: "any" } as Record<string, string>)}
      >
        {viewingJob && (
          <>
            <div className="panel-head">
              <h2 id={viewerTitleId}>
                {viewingJob.kind === "video" ? "Video" : "Image"} —{" "}
                {viewingJob.width}×{viewingJob.height}
              </h2>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                aria-label="Close viewer"
                onClick={() => viewerDialogRef.current?.close()}
              >
                <X size={14} aria-hidden="true" />
              </button>
            </div>
            {viewingJob.file_url && (
              <div className="media-viewer__stage">
                {viewingJob.kind === "video" || viewingJob.mime_type?.startsWith("video/") ? (
                  <video
                    controls
                    playsInline
                    preload="metadata"
                    width={viewingJob.width}
                    height={viewingJob.height}
                    src={viewingJob.file_url}
                    className="media-viewer__media"
                  />
                ) : (
                  <img
                    src={viewingJob.file_url}
                    alt={viewingJob.prompt}
                    width={viewingJob.width}
                    height={viewingJob.height}
                    className="media-viewer__media"
                  />
                )}
              </div>
            )}
            <p className="muted" style={{ margin: 0 }}>
              <strong>Prompt:</strong> {viewingJob.prompt}
            </p>
            <div className="row-actions">
              <span className="pill">{viewingJob.media_engine}</span>
              {viewingJob.model && <span className="pill">{viewingJob.model}</span>}
              {viewingJob.seed !== null && <span className="pill">seed {viewingJob.seed}</span>}
              <span className="pill">{viewingJob.steps} steps</span>
              <span className="pill">CFG {viewingJob.cfg_scale}</span>
              {viewingJob.file_url && (
                <a
                  href={viewingJob.file_url}
                  download={viewingJob.filename ?? `${viewingJob.id}.png`}
                  className="btn btn--sm btn--accent"
                  style={{ marginInlineStart: "auto" }}
                >
                  <Download size={12} aria-hidden="true" /> Download
                </a>
              )}
            </div>
          </>
        )}
      </dialog>
    </div>
  );
}
