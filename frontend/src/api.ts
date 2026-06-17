/** Thin client for the standardized REST surface. The Vue app is just one consumer of
 * the same authenticated contract the CLI and pipelines use. */

// Same-origin by default: the built app ships from the API server itself (one command,
// one port). Dev mode points at uvicorn via frontend/.env.development.
const BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export function currentProject(): string {
  return localStorage.getItem("owcopilot_project") ?? "demo";
}

export function setCurrentProject(name: string): void {
  localStorage.setItem("owcopilot_project", name);
}

export function currentOperator(): string {
  return localStorage.getItem("owcopilot_operator") ?? "";
}

export function setCurrentOperator(name: string): void {
  localStorage.setItem("owcopilot_operator", name);
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const key = localStorage.getItem("owcopilot_api_key");
  if (key) headers["X-API-Key"] = key;
  return headers;
}

/** Turn any thrown error into a calm, human sentence — never the raw `404 ... OWCOPILOT_PROJECTS_JSON`
 * tail. The technical text still goes to the console for debugging; the user sees guidance. */
export function humanizeError(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  if (typeof console !== "undefined") console.warn("[owcopilot]", raw);
  const msg = raw.replace(/^Error:\s*/i, "");
  if (/not registered|无效的项目/i.test(msg) || (/\b404\b/.test(msg) && /project|world/i.test(msg))) {
    return "还没有打开世界。在左侧「管理 · 世界」里新建或选择一个，就能开始。";
  }
  // The LLM-gateway error message always contains "provider", so the SPECIFIC failure categories
  // must be matched before the generic provider/connection branch — otherwise a timeout or a rate
  // limit would mis-guide the user to "go connect a model".
  if (/timeout|timed out|category=timeout|超时/i.test(msg)) {
    return "模型这次响应超时了，稍后再试；若总是这样，可到「设置」换个服务商或检查网络。";
  }
  if (/\b429\b|rate.?limit|category=rate_limit|too many|频繁/i.test(msg)) {
    return "操作太频繁了，稍等片刻再试。";
  }
  if (/\b401\b|category=auth|unauthorized|api[ _-]?key|未接入|没有可用模型|OWCOPILOT_API_KEY/i.test(msg)) {
    return "需要先接入模型。到「设置」填好服务商和 API Key 就能继续。";
  }
  if (/\b503\b|category=(?:connection|provider_error)|provider call failed|无法连接/i.test(msg)) {
    return "模型服务暂时连不上，确认「设置」里的服务商地址、API Key 和网络后再试。";
  }
  if (/failed to fetch|networkerror|connection|fetch/i.test(msg)) {
    return "暂时连不上服务，确认本地服务已启动后再试。";
  }
  // Many backend errors are ALREADY actionable Chinese guidance ("世界已存在，换个名字" / "先填署名")
  // — surface that as-is instead of burying it under a generic line. Strip the leading HTTP status
  // and only trust a message that reads like a human sentence (has CJK, short, no stack markers).
  const detail = msg.replace(/^\d{3}\s+/, "").trim();
  if (/[一-鿿]/.test(detail) && detail.length <= 160 && !/traceback|\bat \b|[{}]/i.test(detail)) {
    return detail;
  }
  return "这一步没有成功，请重试；若反复出现，到「设置」检查模型连接，或确认服务是否正常。";
}

async function ensureOk(response: Response): Promise<void> {
  if (!response.ok) {
    let detail = "";
    try {
      const body = (await response.json()) as { detail?: unknown };
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      detail = await response.text();
    }
    throw new Error(`${response.status} ${detail.slice(0, 300)}`);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { headers: authHeaders() });
  await ensureOk(response);
  return (await response.json()) as T;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  await ensureOk(response);
  return (await response.json()) as T;
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  await ensureOk(response);
  return (await response.json()) as T;
}

export async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { method: "DELETE", headers: authHeaders() });
  await ensureOk(response);
  return (await response.json()) as T;
}

export function apiUrl(path: string): string {
  return `${BASE}${path}`;
}

// ---- model connection: the server holds the provider env; the client remembers the
// chosen model and whether the connection is live, so every generator can send
// llm_mode=real without re-asking.
export interface LlmConfig {
  ready: boolean;
  model: string;
}

export function llmConfig(): LlmConfig {
  return {
    ready: localStorage.getItem("owcopilot_llm_ready") === "1",
    model: localStorage.getItem("owcopilot_model") ?? "",
  };
}

export function setLlmConfig(ready: boolean, model: string): void {
  localStorage.setItem("owcopilot_llm_ready", ready ? "1" : "0");
  if (model) localStorage.setItem("owcopilot_model", model);
}

/** Params every generation call spreads in: real mode when connected, nothing otherwise
 * (the backend then refuses with setup guidance instead of silently faking output). */
export function llmParams(): Record<string, string> {
  const config = llmConfig();
  return config.ready && config.model
    ? { llm_mode: "real", llm_model: config.model }
    : { llm_mode: "real" };
}

export function addSessionCost(usd: number): void {
  const current = Number(sessionStorage.getItem("owcopilot_session_cost") ?? "0");
  sessionStorage.setItem("owcopilot_session_cost", String(current + (usd || 0)));
  window.dispatchEvent(new CustomEvent("ow-cost-changed"));
}

export function sessionCost(): number {
  return Number(sessionStorage.getItem("owcopilot_session_cost") ?? "0");
}

export function costOf(result: { cost_budget?: { used_usd?: number } } | null | undefined): number {
  return Number(result?.cost_budget?.used_usd ?? 0);
}

export interface JobEvent {
  type: string;
  data: Record<string, unknown>;
}

/** Tail a job's SSE stream. Uses fetch+reader instead of EventSource so the X-API-Key
 * header can ride along. Resolves when the stream closes (job terminal). */
export async function streamJobEvents(
  jobId: string,
  onEvent: (event: JobEvent) => void,
): Promise<void> {
  const response = await fetch(`${BASE}/jobs/${jobId}/events`, { headers: authHeaders() });
  await ensureOk(response);
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventType = "message";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline = buffer.indexOf("\n");
    while (newline >= 0) {
      const line = buffer.slice(0, newline).replace(/\r$/, "");
      buffer = buffer.slice(newline + 1);
      if (line.startsWith("event:")) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        let data: Record<string, unknown> = {};
        try {
          data = JSON.parse(line.slice(5).trim()) as Record<string, unknown>;
        } catch {
          data = {};
        }
        onEvent({ type: eventType, data });
        eventType = "message";
      }
      newline = buffer.indexOf("\n");
    }
  }
}
