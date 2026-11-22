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
  temperature: string;
  setTemperature: (val: string) => void;
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
  temperature,
  setTemperature,
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

            <label className="composer-chip chat-temp-field" title="Sampling temperature">
              <Thermometer size={12} aria-hidden="true" className="muted" />
              <span>Temp</span>
              <input
                type="number"
                min={0}
                max={2}
                step={0.1}
                value={temperature}
                onChange={(e) => setTemperature(e.target.value)}
                placeholder="auto"
              />
            </label>

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
    </div>
  );
}
