# Clients & Integrations

Splitch exposes standard inference endpoints on `:8080` so any OpenAI-compatible or Anthropic-compatible CLI, IDE, agent framework, or SDK automatically inherits **multi-node routing, aliases (`auto`, `heavy`, `fast`, `fim`, `embed`), GPU Auto-Yield VRAM eviction, and standby failover**.

---

## Protocol Compatibility Matrix

| Client / Tool | Category | Wire Protocol | Splitch Endpoint | Starter Template |
| --- | --- | --- | --- | --- |
| **Hermes Agent** (NousResearch CLI & Desktop) | Agent CLI | OpenAI Chat | `/v1/chat/completions`, `/v1/models` | [hermes-config.example.yaml](../../infra/clients/hermes-config.example.yaml) |
| **Claude Code** (`claude` CLI) | Agent CLI | Anthropic Messages | `/v1/messages`, `/v1/messages/count_tokens` *(requires modern `llama-server` / Ollama / vLLM backend)* | Shell / `~/.claude/settings.json` below |
| **OpenAI Codex CLI** (`codex`) | Agent CLI | OpenAI Responses | `/v1/responses` *(requires modern `llama-server` / Ollama / vLLM backend)* | [codex-config.example.toml](../../infra/clients/codex-config.example.toml) |
| **OpenCode** | Agent CLI / TUI | OpenAI Chat | `/v1/chat/completions`, `/v1/models` | [opencode.example.json](../../infra/clients/opencode.example.json) + UI Sync |
| **Aider** | Coding CLI | OpenAI Chat | `/v1/chat/completions` | [aider.example.conf.yml](../../infra/clients/aider.example.conf.yml) |
| **Goose** (Block) | Agent CLI | OpenAI Chat | `/v1/chat/completions` | [goose-provider.example.json](../../infra/clients/goose-provider.example.json) |
| **OpenHands** | Autonomous Agent | OpenAI Chat (LiteLLM) | `/v1/chat/completions` | Env / UI below |
| **Zed** | Editor / IDE | OpenAI Chat | `/v1/chat/completions`, `/v1/models` | [zed-settings.example.json](../../infra/clients/zed-settings.example.json) + UI Sync |
| **Cline / Roo Code** | VS Code / JetBrains | OpenAI Chat | `/v1/chat/completions`, `/v1/models` | Settings below |
| **Continue** | VS Code / JetBrains | OpenAI Chat + FIM + Embeddings | `/v1/chat/completions`, `/v1/embeddings` | [continue-config.example.json](../../infra/clients/continue-config.example.json) |
| **Cursor** | Editor / IDE | OpenAI Chat | `/v1/chat/completions` | Settings below |
| **Open WebUI** | Self-Hosted Chat UI | OpenAI Chat + Images | `/v1/models`, `/v1/chat/completions`, `/v1/images/generations` | Env below |
| **LiteLLM Proxy** | Gateway | OpenAI Chat / Embeddings | `/v1/*` | YAML below |
| **Python / Node SDKs** (`openai`, `anthropic`) | Libraries | Chat, Responses, Messages, Embeddings, Images | `/v1/*` | Snippets below |
| **LangChain / LlamaIndex / CrewAI** | Frameworks | OpenAI Chat + Embeddings | `/v1/chat/completions`, `/v1/embeddings` | Snippets below |

> **Auth note:** If `SPLITCH_API_KEY` is unset, pass any placeholder string (e.g., `splitch-local`) for clients that require a non-empty API key. If `SPLITCH_API_KEY` is set, pass it via `Authorization: Bearer <key>` or `X-API-Key: <key>` (both are accepted on all `/v1/*` and `/infill` routes).

> **Context window note for agentic CLIs:** Hermes, Claude Code, Codex, OpenCode, and Cline send large tool schemas and multi-turn diffs. Launch `llama-server` with at least `-c 32768` (ideally `-c 65536`) so tool loops do not truncate.

---

## Choosing a Model ID

Every client can target Splitch at three levels of granularity (inspect live IDs with `curl http://<router>:8080/v1/models`):

- **`auto`** — Routes tool-calling or long prompts (`>= auto_heavy_char_threshold`) to `heavy`, short prompts to `fast`, embeddings to `embed`, and `/infill` to `fim`.
- **Logical alias (`heavy`, `fast`, `fim`, `embed`, or custom aliases)** — Routes to the alias's primary node and automatically fails over to its standby fallback if the GPU yields for a game/3D app, drops low on VRAM, or goes offline.
- **Explicit node target (`<node_id>/<model_id>`)** — Pins directly to a specific machine and model (e.g., `gpu-rig/qwen3-coder-30b-a3b`).

