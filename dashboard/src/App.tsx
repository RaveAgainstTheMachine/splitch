import { useEffect, useId, useRef, useState } from "react";
import {
  Activity,
  BookOpen,
  Cpu,
  Gamepad2,
  Gauge,
  HardDrive,
  Layers,
  Lock,
  MessageSquare,
  Plus,
  Power,
  Server,
  Settings,
  Sparkles,
  Trash2,
  Wrench
} from "lucide-react";
import { ChatTab } from "./components/ChatTab";
import { ConfirmDialog, type ConfirmState } from "./components/ConfirmDialog";
import { McpTab } from "./components/McpTab";
import { MediaTab } from "./components/MediaTab";
import { ModelsTab } from "./components/ModelsTab";
import { SettingsDialog, type SettingsTabId } from "./components/SettingsDialog";
import { SkillsTab } from "./components/SkillsTab";
import { SplitchLogo } from "./components/SplitchLogo";
import { syncFaviconFromTheme } from "./utils/favicon";
import type {
  AuthStatusResponse,
  ControlState,
  DashboardSnapshot,
  NodeStatus
} from "./types";

type TabId = "chat" | "media" | "overview" | "models" | "mcp" | "skills";
const TAB_ORDER: TabId[] = ["chat", "media", "overview", "models", "mcp", "skills"];

function fmtGb(mb: number): string {
  return `${(mb / 1024).toFixed(1)} GB`;
}

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

function fmtUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m ${s}s`;
}

export default function App() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TabId>("chat");
  const [confirmState, setConfirmState] = useState<ConfirmState | null>(null);
  const [authStatus, setAuthStatus] = useState<AuthStatusResponse>({
    auth_required: false,
    authenticated: true
  });
  const [loginKey, setLoginKey] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);

  const tabRefs = useRef<Partial<Record<TabId, HTMLButtonElement | null>>>({});
  const panelRefs = useRef<Partial<Record<TabId, HTMLDivElement | null>>>({});
  const settingsDialogRef = useRef<HTMLDialogElement | null>(null);
  const authDialogRef = useRef<HTMLDialogElement | null>(null);
  const [settingsTab, setSettingsTab] = useState<SettingsTabId>("clients");

  const openSettings = (tab: SettingsTabId) => {
    setSettingsTab(tab);
    if (!settingsDialogRef.current?.open) {
      settingsDialogRef.current?.showModal();
    }
  };

  const aliasCaptionId = useId();
  const traceCaptionId = useId();
  const autoYieldTipId = useId();
  const authTitleId = useId();
  const tabIds: Record<TabId, string> = {
    chat: useId(),
    media: useId(),
    overview: useId(),
    models: useId(),
    mcp: useId(),
    skills: useId()
  };

  const checkAuthStatus = async () => {
    try {
      const res = await fetch("/api/auth/status");
      if (res.ok) {
        const st = (await res.json()) as AuthStatusResponse;
        setAuthStatus(st);
        if (st.auth_required && !st.authenticated && !authDialogRef.current?.open) {
          authDialogRef.current?.showModal();
        }
      }
    } catch {
      // ignore
    }
  };

  const submitLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoginError(null);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: loginKey })
      });
      if (!res.ok) {
        setLoginError("Invalid SPLITCH_API_KEY");
        return;
      }
      const st = (await res.json()) as AuthStatusResponse;
      setAuthStatus(st);
      setLoginKey("");
      authDialogRef.current?.close();
      await fetchStatus();
      window.location.reload();
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : "Login failed");
    }
  };

  const logoutSession = async () => {
    try {
      const res = await fetch("/api/auth/logout", { method: "POST" });
      if (res.ok) {
        const st = (await res.json()) as AuthStatusResponse;
        setAuthStatus(st);
        if (st.auth_required && !st.authenticated) {
          authDialogRef.current?.showModal();
        }
      }
    } catch {
      // ignore
    }
  };

  const fetchStatus = async () => {
    try {
      const res = await fetch("/api/status");
      if (res.status === 401) {
        await checkAuthStatus();
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: DashboardSnapshot = await res.json();
      setSnapshot(data);
      setErrorMsg(null);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to reach Splitch router");
    }
  };

  // Sync hidden="until-found" beforematch events to tab selection
  useEffect(() => {
    const cleanups: Array<() => void> = [];
    for (const id of TAB_ORDER) {
      const el = panelRefs.current[id];
      if (!el) continue;
      const onBeforeMatch = () => setActiveTab(id);
      el.addEventListener("beforematch", onBeforeMatch);
      cleanups.push(() => el.removeEventListener("beforematch", onBeforeMatch));
    }
    return () => cleanups.forEach((fn) => fn());
  }, []);

  // interest-triggered-tooltips: upfront ARIA wiring, popover="hint" beforetoggle fallback, and conditional polyfills
  useEffect(() => {
    const ownedTriggers = new Set<HTMLElement>();
    const wireHints = () => {
      document.querySelectorAll<HTMLElement>("[interestfor]").forEach((trigger) => {
        const targetId = trigger.getAttribute("interestfor");
        if (!targetId) return;
        const hint = document.getElementById(targetId);
        if (!hint || hint.getAttribute("popover") !== "hint") return;

        const hintText = (hint.textContent ?? "").trim().replace(/\s+/g, " ");
        const triggerName = (
          trigger.getAttribute("aria-label") ??
          trigger.textContent ??
          trigger.getAttribute("title") ??
          ""
        )
          .trim()
          .replace(/\s+/g, " ");
        if (hintText && hintText === triggerName) return;

        if (!hint.hasAttribute("role")) {
          hint.setAttribute("role", "tooltip");
        }
        if (!triggerName) {
          if (!trigger.hasAttribute("aria-labelledby")) {
            trigger.setAttribute("aria-labelledby", targetId);
          }
        } else if (
          trigger.getAttribute("popovertarget") === targetId ||
          trigger.getAttribute("commandfor") === targetId
        ) {
          if (!trigger.hasAttribute("aria-expanded") || ownedTriggers.has(trigger)) {
            ownedTriggers.add(trigger);
            const isOpen = hint.matches(":popover-open, .\\:popover-open");
            trigger.setAttribute("aria-expanded", String(isOpen));
          }
        } else if (!trigger.hasAttribute("aria-describedby")) {
          trigger.setAttribute("aria-describedby", targetId);
        }
      });
    };

    wireHints();

    const onToggle = (ev: Event) => {
      const target = ev.target;
      if (!(target instanceof HTMLElement) || !target.id) return;
      const isOpen =
        (ev as ToggleEvent).newState === "open" ||
        target.matches(":popover-open, .\\:popover-open");
      document
        .querySelectorAll<HTMLElement>(`[interestfor="${CSS.escape(target.id)}"]`)
        .forEach((tr) => {
          if (ownedTriggers.has(tr)) {
            tr.setAttribute("aria-expanded", String(isOpen));
          }
        });
    };
    document.addEventListener("toggle", onToggle, true);

    const probe = document.createElement("div");
    probe.setAttribute("popover", "hint");
    let onBeforeToggle: ((ev: Event) => void) | null = null;
    if ((probe as HTMLElement & { popover?: string }).popover !== "hint") {
      onBeforeToggle = (ev: Event) => {
        const target = ev.target;
        const newState = (ev as ToggleEvent).newState;
        if (!(target instanceof HTMLElement) || !target.id) return;
        const isToggletip =
          target.getAttribute("popover") === "hint" &&
          document.querySelector(`[popovertarget="${CSS.escape(target.id)}"]`);
        if (newState === "open" && isToggletip) {
          target.setAttribute("popover", "auto");
        }
      };
      document.addEventListener("beforetoggle", onBeforeToggle, true);
    }

    if (!Object.hasOwn(HTMLElement.prototype, "popover")) {
      void import("@oddbird/popover-polyfill");
    }
    if (!("interestForElement" in HTMLButtonElement.prototype)) {
      void import("interestfor");
    }
    if (!("switch" in HTMLInputElement.prototype)) {
      void import("input-switch-polyfill");
    }

    syncFaviconFromTheme();
    const mq =
      typeof window !== "undefined" && typeof window.matchMedia === "function"
        ? window.matchMedia("(prefers-color-scheme: light)")
        : null;
    const onSchemeChange = () => syncFaviconFromTheme();
    mq?.addEventListener("change", onSchemeChange);

    return () => {
      document.removeEventListener("toggle", onToggle, true);
      if (onBeforeToggle) {
        document.removeEventListener("beforetoggle", onBeforeToggle, true);
      }
      mq?.removeEventListener("change", onSchemeChange);
    };
  }, []);

  useEffect(() => {
    void checkAuthStatus();
    fetchStatus();
    let ws: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let disposed = false;

    const connect = () => {
      if (disposed) return;
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      ws = new WebSocket(`${proto}//${window.location.host}/ws/status`);
      ws.onopen = () => setWsConnected(true);
      ws.onmessage = (evt) => {
        try {
          setSnapshot(JSON.parse(evt.data) as DashboardSnapshot);
          setErrorMsg(null);
        } catch {
          // ignore malformed frames
        }
      };
      ws.onclose = () => {
        setWsConnected(false);
        if (!disposed) {
          reconnectTimer = window.setTimeout(connect, 2500);
        }
      };
      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      disposed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, []);

  const selectTab = (id: TabId) => {
    setActiveTab(id);
    tabRefs.current[id]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  };

  const handleTabKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>, current: TabId) => {
    const idx = TAB_ORDER.indexOf(current);
    let nextIdx = -1;
    if (e.key === "ArrowRight") nextIdx = (idx + 1) % TAB_ORDER.length;
    else if (e.key === "ArrowLeft") nextIdx = (idx - 1 + TAB_ORDER.length) % TAB_ORDER.length;
    else if (e.key === "Home") nextIdx = 0;
    else if (e.key === "End") nextIdx = TAB_ORDER.length - 1;
    if (nextIdx >= 0) {
      e.preventDefault();
      const target = TAB_ORDER[nextIdx];
      selectTab(target);
      tabRefs.current[target]?.focus();
    }
  };

  const updateControl = async (patch: Partial<ControlState>) => {
    setBusyAction("control");
    try {
      const res = await fetch("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch)
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await fetchStatus();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Control update failed");
    } finally {
      setBusyAction(null);
    }
  };

  const triggerModelAction = async (
    nodeId: string,
    model: string,
    action: "load" | "unload"
  ) => {
    const key = `${nodeId}:${model}:${action}`;
    setBusyAction(key);
    try {
      const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/models/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      await fetchStatus();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Model action failed");
    } finally {
      setBusyAction(null);
    }
  };

  const removeNode = (nodeId: string, displayName: string) => {
    setConfirmState({
      title: `Remove node "${displayName}"?`,
      message: `Splitch will stop polling and routing to ${nodeId}. You can re-add it anytime.`,
      confirmLabel: "Remove Node",
      danger: true,
      onConfirm: async () => {
        setBusyAction(`rm:${nodeId}`);
        try {
          const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, { method: "DELETE" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          await fetchStatus();
        } catch (err) {
          setErrorMsg(err instanceof Error ? err.message : "Failed to remove node");
        } finally {
          setBusyAction(null);
        }
      }
    });
  };

  const control = snapshot?.control ?? { gaming_mode: false, force_node: "auto" };
  const hostLabel = window.location.host || "localhost:8080";

  return (
    <div className={`shell ${activeTab === "chat" ? "shell--ide" : ""}`}>
      <header className="topbar">
        <div className="brand">
          <SplitchLogo size={26} />
          <h1>SPLITCH</h1>
        </div>

        <div
          className="tablist topbar__tabs"
          role="tablist"
          aria-label="Splitch workspace sections"
          {...({ focusgroup: "tablist nomemory" } as Record<string, string>)}
        >
          {(
            [
              { id: "chat", label: "Agent Chat", icon: MessageSquare, dividerAfter: false },
              { id: "media", label: "Media Studio", icon: Sparkles, dividerAfter: true },
              { id: "overview", label: "Overview", icon: Activity, dividerAfter: false },
              { id: "models", label: "Models & Aliases", icon: Layers, dividerAfter: false },
              { id: "mcp", label: "MCP Servers", icon: Wrench, dividerAfter: false },
              { id: "skills", label: "Skills Library", icon: BookOpen, dividerAfter: false }
            ] as const
          ).map(({ id, label, icon: Icon, dividerAfter }) => {
            const selected = activeTab === id;
            return (
              <button
                key={id}
                ref={(el) => {
                  tabRefs.current[id] = el;
                }}
                type="button"
                role="tab"
                id={tabIds[id]}
                className={dividerAfter ? "tab--divider-after" : undefined}
                aria-selected={selected}
                tabIndex={selected ? 0 : -1}
                {...(selected ? ({ focusgroupstart: "" } as Record<string, string>) : {})}
                onClick={() => selectTab(id)}
                onKeyDown={(e) => handleTabKeyDown(e, id)}
              >
                <Icon size={13} aria-hidden="true" />
                <span>{label}</span>
              </button>
            );
          })}
        </div>

        <div className="topbar__meta">
          <button
            type="button"
            className={`endpoint-pill endpoint-pill--btn status-pill ${
              wsConnected ? "status-pill--ok" : "status-pill--warn"
            }`}
            onClick={() => openSettings("clients")}
            title="Connect CLI agents, IDEs & SDKs (/v1)"
          >
            <span className="status-pill__dot" aria-hidden="true" />
            <strong className="status-pill__state">
              {wsConnected ? "LIVE" : "RECONNECTING"}
            </strong>
            <span className="status-pill__host">
              <span className="status-pill__sep">•</span>
              <code>{hostLabel}</code>
            </span>
            {snapshot && (
              <span className="status-pill__uptime">
                <span className="status-pill__sep">•</span>
                <span>{fmtUptime(snapshot.uptime_s)}</span>
              </span>
            )}
          </button>
          {authStatus.auth_required && authStatus.authenticated && (
            <button
              type="button"
              className="btn btn--ghost topbar__icon-btn"
              onClick={() => void logoutSession()}
              title="Lock session (log out)"
              aria-label="Lock session"
            >
              <Lock size={14} aria-hidden="true" />
            </button>
          )}
          <button
            type="button"
            className="btn btn--ghost topbar__icon-btn"
            onClick={() => openSettings("clients")}
            aria-label="Settings"
            title="Settings"
          >
            <Settings size={15} aria-hidden="true" />
          </button>
        </div>
      </header>

      {errorMsg && (
        <div role="alert" className="banner banner--error">
          {errorMsg}
        </div>
      )}

      <div className="tabs">
        <div className="tabpanels-stage">
          <div
            ref={(el) => {
              panelRefs.current.chat = el;
            }}
            role="tabpanel"
            aria-labelledby={tabIds.chat}
            hidden={activeTab !== "chat" ? ("until-found" as unknown as boolean) : undefined}
          >
            <ChatTab snapshot={snapshot} onError={setErrorMsg} onConfirm={setConfirmState} />
          </div>

          <div
            ref={(el) => {
              panelRefs.current.media = el;
            }}
            role="tabpanel"
            aria-labelledby={tabIds.media}
            hidden={activeTab !== "media" ? ("until-found" as unknown as boolean) : undefined}
          >
            <MediaTab onError={setErrorMsg} onConfirm={setConfirmState} />
          </div>

          <div
            ref={(el) => {
              panelRefs.current.overview = el;
            }}
            role="tabpanel"
            aria-labelledby={tabIds.overview}
            hidden={activeTab !== "overview" ? ("until-found" as unknown as boolean) : undefined}
          >
            <div className="tab-stack">
              <section className="control-strip" aria-label="Routing Controls">
                {(() => {
                  const yieldedNodes = (snapshot?.nodes ?? []).filter(
                    (n) =>
                      n.yield_on_gaming &&
                      n.busy_reason &&
                      (n.busy_reason.endsWith("_gpu_busy_app") ||
                        n.busy_reason.endsWith("_gpu_busy") ||
                        n.busy_reason.endsWith("_gpu_cooldown") ||
                        n.busy_reason === "gaming_mode")
                  );
                  const isYielding = yieldedNodes.length > 0;
                  return (
                    <div className="yield-indicator">
                      <button
                        type="button"
                        className="yield-info-btn"
                        {...({
                          interestfor: autoYieldTipId,
                          popovertarget: autoYieldTipId,
                          popovertargetaction: "show"
                        } as Record<string, string>)}
                      >
                        <Gamepad2 size={15} aria-hidden="true" />
                        <strong className="yield-info-btn__text">GPU Auto-Yield</strong>
                        {isYielding && (
                          <span className="pill pill--warn">
                            YIELDING ({yieldedNodes.map((n) => n.display_name).join(", ")})
                          </span>
                        )}
                      </button>
                      <div
                        id={autoYieldTipId}
                        {...({ popover: "hint" } as Record<string, string>)}
                        className="hint-tooltip"
                      >
                        {isYielding
                          ? "Game/3D VRAM detected or 60s cooldown active — loaded GPU models evicted from VRAM and traffic routed to standby."
                          : "Automatically detects game/3D VRAM or external GPU load, evicts loaded GPU models from VRAM, and fails over to standby nodes (with 60s cooldown hold)."}
                      </div>
                    </div>
                  );
                })()}

                <fieldset className="mode-group">
                  <legend>Routing Target</legend>
                  <label className="choice-card">
                    <input
                      type="radio"
                      name="force_node"
                      value="auto"
                      checked={control.force_node === "auto"}
                      disabled={busyAction === "control"}
                      onChange={() => updateControl({ force_node: "auto" })}
                    />
                    <span>Auto Split</span>
                  </label>
                  {(snapshot?.nodes ?? []).map((n) => (
                    <label key={n.node_id} className="choice-card">
                      <input
                        type="radio"
                        name="force_node"
                        value={n.node_id}
                        checked={control.force_node === n.node_id}
                        disabled={busyAction === "control"}
                        onChange={() => updateControl({ force_node: n.node_id })}
                      />
                      <span>{n.display_name}</span>
                    </label>
                  ))}
                </fieldset>
              </section>

              <div className="panel-head">
                <h2 className="muted" style={{ margin: 0, textTransform: "uppercase", fontSize: "0.78rem" }}>
                  Compute Nodes ({snapshot?.nodes.length ?? 0})
                </h2>
                <div className="row-actions">
                  <button
                    type="button"
                    className="btn btn--sm"
                    onClick={() => openSettings("benchmarks")}
                  >
                    <Gauge size={12} aria-hidden="true" /> Run Benchmark
                  </button>
                  <button
                    type="button"
                    className="btn btn--sm btn--accent"
                    onClick={() => openSettings("nodes")}
                  >
                    <Plus size={12} aria-hidden="true" /> Add Node
                  </button>
                </div>
              </div>

              <section aria-label="Compute Nodes" className="nodes-grid">
                {(snapshot?.nodes ?? []).map((node) => (
                  <NodeCard
                    key={node.node_id}
                    node={node}
                    canRemove={(snapshot?.nodes.length ?? 0) > 1}
                    busyAction={busyAction}
                    onModelAction={triggerModelAction}
                    onRemove={removeNode}
                  />
                ))}
              </section>

              <section className="panel" aria-labelledby={aliasCaptionId}>
                <div
                  tabIndex={0}
                  role="region"
                  aria-labelledby={aliasCaptionId}
                  className="table-wrapper"
                >
                  <table className="data-table aliases-table">
                    <caption id={aliasCaptionId}>
                      <Layers size={14} aria-hidden="true" /> Logical Model Aliases
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Alias</th>
                        <th scope="col">Kind</th>
                        <th scope="col">Primary</th>
                        <th scope="col">Fallback</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(snapshot?.aliases ?? []).map((a) => (
                        <tr key={a.alias}>
                          <th scope="row">
                            <code>{a.alias}</code>
                          </th>
                          <td>{a.task_kind}</td>
                          <td>
                            <span className="node-tag">{a.primary_node}</span>{" "}
                            <code>{a.primary_model}</code>
                          </td>
                          <td>
                            {a.fallback_node ? (
                              <>
                                <span className="node-tag">{a.fallback_node}</span>{" "}
                                <code>{a.fallback_model}</code>
                              </>
                            ) : (
                              <span className="muted">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="panel" aria-labelledby={traceCaptionId}>
                <div
                  tabIndex={0}
                  role="region"
                  aria-labelledby={traceCaptionId}
                  className="table-wrapper"
                >
                  <table className="data-table traces-table">
                    <caption id={traceCaptionId}>
                      <Server size={14} aria-hidden="true" /> Recent Routing Traces
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Time</th>
                        <th scope="col">Requested</th>
                        <th scope="col">Routed Target</th>
                        <th scope="col">Status</th>
                        <th scope="col">TTFT</th>
                        <th scope="col">Speed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(snapshot?.traces ?? []).length === 0 ? (
                        <tr>
                          <td colSpan={6} className="muted empty-cell">
                            No requests yet.
                          </td>
                        </tr>
                      ) : (
                        (snapshot?.traces ?? []).map((t) => (
                          <tr key={t.id}>
                            <th scope="row">{fmtTime(t.timestamp)}</th>
                            <td>
                              <code>{t.requested_model}</code>{" "}
                              <span className="muted">{t.endpoint}</span>
                            </td>
                            <td>
                              <span className="node-tag">{t.resolved_node}</span>{" "}
                              <code>{t.resolved_model}</code>
                              {t.fallback_reason && (
                                <span className="pill pill--warn">{t.fallback_reason}</span>
                              )}
                            </td>
                            <td>
                              <span
                                className={`pill ${t.status_code < 400 ? "pill--ok" : "pill--err"}`}
                              >
                                {t.status_code}
                              </span>
                            </td>
                            <td>{t.ttft_ms !== null ? `${t.ttft_ms}ms` : "—"}</td>
                            <td>
                              {t.tokens_per_sec !== null
                                ? `${t.tokens_per_sec} t/s`
                                : `${t.duration_ms}ms`}
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </section>
            </div>
          </div>

          <div
            ref={(el) => {
              panelRefs.current.models = el;
            }}
            role="tabpanel"
            aria-labelledby={tabIds.models}
            hidden={activeTab !== "models" ? ("until-found" as unknown as boolean) : undefined}
          >
            <ModelsTab
              nodes={snapshot?.nodes ?? []}
              aliases={snapshot?.aliases ?? []}
              onStatusRefresh={fetchStatus}
              onError={setErrorMsg}
              onConfirm={setConfirmState}
            />
          </div>

          <div
            ref={(el) => {
              panelRefs.current.mcp = el;
            }}
            role="tabpanel"
            aria-labelledby={tabIds.mcp}
            hidden={activeTab !== "mcp" ? ("until-found" as unknown as boolean) : undefined}
          >
            <McpTab
              nodes={snapshot?.nodes ?? []}
              onError={setErrorMsg}
              onConfirm={setConfirmState}
            />
          </div>

          <div
            ref={(el) => {
              panelRefs.current.skills = el;
            }}
            role="tabpanel"
            aria-labelledby={tabIds.skills}
            hidden={activeTab !== "skills" ? ("until-found" as unknown as boolean) : undefined}
          >
            <SkillsTab onError={setErrorMsg} onConfirm={setConfirmState} />
          </div>
        </div>
      </div>

      <SettingsDialog
        dialogRef={settingsDialogRef}
        activeTab={settingsTab}
        onTabChange={setSettingsTab}
        snapshot={snapshot}
        onStatusRefresh={fetchStatus}
        onError={setErrorMsg}
      />

      <dialog
        ref={authDialogRef}
        aria-labelledby={authTitleId}
        {...({ closedby: "none" } as Record<string, string>)}
      >
        <h2 id={authTitleId}>
          <Lock size={16} aria-hidden="true" /> Unlock Splitch
        </h2>
        {loginError && (
          <div role="alert" className="banner banner--error">
            {loginError}
          </div>
        )}
        <form className="modal-form" onSubmit={submitLogin}>
          <label>
            API Key
            <input
              type="password"
              required
              autoFocus
              autoComplete="current-password"
              value={loginKey}
              onChange={(e) => setLoginKey(e.target.value)}
              placeholder="Enter SPLITCH_API_KEY…"
            />
          </label>
          <div className="modal-actions">
            <button type="submit" className="btn btn--accent">
              Unlock Session
            </button>
          </div>
        </form>
      </dialog>

      <ConfirmDialog pending={confirmState} onClose={() => setConfirmState(null)} />
    </div>
  );
}

function NodeCard({
  node,
  canRemove,
  busyAction,
  onModelAction,
  onRemove
}: {
  node: NodeStatus;
  canRemove: boolean;
  busyAction: string | null;
  onModelAction: (nodeId: string, model: string, action: "load" | "unload") => void;
  onRemove: (nodeId: string, displayName: string) => void;
}) {
  const t = node.telemetry;
  const vramPct =
    t && t.vram_total_mb > 0
      ? Math.min(100, Math.round((t.vram_used_mb / t.vram_total_mb) * 100))
      : 0;
  const ramPct =
    t && t.ram_total_mb > 0
      ? Math.min(100, Math.round((t.ram_used_mb / t.ram_total_mb) * 100))
      : 0;
  const diskUsedGb = t ? Math.max(0, t.storage_total_gb - t.storage_free_gb) : 0;
  const diskPct =
    t && t.storage_total_gb > 0
      ? Math.min(100, Math.round((diskUsedGb / t.storage_total_gb) * 100))
      : 0;

  return (
    <article className={`node-card ${node.online ? "" : "node-card--offline"}`}>
      <header className="node-card__header">
        <div>
          <h2>{node.display_name}</h2>
          <p className="muted">
            {t?.gpu_name ?? (node.role === "gpu" ? "GPU Node" : node.role === "unified" ? "Unified Memory" : "CPU Node")}{" "}
            • {node.online ? `${node.latency_ms}ms` : "unreachable"}
          </p>
        </div>
        <div className="node-card__badges">
          {t?.gpus && t.gpus.length > 1 && (
            <span className="pill pill--accent">
              {t.gpus.length} GPUs{t.recommended_backend ? ` · ${t.recommended_backend}` : ""}
            </span>
          )}
          {t?.engine && t.engine !== "unknown" && <span className="pill">{t.engine}</span>}
          {t?.media_healthy && t.media_engine && t.media_engine !== "none" && (
            <span className="pill pill--accent">{t.media_engine}</span>
          )}
          {node.yield_on_gaming && <span className="pill">YIELD</span>}
          <span
            className={`pill ${
              node.online && t?.engine_healthy && !node.busy_reason
                ? "pill--ok"
                : node.online
                  ? "pill--warn"
                  : "pill--err"
            }`}
          >
            {!node.online
              ? "OFFLINE"
              : !node.busy_reason
                ? "READY"
                : node.busy_reason.endsWith("_engine_down")
                  ? "ENGINE OFFLINE"
                  : node.busy_reason.endsWith("_gpu_busy_app") ||
                      node.busy_reason.endsWith("_gpu_busy") ||
                      node.busy_reason === "gaming_mode"
                    ? "GPU YIELDED"
                    : node.busy_reason.endsWith("_gpu_cooldown")
                      ? "COOLDOWN"
                      : node.busy_reason.endsWith("_vram_low")
                        ? "LOW VRAM"
                        : node.busy_reason.endsWith("_storage_unmounted")
                          ? "UNMOUNTED"
                          : node.busy_reason.startsWith("forced_")
                            ? "STANDBY"
                            : node.busy_reason.replace(`${node.node_id}_`, "").toUpperCase()}
          </span>
          {node.in_flight > 0 && <span className="pill">{node.in_flight} ACTIVE</span>}
          {canRemove && (
            <button
              type="button"
              className="btn btn--sm btn--ghost btn--danger"
              aria-label={`Remove ${node.display_name}`}
              disabled={busyAction === `rm:${node.node_id}`}
              onClick={() => onRemove(node.node_id, node.display_name)}
            >
              <Trash2 size={12} aria-hidden="true" />
            </button>
          )}
        </div>
      </header>

      <div className="node-card__body">
        <div className="metrics-grid">
          <div className="meter">
            <span className="meter__label">
              <Cpu size={13} aria-hidden="true" />
              <span>
                {node.role === "gpu"
                  ? "VRAM"
                  : node.role === "unified"
                    ? "Unified GPU"
                    : "Accelerator"}
              </span>
            </span>
            <progress
              max={100}
              value={vramPct}
              aria-label={`${node.display_name} GPU memory usage`}
            />
            <strong className="meter__val">
              {t && t.vram_total_mb > 0
                ? `${(t.vram_used_mb / 1024).toFixed(1)} / ${(t.vram_total_mb / 1024).toFixed(1)} GB`
                : "—"}
            </strong>
            <small className="muted meter__sub">
              {t && node.role === "gpu"
                ? `Util ${t.gpu_util_percent}% • ${t.gpu_temp_c !== null ? `${t.gpu_temp_c.toFixed(0)}°C` : "—"} • Non-LLM ${fmtGb(t.non_llm_vram_mb)}`
                : node.role === "unified"
                  ? "Unified memory pool"
                  : "—"}
            </small>
          </div>

          <div className="meter">
            <span className="meter__label">
              <Activity size={13} aria-hidden="true" />
              <span>RAM</span>
            </span>
            <progress
              max={100}
              value={ramPct}
              aria-label={`${node.display_name} System RAM usage`}
            />
            <strong className="meter__val">
              {t
                ? `${(t.ram_used_mb / 1024).toFixed(1)} / ${(t.ram_total_mb / 1024).toFixed(1)} GB`
                : "—"}
            </strong>
            <small className="muted meter__sub">{t ? `CPU ${t.cpu_percent}%` : "—"}</small>
          </div>

          <div className="meter">
            <span className="meter__label">
              <HardDrive size={13} aria-hidden="true" />
              <span>Storage</span>
            </span>
            <progress
              max={100}
              value={diskPct}
              aria-label={`${node.display_name} Model storage usage`}
            />
            <strong className="meter__val">
              {t && t.storage_mounted
                ? `${t.storage_free_gb.toFixed(0)} GB free`
                : "UNMOUNTED"}
            </strong>
            <small className="muted meter__sub" title={t?.storage_path || ""}>
              {t?.storage_path || "—"}
            </small>
          </div>
        </div>

        {t?.gpus && t.gpus.length > 1 && (
          <div className="gpu-sublist" aria-label={`${node.display_name} GPU devices`}>
            {t.gpus.map((g) => {
              const gPct =
                g.vram_total_mb > 0
                  ? Math.min(100, Math.round((g.vram_used_mb / g.vram_total_mb) * 100))
                  : 0;
              const watchedList = node.yield_gpu_indices ?? [];
              const isWatched = watchedList.length === 0 || watchedList.includes(g.index);
              return (
                <div key={`${g.index}-${g.bus_id ?? g.name}`} className="gpu-subrow">
                  <span className="gpu-subrow__id">
                    <span className="pill">GPU {g.index}</span>
                    <span className="pill">{g.vendor.toUpperCase()}</span>
                    <span className="gpu-subrow__name" title={g.name}>
                      {g.name}
                    </span>
                  </span>
                  <progress
                    max={100}
                    value={gPct}
                    aria-label={`GPU ${g.index} ${g.name} VRAM usage`}
                  />
                  <strong className="meter__val">
                    {g.vram_total_mb > 0
                      ? `${(g.vram_used_mb / 1024).toFixed(1)} / ${(g.vram_total_mb / 1024).toFixed(1)} GB`
                      : "—"}
                  </strong>
                  <small className="muted meter__sub">
                    {`${g.util_percent}% • ${g.temp_c !== null ? `${g.temp_c.toFixed(0)}°C` : "—"}`}
                    {g.non_llm_vram_mb > 0 ? ` • Ext ${fmtGb(g.non_llm_vram_mb)}` : ""}
                    {node.yield_on_gaming && watchedList.length > 0 && !isWatched ? " • Unwatched" : ""}
                  </small>
                </div>
              );
            })}
          </div>
        )}

        <div className="models-list">
          <h3>Engine Models</h3>
          {!t || !t.engine_healthy ? (
            <p className="muted">Offline ({node.engine_url})</p>
          ) : t.models.length === 0 ? (
            <p className="muted">No models.</p>
          ) : (
            <ul>
              {t.models.map((m) => {
                const isLoaded = m.status === "loaded";
                const action = isLoaded ? "unload" : "load";
                const key = `${node.node_id}:${m.id}:${action}`;
                return (
                  <li key={m.id} className="model-row">
                    <div>
                      <code>{m.id}</code>
                      <span className={`pill ${isLoaded ? "pill--ok" : ""}`}>{m.status}</span>
                    </div>
                    {(t.engine === "llama-server" || t.engine === "ollama") && (
                      <button
                        type="button"
                        className="btn btn--sm"
                        disabled={busyAction === key}
                        onClick={() => onModelAction(node.node_id, m.id, action)}
                      >
                        <Power size={12} aria-hidden="true" />
                        {isLoaded ? "Unload" : "Load"}
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>

      <footer className="node-card__footer muted">
        <span>Agent: {node.agent_url}</span>
        <span>Engine: {node.engine_url}</span>
      </footer>
    </article>
  );
}
