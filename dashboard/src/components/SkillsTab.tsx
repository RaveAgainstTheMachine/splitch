import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  BookOpen,
  Download,
  ExternalLink,
  Plus,
  RefreshCw,
  Search,
  Send,
  Trash2
} from "lucide-react";
import type {
  ScopedExtensionsConfig,
  SkillItem,
  SkillRegistryResult,
  SyncResult
} from "../types";
import type { ConfirmState } from "./ConfirmDialog";
import { useDialogLightDismiss } from "./ModelsTab";

const SKILL_TEMPLATE = `---
name: my-skill
description: When and how an agent should apply this skill.
---

# Instructions

1. Step one
2. Step two
`;

function fmtInstalls(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export function SkillsTab({
  onError,
  onConfirm
}: {
  onError: (msg: string | null) => void;
  onConfirm: (state: ConfirmState) => void;
}) {
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [globalSkills, setGlobalSkills] = useState<string[]>([]);
  const [globalMcps, setGlobalMcps] = useState<string[]>([]);
  const [importUrl, setImportUrl] = useState("");
  const [importing, setImporting] = useState(false);
  const [syncResults, setSyncResults] = useState<SyncResult[]>([]);

  // Live skills.sh discovery state
  const [searchQuery, setSearchQuery] = useState("");
  const [searchSort, setSearchSort] = useState<"installs" | "name">("installs");
  const [searching, setSearching] = useState(false);
  const [installingId, setInstallingId] = useState<string | null>(null);
  const [registryResults, setRegistryResults] = useState<SkillRegistryResult[]>([]);

  const dialogRef = useRef<HTMLDialogElement>(null);
  useDialogLightDismiss(dialogRef);
  const dialogTitleId = useId();
  const tableCaptionId = useId();

  const [form, setForm] = useState({
    id: "",
    name: "",
    description: "",
    content: SKILL_TEMPLATE,
    enabled: true
  });

  const loadSkills = async () => {
    try {
      const [sRes, scRes] = await Promise.all([fetch("/api/skills"), fetch("/api/chat/scopes")]);
      if (sRes.ok) setSkills(await sRes.json());
      if (scRes.ok) {
        const sc = (await scRes.json()) as ScopedExtensionsConfig;
        setGlobalSkills(sc.global_scope.skills ?? []);
        setGlobalMcps(sc.global_scope.mcps ?? []);
      }
    } catch {
      // ignore
    }
  };

  const toggleGlobalSkill = async (id: string, checked: boolean) => {
    const next = checked
      ? Array.from(new Set([...globalSkills, id]))
      : globalSkills.filter((x) => x !== id);
    setGlobalSkills(next);
    try {
      const res = await fetch("/api/chat/scopes/global", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skills: next, mcps: globalMcps })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to update global skill scope");
    }
  };

  useEffect(() => {
    loadSkills();
  }, []);

  const searchRegistry = async (e: React.FormEvent) => {
    e.preventDefault();
    if (searchQuery.trim().length < 2) return;
    setSearching(true);
    try {
      const res = await fetch(
        `/api/skills/search?q=${encodeURIComponent(searchQuery.trim())}&limit=24`
      );
      if (!res.ok) throw new Error(`skills.sh search failed: HTTP ${res.status}`);
      setRegistryResults(await res.json());
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Skill search failed");
    } finally {
      setSearching(false);
    }
  };

  const sortedRegistry = useMemo(() => {
    const copy = [...registryResults];
    if (searchSort === "installs") {
      copy.sort((a, b) => b.installs - a.installs);
    } else {
      copy.sort((a, b) => a.name.localeCompare(b.name));
    }
    return copy;
  }, [registryResults, searchSort]);

  const installFromRegistry = async (item: SkillRegistryResult) => {
    setInstallingId(item.id);
    try {
      const res = await fetch("/api/skills/install-registry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: item.source,
          skill_id: item.skill_id,
          name: item.name
        })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      await loadSkills();
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Skill install failed");
    } finally {
      setInstallingId(null);
    }
  };

  const openEditor = (s?: SkillItem) => {
    if (s) {
      setForm({
        id: s.id,
        name: s.name,
        description: s.description,
        content: s.content,
        enabled: s.enabled
      });
    } else {
      setForm({
        id: "",
        name: "",
        description: "",
        content: SKILL_TEMPLATE,
        enabled: true
      });
    }
    dialogRef.current?.showModal();
  };

  const saveSkill = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const res = await fetch("/api/skills", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form)
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      dialogRef.current?.close();
      await loadSkills();
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Failed to save skill");
    }
  };

  const toggleSkill = async (id: string, enabled: boolean) => {
    try {
      const res = await fetch(`/api/skills/${encodeURIComponent(id)}/toggle?enabled=${enabled}`, {
        method: "POST"
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadSkills();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Toggle failed");
    }
  };

  const deleteSkill = (id: string) => {
    onConfirm({
      title: `Delete skill "${id}"?`,
      message: `Remove "${id}/SKILL.md" from Splitch's canonical skills library?`,
      confirmLabel: "Delete Skill",
      danger: true,
      onConfirm: async () => {
        try {
          const res = await fetch(`/api/skills/${encodeURIComponent(id)}`, { method: "DELETE" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          await loadSkills();
        } catch (err) {
          onError(err instanceof Error ? err.message : "Delete failed");
        }
      }
    });
  };

  const importSkill = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!importUrl.trim()) return;
    setImporting(true);
    try {
      const res = await fetch("/api/skills/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: importUrl.trim() })
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      setImportUrl("");
      await loadSkills();
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "GitHub import failed");
    } finally {
      setImporting(false);
    }
  };

  const syncToNodes = async () => {
    try {
      const res = await fetch("/api/skills/sync", { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSyncResults(await res.json());
      onError(null);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Skill sync failed");
    }
  };

  const installedIds = new Set(skills.map((s) => s.id));

  return (
    <div className="tab-stack">
      <section className="panel">
        <div className="panel-head">
          <h2>Skills Registry</h2>
          <button type="button" className="btn btn--sm btn--accent" onClick={syncToNodes}>
            <Send size={12} aria-hidden="true" /> Sync to Nodes
          </button>
        </div>

        <form className="hf-controls" onSubmit={searchRegistry}>
          <input
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search skills.sh…"
            aria-label="Search skills.sh registry"
          />
          <select
            aria-label="Sort skills by"
            value={searchSort}
            onChange={(e) => setSearchSort(e.target.value as "installs" | "name")}
          >
            <option value="installs">Sort: Most Installed</option>
            <option value="name">Sort: Name (A–Z)</option>
          </select>
          <button type="submit" className="btn btn--accent" disabled={searching}>
            <Search size={14} aria-hidden="true" />
            {searching ? "Searching..." : "Search Skills"}
          </button>
        </form>

        {sortedRegistry.length > 0 && (
          <div className="hf-results">
            <div className="hf-results__meta muted">
              {sortedRegistry.length} skills found on skills.sh
            </div>
            <div className="hf-grid">
              {sortedRegistry.map((item) => {
                const slug = item.skill_id.toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
                const isInstalled = installedIds.has(slug);
                const isGitHub = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(item.source);
                return (
                  <article key={item.id} className="repo-card">
                    <div className="discovery-card">
                      <div className="repo-card__top">
                        <span className="repo-card__owner">{item.source}</span>
                        {isInstalled && <span className="pill pill--ok">INSTALLED</span>}
                      </div>
                      <strong className="repo-card__name">{item.name}</strong>
                      <div className="discovery-card__foot">
                        <div className="row-actions">
                          <span className="stat-chip muted">
                            <Download size={11} aria-hidden="true" /> {fmtInstalls(item.installs)}{" "}
                            installs
                          </span>
                          <a
                            href={`https://skills.sh/${item.id}`}
                            target="_blank"
                            rel="noreferrer"
                            className="btn btn--sm btn--ghost"
                            aria-label={`View ${item.name} on skills.sh`}
                          >
                            <ExternalLink size={12} aria-hidden="true" />
                          </a>
                        </div>
                        <button
                          type="button"
                          className={`btn btn--sm ${isInstalled ? "" : "btn--accent"}`}
                          disabled={!isGitHub || installingId === item.id}
                          title={
                            isGitHub
                              ? "Fetch SKILL.md from GitHub into Splitch library"
                              : "Non-GitHub source — view link to copy manually"
                          }
                          onClick={() => installFromRegistry(item)}
                        >
                          <Download size={12} aria-hidden="true" />
                          {installingId === item.id
                            ? "Installing..."
                            : isInstalled
                              ? "Reinstall"
                              : "Install"}
                        </button>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          </div>
        )}

        <form className="search-row" onSubmit={importSkill}>
          <input
            type="url"
            value={importUrl}
            onChange={(e) => setImportUrl(e.target.value)}
            placeholder="GitHub SKILL.md URL"
            aria-label="Direct GitHub SKILL.md URL"
          />
          <button type="submit" className="btn" disabled={importing}>
            <Download size={14} aria-hidden="true" />
            {importing ? "Importing..." : "Import URL"}
          </button>
        </form>

        {syncResults.length > 0 && (
          <div className="sync-pills">
            {syncResults.map((r) => (
              <span key={r.node_id} className={`pill ${r.ok ? "pill--ok" : "pill--err"}`}>
                {r.node_id}:{" "}
                {r.ok ? `${r.synced_count} skills synced (${r.paths.join(", ")})` : r.error}
              </span>
            ))}
          </div>
        )}
      </section>

      <section className="panel" aria-labelledby={tableCaptionId}>
        <div className="panel-head">
          <h2 id={tableCaptionId}>
            <BookOpen size={14} aria-hidden="true" /> Skills ({skills.length})
          </h2>
          <div className="row-actions">
            <button type="button" className="btn btn--sm" onClick={loadSkills}>
              <RefreshCw size={12} aria-hidden="true" /> Refresh
            </button>
            <button type="button" className="btn btn--sm btn--accent" onClick={() => openEditor()}>
              <Plus size={12} aria-hidden="true" /> New Skill
            </button>
          </div>
        </div>

        <div tabIndex={0} role="region" aria-labelledby={tableCaptionId} className="table-wrapper">
          <table className="data-table skills-table">
            <thead>
              <tr>
                <th scope="col">Skill ID</th>
                <th scope="col">Description</th>
                <th scope="col">Enabled</th>
                <th scope="col" title="Attach to all chats">
                  Global
                </th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {skills.length === 0 ? (
                <tr>
                  <td colSpan={5} className="muted empty-cell">
                    No skills installed.
                  </td>
                </tr>
              ) : (
                skills.map((s) => (
                  <tr key={s.id}>
                    <th scope="row">
                      <code>{s.id}</code>
                      <div className="muted">{s.name}</div>
                    </th>
                    <td className="wrap-cell">{s.description || "—"}</td>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Enable ${s.id}`}
                        checked={s.enabled}
                        onChange={(e) => toggleSkill(s.id, e.target.checked)}
                      />
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Use ${s.id} globally in all chats`}
                        checked={globalSkills.includes(s.id)}
                        onChange={(e) => void toggleGlobalSkill(s.id, e.target.checked)}
                      />
                    </td>
                    <td>
                      <div className="row-actions">
                        <button type="button" className="btn btn--sm" onClick={() => openEditor(s)}>
                          Edit
                        </button>
                        <button
                          type="button"
                          className="btn btn--sm btn--danger"
                          onClick={() => deleteSkill(s.id)}
                        >
                          <Trash2 size={12} aria-hidden="true" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <dialog
        ref={dialogRef}
        aria-labelledby={dialogTitleId}
        {...({ closedby: "any" } as Record<string, string>)}
      >
        <h2 id={dialogTitleId}>Skill</h2>
        <form className="modal-form" onSubmit={saveSkill}>
          <div className="two-col">
            <label>
              Skill ID
              <input
                required
                autoFocus
                value={form.id}
                onChange={(e) => setForm({ ...form, id: e.target.value })}
                placeholder="code-review"
              />
            </label>
            <label>
              Display Name
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="Code Review"
              />
            </label>
          </div>
          <label>
            Description
            <input
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              placeholder="Strict security and type review checklist"
            />
          </label>
          <label>
            SKILL.md Content
            <textarea
              required
              rows={12}
              value={form.content}
              onChange={(e) => setForm({ ...form, content: e.target.value })}
            />
          </label>
          <div className="modal-actions">
            <button type="button" className="btn" onClick={() => dialogRef.current?.close()}>
              Cancel
            </button>
            <button type="submit" className="btn btn--accent">
              Save Skill
            </button>
          </div>
        </form>
      </dialog>
    </div>
  );
}