Every proxied response includes diagnostic headers:
- `X-Splitch-Node` — Node that served the request
- `X-Splitch-Model` — Resolved upstream model ID
- `X-Splitch-Fallback` — Present if failover occurred (e.g., `gpu-rig_gpu_busy_app`, `gaming_mode`, `gpu-rig_gpu_cooldown`)

---

## CLI Agents & Terminal Tools

### 1. Hermes Agent (NousResearch CLI & Desktop)

Hermes connects directly to Splitch's OpenAI Chat Completions endpoint (`/v1/chat/completions`).

**Option A — Interactive CLI setup:**
```bash
hermes model
# Select: Custom Endpoint
# Base URL: http://<ROUTER_HOST>:8080/v1
# API Key:  splitch-local (or your SPLITCH_API_KEY)
# Model ID: heavy   (or auto / fast / <node>/<model>)
```

**Option B — `~/.hermes/config.yaml` (works for both CLI and Hermes Desktop UI picker):**
```yaml
model:
  provider: custom
  default: heavy
  base_url: http://<ROUTER_HOST>:8080/v1
  api_key: splitch-local # or ${HERMES_MODEL_API_KEY} in ~/.hermes/.env
  context_length: 65536

providers:
  custom:
    name: Splitch Cluster
    base_url: http://<ROUTER_HOST>:8080/v1
    api_key: splitch-local
    transport: openai_chat
    api_mode: chat_completions
    discover_models: false
    default_model: heavy
    context_length: 65536
    models:
      auto:
        display_name: Splitch Auto
        context_length: 65536
      heavy:
        display_name: Splitch Heavy
        context_length: 65536
      fast:
        display_name: Splitch Fast
        context_length: 32768
```

If `SPLITCH_API_KEY` is enabled, keep the secret in `~/.hermes/.env`:
```env
HERMES_MODEL_API_KEY=your-splitch-api-key
```

Template: [infra/clients/hermes-config.example.yaml](../../infra/clients/hermes-config.example.yaml).

---

### 2. Claude Code (`claude` CLI)

Splitch natively proxies Anthropic's **`POST /v1/messages`** and **`POST /v1/messages/count_tokens`** routes (forwarding `anthropic-version` and `anthropic-beta` headers and accepting `X-API-Key` / `Authorization: Bearer`) to nodes running modern `llama-server`, Ollama, or vLLM with Anthropic endpoint support.

> **Important:** Do **not** append `/v1` to `ANTHROPIC_BASE_URL` — Claude Code automatically appends `/v1/messages`.

**Shell session:**
```bash
export ANTHROPIC_BASE_URL="http://<ROUTER_HOST>:8080"
export ANTHROPIC_AUTH_TOKEN="splitch-local"   # or your SPLITCH_API_KEY
export ANTHROPIC_MODEL="heavy"                # routes through Splitch alias + GPU Auto-Yield
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"
export CLAUDE_CODE_ATTRIBUTION_HEADER="0"

claude
```

**Persistent `~/.claude/settings.json`:**
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://<ROUTER_HOST>:8080",
    "ANTHROPIC_AUTH_TOKEN": "splitch-local",
    "ANTHROPIC_MODEL": "heavy",
    "ANTHROPIC_SMALL_FAST_MODEL": "fast",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0"
  }
}
```

---

### 3. OpenAI Codex CLI (`codex`)

Modern Codex CLI speaks the **OpenAI Responses API** (`POST /v1/responses`). Splitch natively routes `/v1/responses` through its policy engine to nodes running modern `llama-server`, Ollama, or vLLM.

Add to `~/.codex/config.toml` (Windows: `%USERPROFILE%\.codex\config.toml`):
```toml
model = "heavy"
model_provider = "splitch"

[model_providers.splitch]
name = "Splitch Local Cluster"
base_url = "http://<ROUTER_HOST>:8080/v1"
wire_api = "responses"
env_key = "SPLITCH_API_KEY"
```

Run:
```bash
export SPLITCH_API_KEY="splitch-local"
codex
```

Template: [infra/clients/codex-config.example.toml](../../infra/clients/codex-config.example.toml).

---

### 4. OpenCode

Generate and push `opencode.json` (including all aliases and enabled MCP servers) directly from the Splitch UI under **MCP Servers → Client Config**, or place `~/.config/opencode/opencode.json` manually:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "splitch": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Splitch Local Cluster",
      "options": {
        "baseURL": "http://<ROUTER_HOST>:8080/v1",
        "apiKey": "splitch-local"
      },
      "models": {
        "auto": { "name": "Splitch Auto" },
        "heavy": { "name": "Splitch Heavy" },
        "fast": { "name": "Splitch Fast" }
      }
    }
  },
  "model": "splitch/heavy",
  "small_model": "splitch/fast"
}
```

