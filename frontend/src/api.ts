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

export async function apiGet<T>(path: string): Promise<T> {
  const headers: Record<string, string> = {};
  const key = localStorage.getItem("owcopilot_api_key");
  if (key) headers["X-API-Key"] = key;
  const response = await fetch(`${BASE}${path}`, { headers });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${detail.slice(0, 200)}`);
  }
  return (await response.json()) as T;
}
