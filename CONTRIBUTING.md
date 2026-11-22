# Contributing to Splitch

Thank you for your interest in contributing to **Splitch**! Whether you are fixing a bug, adding support for a new inference backend or GPU telemetry provider, polishing the web UI, or improving documentation, this guide covers everything you need to get started.

---

## Code of Conduct

Be constructive, respectful, and direct in issues, pull requests, and reviews. Critique code and design trade-offs, never people.

---

## Repository Structure

```text
splitch/
├── install.sh                  # POSIX 1-command installer & updater (macOS, Linux, WSL)
├── install.ps1                 # Windows PowerShell 1-command installer & updater
├── backend/
│   ├── cli.py                  # `splitch up|router|agent|init|doctor|update|version` CLI
│   ├── shared/
│   │   ├── models.py           # Canonical Pydantic v2 schemas (mirrored in dashboard/src/types.ts)
│   │   ├── engines.py          # Hardware-aware llama.cpp / ollama installer, updater & version probe
│   │   └── updater.py          # Environment doctor, git self-updater & install metadata helpers
│   ├── router/
│   │   ├── app.py              # FastAPI router (:8080), auth middleware, WebSocket, static SPA host
│   │   ├── config.py           # `splitch.toml` loader & zero-config fallback
│   │   ├── telemetry.py        # Async node poller, dynamic node registry, WS fan-out
│   │   ├── policy.py           # Hardware-aware routing & Gaming-Mode failover policy
│   │   ├── proxy.py            # OpenAI `/v1` & `/infill` streaming proxy + trace recorder
│   │   ├── chat_api.py         # SQLite WAL chat store, SSE agent loop, compaction, bulk actions
│   │   ├── web_search.py       # Web search backends (SearXNG/Brave/Tavily/open) + SSRF-safe fetch
│   │   ├── models_api.py       # Hugging Face GGUF search, fit checks, pull orchestration, aliases
│   │   ├── mcp_api.py          # MCP Registry/npm search, JSON import, Python builder, probe & sync
│   │   └── skills_api.py       # `skills.sh` search, GitHub import, CRUD & multi-node sync
│   └── agent/
│       └── main.py             # Node agent (:8090): GPU/RAM/disk telemetry, pulls, sandboxed tools
├── dashboard/
│   ├── index.html              # Zero-FOUC theme bootstrap + SPA root
│   └── src/
│       ├── types.ts            # TypeScript mirror of backend/shared/models.py
│       ├── App.tsx             # Top-level tabs, Overview, Gaming Mode, Node registration
│       ├── index.css           # Design tokens, dark/light themes, layout & component styles
│       └── components/
│           ├── ChatTab.tsx         # Agent Chat workspace, sidebar, HITL approvals, compaction
│           ├── ArtifactPreview.tsx # Sandboxed HTML/JS/SVG iframe preview + console bridge
│           ├── ContextRing.tsx     # Live context window circular progress ring
│           ├── ModelsTab.tsx       # HF search, node GGUF inventory, alias editor
│           ├── McpTab.tsx          # MCP discovery, presets, builder, live probe, client export
│           ├── SkillsTab.tsx       # Skills library editor & sync
│           ├── ConfirmDialog.tsx   # Reusable native `<dialog>` confirmation prompt
│           ├── ThemeDialog.tsx     # Mode, accent, and dark palette customization
│           └── SplitchLogo.tsx     # Brand mark SVG component
├── docs/
│   ├── assets/                 # Logo SVG and sanitized UI screenshots
│   └── wiki/                   # Complete multi-page documentation wiki
├── infra/
│   ├── clients/                # Example OpenCode & Zed client configs
│   └── templates/              # systemd units, launchd plist, llama-server models.ini
└── tests/
    ├── test_policy.py          # Routing & Gaming-Mode failover unit tests
    ├── test_chat.py            # SQLite store, compaction, bulk actions, pin, SSRF & sandbox tests
    ├── test_management.py      # Dynamic nodes, aliases, MCP probes, skills & HF parser tests
    ├── test_installer.py       # Doctor, real git updater, and install.sh local install/uninstall tests
    └── test_ui_e2e.py          # Real headless-Chromium UI E2E tests (ConfirmDialog, Trash, Bulk, Pin)
```

---

## Local Development Setup

### Prerequisites
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- Node.js 20+ and `npm`

### 1. Install Dependencies

```bash
uv sync
(cd dashboard && npm install)
```

### 2. Run Backend + Frontend with Hot Reload

Open two terminals:

