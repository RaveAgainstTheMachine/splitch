import { useEffect, useId, useMemo, useRef, useState } from "react";
import { AlertCircle, Code2, Maximize2, Play, RotateCw, Terminal, X } from "lucide-react";
import { useDialogLightDismiss } from "./ModelsTab";

interface LogEntry {
  level: "log" | "warn" | "error";
  text: string;
}

export function isRunnableArtifact(lang: string, code: string): boolean {
  const l = lang.toLowerCase();
  const trimmed = code.trim().toLowerCase();
  if (l === "html" || l === "htm" || l === "svg") return true;
  if (trimmed.startsWith("<!doctype html") || trimmed.startsWith("<html") || trimmed.startsWith("<svg")) {
    return true;
  }
  return false;
}

const SANDBOX_CSP =
  "default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; " +
  "style-src 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; " +
  "img-src data: blob: https:; font-src data: https:; connect-src 'none';";

function buildSrcDoc(lang: string, code: string): string {
  const bridge = `<script>
(function(){
  try { window.parent.postMessage({ __splitchPreview: true, type: 'init' }, '*'); } catch (_) {}
  function send(level, args) {
    try {
      var text = Array.prototype.map.call(args, function(a) {
        if (typeof a === 'string') return a;
        try { return JSON.stringify(a); } catch (_) { return String(a); }
      }).join(' ');
      window.parent.postMessage({ __splitchPreview: true, type: 'log', level: level, text: text.slice(0, 500) }, '*');
    } catch (_) {}
  }
  var origErr = console.error, origWarn = console.warn, origLog = console.log;
  console.error = function(){ send('error', arguments); return origErr && origErr.apply(console, arguments); };
  console.warn = function(){ send('warn', arguments); return origWarn && origWarn.apply(console, arguments); };
  console.log = function(){ send('log', arguments); return origLog && origLog.apply(console, arguments); };
  window.addEventListener('error', function(e){ send('error', [e.message + (e.lineno ? ' (line ' + e.lineno + ')' : '')]); });
  window.addEventListener('unhandledrejection', function(e){ send('error', ['Unhandled rejection:', String(e.reason)]); });
})();
</script>`;

  const cspMeta = `<meta http-equiv="Content-Security-Policy" content="${SANDBOX_CSP}">`;
  const trimmed = code.trim();
  const isSvg = lang.toLowerCase() === "svg" || trimmed.toLowerCase().startsWith("<svg");

  if (isSvg) {
    return `<!doctype html><html><head><meta charset="utf-8">${cspMeta}<style>html,body{margin:0;height:100%;display:grid;place-items:center;background:#0f111a;color:#eef0f8;}svg{max-width:95vw;max-height:95vh;height:auto;}</style>${bridge}</head><body>${code}</body></html>`;
  }

  if (/<head[^>]*>/i.test(code)) {
    return code.replace(/<head([^>]*)>/i, `<head$1>${cspMeta}${bridge}`);
  }
  if (/<html[^>]*>/i.test(code)) {
    return code.replace(/<html([^>]*)>/i, `<html$1><head>${cspMeta}${bridge}</head>`);
  }
  return `<!doctype html><html><head><meta charset="utf-8">${cspMeta}<style>body{margin:12px;font-family:system-ui,sans-serif;}</style>${bridge}</head><body>${code}</body></html>`;
}

type OriginConstructor = {
  new (): { isSameOrigin(other: unknown): boolean; opaque?: boolean };
  from(value: unknown): { isSameOrigin(other: unknown): boolean; opaque?: boolean };
};

function SandboxFrame({
  srcDoc,
  title,
  tall = false,
  onLog
}: {
  srcDoc: string;
  title: string;
  tall?: boolean;
  onLog: (entry: LogEntry) => void;
}) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const onLogRef = useRef(onLog);
  onLogRef.current = onLog;

  useEffect(() => {
    const OriginApi = (globalThis as unknown as { Origin?: OriginConstructor }).Origin;
    let pinnedOrigin = OriginApi ? new OriginApi() : null;
    let isPinned = false;

    const handleMessage = (event: MessageEvent) => {
      const expectedWin = iframeRef.current?.contentWindow;
      if (!expectedWin || event.source !== expectedWin) return;
      const data = event.data as {
        __splitchPreview?: boolean;
        type?: string;
        level?: LogEntry["level"];
        text?: string;
      } | null;
      if (!data || !data.__splitchPreview) return;

      // Pin opaque sandbox origin via Origin.from(event) when available, preventing "null" === "null" collisions
      if (OriginApi && pinnedOrigin) {
        try {
          const senderOrigin = OriginApi.from(event);
          if (!isPinned && data.type === "init") {
            pinnedOrigin = senderOrigin;
            isPinned = true;
            return;
          }
          if (!senderOrigin.isSameOrigin(pinnedOrigin)) return;
        } catch {
          return;
        }
      } else if (data.type === "init") {
        return;
      }

      if (data.type === "log" && data.text) {
        const lvl = data.level === "error" || data.level === "warn" ? data.level : "log";
        onLogRef.current({ level: lvl, text: String(data.text) });
      }
    };

    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [srcDoc]);

  return (
    <iframe
      ref={iframeRef}
      title={title}
      srcDoc={srcDoc}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      allow="camera 'none'; microphone 'none'; geolocation 'none'; focus-without-user-activation 'none'"
      className={`artifact-iframe ${tall ? "artifact-iframe--tall" : ""}`}
    />
  );
}

