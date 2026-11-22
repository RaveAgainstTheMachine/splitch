import { useEffect, useState } from "react";
import { Check, Copy, Gauge, RefreshCw } from "lucide-react";
import type { BenchReport } from "../../types";

export function BenchmarksPane({
  availableModels,
  effectiveOrigin,
  copiedKey,
  copyText,
  onStatusRefresh,
  onLocalError
}: {
  availableModels: string[];
  effectiveOrigin: string;
  copiedKey: string | null;
  copyText: (key: string, text: string) => Promise<void>;
  onStatusRefresh: () => Promise<void>;
  onLocalError: (msg: string | null) => void;
}) {
  const [benchModels, setBenchModels] = useState<string[]>(["fast"]);
  const [benchMaxTokens, setBenchMaxTokens] = useState<number>(64);
  const [benchConcurrency, setBenchConcurrency] = useState<number>(1);
  const [benchTools, setBenchTools] = useState<boolean>(true);
  const [benchJson, setBenchJson] = useState<boolean>(true);
  const [benchPrompt, setBenchPrompt] = useState<string>("");
  const [benchRunning, setBenchRunning] = useState<boolean>(false);
  const [benchReport, setBenchReport] = useState<BenchReport | null>(null);
  const [benchHistory, setBenchHistory] = useState<BenchReport[]>([]);

  const loadBenchHistory = async () => {
    try {
      const res = await fetch("/api/bench/history");
      if (res.ok) {
        const list = (await res.json()) as BenchReport[];
        setBenchHistory(list);
      }
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    void loadBenchHistory();
  }, []);

  useEffect(() => {
    setBenchModels((prev) => {
      const valid = prev.filter((m) => availableModels.includes(m));
      if (valid.length > 0) return valid;
      if (availableModels.includes("fast")) return ["fast"];
      return [availableModels[0] ?? "auto"];
    });
  }, [availableModels]);

  const runClusterBenchmark = async (e: React.FormEvent) => {
    e.preventDefault();
    const targets = benchModels.length > 0 ? benchModels : ["auto"];
    setBenchRunning(true);
    onLocalError(null);
    try {
      const res = await fetch("/api/bench/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          models: targets,
          max_tokens: benchMaxTokens,
          concurrency: benchConcurrency,
          include_tool_eval: benchTools,
          include_json_eval: benchJson,
          prompt: benchPrompt
        })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      const report = body as BenchReport;
      setBenchReport(report);
      await loadBenchHistory();
      await onStatusRefresh();
    } catch (err) {
      onLocalError(err instanceof Error ? err.message : "Benchmark failed");
    } finally {
      setBenchRunning(false);
    }
  };

  const cliCmd = `splitch bench --router ${effectiveOrigin} --model ${benchModels.join(",")} --max-tokens ${benchMaxTokens} --concurrency ${benchConcurrency}${benchTools ? "" : " --no-tools"}${benchJson ? "" : " --no-json-eval"}`;

  return (
    <div className="settings-stack">
      <form className="settings-stack" onSubmit={runClusterBenchmark}>
        <div className="panel-head">
          <div>
            <h3 style={{ margin: 0 }}>Cluster &amp; Model Benchmark Harness</h3>
            <p className="muted" style={{ margin: "0.2rem 0 0", fontSize: "0.78rem" }}>
              Measure TTFT, prompt processing (PP t/s), token generation (TG t/s), concurrent
              throughput, tool-calling accuracy, and JSON adherence across models &amp; nodes.
            </p>
          </div>
          <button
            type="submit"
            className="btn btn--accent"
            disabled={benchRunning || benchModels.length === 0}
          >
            <Gauge size={13} aria-hidden="true" />
            {benchRunning ? "Running Benchmark…" : "Run Benchmark"}
          </button>
        </div>

        <div className="settings-subcard">
          <span className="muted" style={{ fontSize: "0.76rem", fontWeight: 600 }}>
            Target Models / Aliases (select one or more to compare)
          </span>
          <div className="client-pill-row" role="group" aria-label="Benchmark targets">
            {availableModels.map((m) => {
              const selected = benchModels.includes(m);
              return (
                <button
                  key={m}
                  type="button"
                  className={`btn btn--sm ${selected ? "btn--accent" : ""}`}
                  aria-pressed={selected}
                  onClick={() =>
                    setBenchModels((prev) =>
                      prev.includes(m)
                        ? prev.length > 1
                          ? prev.filter((x) => x !== m)
                          : prev
                        : [...prev, m]
                    )
                  }
                >
                  {m}
                </button>
              );
            })}
          </div>
        </div>

        <div className="two-col">
          <label className="media-field">
            <span>Max Completion Tokens</span>
            <input
              type="number"
              min={16}
              max={512}
              value={benchMaxTokens}
              onChange={(e) => setBenchMaxTokens(Number(e.target.value))}
            />
          </label>
          <label className="media-field">
            <span>Concurrency (parallel streams)</span>
            <input
              type="number"
              min={1}
              max={8}
              value={benchConcurrency}
              onChange={(e) => setBenchConcurrency(Number(e.target.value))}
            />
          </label>
        </div>

        <div className="checkbox-row">
          <label className="gaming-toggle">
            <input
              type="checkbox"
              {...({ switch: "" } as Record<string, string>)}
              checked={benchTools}
              onChange={(e) => setBenchTools(e.target.checked)}
            />
            <span>Tool-Calling Accuracy Eval</span>
          </label>
          <label className="gaming-toggle">
            <input
              type="checkbox"
              {...({ switch: "" } as Record<string, string>)}
              checked={benchJson}
              onChange={(e) => setBenchJson(e.target.checked)}
            />
            <span>Structured JSON Adherence Eval</span>
          </label>
        </div>

        <label className="media-field">
          <span>Custom Prompt Override (optional)</span>
          <input
            value={benchPrompt}
            onChange={(e) => setBenchPrompt(e.target.value)}
            placeholder="Leave blank to use Splitch's standardized multi-GPU benchmark prompt"
          />
        </label>
      </form>

      {benchReport && (
        <div className="settings-subcard">
          <div className="panel-head">
            <strong>
              Latest Result ({benchReport.id}) · c={benchReport.concurrency} ·{" "}
              {benchReport.max_tokens} tok
            </strong>
            <span className="muted" style={{ fontSize: "0.75rem" }}>
              {new Date(benchReport.timestamp * 1000).toLocaleTimeString()}
            </span>
          </div>
          <div className="table-wrapper">
            <table className="data-table bench-table">
              <thead>
                <tr>
                  <th scope="col">Target</th>
                  <th scope="col">Node</th>
                  <th scope="col">Model</th>
                  <th scope="col">TTFT</th>
                  <th scope="col">PP t/s</th>
                  <th scope="col">TG t/s</th>
                  <th scope="col">Agg t/s</th>
                  <th scope="col">Tools</th>
                  <th scope="col">JSON</th>
                </tr>
              </thead>
              <tbody>
                {benchReport.results.map((r) => (
                  <tr key={`${benchReport.id}-${r.requested_model}`}>
                    <th scope="row">
                      <code>{r.requested_model}</code>
                    </th>
                    <td>
                      {r.ok ? (
                        <span className="node-tag">{r.resolved_node || "—"}</span>
                      ) : (
                        <span className="pill pill--err">ERR</span>
                      )}
                    </td>
                    <td>
                      {r.ok ? (
                        <code>{r.resolved_model}</code>
                      ) : (
                        <span className="text-err">{r.error}</span>
                      )}
                    </td>
                    <td>{r.ok && r.ttft_ms !== null ? `${r.ttft_ms}ms` : "—"}</td>
                    <td>{r.ok && r.pp_tokens_per_sec !== null ? r.pp_tokens_per_sec : "—"}</td>
                    <td>
                      <strong>{r.ok && r.tg_tokens_per_sec !== null ? r.tg_tokens_per_sec : "—"}</strong>
                    </td>
                    <td>
                      {r.ok && r.aggregate_tokens_per_sec !== null
                        ? r.aggregate_tokens_per_sec
                        : "—"}
                    </td>
                    <td title={r.tool_eval_detail ?? ""}>
                      {r.tool_eval_passed === null ? (
                        "—"
                      ) : (
                        <span className={`pill ${r.tool_eval_passed ? "pill--ok" : "pill--err"}`}>
                          {r.tool_eval_passed ? "PASS" : "FAIL"}
                        </span>
                      )}
                    </td>
                    <td>
                      {r.json_eval_passed === null ? (
                        "—"
                      ) : (
                        <span className={`pill ${r.json_eval_passed ? "pill--ok" : "pill--err"}`}>
                          {r.json_eval_passed ? "PASS" : "FAIL"}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="snippet-card">
        <div className="snippet-card__head">
          <span className="muted">CLI Equivalent</span>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void copyText("bench-cli", cliCmd)}
          >
            {copiedKey === "bench-cli" ? <Check size={12} /> : <Copy size={12} />}
            {copiedKey === "bench-cli" ? "Copied" : "Copy"}
          </button>
        </div>
        <pre className="snippet-pre">
          <code>{cliCmd}</code>
        </pre>
      </div>

      {benchHistory.length > 0 && (
        <div className="settings-subcard">
          <div className="panel-head">
            <strong>Recent Benchmark Runs ({benchHistory.length})</strong>
            <button type="button" className="btn btn--sm" onClick={() => void loadBenchHistory()}>
              <RefreshCw size={12} aria-hidden="true" /> Refresh
            </button>
          </div>
          <div className="probe-list">
            {benchHistory.slice(0, 8).map((h) => (
              <div key={h.id} className="probe-row">
                <code>{h.id}</code>
                <span className="muted">
                  {new Date(h.timestamp * 1000).toLocaleString()} · c={h.concurrency}
                </span>
                {h.results.map((r) => (
                  <span
                    key={`${h.id}-${r.requested_model}`}
                    className={`pill ${r.ok ? "pill--ok" : "pill--err"}`}
                  >
                    {r.requested_model}
                    {r.ok && r.tg_tokens_per_sec !== null ? ` ${r.tg_tokens_per_sec} t/s` : " ERR"}
                  </span>
                ))}
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  style={{ marginInlineStart: "auto" }}
                  onClick={() => setBenchReport(h)}
                >
                  Inspect
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