Template: [infra/clients/opencode.example.json](../../infra/clients/opencode.example.json).

---

### 5. Aider

**Environment variables + split heavy/fast models:**
```bash
export OPENAI_API_BASE="http://<ROUTER_HOST>:8080/v1"
export OPENAI_API_KEY="splitch-local"
aider --model openai/heavy --weak-model openai/fast
```

**Persistent `~/.aider.conf.yml`:**
```yaml
openai-api-base: http://<ROUTER_HOST>:8080/v1
openai-api-key: splitch-local
model: openai/heavy
weak-model: openai/fast
editor-model: openai/heavy
```

Template: [infra/clients/aider.example.conf.yml](../../infra/clients/aider.example.conf.yml).

---

### 6. Goose (Block)

**Option A — Environment variables:**
```bash
export GOOSE_PROVIDER=openai
export GOOSE_HOST="http://<ROUTER_HOST>:8080"
export OPENAI_API_KEY="splitch-local"
export GOOSE_MODEL="heavy"
goose session
```

**Option B — Named custom provider (`~/.config/goose/custom_providers/splitch.json`):**
```json
{
  "name": "splitch",
  "engine": "openai",
  "display_name": "Splitch Local Cluster",
  "base_url": "http://<ROUTER_HOST>:8080/v1",
  "api_key_env": "SPLITCH_API_KEY",
  "models": ["auto", "heavy", "fast"],
  "stream": true
}
```

Then run:
```bash
export GOOSE_PROVIDER=splitch
export SPLITCH_API_KEY="splitch-local"
export GOOSE_MODEL=heavy
goose session
```

Template: [infra/clients/goose-provider.example.json](../../infra/clients/goose-provider.example.json).

---

### 7. OpenHands

```bash
export LLM_BASE_URL="http://<ROUTER_HOST>:8080/v1"
export LLM_API_KEY="splitch-local"
export LLM_MODEL="openai/heavy"
```

In the OpenHands Web UI: **Settings → LLM → Advanced**, set **Custom Model** to `openai/heavy`, **Base URL** to `http://<ROUTER_HOST>:8080/v1`, and **API Key** to `splitch-local` (or `SPLITCH_API_KEY`).

---

## IDEs & Editor Extensions

### 8. Zed

Generate and push `settings.json` from **MCP Servers → Client Config** in the Splitch UI, or add to `~/.config/zed/settings.json`:

```json
{
  "language_models": {
    "openai_compatible": {
      "Splitch": {
        "api_url": "http://<ROUTER_HOST>:8080/v1",
        "available_models": [
          {
            "name": "auto",
            "display_name": "Splitch Auto",
            "max_tokens": 65536,
            "capabilities": {
              "tools": true,
              "images": false,
              "parallel_tool_calls": true,
              "prompt_cache_key": false
            }
          },
          {
            "name": "heavy",
            "display_name": "Splitch Heavy",
            "max_tokens": 65536,
            "capabilities": {
              "tools": true,
              "images": false,
              "parallel_tool_calls": true,
              "prompt_cache_key": false
            }
          }
        ]
      }
    }
  }
}
```

Template: [infra/clients/zed-settings.example.json](../../infra/clients/zed-settings.example.json).

---

### 9. Cline & Roo Code (VS Code / JetBrains)

1. Open extension **Settings**.
2. Set **API Provider** to **OpenAI Compatible**.
3. Configure:
   - **Base URL:** `http://<ROUTER_HOST>:8080/v1`
   - **API Key:** `splitch-local` (or your `SPLITCH_API_KEY`)
   - **Model ID:** `heavy` (or `auto`, `fast`, `<node>/<model>`)
   - **Context Window:** Match your node's `-c` setting (e.g., `65536`).

---

### 10. Continue (VS Code / JetBrains)

Add to `~/.continue/config.json` (routes chat to `heavy`/`fast`, tab autocomplete to `fim`, and `@codebase` indexing to `embed`):

```json
{
  "models": [
    {
      "title": "Splitch Heavy",
      "provider": "openai",
      "model": "heavy",
      "apiBase": "http://<ROUTER_HOST>:8080/v1",
      "apiKey": "splitch-local"
    },
    {
      "title": "Splitch Fast",
      "provider": "openai",
      "model": "fast",
      "apiBase": "http://<ROUTER_HOST>:8080/v1",
      "apiKey": "splitch-local"
    }
  ],
  "tabAutocompleteModel": {
    "title": "Splitch FIM",
    "provider": "openai",
    "model": "fim",
    "apiBase": "http://<ROUTER_HOST>:8080/v1",
    "apiKey": "splitch-local"
  },
  "embeddingsProvider": {
    "provider": "openai",
    "model": "embed",
    "apiBase": "http://<ROUTER_HOST>:8080/v1",
    "apiKey": "splitch-local"
  }
}
```

