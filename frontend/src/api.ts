/** Thin client for the standardized REST surface. The Vue app is just one consumer of
 * the same authenticated contract the Streamlit UI, CLI and pipelines use. */

const BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:8000";

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
