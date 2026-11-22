import { useEffect, useId, useRef, useState } from "react";
import { Monitor, Moon, Palette, RotateCcw, Sun } from "lucide-react";
import { useDialogLightDismiss } from "./ModelsTab";

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

function applyScheme(pref: ColorSchemePref) {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="color-scheme"]');
  if (meta) {
    meta.content = pref === "system" ? "light dark" : pref;
  }
  try {
    if (pref === "system") {
      localStorage.removeItem("color-scheme");
    } else {
      localStorage.setItem("color-scheme", pref);
    }
  } catch {
    // storage disabled; still applied in DOM
  }
}

function applyAccent(hex: string) {
  if (hex) {
    document.documentElement.style.setProperty("--user-accent", hex);
  } else {
    document.documentElement.style.removeProperty("--user-accent");
  }
  try {
    if (hex) localStorage.setItem("splitch-accent", hex);
    else localStorage.removeItem("splitch-accent");
  } catch {
    // ignore storage errors
  }
}

function applySurface(surface: SurfacePreset) {
  if (surface === "default") {
    delete document.documentElement.dataset.surface;
  } else {
    document.documentElement.dataset.surface = surface;
  }
  try {
    if (surface === "default") localStorage.removeItem("splitch-surface");
    else localStorage.setItem("splitch-surface", surface);
  } catch {
    // ignore
  }
}

function readInitialScheme(): ColorSchemePref {
  try {
    const v = localStorage.getItem("color-scheme");
    if (v === "dark" || v === "light") return v;
  } catch {
    // ignore
  }
  return "system";
}

function readInitialAccent(): string {
  try {
    const v = localStorage.getItem("splitch-accent");
    if (v && /^#[0-9a-fA-F]{6}$/.test(v)) return v;
  } catch {
    // ignore
  }
  return "";
}

function readInitialSurface(): SurfacePreset {
  try {
    const v = localStorage.getItem("splitch-surface");
    if (v === "oled" || v === "slate" || v === "charcoal") return v;
  } catch {
    // ignore
  }
  return "default";
}

export function AppearancePanel() {
  const colorInputId = useId();
  const [scheme, setScheme] = useState<ColorSchemePref>(readInitialScheme);
  const [accent, setAccent] = useState<string>(readInitialAccent);
  const [surface, setSurface] = useState<SurfacePreset>(readInitialSurface);

  // Cross-tab storage synchronization
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === "color-scheme") {
        const next: ColorSchemePref =
          e.newValue === "dark" || e.newValue === "light" ? e.newValue : "system";
        setScheme(next);
        const meta = document.querySelector<HTMLMetaElement>('meta[name="color-scheme"]');
        if (meta) meta.content = next === "system" ? "light dark" : next;
      } else if (e.key === "splitch-accent") {
        const val = e.newValue ?? "";
        setAccent(val);
        if (val) document.documentElement.style.setProperty("--user-accent", val);
        else document.documentElement.style.removeProperty("--user-accent");
      } else if (e.key === "splitch-surface") {
        const val =
          e.newValue === "oled" || e.newValue === "slate" || e.newValue === "charcoal"
            ? e.newValue
            : "default";
        setSurface(val);
        if (val === "default") delete document.documentElement.dataset.surface;
        else document.documentElement.dataset.surface = val;
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const chooseScheme = (next: ColorSchemePref) => {
    setScheme(next);
    applyScheme(next);
  };

  const chooseAccent = (hex: string) => {
    setAccent(hex);
    applyAccent(hex);
  };

  const chooseSurface = (next: SurfacePreset) => {
    setSurface(next);
    applySurface(next);
  };

  const resetAll = () => {
    chooseScheme("system");
    chooseAccent("");
    chooseSurface("default");
  };

  return (
    <div className="modal-form">
      <div className="panel-head">
        <h3>Appearance &amp; Theme</h3>
        <button type="button" className="btn btn--sm" onClick={resetAll}>
          <RotateCcw size={12} aria-hidden="true" /> Reset Defaults
        </button>
      </div>

      <fieldset className="theme-fieldset">
        <legend>Color Scheme</legend>
        <div className="theme-scheme-grid">
          {(
            [
              { id: "system", label: "System", icon: Monitor },
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
                onChange={() => chooseScheme(id)}
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
                onClick={() => chooseAccent(p.hex)}
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
              onChange={(e) => chooseAccent(e.target.value)}
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
                onChange={() => chooseSurface(s.id)}
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
