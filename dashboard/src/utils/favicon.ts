/**
 * Dynamic Splitch SVG favicon generator synchronized with the user's active
 * color scheme (system/dark/light), surface preset, and accent color.
 */

function clampByte(n: number): number {
  return Math.max(0, Math.min(255, Math.round(n)));
}

function mixHex(hexA: string, hexB: string, ratioA: number): string {
  const parse = (h: string): [number, number, number] => {
    const clean = h.replace("#", "");
    return [
      parseInt(clean.slice(0, 2), 16),
      parseInt(clean.slice(2, 4), 16),
      parseInt(clean.slice(4, 6), 16)
    ];
  };
  try {
    const [r1, g1, b1] = parse(hexA);
    const [r2, g2, b2] = parse(hexB);
    const r = clampByte(r1 * ratioA + r2 * (1 - ratioA));
    const g = clampByte(g1 * ratioA + g2 * (1 - ratioA));
    const b = clampByte(b1 * ratioA + b2 * (1 - ratioA));
    return `#${[r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
  } catch {
    return hexA;
  }
}

export function resolveEffectiveTheme(): {
  isLight: boolean;
  accent: string;
  surface: string;
} {
  let schemeMeta = "light dark";
  const meta = document.querySelector<HTMLMetaElement>('meta[name="color-scheme"]');
  if (meta?.content) schemeMeta = meta.content.trim();

  const osLight =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: light)").matches;

  const isLight = schemeMeta === "light" || (schemeMeta !== "dark" && osLight);

  let rawAccent = document.documentElement.style.getPropertyValue("--user-accent").trim();
  if (!/^#[0-9a-fA-F]{6}$/.test(rawAccent)) {
    try {
      const saved = localStorage.getItem("splitch-accent") || "";
      if (/^#[0-9a-fA-F]{6}$/.test(saved)) rawAccent = saved;
    } catch {
      // ignore
    }
  }
  const accent = /^#[0-9a-fA-F]{6}$/.test(rawAccent)
    ? rawAccent
    : isLight
      ? "#5b21b6"
      : "#a78bfa";

  const surface = document.documentElement.dataset.surface || "default";
  return { isLight, accent, surface };
}

export function buildSplitchFaviconSvg(opts?: {
  isLight?: boolean;
  accent?: string;
  surface?: string;
}): string {
  const cur = resolveEffectiveTheme();
  const isLight = opts?.isLight ?? cur.isLight;
  const accent = opts?.accent && /^#[0-9a-fA-F]{6}$/.test(opts.accent) ? opts.accent : cur.accent;
  const surface = opts?.surface ?? cur.surface;

  let bg = "#0a0b10";
  let border = "#262a3d";
  let upperStart = "#ffffff";
  let upperEnd = mixHex(accent, "#ffffff", 0.18);
  if (isLight) {
    bg = "#f6f7fb";
    border = "#d7dbe8";
    upperStart = "#121521";
    upperEnd = "#262a3d";
  } else if (surface === "oled") {
    bg = "#000000";
    border = "#222536";
  } else if (surface === "slate") {
    bg = "#0b1120";
    border = "#334155";
  } else if (surface === "charcoal") {
    bg = "#141416";
    border = "#383842";
  }

  const lowerStart = mixHex(accent, "#ffffff", 0.82);
  const lowerEnd = mixHex(accent, "#0f172a", 0.72);

  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">` +
    `<defs>` +
    `<linearGradient id="sl" x1="16" y1="32" x2="52" y2="56" gradientUnits="userSpaceOnUse">` +
    `<stop offset="0%" stop-color="${lowerStart}"/>` +
    `<stop offset="50%" stop-color="${accent}"/>` +
    `<stop offset="100%" stop-color="${lowerEnd}"/>` +
    `</linearGradient>` +
    `<linearGradient id="su" x1="14" y1="8" x2="48" y2="32" gradientUnits="userSpaceOnUse">` +
    `<stop offset="0%" stop-color="${upperStart}"/>` +
    `<stop offset="100%" stop-color="${upperEnd}"/>` +
    `</linearGradient>` +
    `<clipPath id="ct"><rect x="0" y="0" width="64" height="30.6"/></clipPath>` +
    `<clipPath id="cb"><rect x="0" y="33.4" width="64" height="30.6"/></clipPath>` +
    `</defs>` +
    `<rect width="64" height="64" rx="14" fill="${bg}"/>` +
    `<rect x="1" y="1" width="62" height="62" rx="13" stroke="${border}" stroke-width="1.8"/>` +
    `<g clip-path="url(#ct)" transform="translate(-1.8 0)">` +
    `<path d="M 44 21.5 C 44 15.8 39 13 32 13 C 24.5 13 20 16.2 20 21.5 C 20 27 25 29.2 32.5 31 C 40 32.8 45 35.2 45 42 C 45 48 39.5 51 31.5 51 C 24 51 19 48 19 42.5" stroke="url(#su)" stroke-width="10.5" stroke-linecap="butt"/>` +
    `<rect x="12.5" y="29" width="29" height="1.7" rx="0.85" fill="${upperStart}"/>` +
    `</g>` +
    `<g clip-path="url(#cb)" transform="translate(1.8 0)">` +
    `<path d="M 44 21.5 C 44 15.8 39 13 32 13 C 24.5 13 20 16.2 20 21.5 C 20 27 25 29.2 32.5 31 C 40 32.8 45 35.2 45 42 C 45 48 39.5 51 31.5 51 C 24 51 19 48 19 42.5" stroke="url(#sl)" stroke-width="10.5" stroke-linecap="butt"/>` +
    `<rect x="22.5" y="33.4" width="29" height="1.7" rx="0.85" fill="${accent}"/>` +
    `</g>` +
    `</svg>`
  );
}

export function syncFaviconFromTheme(): string {
  const svg = buildSplitchFaviconSvg();
  const dataUrl = `data:image/svg+xml,${encodeURIComponent(svg)}`;
  let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    link.type = "image/svg+xml";
    document.head.appendChild(link);
  }
  link.type = "image/svg+xml";
  link.href = dataUrl;
  return dataUrl;
}