export function ArtifactPreview({
  lang,
  code,
  defaultOpen = false,
  label
}: {
  lang: string;
  code: string;
  defaultOpen?: boolean;
  label?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [showConsole, setShowConsole] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  const fsDialogRef = useRef<HTMLDialogElement | null>(null);
  const fsTitleId = useId();
  useDialogLightDismiss(fsDialogRef);

  const srcDoc = useMemo(() => buildSrcDoc(lang, code), [lang, code]);
  const errorCount = logs.filter((l) => l.level === "error").length;

  useEffect(() => {
    const d = fsDialogRef.current;
    if (!d) return;
    const onClose = () => setFullscreen(false);
    d.addEventListener("close", onClose);
    return () => d.removeEventListener("close", onClose);
  }, []);

  const openFullscreen = () => {
    setLogs([]);
    setFullscreen(true);
    fsDialogRef.current?.showModal();
  };

  const reload = () => {
    setLogs([]);
    setReloadNonce((n) => n + 1);
  };

  const appendLog = (entry: LogEntry) => {
    setLogs((prev) => [...prev.slice(-49), entry]);
  };

  return (
    <div className="artifact-preview">
      <div className="artifact-preview__bar">
        <div className="row-actions">
          <button
            type="button"
            className={`btn btn--sm ${open ? "btn--accent" : ""}`}
            aria-pressed={open}
            onClick={() => {
              if (!open) setLogs([]);
              setOpen((v) => !v);
            }}
          >
            {open ? <Code2 size={12} aria-hidden="true" /> : <Play size={12} aria-hidden="true" />}
            {open ? "Hide Preview" : `Run ${label || lang.toUpperCase()}`}
          </button>

          {open && (
            <>
              <button type="button" className="btn btn--sm btn--ghost" onClick={reload} title="Reload preview">
                <RotateCw size={12} aria-hidden="true" />
              </button>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={openFullscreen}
                title="Open fullscreen preview"
              >
                <Maximize2 size={12} aria-hidden="true" />
              </button>
              <button
                type="button"
                className={`btn btn--sm btn--ghost ${errorCount > 0 ? "text-err" : ""}`}
                onClick={() => setShowConsole((v) => !v)}
              >
                {errorCount > 0 ? (
                  <AlertCircle size={12} aria-hidden="true" />
                ) : (
                  <Terminal size={12} aria-hidden="true" />
                )}
                Console ({logs.length})
              </button>
            </>
          )}
        </div>
        <span className="pill" title="Isolated opaque origin; connect-src none; no Splitch API access">
          sandboxed
        </span>
      </div>

      {open && !fullscreen && (
        <div className="artifact-preview__stage">
          <SandboxFrame
            key={`${reloadNonce}-${srcDoc.length}`}
            srcDoc={srcDoc}
            title={`Sandboxed ${label || lang} preview`}
            onLog={appendLog}
          />
          {showConsole && (
            <div className="artifact-console">
              {logs.length === 0 ? (
                <span className="muted">No console output.</span>
              ) : (
                logs.map((entry, idx) => (
                  <div key={idx} className={`artifact-console__line artifact-console__line--${entry.level}`}>
                    [{entry.level}] {entry.text}
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}

      <dialog
        ref={fsDialogRef}
        className="artifact-dialog"
        aria-labelledby={fsTitleId}
        {...({ closedby: "any" } as Record<string, string>)}
      >
        <div className="panel-head">
          <h2 id={fsTitleId}>Sandboxed Preview — {label || lang.toUpperCase()}</h2>
          <div className="row-actions">
            <button type="button" className="btn btn--sm" onClick={reload}>
              <RotateCw size={12} aria-hidden="true" /> Reload
            </button>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => fsDialogRef.current?.close()}
            >
              <X size={12} aria-hidden="true" /> Close
            </button>
          </div>
        </div>
        {fullscreen && (
          <SandboxFrame
            key={`fs-${reloadNonce}-${srcDoc.length}`}
            srcDoc={srcDoc}
            title={`Fullscreen sandboxed ${label || lang} preview`}
            tall
            onLog={appendLog}
          />
        )}
      </dialog>
    </div>
  );
}
