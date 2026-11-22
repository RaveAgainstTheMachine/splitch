import { useEffect, useRef } from "react";

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/**
 * Semantic circular context window indicator built on native <progress>.
 * Uses CSS attr(value type(<number>)) + conic-gradient + background-clip: border-area,
 * with a MutationObserver fallback for browsers lacking typed attr().
 */
export function ContextRing({
  usedTokens,
  maxTokens,
  estimated = false
}: {
  usedTokens: number;
  maxTokens: number;
  estimated?: boolean;
}) {
  const progressRef = useRef<HTMLProgressElement | null>(null);
  const safeMax = Math.max(1024, maxTokens);
  const pct = Math.min(100, Math.max(0, Math.round((usedTokens / safeMax) * 100)));
  const tone = pct >= 90 ? "err" : pct >= 70 ? "warn" : "ok";
  const valueText = `${estimated ? "~" : ""}${fmtTokens(usedTokens)} of ${fmtTokens(safeMax)} tokens (${pct}%)`;

  useEffect(() => {
    const el = progressRef.current;
    if (!el) return;
    if (!CSS.supports("width: attr(value type(<number>))")) {
      const sync = () => {
        el.style.setProperty("--value", el.getAttribute("value") || "0");
      };
      sync();
      const observer = new MutationObserver(sync);
      observer.observe(el, { attributes: true, attributeFilter: ["value"] });
      return () => observer.disconnect();
    }
  }, []);

  return (
    <div className="ctx-meter" title={`Context window: ${valueText}`}>
      <div className="progress-ring-wrapper">
        <progress
          ref={progressRef}
          value={pct}
          max={100}
          aria-label="Context window usage"
          aria-valuetext={valueText}
          className={`progress-ring progress-ring--${tone}`}
        />
        <div className="progress-ring-content" aria-hidden="true">
          {pct}%
        </div>
      </div>
      <span className="ctx-meter__text muted">
        {estimated ? "~" : ""}
        {fmtTokens(usedTokens)} / {fmtTokens(safeMax)}
      </span>
    </div>
  );
}
