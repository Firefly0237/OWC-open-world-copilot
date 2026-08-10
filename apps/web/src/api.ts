/** Thin client for the standardized REST surface. The Vue app is just one consumer of
 * the same authenticated contract the CLI and pipelines use. */

import { buildTeamIntakeUploadPath } from "./teamIntakeState";

// Same-origin by default: production is served by the API and Vite proxies the same roots in dev.
const BASE: string = ((import.meta.env.VITE_API_BASE as string | undefined) ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
    public readonly code = "",
    public readonly domain: "local" | "team" | "platform" = "local",
  ) {
    super(`${status} ${detail}`.trim());
    this.name = "ApiError";
  }
}

export interface ApiRequestOptions {
  /** End-to-end browser deadline. The server/provider may have a longer task-specific floor. */
  timeoutMs?: number;
}

export const REQUEST_TIMEOUT_MS = Object.freeze({
  DEFAULT: 30_000,
  AI_DEFAULT: 90_000,
  AI_GENERATION: 120_000,
  AI_LONG_GENERATION: 150_000,
  AI_EXTRACTION: 270_000,
  FILE_TRANSFER: 120_000,
});

/**
 * Keep synchronous AI requests alive beyond the matching backend provider floor. Async job
 * creation is deliberately absent: `/jobs` returns immediately and its work is followed by SSE.
 * Callers can always override this with `ApiRequestOptions.timeoutMs`.
 */
export function defaultRequestTimeoutMs(path: string): number {
  if (/\/(?:extractions:run|assist\/extract)(?:\?|$)/.test(path)) {
    return REQUEST_TIMEOUT_MS.AI_EXTRACTION;
  }
  if (/\/(?:assist\/dialogue_trees:draft|characters:draft)(?:\?|$)/.test(path)) {
    return REQUEST_TIMEOUT_MS.AI_LONG_GENERATION;
  }
  if (
    /\/(?:contents\/quests:draft|assist\/(?:barks|flavor):batch|patches:suggest|repairs:plan|quests\/[^/]+\/logic:draft|review_items\/[^/]+:revise|engine:import)(?:\?|$)/
      .test(path)
  ) {
    return REQUEST_TIMEOUT_MS.AI_GENERATION;
  }
  if (
    /\/(?:ask|assist\/ask|contradictions:scan|sweeps:run|styles\/[^/]+:sweep|quality:run|compliance:scan|compliance\/cases\/[^/]+:rescan)(?:\?|$)/
      .test(path)
  ) {
    return REQUEST_TIMEOUT_MS.AI_DEFAULT;
  }
  return REQUEST_TIMEOUT_MS.DEFAULT;
}

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

export function currentServiceApiKey(): string {
  return sessionStorage.getItem("owcopilot_api_key")
    ?? localStorage.getItem("owcopilot_api_key")
    ?? "";
}

export function setServiceApiKey(value: string, remember = false): void {
  const key = value.trim();
  sessionStorage.removeItem("owcopilot_api_key");
  localStorage.removeItem("owcopilot_api_key");
  if (!key) return;
  (remember ? localStorage : sessionStorage).setItem("owcopilot_api_key", key);
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const key = currentServiceApiKey();
  if (key) headers["X-API-Key"] = key;
  return headers;
}

