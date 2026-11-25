export interface DraftChatConfig {
  model: string;
  exec_node: string;
  workspace_subpath: string;
  tools_enabled: boolean;
  web_search_enabled: boolean;
  swarm_enabled: boolean;
  auto_approve: boolean;
  auto_compact: boolean;
  enabled_skills: string[];
  enabled_mcps: string[];
  system_prompt: string;
}

export interface ParsedSseEvent {
  event: string;
  data: Record<string, unknown>;
}

export const DEFAULT_DRAFT: DraftChatConfig = {
  model: "auto",
  exec_node: "",
  workspace_subpath: "",
  tools_enabled: true,
  web_search_enabled: false,
  swarm_enabled: false,
  auto_approve: false,
  auto_compact: true,
  enabled_skills: [],
  enabled_mcps: [],
  system_prompt: ""
};

export function parseSseChunk(rawBlock: string): ParsedSseEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of rawBlock.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (dataLines.length === 0) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) as Record<string, unknown> };
  } catch {
    return null;
  }
}

export function fmtShortDate(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("Failed to read image"));
    reader.readAsDataURL(file);
  });
}

export function fmtTokenCompact(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(n);
}
