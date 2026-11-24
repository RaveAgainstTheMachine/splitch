# Installation & Deployment

This guide walks through setting up Splitch on a single laptop, across a multi-machine LAN or Tailscale mesh, inside Docker, and as persistent OS background services.

---

## 1. One-Command Installer & Updater (Recommended)

Splitch ships cross-platform installers ([install.sh](../../install.sh) and [install.ps1](../../install.ps1)) that automatically bootstrap all runtime dependencies (`uv`, isolated Python 3.12, standalone Node.js 22 without `sudo`, and a hardware-matched `llama.cpp` inference engine when none is installed), build the dashboard bundle, and install the global `splitch` + `llama-server` CLI shims. **Re-running the same command updates both Splitch and your managed inference engine while preserving your configs, chats, skills, and models.**

### macOS, Linux & WSL

```bash
# Install or update full router + dashboard + agent + hardware-matched llama.cpp
curl -fsSL https://raw.githubusercontent.com/RaveAgainstTheMachine/splitch/main/install.sh | sh

# Include local Image & Video engine (stable-diffusion.cpp `sd-server`) automatically
curl -fsSL https://raw.githubusercontent.com/RaveAgainstTheMachine/splitch/main/install.sh | SPLITCH_WITH_MEDIA=1 sh

# Headless GPU/worker install (skips Node.js & UI build; keeps CUDA/ROCm llama.cpp install)
curl -fsSL https://raw.githubusercontent.com/RaveAgainstTheMachine/splitch/main/install.sh | SPLITCH_AGENT_ONLY=1 sh

# Use Ollama instead of llama.cpp, or skip engine management (--engine none)
curl -fsSL https://raw.githubusercontent.com/RaveAgainstTheMachine/splitch/main/install.sh | SPLITCH_ENGINE=ollama sh

# Install/update from an existing local git checkout (optionally with media engine)
./install.sh --local --with-media

# Clean uninstall (preserves ~/.local/share/splitch data unless --purge is added)
./install.sh --uninstall
```

### Windows (PowerShell)

```powershell
# Install or update full stack + hardware-matched llama.cpp (CUDA / Vulkan / CPU)
irm https://raw.githubusercontent.com/RaveAgainstTheMachine/splitch/main/install.ps1 | iex

# Local checkout, headless GPU worker, media engine, or explicit engine choice
.\install.ps1 -Local -WithMedia
.\install.ps1 -AgentOnly -Backend cuda -WithMedia
.\install.ps1 -Engine ollama
```

### Built-In Engine Manager, Self-Updater & Environment Doctor

> **Zero-CLI GUI Setup:** Users do not need to run terminal commands to enable Image & Video generation. Open **Media Studio → Media Engine Manager** in the dashboard to 1-click install `sd-server`, pull a starter checkpoint (`SDXL Turbo`, `SDXL 1.0`, or `SD 1.5`), and click **Start sd-server** directly on any online node.

```bash
splitch doctor --check-updates        # checks Python, uv, Node, GPU, LLM & media engines, SQLite & auth
splitch doctor --fix --with-media     # auto-installs missing LLM + sd.cpp engines & rebuilds missing UI
splitch engine status --check-remote  # inspect GPU accelerator & matched upstream releases
splitch engine install llama.cpp      # install or replace managed llama-server (+ CUDA runtime)
splitch engine install sd.cpp         # install or replace managed stable-diffusion.cpp (sd-server)
splitch engine update                 # atomically upgrade managed llama.cpp or ollama
splitch update --with-engine          # update Splitch + Python/UI deps + managed engines + services
```

---

## 2. Manual Prerequisites (If Not Using `install.sh`)