function teamAuthHeaders(): Record<string, string> {
  const token = currentTeamBearer().trim();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function setTeamBearer(token: string, remember = false): void {
  const value = token.trim();
  sessionStorage.removeItem("owcopilot_team_bearer");
  localStorage.removeItem("owcopilot_team_bearer");
  if (value) (remember ? localStorage : sessionStorage).setItem("owcopilot_team_bearer", value);
}

export function currentTeamBearer(): string {
  return sessionStorage.getItem("owcopilot_team_bearer")
    ?? localStorage.getItem("owcopilot_team_bearer")
    ?? "";
}

export function setPlatformBearer(token: string, remember = false): void {
  const value = token.trim();
  sessionStorage.removeItem("owcopilot_platform_bearer");
  localStorage.removeItem("owcopilot_platform_bearer");
  if (value) {
    (remember ? localStorage : sessionStorage).setItem("owcopilot_platform_bearer", value);
  }
}

export function currentPlatformBearer(): string {
  return sessionStorage.getItem("owcopilot_platform_bearer")
    ?? localStorage.getItem("owcopilot_platform_bearer")
    ?? "";
}

function platformAuthHeaders(): Record<string, string> {
  const headers = authHeaders();
  const token = currentPlatformBearer().trim();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

/** Turn any thrown error into a calm, human sentence — never the raw `404 ... OWCOPILOT_PROJECTS_JSON`
 * tail. The technical text still goes to the console for debugging; the user sees guidance. */
export function humanizeError(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  if (typeof console !== "undefined") console.warn("[owcopilot]", raw);
  const msg = raw.replace(/^Error:\s*/i, "");
  if (e instanceof ApiError && e.domain === "team") {
    const code = e.code.toUpperCase();
    if (e.status === 401) return "团队访问令牌无效或已过期，请重新填写 Bearer Token。";
    if (e.status === 403) {
      if (code === "DRAFTING_NOT_AUTHORIZED") {
        return "当前来源未获 DRAFTING 授权；请先核对权利声明与允许用途。";
      }
      return "当前账号没有访问这个团队容器的权限，请核对成员身份与容器 ID。";
    }
    if (e.status === 409) return "团队数据已在别处更新，已停止覆盖；请重新加载最新一页后再操作。";
    if (e.status === 408 || code === "REQUEST_TIMEOUT") {
      return "团队服务响应超时，请确认团队服务与数据库状态后重试。";
    }
    if (e.status === 413 || code === "UPLOAD_TOO_LARGE") {
      return "文件超过团队服务允许的上传大小，请压缩或拆分后重试。";
    }
    if (e.status === 404) return "没有找到这个团队 Run 或正典修订，请核对容器与 Run ID。";
    if (e.status === 422) return "团队请求参数无效，请核对 UUID、必填项与分页条件。";
    if (e.status === 503 && /DATABASE|SCHEMA|INTEGRITY/.test(code)) {
      return "团队资料库暂不可用或尚未迁移完成，请联系部署管理员后重试。";
    }
    if (e.detail && e.detail.length <= 180) return e.detail;
    return "团队操作没有成功，请核对令牌、容器与服务状态后重试。";
  }
  if (e instanceof ApiError && e.domain === "platform" && [401, 403].includes(e.status)) {
    return e.status === 401
      ? "平台令牌无效或已过期，请重新填写平台 Bearer Token。"
      : "当前平台身份没有执行此操作的权限。";
  }
  if (e instanceof ApiError && e.code === "REQUEST_TIMEOUT") {
    return "服务响应超时，请确认服务仍在运行并稍后重试。";
  }
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
  if (/missing or invalid X-API-Key/i.test(msg)) {
    return "服务已启用访问保护，请输入服务访问密钥后继续。";
  }
  if (/\b401\b|category=auth|unauthorized|provider.*api[ _-]?key|未接入|没有可用模型/i.test(msg)) {
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
  return "这一步没有成功，请核对输入与服务状态后重试。";
}

async function ensureOk(
  response: Response,
  domain: "local" | "team" | "platform" = "local",
): Promise<void> {
  if (!response.ok) {
    const raw = await response.text();
    let detail = raw;
    let code = "";
    try {
      const body = JSON.parse(raw) as { detail?: unknown; code?: unknown; message?: unknown };
      const structured = body.detail && typeof body.detail === "object"
        ? body.detail as { code?: unknown; message?: unknown }
        : body;
      code = typeof structured.code === "string" ? structured.code : "";
      detail = typeof body.detail === "string"
        ? body.detail
        : typeof structured.message === "string"
          ? structured.message
          : JSON.stringify(body.detail ?? body);
    } catch {
      // Text/HTML gateway failures are common in development; keep the original body instead of
      // attempting to consume the response stream twice and hiding the useful error.
    }
    throw new ApiError(response.status, detail.slice(0, 300), code, domain);
  }
}

async function fetchWithTimeout(
  path: string,
  init: RequestInit = {},
  timeoutMs = defaultRequestTimeoutMs(path),
  domain: "local" | "team" | "platform" = "local",
): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`${BASE}${path}`, { ...init, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) {
      const modelRequest = defaultRequestTimeoutMs(path) !== REQUEST_TIMEOUT_MS.DEFAULT;
      throw new ApiError(
        408,
        modelRequest ? "模型请求超时，请稍后重试。" : "请求超时，请检查服务连接后重试。",
        modelRequest ? "MODEL_REQUEST_TIMEOUT" : "REQUEST_TIMEOUT",
        domain,
      );
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

export async function apiGet<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const response = await fetchWithTimeout(path, { headers: authHeaders() }, options.timeoutMs);
  await ensureOk(response);
  return (await response.json()) as T;
}

export async function apiPost<T>(
  path: string,
  body: unknown,
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await fetchWithTimeout(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  }, options.timeoutMs);
  await ensureOk(response);
  return (await response.json()) as T;
}

export async function apiPatch<T>(
  path: string,
  body: unknown,
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await fetchWithTimeout(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  }, options.timeoutMs);
  await ensureOk(response);
  return (await response.json()) as T;
}

export async function apiDelete<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const response = await fetchWithTimeout(
    path,
    { method: "DELETE", headers: authHeaders() },
    options.timeoutMs,
  );
  await ensureOk(response);
  return (await response.json()) as T;
}

