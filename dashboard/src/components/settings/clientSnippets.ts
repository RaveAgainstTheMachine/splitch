import { Gauge, Palette, Server, Shield, Sliders, Terminal } from "lucide-react";

export type SettingsTabId =
  | "clients"
  | "routing"
  | "benchmarks"
  | "nodes"
  | "security"
  | "appearance";

export const SETTINGS_TABS: Array<{ id: SettingsTabId; label: string; icon: typeof Terminal }> = [
  { id: "clients", label: "Connect Clients", icon: Terminal },
  { id: "routing", label: "Routing & Guards", icon: Sliders },
  { id: "benchmarks", label: "Benchmarks", icon: Gauge },
  { id: "nodes", label: "Install Nodes", icon: Server },
  { id: "security", label: "Security & System", icon: Shield },
  { id: "appearance", label: "Appearance", icon: Palette }
];

export type ClientId =
  | "hermes"
  | "claude_code"
  | "codex"
  | "opencode"
  | "aider"
  | "goose"
  | "zed"
  | "cline"
  | "python"
  | "curl";

export const PUSHABLE_CLIENTS = new Set<ClientId>([
  "hermes",
  "claude_code",
  "codex",
  "opencode",
  "aider",
  "zed"
]);

export const CLIENTS: Array<{ id: ClientId; label: string; proto: string }> = [
  { id: "hermes", label: "Hermes", proto: "OpenAI /v1" },
  { id: "claude_code", label: "Claude Code", proto: "Anthropic /v1/messages" },
  { id: "codex", label: "Codex CLI", proto: "Responses /v1/responses" },
  { id: "opencode", label: "OpenCode", proto: "OpenAI /v1 + MCP" },
  { id: "aider", label: "Aider", proto: "OpenAI /v1" },
  { id: "goose", label: "Goose", proto: "OpenAI /v1" },
  { id: "zed", label: "Zed", proto: "OpenAI /v1 + MCP" },
  { id: "cline", label: "Cline / Roo", proto: "OpenAI /v1" },
  { id: "python", label: "Python SDK", proto: "OpenAI /v1" },
  { id: "curl", label: "curl", proto: "HTTP" }
];

export function buildClientSnippets(
  client: ClientId,
  baseOrigin: string,
  model: string,
  apiKey: string
): { runCmd: string; configTitle?: string; configBody?: string } {
  const origin = baseOrigin.replace(/\/+$/, "");
  const v1 = `${origin}/v1`;
  switch (client) {
    case "hermes":
      return {
        runCmd: `hermes chat --model ${model}`,
        configTitle: "~/.hermes/config.yaml",
        configBody: `model:\n  provider: custom\n  default: ${model}\n  base_url: ${v1}\n  api_key: ${apiKey}\n  context_length: 65536`
      };
    case "claude_code":
      return {
        runCmd: `ANTHROPIC_BASE_URL="${origin}" ANTHROPIC_AUTH_TOKEN="${apiKey}" ANTHROPIC_MODEL="${model}" claude`,
        configTitle: "~/.claude/settings.json",
        configBody: JSON.stringify(
          {
            env: {
              ANTHROPIC_BASE_URL: origin,
              ANTHROPIC_AUTH_TOKEN: apiKey,
              ANTHROPIC_MODEL: model
            }
          },
          null,
          2
        )
      };
    case "codex":
      return {
        runCmd: `SPLITCH_API_KEY="${apiKey}" codex -m ${model}`,
        configTitle: "~/.codex/config.toml",
        configBody: `model = "${model}"\nmodel_provider = "splitch"\n\n[model_providers.splitch]\nname = "Splitch"\nbase_url = "${v1}"\nwire_api = "responses"\nenv_key = "SPLITCH_API_KEY"`
      };
    case "opencode":
      return {
        runCmd: `opencode -m splitch/${model}`,
        configTitle: "~/.config/opencode/opencode.json",
        configBody: JSON.stringify(
          {
            $schema: "https://opencode.ai/config.json",
            provider: {
              splitch: {
                npm: "@ai-sdk/openai-compatible",
                name: "Splitch Local Cluster",
                options: { baseURL: v1, apiKey },
                models: { [model]: { name: model } }
              }
            },
            model: `splitch/${model}`
          },
          null,
          2
        )
      };
    case "aider":
      return {
        runCmd: `OPENAI_API_BASE="${v1}" OPENAI_API_KEY="${apiKey}" aider --model openai/${model} --weak-model openai/fast`,
        configTitle: "~/.aider.conf.yml",
        configBody: `openai-api-base: ${v1}\nopenai-api-key: ${apiKey}\nmodel: openai/${model}\nweak-model: openai/fast`
      };
    case "goose":
      return {
        runCmd: `OPENAI_HOST="${origin}" OPENAI_API_KEY="${apiKey}" GOOSE_PROVIDER=openai GOOSE_MODEL="${model}" goose session`
      };
    case "zed":
      return {
        runCmd: `zed .`,
        configTitle: "~/.config/zed/settings.json",
        configBody: JSON.stringify(
          {
            language_models: {
              openai_compatible: {
                Splitch: {
                  api_url: v1,
                  available_models: [
                    { name: model, display_name: `Splitch ${model}`, max_tokens: 65536 }
                  ]
                }
              }
            }
          },
          null,
          2
        )
      };
    case "cline":
      return {
        runCmd: `Provider: OpenAI Compatible  |  Base URL: ${v1}  |  API Key: ${apiKey}  |  Model: ${model}`
      };
    case "python":
      return {
        runCmd: `from openai import OpenAI\n\nclient = OpenAI(base_url="${v1}", api_key="${apiKey}")\nresp = client.chat.completions.create(\n    model="${model}",\n    messages=[{"role": "user", "content": "Hello from Splitch"}],\n)\nprint(resp.choices[0].message.content)`
      };
    case "curl":
      return {
        runCmd: `curl ${v1}/chat/completions \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${apiKey}" \\\n  -d '{"model":"${model}","messages":[{"role":"user","content":"Hello"}]}'`
      };
  }
}