| Dependency | Version | Required On |
| --- | --- | --- |
| **Python** + [`uv`](https://docs.astral.sh/uv/) | Python 3.11+ | Router & every Node Agent |
| **Node.js** + `npm` | Node 20+ | Router host only (to build `dashboard/dist`) |
| **Local Inference Engine** | Any current version | Every compute node (`llama-server`, Ollama, LM Studio, or vLLM) |

---

## 3. Prepare Your Inference Backend

Splitch sits in front of your inference engine(s). Pick whichever engine you prefer on each machine:

### A. `llama-server` in Router Mode (Recommended for Multi-Model Nodes)

`llama.cpp`'s `llama-server` supports on-demand model loading/unloading via a preset file (`models.ini`). Start from [infra/templates/models.ini.example](../../infra/templates/models.ini.example):

```ini
version = 1

[*]
ctx-size = 32768
flash-attn = on
parallel = 2

[coder-heavy]
model = /path/to/models/Qwen3-Coder-30B-A3B-Q4_K_M.gguf

[fast-small]
model = /path/to/models/Qwen3-8B-Q4_K_M.gguf

[embed-bge]
model = /path/to/models/bge-small-en-v1.5-f16.gguf
embeddings = true
```

Launch `llama-server` listening on LAN (or localhost if single-machine):

```bash
llama-server \
  --models-preset /path/to/models.ini \
  --host 0.0.0.0 \
  --port 8081
```

> **Why router mode shines with Splitch:** Splitch can load and unload models directly from the **Overview** tab and auto-discover every configured preset model on `:8081/models`.

### B. Ollama

Ollama listens on `http://127.0.0.1:11434` by default. For multi-node access from a remote Splitch router, expose Ollama to your LAN or Tailscale IP:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Point your agent to `--engine-url http://127.0.0.1:11434` (Splitch auto-detects Ollama via `/api/tags`).

### C. LM Studio / vLLM / MLX Server / Strata

Any server implementing `/v1/models` and `/v1/chat/completions` works out of the box:
- **LM Studio:** Enable Local Server (`http://0.0.0.0:1234`), pass `--engine-url http://127.0.0.1:1234`.
- **vLLM:** `vllm serve <model> --host 0.0.0.0 --port 8000`, pass `--engine-url http://127.0.0.1:8000`.
- **Strata (`Niko1221/Strata`):** Note that `strata-server` defaults to `:8080`, which collides with `splitch-router`. Run Strata on `:8081` (`--port 8081`) and pass `--engine-url http://127.0.0.1:8081` (auto-detected as `engine="strata"` and excluded from false GPU Auto-Yield).

---

## 4. Single-Machine Manual Quickstart (Zero Config)

```bash
git clone https://github.com/RaveAgainstTheMachine/splitch.git splitch
cd splitch

# 1. Build the static React dashboard
(cd dashboard && npm install && npm run build)

# 2. Install Python dependencies
uv sync

# 3. Launch local agent + router + dashboard together
uv run splitch up
```

`splitch up` automatically probes `:8081` (`llama-server`), `:11434` (Ollama), `:1234` (LM Studio), and `:8000` (vLLM), starts a local agent on `:8090`, and serves the dashboard & OpenAI API at **`http://localhost:8080`**.

### Useful `splitch up` Flags

```bash
uv run splitch up \
  --port 8080 \
  --agent-port 8090 \
  --engine-url http://127.0.0.1:11434 \
  --enable-tools \
  --workspace ~/code
```

---

## 5. Multi-Node Cluster (LAN or Tailscale)

A classic two-node setup pairs an **always-on host** (Apple Silicon Mac Mini, Linux mini-PC, or home server running `splitch-router` + local `splitch-agent`) with a **gaming desktop / GPU workstation** (running `splitch-agent` with `--yield-on-gaming`).

### Step 1: Start the Router Host

```bash
# On the always-on machine (e.g. 192.168.1.10)
git clone https://github.com/RaveAgainstTheMachine/splitch.git splitch && cd splitch
(cd dashboard && npm install && npm run build)
uv sync

# Generate a starter splitch.toml (or copy splitch.example.toml)
uv run splitch init

# Start the local node agent + router
uv run splitch agent --id always-on --name "Always-On Node" --role unified &
uv run splitch router --host 0.0.0.0 --port 8080
```

### Step 2: Join Each Remote Compute Node

On your GPU desktop (e.g. `192.168.1.50` or a Tailscale `100.x.y.z` IP):

```bash
git clone https://github.com/RaveAgainstTheMachine/splitch.git splitch && cd splitch
uv sync   # no Node.js needed on agent-only machines!

uv run splitch agent \
  --id gpu-rig \
  --name "GPU Workstation" \
  --role gpu \
  --yield-on-gaming \
  --engine-url http://127.0.0.1:8081 \
  --storage-path /srv/models \
  --advertise-host 192.168.1.50 \
  --join http://192.168.1.10:8080
```

> **Firewall ports to allow on your LAN / Tailscale interface:**
> - Router host: TCP `8080` (dashboard + `/v1` API)
> - Compute nodes: TCP `8090` (`splitch-agent`) and your engine port (e.g. `8081` or `11434`)

> **Tailscale Tip:** Pass `--advertise-host $(tailscale ip -4)` on remote nodes and set `SPLITCH_AGENT_TOKEN` + `SPLITCH_API_KEY` on both sides for authenticated cross-site routing.

---

## 6. Docker & Docker Compose

Build and run the router in a container while pointing to node agents on your host or LAN:

```bash
cp splitch.example.toml splitch.toml
# Edit splitch.toml with reachable agent_url and engine_url entries
# Tip: On Docker Desktop / Linux with host-gateway, use http://host.docker.internal:8090
docker compose up -d --build
```

Verify health:

```bash
curl http://localhost:8080/health
```

Persistent chats, aliases, MCP registry, and skills are stored in the named `splitch_data` volume mounted at `/data`.

---

## 7. Persistent OS Background Services

### Linux (`systemd` User Service)

Copy the templates from `infra/templates/` and customize paths/env vars:

```bash
mkdir -p ~/.config/systemd/user

# Agent service (on every Linux compute node)
cp infra/templates/splitch-agent.service ~/.config/systemd/user/
# Router service (on your primary router host)
cp infra/templates/splitch-router.service ~/.config/systemd/user/

# Edit WorkingDirectory, SPLITCH_ENGINE_URL, SPLITCH_STORAGE_PATH, SPLITCH_ENABLE_TOOLS as needed
systemctl --user daemon-reload
systemctl --user enable --now splitch-agent.service
systemctl --user enable --now splitch-router.service

# Optional: allow user services to start at boot without interactive login
loginctl enable-linger "$USER"
```

Check logs:
```bash
journalctl --user -u splitch-agent -f
journalctl --user -u splitch-router -f
```

### macOS (`launchd`)

1. Copy [infra/templates/com.splitch.agent.plist](../../infra/templates/com.splitch.agent.plist) to `~/Library/LaunchAgents/com.splitch.agent.plist`.
2. Update `/path/to/splitch` to your actual checkout path and set `SPLITCH_ENGINE_URL` / `SPLITCH_STORAGE_PATH`.
3. Load the agent:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.splitch.agent.plist
launchctl list | grep splitch
```

Create a matching `com.splitch.router.plist` invoking `.venv/bin/splitch router --host 0.0.0.0 --port 8080` if the Mac also hosts the router.

---

Next: **[Configuration & Routing →](Configuration-and-Routing.md)**