export async function teamApiGet<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const response = await fetchWithTimeout(path, { headers: teamAuthHeaders() }, options.timeoutMs, "team");
  await ensureOk(response, "team");
  return (await response.json()) as T;
}

export async function teamApiPost<T>(
  path: string,
  body: unknown,
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await fetchWithTimeout(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...teamAuthHeaders() },
    body: JSON.stringify(body),
  }, options.timeoutMs, "team");
  await ensureOk(response, "team");
  return (await response.json()) as T;
}

export async function platformApiGet<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const response = await fetchWithTimeout(
    path,
    { headers: platformAuthHeaders() },
    options.timeoutMs,
    "platform",
  );
  await ensureOk(response, "platform");
  return (await response.json()) as T;
}

export async function platformApiPost<T>(
  path: string,
  body: unknown,
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await fetchWithTimeout(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...platformAuthHeaders() },
    body: JSON.stringify(body),
  }, options.timeoutMs, "platform");
  await ensureOk(response, "platform");
  return (await response.json()) as T;
}

export async function platformApiDelete<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await fetchWithTimeout(path, {
    method: "DELETE",
    headers: platformAuthHeaders(),
  }, options.timeoutMs, "platform");
  await ensureOk(response, "platform");
  return (await response.json()) as T;
}

export async function teamApiUpload<T>(
  containerId: string,
  file: File,
  artifactId = "",
  options: ApiRequestOptions = {},
): Promise<T> {
  const extension = file.name.toLowerCase().split(".").pop() ?? "";
  const mime =
    extension === "docx"
      ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
      : extension === "pdf"
        ? "application/pdf"
        : extension === "xlsx"
          ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          : "application/octet-stream";
  const path = buildTeamIntakeUploadPath(containerId, file.name, artifactId);
  const response = await fetchWithTimeout(path, {
    method: "POST",
    headers: { "Content-Type": mime, ...teamAuthHeaders() },
    body: file,
  }, options.timeoutMs ?? REQUEST_TIMEOUT_MS.FILE_TRANSFER, "team");
  await ensureOk(response, "team");
  return (await response.json()) as T;
}

export function apiUrl(path: string): string {
  return `${BASE}${path}`;
}

/** Download protected exports without losing the X-API-Key header as a plain <a> would. */
export async function apiDownload(
  path: string,
  fallbackName: string,
  options: ApiRequestOptions = {},
): Promise<void> {
  const response = await fetchWithTimeout(
    path,
    { headers: authHeaders() },
    options.timeoutMs ?? REQUEST_TIMEOUT_MS.FILE_TRANSFER,
  );
  await ensureOk(response);
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const plain = disposition.match(/filename="?([^";]+)"?/i)?.[1];
  const filename = encoded ? decodeURIComponent(encoded) : (plain ?? fallbackName);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
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