**Terminal 1 — Backend (Agent + Router on `:8080`):**
```bash
# Optional: create a gitignored splitch.local.toml for your own LAN nodes
uv run splitch up --enable-tools --workspace /tmp/splitch-playground
```

**Terminal 2 — Vite Dev Server (`:5173`, proxies `/api`, `/v1`, `/ws` to `:8080`):**
```bash
cd dashboard
npm run dev
```

Open `http://localhost:5173` for instant HMR on React/CSS edits. Before submitting a PR, always build the production bundle (`cd dashboard && npm run build`) so TypeScript strict checking (`tsc -b`) passes cleanly.

---

## Engineering & Security Invariants

Please preserve these core principles in every contribution:

### Backend (Python / FastAPI)
1. **Never block the asyncio event loop:** Offload SQLite queries, filesystem walks, and blocking subprocesses via `asyncio.to_thread(...)` or async `asyncio.create_subprocess_exec(...)`.
2. **Strict schema mirroring:** Whenever you add or rename a field in `backend/shared/models.py`, update the matching TypeScript interface in `dashboard/src/types.ts` in the same PR.
3. **Safe SQLite migrations:** New columns on `conversations` or `messages` must include idempotent `ALTER TABLE ... ADD COLUMN` checks inside `ChatStore._init_db()` so existing user databases upgrade automatically without data loss.
4. **Defense-in-depth for tools & network calls:**
   - Workspace tools remain opt-in (`SPLITCH_ENABLE_TOOLS=0` by default).
   - Every file path must be resolved and checked with `Path.is_relative_to(workspace_root)`.
   - Any URL fetcher must validate resolved IPs against loopback/private/link-local ranges (`_is_public_url` in `backend/router/web_search.py`).
5. **Formatting & Linting:** Code must pass `uv run ruff check backend tests` (`line-length = 100`, Python 3.11+ typing).

### Frontend (React / TypeScript / CSS)
1. **Zero bloat:** Prefer native web platform primitives (`<dialog>`, `<details>`, `<progress>`, CSS `:has()`, `color-mix()`, `light-dark()`) over adding heavy third-party UI libraries.
2. **Accessibility first:**
   - Associate form inputs with explicit `<label htmlFor>` / `id` pairs.
   - Use `:focus-visible` (never remove focus outlines) and maintain >= `24x24px` interactive targets (`44px` on checkbox/touch labels).
   - Keep `aria-pressed` / `aria-expanded` cleanly separated from static accessible names.
   - Gate every destructive delete behind `ConfirmDialog`.
3. **Preview iframe security:** Never add `allow-same-origin` to `ArtifactPreview.tsx`'s `<iframe sandbox="allow-scripts">`, keep `connect-src 'none'` in its CSP, and validate `postMessage` frames via `Origin.from(event)` / `event.source`.

### Personal Data Hygiene
- **Never commit personal IPs, hostnames, usernames, API keys, or local paths.** Keep personal cluster configs in gitignored `splitch.local.toml` and private deploy scripts in `scripts/deploy-local.sh`.

---

## Verification & PR Checklist

Run all three checks locally before opening a pull request:

```bash
uv run ruff check backend tests
uv run pytest -v
(cd dashboard && npm run build)
```

Before submitting:
- [ ] All unit tests pass, and new routes, routing rules, or store methods include focused tests under `tests/`.
- [ ] Backend schemas (`backend/shared/models.py`) and frontend types (`dashboard/src/types.ts`) match field-for-field.
- [ ] Dashboard compiles cleanly with zero TypeScript errors (`tsc -b && vite build`).
- [ ] No private IPs, secrets, or machine-specific paths appear in `git diff`.
- [ ] Docs (`README.md` or `docs/wiki/`) updated if user-facing config, CLI flags, or endpoints changed.
- [ ] Commit messages follow **Conventional Commits** (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).

---

## High-Impact Areas for Contributors

- **GPU & Accelerator Telemetry:** Expand `backend/agent/main.py` for Intel Arc (`xpu-smi`), Windows WMI/NVML refinements, or multi-GPU per-node reporting.
- **Engine Adapters:** Deepen model load/unload & context-length introspection for Ollama, LM Studio, vLLM, and MLX servers.
- **Client Config Exporters:** Add exporters in `backend/router/mcp_api.py` for Continue, Cline, Claude Desktop, or Cursor.
- **UI Polish & Keyboard Navigation:** Command palette, prompt templates, or split-pane code/preview view.
- **Packaging:** Homebrew formula, Nix flake, or pre-built multi-arch Docker images.
