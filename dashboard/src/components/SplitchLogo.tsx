import { useId } from "react";

/**
 * Splitch brand emblem (Concept 6 — Architectural Shear S).
 * Adapts automatically to the active light/dark color scheme and user-selected --accent color.
 */
export function SplitchLogo({ size = 28 }: { size?: number }) {
  const uid = useId().replace(/:/g, "");
  const topClipId = `splitch-top-${uid}`;
  const botClipId = `splitch-bot-${uid}`;
  const lowerGradId = `splitch-lower-${uid}`;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden="true"
      className="splitch-logo"
    >
      <defs>
        <linearGradient id={lowerGradId} x1="16" y1="32" x2="52" y2="56" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="var(--accent)" />
          <stop offset="100%" stopColor="color-mix(in srgb, var(--accent) 68%, #4f46e5)" />
        </linearGradient>
        <clipPath id={topClipId}>
          <rect x="0" y="0" width="64" height="30.6" />
        </clipPath>
        <clipPath id={botClipId}>
          <rect x="0" y="33.4" width="64" height="30.6" />
        </clipPath>
      </defs>

      <rect width="64" height="64" rx="14" fill="var(--surface-2)" />
      <rect x="1" y="1" width="62" height="62" rx="13" stroke="var(--border)" strokeWidth="2" />

      <g clipPath={`url(#${topClipId})`} transform="translate(-1.8 0)">
        <path
          d="M 44 21.5 C 44 15.8 39 13 32 13 C 24.5 13 20 16.2 20 21.5 C 20 27 25 29.2 32.5 31 C 40 32.8 45 35.2 45 42 C 45 48 39.5 51 31.5 51 C 24 51 19 48 19 42.5"
          stroke="var(--text)"
          strokeWidth="10.5"
          strokeLinecap="butt"
        />
        <rect x="12.5" y="29" width="29" height="1.7" rx="0.85" fill="var(--text)" />
      </g>

      <g clipPath={`url(#${botClipId})`} transform="translate(1.8 0)">
        <path
          d="M 44 21.5 C 44 15.8 39 13 32 13 C 24.5 13 20 16.2 20 21.5 C 20 27 25 29.2 32.5 31 C 40 32.8 45 35.2 45 42 C 45 48 39.5 51 31.5 51 C 24 51 19 48 19 42.5"
          stroke={`url(#${lowerGradId})`}
          strokeWidth="10.5"
          strokeLinecap="butt"
        />
        <rect x="22.5" y="33.4" width="29" height="1.7" rx="0.85" fill="var(--accent)" />
      </g>
    </svg>
  );
}
