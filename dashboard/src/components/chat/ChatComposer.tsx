import { useState } from "react";
import type React from "react";
import {
  AlertTriangle,
  Globe,
  ImagePlus,
  ShieldAlert,
  SlidersHorizontal,
  Square,
  Thermometer,
  X
} from "lucide-react";
import type { McpServerConfig } from "../../types";
import { type DraftChatConfig, fmtTokenCompact } from "./chatHelpers";

export interface SamplingConfig {
  temperature: string;
  topP: string;
  topK: string;
  minP: string;
  repeatPenalty: string;
  maxTokens: string;
  seed: string;
  stop: string;
  reasoningEffort: "" | "off" | "low" | "medium" | "high";
  enableThinking: "" | "on" | "off";
}

export const DEFAULT_SAMPLING: SamplingConfig = {
  temperature: "",
  topP: "",
  topK: "",
  minP: "",
  repeatPenalty: "",
  maxTokens: "",
  seed: "",
  stop: "",
  reasoningEffort: "",
  enableThinking: ""
};

export interface ChatComposerProps {
  formRef: React.RefObject<HTMLFormElement | null>;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  composerId: string;
  dragActive: boolean;
  setDragActive: (val: boolean) => void;
  attachedImages: string[];
  setAttachedImages: React.Dispatch<React.SetStateAction<string[]>>;
  composerText: string;
  setComposerText: (val: string) => void;
  streaming: boolean;
  effectiveConfig: DraftChatConfig;
  updateConfig: (patch: Partial<DraftChatConfig>) => void;
  effectiveSkillCount: number;
  effectiveMcpCount: number;
  activeExternalMcps: McpServerConfig[];
  sampling: SamplingConfig;
  setSampling: React.Dispatch<React.SetStateAction<SamplingConfig>>;
  gamingMode: boolean;
  compactNotice: string | null;
  contextStats: { used: number; max: number; estimated: boolean };
  handleSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
  handleFiles: (files: FileList | File[]) => Promise<void>;
  handleComposerKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  handlePaste: (e: React.ClipboardEvent<HTMLTextAreaElement>) => void;
  openContextDialog: () => void;
  stopGeneration: () => void;
}

