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
      const body = await response.text();
      const contentType = response.headers.get("content-type") ?? "";
      if (response.status === 403 && !contentType.includes("json") && body.trim().toLowerCase() === "forbidden") {
        detail = "前端预览服务器拒绝了 API 请求，请确认 Vite preview/dev 代理已启动。";
      } else {
        const payload = JSON.parse(body) as { detail?: JsonValue; message?: string };
        if (typeof payload.message === "string" && payload.message.trim()) {
          detail = payload.message;
        } else if (payload.detail !== undefined) {
          detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
        }
      }
    } catch {
      // keep default status text
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export type MessageResult = {
  event_id?: string;
  route?: string;
  status?: string;
  response?: string;
  risk_level?: string;
  artifacts?: Record<string, JsonValue>;
  [key: string]: JsonValue | undefined;
};

export type CollectionPayload = {
  status?: string;
  count?: number;
  items?: JsonValue[];
  [key: string]: JsonValue | undefined;
};

export type ProductScope = { user_id: string; session_id: string };
export type ProductInputScope = { channel: string; user_id: string; session_id: string };
export type ProductContext = {
  schema_version?: string;
  status?: string;
  reason?: string | null;
  goal?: Record<string, JsonValue> | null;
  internal_read_scope?: ProductScope | null;
  external_input_scope?: ProductInputScope | null;
  freshness?: string;
  authority?: Record<string, JsonValue>;
  [key: string]: JsonValue | undefined;
};

export type ProductToday = {
  schema_version?: string;
  status?: string;
  reason?: string;
  scope?: ProductScope;
  goal?: Record<string, JsonValue> | null;
  situations?: JsonValue[];
  attention?: JsonValue[];
  suggestions?: JsonValue[];
  questions?: Record<string, JsonValue>;
  waiting?: JsonValue[];
  freshness?: Record<string, JsonValue>;
  [key: string]: JsonValue | undefined;
};

export type ProductMatters = {
  schema_version?: string;
  status?: string;
  scope?: ProductScope;
  goal?: Record<string, JsonValue> | null;
  sections?: Record<string, JsonValue>;
  freshness?: Record<string, JsonValue>;
  [key: string]: JsonValue | undefined;
};

export type ProductStatus = {
  schema_version?: string;
  status?: string;
  product_scope?: Record<string, JsonValue>;
  runtime?: Record<string, JsonValue>;
  evidence?: Record<string, JsonValue>;
  authority?: Record<string, JsonValue>;
  [key: string]: JsonValue | undefined;
};

export async function getProductContext(options: { userId?: string; externalSessionId?: string; signal?: AbortSignal } = {}): Promise<ProductContext> {
  const params = new URLSearchParams();
  if (options.userId) params.set("user_id", options.userId);
  if (options.externalSessionId) params.set("session_id", options.externalSessionId);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return fetchJson<ProductContext>(`/product/context${suffix}`, { signal: options.signal });
}

export async function getProductToday(scope: ProductScope): Promise<ProductToday> {
  const query = `user_id=${encodeURIComponent(scope.user_id)}&session_id=${encodeURIComponent(scope.session_id)}`;
  return fetchJson<ProductToday>(`/product/today?${query}`);
}

export async function getProductMatters(scope: ProductScope): Promise<ProductMatters> {
  const query = `user_id=${encodeURIComponent(scope.user_id)}&session_id=${encodeURIComponent(scope.session_id)}`;
  return fetchJson<ProductMatters>(`/product/matters?${query}`);
}

export async function getProductStatus(): Promise<ProductStatus> {
  return fetchJson<ProductStatus>("/product/status");
}

export async function sendMessage(text: string, userId: string, sessionId: string, messageId?: string, channel = "api"): Promise<MessageResult> {
  return fetchJson<MessageResult>("/events/message", {
    method: "POST",
    body: JSON.stringify({ text, channel, user_id: userId, session_id: sessionId, ...(messageId ? { message_id: messageId } : {}) })
  });
}

export type MessageStreamEvent = {
  schema_version?: string;
  seq?: number;
  type: "accepted" | "phase" | "agent_status" | "review_required" | "message" | "completed" | "failed" | "heartbeat" | "cancelled" | string;
  payload?: unknown;
  phase?: string;
  status?: string;
};

function streamTarget(): string {
  return `${desktopApiBase}/events/message/stream`;
}

/**
 * Consume the server's real lifecycle stream. The stream deliberately carries
 * phases and the verified final message, not simulated token deltas.
 */
export async function streamMessage(
  text: string,
  userId: string,
  sessionId: string,
  messageId: string,
  onEvent?: (event: MessageStreamEvent) => void,
  channel = "api",
): Promise<MessageResult> {
  const response = await fetch(streamTarget(), {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ text, channel, user_id: userId, session_id: sessionId, message_id: messageId }),
  });
  // A desktop sidecar can briefly run an older bundle while it restarts. Keep
  // the conversation usable, but label this as a synchronous compatibility
  // path rather than pretending it is a token stream.
  if (response.status === 404 || response.status === 405) {
    onEvent?.({ type: "accepted", phase: "accepted", status: "compatibility" });
    onEvent?.({ type: "phase", phase: "processing", status: "synchronous" });
    const result = await sendMessage(text, userId, sessionId, messageId, channel);
    onEvent?.({ type: "message", payload: result });
    onEvent?.({ type: "completed", status: String(result.status ?? "completed") });
    return result;
  }
  if (response.status === 403) {
    const body = await response.text();
    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.includes("json") && body.trim().toLowerCase() === "forbidden") {
      onEvent?.({ type: "accepted", phase: "accepted", status: "compatibility" });
      onEvent?.({ type: "phase", phase: "processing", status: "synchronous" });
      const result = await sendMessage(text, userId, sessionId, messageId, channel);
      onEvent?.({ type: "message", payload: result });
      onEvent?.({ type: "completed", status: String(result.status ?? "completed") });
      return result;
    }
    throw new Error("请求被后端治理策略拒绝（403），不是前端流式显示问题。");
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = JSON.parse(await response.text()) as { detail?: JsonValue; message?: string };
      if (typeof payload.message === "string" && payload.message.trim()) detail = payload.message;
      else if (payload.detail !== undefined) detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch { /* retain HTTP status */ }
    throw new Error(detail);
  }
  if (!response.body) throw new Error("Veyra stream is unavailable in this browser");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: MessageResult | null = null;
  const consume = (block: string) => {
    const lines = block.split(/\r?\n/);
    let eventName = "message";
    const data: string[] = [];
    for (const line of lines) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim() || eventName;
      if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return;
    let parsed: MessageStreamEvent;
    try {
      const value = JSON.parse(data.join("\n")) as MessageStreamEvent;
      parsed = { ...value, type: value.type || eventName };
    } catch {
      parsed = { type: eventName, payload: data.join("\n") };
    }
    onEvent?.(parsed);
    if (parsed.type === "message" && parsed.payload && typeof parsed.payload === "object" && !Array.isArray(parsed.payload)) {
      finalResult = parsed.payload as MessageResult;
    }
    if (parsed.type === "failed") {
      const payload = parsed.payload;
      const message = payload && typeof payload === "object" && !Array.isArray(payload) ? String((payload as Record<string, JsonValue>).message ?? "Veyra could not finish this request") : "Veyra could not finish this request";
      throw new Error(message);
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() ?? "";
    for (const block of blocks) consume(block);
    if (done) break;
  }
  if (buffer.trim()) consume(buffer);
  if (!finalResult) throw new Error("Veyra stream ended before a verified response");
  return finalResult;
}

export async function getSetupStatus() {
  return fetchJson<Record<string, JsonValue>>("/setup/status");
}

export async function fetchMatterCollections(scope: { userId: string; sessionId: string }) {
  return getProductMatters({ user_id: scope.userId, session_id: scope.sessionId });
}
