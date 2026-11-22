import { useState } from "react";
import { Check, Copy, FileCode2 } from "lucide-react";
import {
  ArtifactPreview,
  inferArtifactTitle,
  type InspectedArtifact,
  isRunnableArtifact
} from "../ArtifactPreview";

export function CodeBlock({
  className,
  children,
  onInspect,
  activeArtifactCode
}: {
  className?: string;
  children?: React.ReactNode;
  onInspect?: (artifact: InspectedArtifact) => void;
  activeArtifactCode?: string | null;
}) {
  const [copied, setCopied] = useState(false);
  const [showInlineCode, setShowInlineCode] = useState(false);
  const langMatch = /language-([\w-]+)/.exec(className || "");
  const codeText = String(children ?? "").replace(/\n$/, "");
  const isBlock = Boolean(langMatch) || codeText.includes("\n");

  if (!isBlock) {
    return <code className={className}>{children}</code>;
  }

  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(codeText);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // clipboard unavailable
    }
  };

  const lang = langMatch?.[1] || "text";
  const runnable = isRunnableArtifact(lang, codeText);
  const label = inferArtifactTitle(lang, codeText);
  const isInspected = activeArtifactCode === codeText;

  if (runnable && onInspect) {
    return (
      <div className="artifact-chip-wrap">
        <button
          type="button"
          className={`artifact-chip ${isInspected ? "artifact-chip--active" : ""}`}
          onClick={() =>
            onInspect({
              id: `${lang}-${codeText.length}`,
              lang,
              code: codeText,
              label
            })
          }
        >
          <span className="artifact-chip__badge">{lang.toUpperCase()}</span>
          <strong className="artifact-chip__name">{label}</strong>
          <span className="artifact-chip__hint">
            • {isInspected ? "Inspecting in split pane" : "Click to inspect"}
          </span>
        </button>
        <button
          type="button"
          className="btn btn--sm btn--ghost"
          onClick={() => setShowInlineCode((v) => !v)}
          title="Toggle inline source code"
        >
          {showInlineCode ? "Hide source" : "Source"}
        </button>
        {showInlineCode && (
          <div className="md-codeblock">
            <div className="md-codeblock__bar">
              <span>{lang}</span>
              <button type="button" className="btn btn--sm btn--ghost" onClick={copyCode}>
                {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            <pre>
              <code className={className}>{codeText}</code>
            </pre>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="md-codeblock">
      <div className="md-codeblock__bar">
        <span>{lang}</span>
        <div className="row-actions">
          {onInspect && (
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={() =>
                onInspect({
                  id: `${lang}-${codeText.length}`,
                  lang,
                  code: codeText,
                  label
                })
              }
            >
              <FileCode2 size={12} aria-hidden="true" />
              Split Pane
            </button>
          )}
          <button type="button" className="btn btn--sm btn--ghost" onClick={copyCode}>
            {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </div>
      {runnable && <ArtifactPreview lang={lang} code={codeText} />}
      <pre>
        <code className={className}>{codeText}</code>
      </pre>
    </div>
  );
}

export function DiffViewer({ diff }: { diff: string }) {
  return (
    <pre className="diff-block">
      {diff.split("\n").map((line, i) => {
        const cls =
          line.startsWith("+") && !line.startsWith("+++")
            ? "diff-line diff-line--add"
            : line.startsWith("-") && !line.startsWith("---")
              ? "diff-line diff-line--del"
              : line.startsWith("@@")
                ? "diff-line diff-line--hunk"
                : "diff-line";
        return (
          <span key={i} className={cls}>
            {line}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}
