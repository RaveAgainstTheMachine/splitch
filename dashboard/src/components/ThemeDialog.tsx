import { useEffect, useId, useRef, useState } from "react";
import { Monitor, Moon, Palette, RotateCcw, Sun } from "lucide-react";
import { useDialogLightDismiss } from "./ModelsTab";
import {
  applyThemeToDom,
  isOsLightMode,
  pushThemeToRouter,
  readLocalTheme,
  syncFaviconFromTheme,
  THEME_SYNC_EVENT
} from "../utils/favicon";
import type { UiThemeConfig } from "../types";

export type ColorSchemePref = "system" | "dark" | "light";
export type SurfacePreset = "default" | "oled" | "slate" | "charcoal";

const ACCENT_PRESETS: Array<{ id: string; label: string; hex: string }> = [
  { id: "violet", label: "Violet", hex: "#a78bfa" },
  { id: "cyan", label: "Cyan", hex: "#22d3ee" },
  { id: "emerald", label: "Emerald", hex: "#34d399" },
  { id: "amber", label: "Amber", hex: "#f59e0b" },
  { id: "rose", label: "Rose", hex: "#fb7185" },
  { id: "blue", label: "Cobalt", hex: "#60a5fa" }
];

const SURFACE_PRESETS: Array<{ id: SurfacePreset; label: string }> = [
  { id: "default", label: "Midnight" },
  { id: "oled", label: "OLED Black" },
  { id: "slate", label: "Nordic Slate" },
  { id: "charcoal", label: "Warm Graphite" }
];