export function ChatComposer({
  formRef,
  textareaRef,
  fileInputRef,
  composerId,
  dragActive,
  setDragActive,
  attachedImages,
  setAttachedImages,
  composerText,
  setComposerText,
  streaming,
  effectiveConfig,
  updateConfig,
  effectiveSkillCount,
  effectiveMcpCount,
  activeExternalMcps,
  sampling,
  setSampling,
  gamingMode,
  compactNotice,
  contextStats,
  handleSubmit,
  handleFiles,
  handleComposerKeyDown,
  handlePaste,
  openContextDialog,
  stopGeneration
}: ChatComposerProps) {
  const [samplingOpen, setSamplingOpen] = useState(false);

  const hasCustomSampling = Boolean(
    sampling.temperature ||
      sampling.topP ||
      sampling.topK ||
      sampling.minP ||
      sampling.repeatPenalty ||
      sampling.maxTokens ||
      sampling.seed ||
      sampling.stop ||
      sampling.reasoningEffort ||
      sampling.enableThinking
  );

  const samplingSummary = [
    sampling.temperature ? `T=${sampling.temperature}` : null,
    sampling.reasoningEffort ? `Think=${sampling.reasoningEffort}` : null
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="chat-composer-shell">
      <form
        ref={formRef}
        className={`chat-composer ${dragActive ? "chat-composer--drag" : ""}`}
        onSubmit={handleSubmit}
        onDragOver={(e) => {
          e.preventDefault();
          setDragActive(true);
        }}
        onDragLeave={() => setDragActive(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragActive(false);
          if (e.dataTransfer.files.length > 0) {
            void handleFiles(e.dataTransfer.files);
          }
        }}
      >
        {attachedImages.length > 0 && (
          <div className="chat-attachments">
            {attachedImages.map((src, i) => (
              <div key={i} className="chat-attachment">
                <img src={src} alt={`Upload ${i + 1}`} />
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  aria-label="Remove attachment"
                  onClick={() => setAttachedImages((prev) => prev.filter((_, idx) => idx !== i))}
                >
                  <X size={12} aria-hidden="true" />
                </button>
              </div>
            ))}
          </div>
        )}

        <label htmlFor={composerId} className="visually-hidden">
          Message Splitch
        </label>
        <textarea
          ref={textareaRef}
          id={composerId}
          className="chat-composer__input"
          value={composerText}
          onChange={(e) => setComposerText(e.target.value)}
          onKeyDown={handleComposerKeyDown}
          onPaste={handlePaste}
          placeholder="Message Splitch…"
          disabled={streaming}
        />

        <div className="chat-composer__bar">
          <div className="row-actions">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={(e) => {
                if (e.target.files) void handleFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <button
              type="button"
              className="btn btn--sm composer-chip"
              disabled={streaming}
              onClick={() => fileInputRef.current?.click()}
              title="Attach image for vision models"
            >
              <ImagePlus size={13} aria-hidden="true" />
              Image
            </button>

            <button
              type="button"
              className={`btn btn--sm composer-chip ${
                effectiveConfig.web_search_enabled ? "composer-chip--active" : ""
              }`}
              aria-pressed={effectiveConfig.web_search_enabled}
              disabled={streaming}
              onClick={() =>
                updateConfig({ web_search_enabled: !effectiveConfig.web_search_enabled })
              }
              title="Enable web_search and web_fetch tools for live web lookups"
            >
              <Globe size={13} aria-hidden="true" />
              Web Search
            </button>

            <button
              type="button"
              className="btn btn--sm composer-chip"
              disabled={streaming}
              onClick={openContextDialog}
              title="Configure System Prompt, Skills, MCP Servers & Context Compaction"
            >
              <SlidersHorizontal size={13} aria-hidden="true" />
              Context
              {effectiveSkillCount + effectiveMcpCount > 0 && (
                <span>({effectiveSkillCount + effectiveMcpCount})</span>
              )}
            </button>

            {activeExternalMcps.length > 0 && (
              <span
                className="pill pill--warn"
                title={`External MCP active (${activeExternalMcps.map((m) => m.name).join(", ")}) — tool calls send data outside your local network and pause for approval`}
              >
                <ShieldAlert size={11} aria-hidden="true" /> Ext MCP ({activeExternalMcps.length})
              </span>
            )}

            <div className="sampling-anchor">
              <button
                type="button"
                className={`btn btn--sm composer-chip ${
                  hasCustomSampling ? "composer-chip--active" : ""
                }`}
                aria-expanded={samplingOpen}
                disabled={streaming}
                onClick={() => setSamplingOpen((v) => !v)}
                title="Sampling & Reasoning Effort controls"
              >
                <Thermometer size={12} aria-hidden="true" />
                {samplingSummary || "Sampling"}
              </button>

              {samplingOpen && (
                <div className="sampling-popover" role="region" aria-label="Sampling & Reasoning">
                  <div className="panel-head" style={{ marginBottom: "0.5rem" }}>
                    <strong>Sampling &amp; Reasoning</strong>
                    <div className="row-actions" style={{ flexWrap: "wrap" }}>
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() =>
                          setSampling({
                            ...DEFAULT_SAMPLING,
                            temperature: "0.2",
                            topP: "0.9"
                          })
                        }
                      >
                        Coding
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() =>
                          setSampling({
                            ...DEFAULT_SAMPLING,
                            temperature: "0.7",
                            topP: "0.95"
                          })
                        }
                      >
                        Balanced
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() =>
                          setSampling({
                            ...DEFAULT_SAMPLING,
                            temperature: "1.0",
                            minP: "0.05"
                          })
                        }
                      >
                        Creative
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() =>
                          setSampling({
                            ...DEFAULT_SAMPLING,
                            temperature: "0.6",
                            reasoningEffort: "high",
                            enableThinking: "on"
                          })
                        }
                      >
                        Reasoning
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm btn--ghost"
                        onClick={() => setSampling(DEFAULT_SAMPLING)}
                      >
                        Reset
                      </button>
                    </div>
                  </div>

                  <div className="two-col" style={{ gap: "0.45rem" }}>
                    <label className="media-field">
                      <span>Temperature</span>
                      <input
                        type="number"
                        min={0}
                        max={2}
                        step={0.05}
                        value={sampling.temperature}
                        onChange={(e) => setSampling((p) => ({ ...p, temperature: e.target.value }))}
                        placeholder="auto"
                      />
                    </label>
                    <label className="media-field">
                      <span>Top P</span>
                      <input
                        type="number"
                        min={0}
                        max={1}
                        step={0.05}
                        value={sampling.topP}
                        onChange={(e) => setSampling((p) => ({ ...p, topP: e.target.value }))}
                        placeholder="auto"
                      />
                    </label>
                    <label className="media-field">
                      <span>Top K</span>
                      <input
                        type="number"
                        min={0}
                        max={500}
                        value={sampling.topK}
                        onChange={(e) => setSampling((p) => ({ ...p, topK: e.target.value }))}
                        placeholder="auto"
                      />
                    </label>
                    <label className="media-field">
                      <span>Min P</span>
                      <input
                        type="number"
                        min={0}
                        max={1}
                        step={0.01}
                        value={sampling.minP}
                        onChange={(e) => setSampling((p) => ({ ...p, minP: e.target.value }))}
                        placeholder="auto"
                      />
                    </label>
                    <label className="media-field">
                      <span>Repeat Penalty</span>
                      <input
                        type="number"
                        min={0.5}
                        max={2.5}
                        step={0.05}
                        value={sampling.repeatPenalty}
                        onChange={(e) =>
                          setSampling((p) => ({ ...p, repeatPenalty: e.target.value }))
                        }
                        placeholder="auto"
                      />
                    </label>
                    <label className="media-field">
                      <span>Max Tokens</span>
                      <input
                        type="number"
                        min={1}
                        max={262144}
                        value={sampling.maxTokens}
                        onChange={(e) => setSampling((p) => ({ ...p, maxTokens: e.target.value }))}
                        placeholder="auto"
                      />
                    </label>
                    <label className="media-field">
                      <span>Reasoning Effort</span>
                      <select
                        value={sampling.reasoningEffort}
                        onChange={(e) =>
                          setSampling((p) => ({
                            ...p,
                            reasoningEffort: e.target.value as SamplingConfig["reasoningEffort"]
                          }))
                        }
                      >
                        <option value="">Auto</option>
                        <option value="off">Off</option>
                        <option value="low">Low</option>
                        <option value="medium">Medium</option>
                        <option value="high">High</option>
                      </select>
                    </label>
                    <label className="media-field">
                      <span>Thinking Tokens</span>
                      <select
                        value={sampling.enableThinking}
                        onChange={(e) =>
                          setSampling((p) => ({
                            ...p,
                            enableThinking: e.target.value as SamplingConfig["enableThinking"]
                          }))
                        }
                      >
                        <option value="">Auto</option>
                        <option value="on">Enable Thinking</option>
                        <option value="off">Disable Thinking</option>
                      </select>
                    </label>
                    <label className="media-field">
                      <span>Seed</span>
                      <input
                        type="number"
                        value={sampling.seed}
                        onChange={(e) => setSampling((p) => ({ ...p, seed: e.target.value }))}
                        placeholder="random"
                      />
                    </label>
                    <label className="media-field">
                      <span>Stop Sequences (comma)</span>
                      <input
                        value={sampling.stop}
                        onChange={(e) => setSampling((p) => ({ ...p, stop: e.target.value }))}
                        placeholder="END, ###"
                      />
                    </label>
                  </div>
                </div>
              )}
            </div>

            {gamingMode && (
              <span className="pill pill--warn">
                <AlertTriangle size={11} aria-hidden="true" /> Gaming Yield
              </span>
            )}
          </div>

          <div className="row-actions">
            {compactNotice && <span className="pill pill--ok">{compactNotice}</span>}
            <button
              type="button"
              className="composer-tokens"
              onClick={openContextDialog}
              title={`Context window: ${contextStats.used.toLocaleString()} / ${contextStats.max.toLocaleString()} tokens (${
                effectiveConfig.auto_compact ? "Auto-Compact ON" : "Auto-Compact OFF"
              }) — click to configure or compact`}
            >
              {fmtTokenCompact(contextStats.used)} / {fmtTokenCompact(contextStats.max)}
            </button>
            {streaming ? (
              <button type="button" className="btn btn--danger" onClick={stopGeneration}>
                <Square size={13} aria-hidden="true" />
                Stop
              </button>
            ) : (
              <button
                type="submit"
                className="btn btn--accent chat-send-btn"
                disabled={!composerText.trim() && attachedImages.length === 0}
              >
                Send
              </button>
            )}
          </div>
        </div>
      </form>
      <p className="chat-disclaimer">
        Splitch is AI and can make mistakes — including in text, code, images and video. Check important output.
      </p>
    </div>
  );
}
