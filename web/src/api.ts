export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export function detectDesktopRuntime(): boolean {
  if (typeof window === "undefined") return false;
  const w = window as Window & { __TAURI_INTERNALS__?: unknown; __TAURI__?: unknown };
  if (w.__TAURI_INTERNALS__ != null || w.__TAURI__ != null) return true;
  return window.location.protocol === "tauri:" || window.location.hostname === "tauri.localhost";
}

export const isDesktopRuntime = detectDesktopRuntime();
export const desktopApiBase = isDesktopRuntime ? "http://127.0.0.1:8000" : "";

export async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (options?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const target = url.startsWith("http") ? url : `${desktopApiBase}${url}`;
  const response = await fetch(target, options ? { ...options, headers } : undefined);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: JsonValue; message?: string };
      if (typeof payload.message === "string" && payload.message.trim()) {
        detail = payload.message;
      } else if (payload.detail !== undefined) {
        detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
      }
    } catch {
      // keep default status text
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}