export function AppearancePanel() {
  const colorInputId = useId();
  const initial = readLocalTheme();
  const [scheme, setScheme] = useState<ColorSchemePref>(initial.scheme);
  const [accent, setAccent] = useState<string>(initial.accent);
  const [surface, setSurface] = useState<SurfacePreset>(initial.surface);
  const [osLight, setOsLight] = useState<boolean>(isOsLightMode);
  const [faviconUrl, setFaviconUrl] = useState<string>(() => syncFaviconFromTheme());
  const [syncBadge, setSyncBadge] = useState<{ text: string; ok: boolean } | null>(null);
  const debounceTimerRef = useRef<number | null>(null);
  const pendingThemeRef = useRef<UiThemeConfig | null>(null);
  const badgeTimerRef = useRef<number | null>(null);

  const scheduleRouterSync = (next: UiThemeConfig, debounced: boolean) => {
    if (debounceTimerRef.current !== null) {
      window.clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }
    pendingThemeRef.current = next;
    const send = async () => {
      pendingThemeRef.current = null;
      const saved = await pushThemeToRouter(next);
      if (saved) {
        window.dispatchEvent(new CustomEvent<UiThemeConfig>(THEME_SYNC_EVENT, { detail: saved }));
        setSyncBadge({ text: "Icons synced", ok: true });
      } else {
        setSyncBadge({ text: "Sync failed", ok: false });
      }
      if (badgeTimerRef.current !== null) window.clearTimeout(badgeTimerRef.current);
      badgeTimerRef.current = window.setTimeout(() => setSyncBadge(null), 2400);
    };
    if (debounced) {
      debounceTimerRef.current = window.setTimeout(() => {
        debounceTimerRef.current = null;
        void send();
      }, 180);
    } else {
      void send();
    }
  };

  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === "color-scheme" || e.key === "splitch-accent" || e.key === "splitch-surface") {
        const cur = readLocalTheme();
        setScheme(cur.scheme);
        setAccent(cur.accent);
        setSurface(cur.surface);
        setFaviconUrl(applyThemeToDom(cur));
      }
    };
    const onThemeSync = (e: Event) => {
      const detail = (e as CustomEvent<UiThemeConfig>).detail;
      if (!detail) return;
      setScheme(detail.scheme);
      setAccent(detail.accent);
      setSurface(detail.surface);
      setFaviconUrl(syncFaviconFromTheme());
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener(THEME_SYNC_EVENT, onThemeSync);

    const mq =
      typeof window !== "undefined" && typeof window.matchMedia === "function"
        ? window.matchMedia("(prefers-color-scheme: light)")
        : null;
    const onMedia = (ev: MediaQueryListEvent) => {
      setOsLight(ev.matches);
      setFaviconUrl(syncFaviconFromTheme());
    };
    mq?.addEventListener("change", onMedia);

    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(THEME_SYNC_EVENT, onThemeSync);
      mq?.removeEventListener("change", onMedia);
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
        if (pendingThemeRef.current) {
          const toFlush = pendingThemeRef.current;
          pendingThemeRef.current = null;
          void pushThemeToRouter(toFlush);
        }
      }
      if (badgeTimerRef.current !== null) window.clearTimeout(badgeTimerRef.current);
    };
  }, []);

  const commitTheme = (
    nextScheme: ColorSchemePref,
    nextAccent: string,
    nextSurface: SurfacePreset,
    debounced = false
  ) => {
    const next: UiThemeConfig = {
      scheme: nextScheme,
      accent: nextAccent,
      surface: nextSurface,
      resolved_light: nextScheme === "light" || (nextScheme === "system" && isOsLightMode())
    };
    setScheme(nextScheme);
    setAccent(nextAccent);
    setSurface(nextSurface);
    setFaviconUrl(applyThemeToDom(next));
    scheduleRouterSync(next, debounced);
  };

  const resetAll = () => {
    commitTheme("system", "", "default", false);
  };

  return (
    <div className="modal-form">
      <div className="panel-head">
        <div className="row-actions">
          <img
            src={faviconUrl}
            alt="Live Splitch icon preview"
            width={24}
            height={24}
            className="favicon-preview"
          />
          <div>
            <h3 style={{ margin: 0 }}>Appearance &amp; Theme</h3>
            <span className="muted" style={{ fontSize: "0.72rem" }}>
              Syncs browser favicon, system tray, desktop &amp; app menu icons automatically
            </span>
          </div>
        </div>
        <div className="row-actions">
          {syncBadge && (
            <span className={`pill ${syncBadge.ok ? "pill--ok" : "pill--warn"}`}>
              {syncBadge.text}
            </span>
          )}
          <button type="button" className="btn btn--sm" onClick={resetAll}>
            <RotateCcw size={12} aria-hidden="true" /> Reset Defaults
          </button>
        </div>
      </div>

      <fieldset className="theme-fieldset">
        <legend>Color Scheme</legend>
        <div className="theme-scheme-grid">
          {(
            [
              { id: "system", label: `System (${osLight ? "Light" : "Dark"})`, icon: Monitor },
              { id: "dark", label: "Dark", icon: Moon },
              { id: "light", label: "Light", icon: Sun }
            ] as const
          ).map(({ id, label, icon: Icon }) => (
            <label key={id} className="choice-card">
              <input
                type="radio"
                name="splitch_color_scheme"
                value={id}
                checked={scheme === id}
                onChange={() => commitTheme(id, accent, surface, false)}
              />
              <Icon size={13} aria-hidden="true" />
              <span>{label}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <fieldset className="theme-fieldset">
        <legend>Accent Color</legend>
        <div className="theme-swatch-row">
          {ACCENT_PRESETS.map((p) => {
            const active = accent.toLowerCase() === p.hex.toLowerCase();
            return (
              <button
                key={p.id}
                type="button"
                className={`theme-swatch ${active ? "theme-swatch--active" : ""}`}
                style={{ "--swatch-bg": p.hex } as React.CSSProperties}
                aria-pressed={active}
                onClick={() => commitTheme(scheme, p.hex, surface, false)}
              >
                {p.label}
              </button>
            );
          })}
          <label htmlFor={colorInputId} className="theme-custom-picker">
            <input
              id={colorInputId}
              type="color"
              value={accent || "#a78bfa"}
              onChange={(e) => commitTheme(scheme, e.target.value, surface, true)}
            />
            <span>Custom</span>
          </label>
        </div>
      </fieldset>

      <fieldset className="theme-fieldset">
        <legend>Surface</legend>
        <div className="theme-surface-grid">
          {SURFACE_PRESETS.map((s) => (
            <label key={s.id} className="choice-card theme-surface-card">
              <input
                type="radio"
                name="splitch_surface"
                value={s.id}
                checked={surface === s.id}
                onChange={() => commitTheme(scheme, accent, s.id, false)}
              />
              <span>{s.label}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div
        className="muted"
        style={{
          fontSize: "0.73rem",
          paddingTop: "0.5rem",
          borderTop: "1px solid var(--border)"
        }}
      >
        Powered by:{" "}
        <a href="https://github.com/ggml-org/llama.cpp" target="_blank" rel="noreferrer">
          llama.cpp
        </a>
        {" • "}
        <a href="https://github.com/leejet/stable-diffusion.cpp" target="_blank" rel="noreferrer">
          sd.cpp
        </a>
        {" • "}
        <a href="https://github.com/comfyanonymous/ComfyUI" target="_blank" rel="noreferrer">
          ComfyUI
        </a>
        {" • "}
        <a href="https://ollama.com" target="_blank" rel="noreferrer">
          Ollama
        </a>
        {" • "}
        <a href="https://pypi.org/project/graphifyy/" target="_blank" rel="noreferrer">
          Graphify
        </a>
        {" • "}
        <a href="https://modelcontextprotocol.io" target="_blank" rel="noreferrer">
          MCP
        </a>
        {" • "}
        <a href="https://skills.sh" target="_blank" rel="noreferrer">
          skills.sh
        </a>
        {" • "}
        <a href="https://github.com/unslothai/unsloth" target="_blank" rel="noreferrer">
          Unsloth
        </a>
        {" • "}
        <a href="https://huggingface.co" target="_blank" rel="noreferrer">
          Hugging Face
        </a>
        <div style={{ marginTop: "0.3rem" }}>
          UI improvements contributed by <strong>𝔍𝔞𝔠𝔨𝔞𝔩𝔬𝔭𝔢 𝔐𝔬𝔬𝔫</strong> (Jackalope Moon)
        </div>
      </div>
    </div>
  );
}

export function ThemeSettingsButton() {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const titleId = useId();
  useDialogLightDismiss(dialogRef);

  return (
    <>
      <button
        type="button"
        className="btn btn--ghost"
        onClick={() => dialogRef.current?.showModal()}
        aria-label="Appearance and theme settings"
        title="Appearance & Theme"
      >
        <Palette size={14} aria-hidden="true" />
      </button>

      <dialog
        ref={dialogRef}
        aria-labelledby={titleId}
        {...({ closedby: "any" } as Record<string, string>)}
      >
        <h2 id={titleId} className="visually-hidden">
          Appearance &amp; Theme
        </h2>
        <AppearancePanel />
        <div className="modal-actions" style={{ marginTop: "0.75rem" }}>
          <button
            type="button"
            className="btn btn--accent"
            onClick={() => dialogRef.current?.close()}
          >
            Done
          </button>
        </div>
      </dialog>
    </>
  );
}
