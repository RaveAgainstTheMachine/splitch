import { useEffect, useId, useMemo, useRef, useState } from "react";
import { AlertCircle, Check, Code2, Copy, Maximize2, Play, RotateCw, Terminal, X } from "lucide-react";
import { useDialogLightDismiss } from "./ModelsTab";

export interface LogEntry {
  level: "log" | "warn" | "error";
  text: string;
}

export interface InspectedArtifact {
  id: string;
  lang: string;
  code: string;
  label: string;
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

export function inferArtifactTitle(lang: string, code: string, fallback?: string): string {
  if (fallback) return fallback;
  const titleMatch = /<title[^>]*>([^<]{1,48})<\/title>/i.exec(code);
  if (titleMatch?.[1]?.trim()) {
    const slug = titleMatch[1]
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "");
    if (slug) return `${slug}.${lang.toLowerCase() === "svg" ? "svg" : "html"}`;
  }
  const l = lang.toLowerCase();
  if (l === "svg" || code.trim().toLowerCase().startsWith("<svg")) return "vector-preview.svg";
  if (l === "html" || l === "htm" || isRunnableArtifact(l, code)) return "interactive-preview.html";
  return `snippet.${l || "txt"}`;
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

export function ArtifactSplitPane({
  artifact,
  onClose
}: {
  artifact: InspectedArtifact;
  onClose: () => void;
}) {
  const playable = isRunnableArtifact(artifact.lang, artifact.code);
  const [tab, setTab] = useState<"preview" | "code" | "console">(playable ? "preview" : "code");
  const [reloadNonce, setReloadNonce] = useState(0);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [copied, setCopied] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  const fsDialogRef = useRef<HTMLDialogElement | null>(null);
  const fsTitleId = useId();
  useDialogLightDismiss(fsDialogRef);

  useEffect(() => {
    setLogs([]);
    setTab(isRunnableArtifact(artifact.lang, artifact.code) ? "preview" : "code");
  }, [artifact.id, artifact.lang, artifact.code]);

  useEffect(() => {
    const d = fsDialogRef.current;
    if (!d) return;
    const onDialogClose = () => setFullscreen(false);
    d.addEventListener("close", onDialogClose);
    return () => d.removeEventListener("close", onDialogClose);
  }, []);

  const srcDoc = useMemo(
    () => (playable ? buildSrcDoc(artifact.lang, artifact.code) : ""),
    [playable, artifact.lang, artifact.code]
  );
  const errorCount = logs.filter((l) => l.level === "error").length;

  const appendLog = (entry: LogEntry) => {
    setLogs((prev) => [...prev.slice(-49), entry]);
  };

  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(artifact.code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore
    }
  };

  const openFullscreen = () => {
    setFullscreen(true);
    fsDialogRef.current?.showModal();
  };

  const reload = () => {
    setLogs([]);
    setReloadNonce((n) => n + 1);
  };

  return (
    <aside className="artifact-pane" aria-label="Artifact Inspector">
      <header className="artifact-pane__head">
        <div className="artifact-pane__tabs" role="tablist" aria-label="Artifact view">
          {playable && (
            <button
              type="button"
              role="tab"
              aria-selected={tab === "preview"}
              className={`artifact-pane__tab ${tab === "preview" ? "artifact-pane__tab--active" : ""}`}
              onClick={() => setTab("preview")}
            >
              Preview
            </button>
          )}
          <button
            type="button"
            role="tab"
            aria-selected={tab === "code"}
            className={`artifact-pane__tab ${tab === "code" ? "artifact-pane__tab--active" : ""}`}
            onClick={() => setTab("code")}
          >
            Code
          </button>
          {playable && (
            <button
              type="button"
              role="tab"
              aria-selected={tab === "console"}
              className={`artifact-pane__tab ${tab === "console" ? "artifact-pane__tab--active" : ""} ${
                errorCount > 0 ? "text-err" : ""
              }`}
              onClick={() => setTab("console")}
            >
              Console ({logs.length})
            </button>
          )}
        </div>

        <div className="artifact-pane__actions">
          {playable && (
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={reload}
              title="Reload preview"
              aria-label="Reload preview"
            >
              <RotateCw size={12} aria-hidden="true" />
            </button>
          )}
          <button type="button" className="btn btn--sm btn--ghost" onClick={copyCode}>
            {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
            {copied ? "Copied" : "Copy"}
          </button>
          {playable && (
            <button type="button" className="btn btn--sm btn--ghost" onClick={openFullscreen}>
              <Maximize2 size={12} aria-hidden="true" />
              Expand
            </button>
          )}
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={onClose}
            aria-label="Close artifact inspector"
            title="Close inspector"
          >
            <X size={13} aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="artifact-pane__body">
        {playable && tab === "preview" && !fullscreen && (
          <SandboxFrame
            key={`pane-${reloadNonce}-${srcDoc.length}`}
            srcDoc={srcDoc}
            title={`Sandboxed ${artifact.label} preview`}
            onLog={appendLog}
          />
        )}
        {tab === "code" && (
          <pre className="artifact-pane__code">
            <code>{artifact.code}</code>
          </pre>
        )}
        {tab === "console" && (
          <div className="artifact-pane__console">
            {logs.length === 0 ? (
              <span className="muted">No console output from sandbox.</span>
            ) : (
              logs.map((entry, idx) => (
                <div
                  key={idx}
                  className={`artifact-console__line artifact-console__line--${entry.level}`}
                >
                  [{entry.level}] {entry.text}
                </div>
              ))
            )}
          </div>
        )}
      </div>

      <dialog
        ref={fsDialogRef}
        className="artifact-dialog"
        aria-labelledby={fsTitleId}
        {...({ closedby: "any" } as Record<string, string>)}
      >
        <div className="panel-head">
          <h2 id={fsTitleId}>Sandboxed Preview — {artifact.label}</h2>
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
        {fullscreen && playable && (
          <SandboxFrame
            key={`pane-fs-${reloadNonce}-${srcDoc.length}`}
            srcDoc={srcDoc}
            title={`Fullscreen sandboxed ${artifact.label} preview`}
            tall
            onLog={appendLog}
          />
        )}
      </dialog>
    </aside>
  );
}