Template: [infra/clients/continue-config.example.json](../../infra/clients/continue-config.example.json).

---

### 11. Cursor

1. Open **Settings → Models → OpenAI API Key**.
2. Toggle **Override OpenAI Base URL** and set `http://<ROUTER_HOST>:8080/v1`.
3. Add custom model IDs: `auto`, `heavy`, `fast`.

---

## Web UIs & Gateways

### 12. Open WebUI

Point Open WebUI at Splitch to automatically populate `/v1/models` and use Splitch's Media Studio (`/v1/images/generations`) for image generation:

```bash
docker run -d -p 3000:8080 \
  -e OPENAI_API_BASE_URL="http://<ROUTER_HOST>:8080/v1" \
  -e OPENAI_API_KEY="splitch-local" \
  --name open-webui \
  ghcr.io/open-webui/open-webui:main
```

### 13. LiteLLM Proxy

```yaml
model_list:
  - model_name: local-heavy
    litellm_params:
      model: openai/heavy
      api_base: http://<ROUTER_HOST>:8080/v1
      api_key: splitch-local
  - model_name: local-fast
    litellm_params:
      model: openai/fast
      api_base: http://<ROUTER_HOST>:8080/v1
      api_key: splitch-local
```

---

## SDKs, Frameworks & `curl`

### 14. Python `openai` SDK (Chat, Streaming, Tools, Embeddings, Images)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="splitch-local",  # or your SPLITCH_API_KEY
)

# Streaming chat with automatic node routing & GPU Auto-Yield failover
stream = client.chat.completions.create(
    model="heavy",
    stream=True,
    messages=[{"role": "user", "content": "Write a Go ring buffer."}],
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)

# Embeddings routed via the `embed` alias
emb = client.embeddings.create(model="embed", input="splitch routing")

# Image generation via Splitch Media Studio (auto-evicts GPU LLM VRAM)
img = client.images.generate(
    model="auto",
    prompt="isometric mechanical keyboard on dark slate, studio lighting",
    size="1024x1024",
    response_format="url",
)
print(img.data[0].url)
```

### 15. TypeScript / Node `openai` SDK

```ts
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://localhost:8080/v1",
  apiKey: process.env.SPLITCH_API_KEY ?? "splitch-local"
});

const resp = await client.chat.completions.create({
  model: "auto",
  messages: [{ role: "user", content: "Summarize HTTP/3 QUIC in 3 bullets." }]
});
console.log(resp.choices[0]?.message.content);
```

### 16. Python `anthropic` SDK

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://localhost:8080",  # no /v1 suffix
    api_key="splitch-local",
)

msg = client.messages.create(
    model="heavy",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Explain Rust lifetimes briefly."}],
)
print(msg.content[0].text)
```

### 17. LangChain / CrewAI

```python
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

llm = ChatOpenAI(
    model="heavy",
    base_url="http://localhost:8080/v1",
    api_key="splitch-local",
)
embeddings = OpenAIEmbeddings(
    model="embed",
    base_url="http://localhost:8080/v1",
    api_key="splitch-local",
)
```

### 18. Direct `curl` (All Wire Protocols)

```bash
# 1. List available aliases and node models
curl http://localhost:8080/v1/models

# 2. OpenAI Chat Completions (inspect X-Splitch-Node / X-Splitch-Fallback headers)
curl -i http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}'

# 3. OpenAI Responses API (Codex CLI / Agents SDK)
curl http://localhost:8080/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"heavy","input":"Write a one-line bash port scanner."}'

# 4. Anthropic Messages API (Claude Code / Anthropic SDK)
curl http://localhost:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"heavy","max_tokens":256,"messages":[{"role":"user","content":"Hi"}]}'

# 5. Embeddings & llama.cpp FIM Infill
curl http://localhost:8080/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"embed","input":"local inference routing"}'

curl http://localhost:8080/infill \
  -H "Content-Type: application/json" \
  -d '{"model":"fim","input_prefix":"def add(a, b):\n    ","input_suffix":"\n"}'

# 6. Image & Video Generation
curl http://localhost:8080/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"prompt":"brutalist concrete library at dusk","size":"1024x1024","response_format":"url"}'

curl http://localhost:8080/v1/videos/generations \
  -H "Content-Type: application/json" \
  -d '{"prompt":"slow camera pan across foggy pine forest","width":768,"height":512,"frames":16,"fps":8}'
```

---

Next: **[API Reference →](API-Reference.md)**
