import { useEffect, useState } from "react";
import { Bell, Plus, Save, ShieldCheck, Trash2 } from "lucide-react";
import type {
  ApprovalPolicy,
  ChatRulesConfig,
  PromptPersona,
  RuntimeSettings,
  ThinkingDisplay
} from "../../types";

export function AgentChatSettingsPane({
  settings,
  setSettings,
  saving,
  saveSettings,
  onSavedNotice,
  onLocalError
}: {
  settings: RuntimeSettings;
  setSettings: React.Dispatch<React.SetStateAction<RuntimeSettings | null>>;
  saving: boolean;
  saveSettings: (e: React.FormEvent) => Promise<void>;
  onSavedNotice: (msg: string) => void;
  onLocalError: (msg: string | null) => void;
}) {
  const [allowText, setAllowText] = useState(settings.shell_allowlist.join(", "));
  const [denyText, setDenyText] = useState(settings.shell_denylist.join(", "));
  const [rules, setRules] = useState<ChatRulesConfig>({ global_rules: "", personas: [] });
  const [rulesBusy, setRulesBusy] = useState(false);
  const [newPersonaName, setNewPersonaName] = useState("");
  const [newPersonaPrompt, setNewPersonaPrompt] = useState("");

  useEffect(() => {
    setAllowText(settings.shell_allowlist.join(", "));
    setDenyText(settings.shell_denylist.join(", "));
  }, [settings.shell_allowlist, settings.shell_denylist]);

  useEffect(() => {
    void (async () => {
      try {
        const res = await fetch("/api/chat/rules");
        if (res.ok) {
          setRules((await res.json()) as ChatRulesConfig);
        }
      } catch {
        // ignore transient errors
      }
    })();
  }, []);

  const handleSaveSettings = (e: React.FormEvent) => {
    const shell_allowlist = allowText
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const shell_denylist = denyText
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    setSettings((prev) => (prev ? { ...prev, shell_allowlist, shell_denylist } : prev));
    void saveSettings(e);
  };

  const saveRules = async (nextRules: ChatRulesConfig) => {
    setRulesBusy(true);
    onLocalError(null);
    try {
      const res = await fetch("/api/chat/rules", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(nextRules)
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      setRules(body as ChatRulesConfig);
      onSavedNotice("Saved global rules & personas");
    } catch (err) {
      onLocalError(err instanceof Error ? err.message : "Failed to save rules");
    } finally {
      setRulesBusy(false);
    }
  };

  const addPersona = () => {
    const name = newPersonaName.trim();
    const prompt = newPersonaPrompt.trim();
    if (!name || !prompt) return;
    const item: PromptPersona = {
      id: `${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-${Date.now().toString(36)}`,
      name,
      prompt
    };
    const next = { ...rules, personas: [...rules.personas, item] };
    setNewPersonaName("");
    setNewPersonaPrompt("");
    void saveRules(next);
  };

  const toggleNotifications = async (checked: boolean) => {
    if (checked && typeof Notification !== "undefined" && Notification.permission === "default") {
      try {
        await Notification.requestPermission();
      } catch {
        // ignore
      }
    }
    setSettings((prev) => (prev ? { ...prev, completion_notifications: checked } : prev));
  };

  return (
    <div className="settings-stack">
      <form className="settings-stack" onSubmit={handleSaveSettings}>
        <div className="settings-subcard settings-stack">
          <div className="panel-head">
            <strong>
              <ShieldCheck size={13} aria-hidden="true" /> Tool Approval Policy &amp; Shell Guard
            </strong>
            <span className="pill">Denylist Always Wins</span>
          </div>

          <div className="two-col">
            <label className="media-field">
              <span>Default Approval Policy</span>
              <select
                value={settings.approval_policy}
                onChange={(e) =>
                  setSettings({
                    ...settings,
                    approval_policy: e.target.value as ApprovalPolicy
                  })
                }
              >
                <option value="ask">Always Ask (Edits &amp; Shell)</option>
                <option value="auto_edits">Auto-Edit Files, Ask Shell</option>
                <option value="turbo_safe">Turbo Safe (Auto-Edit + Allowlisted Shell)</option>
                <option value="full_auto">Full Auto (Except Shell Denylist)</option>
              </select>
            </label>
            <label className="media-field">
              <span>Thinking Block Display</span>
              <select
                value={settings.thinking_display}
                onChange={(e) =>
                  setSettings({
                    ...settings,
                    thinking_display: e.target.value as ThinkingDisplay
                  })
                }
              >
                <option value="collapsed">Collapsed (Expand while streaming)</option>
                <option value="expanded">Always Expanded</option>
                <option value="hidden">Hidden</option>
              </select>
            </label>
            <label className="media-field">
              <span>Max Agent Tool Steps / Turn</span>
              <input
                type="number"
                min={1}
                max={32}
                value={settings.max_agent_steps}
                onChange={(e) =>
                  setSettings({ ...settings, max_agent_steps: Number(e.target.value) })
                }
              />
            </label>
            <label className="media-field">
              <span>
                Auto-Compact Trigger ({Math.round(settings.auto_compact_threshold * 100)}%)
              </span>
              <input
                type="range"
                min={50}
                max={95}
                step={5}
                value={Math.round(settings.auto_compact_threshold * 100)}
                onChange={(e) =>
                  setSettings({
                    ...settings,
                    auto_compact_threshold: Number(e.target.value) / 100
                  })
                }
              />
            </label>
          </div>

          <label className="media-field">
            <span>Shell Allowlist (comma-separated prefixes for Turbo Safe)</span>
            <input
              value={allowText}
              onChange={(e) => {
                const v = e.target.value;
                setAllowText(v);
                setSettings({
                  ...settings,
                  shell_allowlist: v
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean)
                });
              }}
              placeholder="pytest, ruff, git status, git diff, rg, ls"
            />
          </label>
          <label className="media-field">
            <span>Shell Denylist (comma-separated patterns — always prompt)</span>
            <input
              value={denyText}
              onChange={(e) => {
                const v = e.target.value;
                setDenyText(v);
                setSettings({
                  ...settings,
                  shell_denylist: v
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean)
                });
              }}
              placeholder="rm -rf, sudo, git push, mkfs, dd if="
            />
          </label>

          <div className="row-actions">
            <label className="gaming-toggle">
              <input
                type="checkbox"
                {...({ switch: "" } as Record<string, string>)}
                checked={settings.completion_notifications}
                onChange={(e) => void toggleNotifications(e.target.checked)}
              />
              <span>
                <Bell size={12} aria-hidden="true" /> Desktop notification when background response
                finishes
              </span>
            </label>
            <button
              type="submit"
              className="btn btn--accent"
              style={{ marginInlineStart: "auto" }}
              disabled={saving}
            >
              <Save size={13} aria-hidden="true" />
              {saving ? "Saving…" : "Save Agent Settings"}
            </button>
          </div>
        </div>
      </form>

      <div className="settings-subcard settings-stack">
        <div className="panel-head">
          <strong>Global User Rules &amp; Prompt Personas</strong>
          <button
            type="button"
            className="btn btn--sm btn--accent"
            disabled={rulesBusy}
            onClick={() => void saveRules(rules)}
          >
            <Save size={12} aria-hidden="true" /> {rulesBusy ? "Saving…" : "Save Rules"}
          </button>
        </div>
        <label className="media-field">
          <span>Global Rules (injected into all Agent Chat turns)</span>
          <textarea
            rows={3}
            value={rules.global_rules}
            onChange={(e) => setRules({ ...rules, global_rules: e.target.value })}
            placeholder="e.g. Always keep answers concise; prefer pytest over unittest."
          />
        </label>

        {rules.personas.length > 0 && (
          <div className="probe-list">
            {rules.personas.map((p) => (
              <div key={p.id} className="probe-row">
                <strong>{p.name}</strong>
                <span className="muted probe-row__snippet">{p.prompt}</span>
                <button
                  type="button"
                  className="btn btn--sm btn--ghost btn--danger"
                  disabled={rulesBusy}
                  aria-label={`Delete persona ${p.name}`}
                  onClick={() =>
                    void saveRules({
                      ...rules,
                      personas: rules.personas.filter((x) => x.id !== p.id)
                    })
                  }
                >
                  <Trash2 size={12} aria-hidden="true" />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="two-col" style={{ gap: "0.5rem" }}>
          <label className="media-field">
            <span>New Persona Name</span>
            <input
              value={newPersonaName}
              onChange={(e) => setNewPersonaName(e.target.value)}
              placeholder="Senior Reviewer"
            />
          </label>
          <label className="media-field">
            <span>Persona System Prompt</span>
            <input
              value={newPersonaPrompt}
              onChange={(e) => setNewPersonaPrompt(e.target.value)}
              placeholder="Audit diffs strictly for edge cases and regressions."
            />
          </label>
        </div>
        <div className="row-actions">
          <button
            type="button"
            className="btn btn--sm"
            disabled={rulesBusy || !newPersonaName.trim() || !newPersonaPrompt.trim()}
            onClick={addPersona}
          >
            <Plus size={12} aria-hidden="true" /> Add Persona
          </button>
        </div>
      </div>
    </div>
  );
}
