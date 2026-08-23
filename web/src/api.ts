import { safeText } from "./shared";

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export function detectDesktopRuntime(): boolean {
  if (typeof window === "undefined") return false;
  const w = window as Window & { __TAURI_INTERNALS__?: unknown; __TAURI__?: unknown };
  if (w.__TAURI_INTERNALS__ != null || w.__TAURI__ != null) return true;
  return window.location.protocol === "tauri:" || window.location.hostname === "tauri.localhost";
}

export const isDesktopRuntime = detectDesktopRuntime();
export const desktopApiBase = isDesktopRuntime ? "http://127.0.0.1:8000" : "";

export class HttpError extends Error {
  readonly status: number;
  readonly statusText: string;

  constructor(status: number, statusText: string, message: string) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.statusText = statusText;
  }
}

export async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (options?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const target = url.startsWith("http") ? url : `${desktopApiBase}${url}`;
  const response = await fetch(target, { ...(options ?? {}), headers, cache: options?.cache ?? "no-store" });
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
        } else if (typeof payload.detail === "string" && payload.detail.trim()) {
          // FastAPI's generic 404 is the one response that does not identify
          // a product resource. Treat it as a stale frontend/backend pair,
          // while preserving resource-specific 404 details for the caller.
          detail = response.status === 404 && contentType.includes("json") && payload.detail === "Not Found"
            ? "Runtime version mismatch: the frontend and running Veyra backend are different revisions. / 运行版本不匹配：当前前端与正在运行的 Veyra 后端不是同一版本。"
            : payload.detail;
        } else if (payload.detail !== undefined) {
          // Product surfaces must never render raw server objects, paths, or
          // opaque tokens. Keep the status useful without leaking the body.
          detail = `${response.status} ${response.statusText || "Request failed"}`;
        }
      }
    } catch {
      // keep default status text
    }
    throw new HttpError(response.status, response.statusText, detail);
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

export type ProductSituation = Record<string, JsonValue>;
export type ProductQuestion = Record<string, JsonValue>;
export type ProductReaction = Record<string, JsonValue>;
export type ProductSources = {
  schema_version?: string;
  status?: string;
  scope?: ProductScope;
  items?: Record<string, Record<string, JsonValue>>;
  authority?: Record<string, JsonValue>;
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

export async function getProductToday(scope: ProductScope, options: { firstMeeting?: boolean; signal?: AbortSignal } = {}): Promise<ProductToday> {
  const query = `user_id=${encodeURIComponent(scope.user_id)}&session_id=${encodeURIComponent(scope.session_id)}`;
  const firstMeeting = options.firstMeeting === false ? "" : "&first_meeting=true";
  return fetchJson<ProductToday>(`/product/today?${query}${firstMeeting}`, { signal: options.signal });
}

function scopedQuery(scope: ProductScope, extra: Record<string, string | number | undefined> = {}): string {
  const params = new URLSearchParams({ user_id: scope.user_id, session_id: scope.session_id });
  for (const [key, value] of Object.entries(extra)) if (value !== undefined && value !== "") params.set(key, String(value));
  return params.toString();
}

export async function getProductSituations(scope: ProductScope, options: { signal?: AbortSignal } = {}): Promise<{ status?: string; items?: ProductSituation[]; recent_terminal?: ProductSituation[]; count?: number; [key: string]: JsonValue | undefined }> {
  return fetchJson(`/product/situations?${scopedQuery(scope)}`, { signal: options.signal });
}

export async function getProductSituation(situationId: string, scope: ProductScope, options: { signal?: AbortSignal } = {}): Promise<{ status?: string; situation?: ProductSituation | null; questions?: ProductQuestion[]; reactions?: ProductReaction[]; [key: string]: JsonValue | undefined }> {
  return fetchJson(`/product/situations/${encodeURIComponent(situationId)}?${scopedQuery(scope)}`, { signal: options.signal });
}

export type SituationCommand = "correct" | "resolve" | "reopen" | "quiet";
export async function commandProductSituation(situationId: string, scope: ProductScope, body: { command: SituationCommand; expected_revision: number; patch?: Record<string, JsonValue>; reason?: string; event_id?: string }): Promise<Record<string, JsonValue>> {
  return fetchJson(`/product/situations/${encodeURIComponent(situationId)}/command?${scopedQuery(scope)}`, { method: "POST", body: JSON.stringify(body) });
}

export async function getProductQuestions(scope: ProductScope, options: { signal?: AbortSignal } = {}): Promise<{ status?: string; items?: ProductQuestion[]; [key: string]: JsonValue | undefined }> {
  return fetchJson(`/product/questions?${scopedQuery(scope)}`, { signal: options.signal });
}

export async function answerProductQuestion(needId: string, scope: ProductScope, body: { answer: string; expected_generation: number; expected_revision?: number; event_id?: string }): Promise<Record<string, JsonValue>> {
  return fetchJson(`/product/questions/${encodeURIComponent(needId)}/answer?${scopedQuery(scope)}`, { method: "POST", body: JSON.stringify(body) });
}

export async function deferProductQuestion(needId: string, scope: ProductScope, body: { expected_generation: number; event_id?: string }): Promise<Record<string, JsonValue>> {
  return fetchJson(`/product/questions/${encodeURIComponent(needId)}/defer?${scopedQuery(scope)}`, { method: "POST", body: JSON.stringify(body) });
}

export async function dismissProductQuestion(needId: string, scope: ProductScope, body: { expected_generation: number; event_id?: string }): Promise<Record<string, JsonValue>> {
  return fetchJson(`/product/questions/${encodeURIComponent(needId)}/dismiss?${scopedQuery(scope)}`, { method: "POST", body: JSON.stringify(body) });
}

export async function getProductReactions(scope: ProductScope, options: { situationId?: string; situationRevision?: number; signal?: AbortSignal } = {}): Promise<{ status?: string; items?: ProductReaction[]; silent_count?: number; [key: string]: JsonValue | undefined }> {
  return fetchJson(`/product/reactions?${scopedQuery(scope, { situation_id: options.situationId, situation_revision: options.situationRevision })}`, { signal: options.signal });
}

export async function getProductSuggestions(scope: ProductScope, options: { situationId?: string; situationRevision?: number; signal?: AbortSignal } = {}): Promise<{ status?: string; items?: ProductReaction[]; [key: string]: JsonValue | undefined }> {
  return fetchJson(`/product/suggestions?${scopedQuery(scope, { situation_id: options.situationId, situation_revision: options.situationRevision })}`, { signal: options.signal });
}

export type ReactionFeedbackLabel = "useful" | "not_useful" | "ignore" | "resolved" | "too_early" | "too_late" | "too_frequent" | "remind_before" | "remind_offset";
export async function feedbackProductReaction(reactionId: string, scope: ProductScope, body: { label: ReactionFeedbackLabel; situation_revision: number; remind_before_seconds?: number; evidence_refs?: string[] }): Promise<Record<string, JsonValue>> {
  return fetchJson(`/product/reactions/${encodeURIComponent(reactionId)}/feedback?${scopedQuery(scope)}`, { method: "POST", body: JSON.stringify(body) });
}

export async function getProductSources(scope: ProductScope, options: { signal?: AbortSignal } = {}): Promise<ProductSources> {
  return fetchJson(`/product/sources?${scopedQuery(scope)}`, { signal: options.signal });
}

// The current backend intentionally exposes status before provider authority.
// Keep these seams explicit so a missing consent route is shown as unsupported.
export async function consentProductSource(source: string, scope: ProductScope, body: { expected_generation?: number; purpose?: string; consent_id?: string; expires_at?: string } = {}): Promise<Record<string, JsonValue>> {
  return fetchJson(`/product/sources/${encodeURIComponent(source)}/consent?${scopedQuery(scope)}`, { method: "POST", body: JSON.stringify({ expected_generation: body.expected_generation ?? 0, purpose: body.purpose ?? "Veyra V1 read-only Living Context", ...(body.consent_id ? { consent_id: body.consent_id } : {}), ...(body.expires_at ? { expires_at: body.expires_at } : {}) }) });
}

export async function revokeProductSource(source: string, scope: ProductScope, body: { expected_generation: number; consent_id?: string }): Promise<Record<string, JsonValue>> {
  return fetchJson(`/product/sources/${encodeURIComponent(source)}/consent?${scopedQuery(scope)}`, { method: "DELETE", body: JSON.stringify({ expected_generation: body.expected_generation, ...(body.consent_id ? { consent_id: body.consent_id } : {}) }) });
}

export async function getProductMatters(scope: ProductScope): Promise<ProductMatters> {
  const query = `user_id=${encodeURIComponent(scope.user_id)}&session_id=${encodeURIComponent(scope.session_id)}`;
  return fetchJson<ProductMatters>(`/product/matters?${query}`);
}

export async function getProductStatus(): Promise<ProductStatus> {
  return fetchJson<ProductStatus>("/product/status");
}

/**
 * Product conversations are server-owned.  The response parsers deliberately
 * accept the small envelope variations used by the local runtime while
 * keeping the stable identifiers explicit at the UI boundary.
 */
export type ProductConversationSummary = {
  conversation_id?: string;
  id?: string;
  title?: string;
  preview?: string;
  last_message?: string;
  message_count?: number;
  created_at?: string;
  updated_at?: string;
  user_id?: string;
  session_id?: string;
  [key: string]: JsonValue | undefined;
};

export type ProductConversationCategory = "general" | "personal" | "work" | "education" | "health" | "travel" | "logistics" | "finance" | "other";
export type ProductConversationEpistemicStatus = "hypothesis" | "reported" | "inferred" | "observed" | "observation" | "unknown";
export type ProductConversationFactInference = {
  facts: string[];
  inferences: string[];
};
export type ProductConversationFeedbackLabel = "useful" | "not_useful" | "too_early" | "too_frequent" | "resolved";

/**
 * Server-owned metadata for a Product Conversation message.
 *
 * This is deliberately a small allow-list.  The UI can use the stable
 * reaction/situation identity and epistemic labels, but it never needs to
 * retain or display the raw server payload or feedback token.
 */
export type ProductConversationMetadata = {
  reaction_id?: string;
  situation_id?: string;
  situation_revision?: number;
  category?: ProductConversationCategory;
  fact_vs_inference?: ProductConversationFactInference;
  feedback_available?: boolean;
  feedback_label?: ProductConversationFeedbackLabel;
  feedback_at?: string;
  epistemic_status?: ProductConversationEpistemicStatus;
  record_only?: boolean;
};

export type ProductConversationMessage = {
  message_id?: string;
  id?: string;
  conversation_id?: string;
  role?: string;
  kind?: string;
  text?: string;
  content?: string;
  response?: string;
  source?: string;
  created_at?: string;
  updated_at?: string;
  user_id?: string;
  session_id?: string;
  metadata?: ProductConversationMetadata;
  result?: JsonValue;
  payload?: JsonValue;
  [key: string]: JsonValue | undefined;
};

export type ProductConversationCollection = {
  status?: string;
  items?: ProductConversationSummary[];
  conversations?: ProductConversationSummary[];
  count?: number;
  [key: string]: unknown;
};

export type ProductConversationDetail = {
  status?: string;
  conversation?: ProductConversationSummary | null;
  messages?: ProductConversationMessage[];
  [key: string]: unknown;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function productConversationRecord(value: unknown): ProductConversationSummary | null {
  return isRecord(value) ? value as ProductConversationSummary : null;
}

const PRODUCT_CONVERSATION_CATEGORIES = new Set<ProductConversationCategory>([
  "general", "personal", "work", "education", "health", "travel", "logistics", "finance", "other",
]);
const PRODUCT_CONVERSATION_EPISTEMIC_STATUS = new Set<ProductConversationEpistemicStatus>([
  "hypothesis", "reported", "inferred", "observed", "observation", "unknown",
]);
const PRODUCT_CONVERSATION_FEEDBACK_LABELS = new Set<ProductConversationFeedbackLabel>([
  "useful", "not_useful", "too_early", "too_frequent", "resolved",
]);

function boundedMetadataText(value: unknown, maxLength = 240): string | undefined {
  if (typeof value !== "string") return undefined;
  const selected = value.trim().slice(0, maxLength);
  return selected || undefined;
}

function boundedMetadataRevision(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isInteger(value) || !Number.isSafeInteger(value) || value < 1) return undefined;
  return value;
}

function boundedFactInference(value: unknown): ProductConversationFactInference | undefined {
  if (!isRecord(value)) return undefined;
  const values = (candidate: unknown): string[] => Array.isArray(candidate)
    ? candidate.filter((item): item is string => typeof item === "string" && item.trim().length > 0).slice(0, 8).map((item) => item.trim().slice(0, 640))
    : [];
  return { facts: values(value.facts), inferences: values(value.inferences) };
}

export function normalizeProductConversationMetadata(value: unknown): ProductConversationMetadata | undefined {
  if (!isRecord(value)) return undefined;
  const metadata: ProductConversationMetadata = {};
  for (const key of ["reaction_id", "situation_id"] as const) {
    const selected = boundedMetadataText(value[key]);
    if (selected) metadata[key] = selected;
  }
  const revision = boundedMetadataRevision(value.situation_revision);
  if (revision !== undefined) metadata.situation_revision = revision;
  if (typeof value.category === "string" && PRODUCT_CONVERSATION_CATEGORIES.has(value.category as ProductConversationCategory)) {
    metadata.category = value.category as ProductConversationCategory;
  }
  const factInference = boundedFactInference(value.fact_vs_inference);
  if (factInference) metadata.fact_vs_inference = factInference;
  if (typeof value.feedback_available === "boolean") metadata.feedback_available = value.feedback_available;
  if (typeof value.feedback_label === "string" && PRODUCT_CONVERSATION_FEEDBACK_LABELS.has(value.feedback_label as ProductConversationFeedbackLabel)) {
    metadata.feedback_label = value.feedback_label as ProductConversationFeedbackLabel;
  }
  const feedbackAt = boundedMetadataText(value.feedback_at, 80);
  if (feedbackAt) metadata.feedback_at = feedbackAt;
  if (typeof value.record_only === "boolean") metadata.record_only = value.record_only;
  if (typeof value.epistemic_status === "string" && PRODUCT_CONVERSATION_EPISTEMIC_STATUS.has(value.epistemic_status as ProductConversationEpistemicStatus)) {
    metadata.epistemic_status = value.epistemic_status as ProductConversationEpistemicStatus;
  }
  return Object.keys(metadata).length ? metadata : undefined;
}

function productConversationMessageRecord(value: unknown): ProductConversationMessage | null {
  if (!isRecord(value)) return null;
  const message: ProductConversationMessage = {};
  for (const key of ["message_id", "id", "conversation_id", "role", "kind", "text", "content", "response", "source", "created_at", "updated_at", "user_id", "owner_id", "session_id"] as const) {
    const selected = boundedMetadataText(value[key], key === "text" || key === "content" || key === "response" ? 20000 : 640);
    if (selected) message[key] = selected;
  }
  if (isRecord(value.result) || Array.isArray(value.result)) message.result = value.result as JsonValue;
  if (isRecord(value.payload) || Array.isArray(value.payload)) message.payload = value.payload as JsonValue;
  const nested = normalizeProductConversationMetadata(value.metadata);
  const direct = normalizeProductConversationMetadata(value);
  const metadata = nested || direct ? { ...(nested ?? {}), ...(direct ?? {}) } : undefined;
  if (metadata) message.metadata = metadata;
  return message;
}

export function productConversationId(value: unknown): string {
  if (!isRecord(value)) return "";
  const id = value.conversation_id ?? value.id ?? value.conversationId;
  return typeof id === "string" ? id.trim() : "";
}

export function productConversationSummaries(payload: unknown): ProductConversationSummary[] {
  if (Array.isArray(payload)) return payload.map(productConversationRecord).filter((item): item is ProductConversationSummary => item !== null);
  if (!isRecord(payload)) return [];
  const rows = Array.isArray(payload.items) ? payload.items : Array.isArray(payload.conversations) ? payload.conversations : [];
  return rows.map(productConversationRecord).filter((item): item is ProductConversationSummary => item !== null);
}

export function productConversationMessages(payload: unknown): ProductConversationMessage[] {
  if (!isRecord(payload) || !Array.isArray(payload.messages)) return [];
  return payload.messages.map(productConversationMessageRecord).filter((item): item is ProductConversationMessage => item !== null);
}

function productConversationQuery(scope: ProductScope): string {
  return scopedQuery(scope);
}

export async function getProductConversations(scope: ProductScope, options: { signal?: AbortSignal } = {}): Promise<ProductConversationCollection> {
  const payload = await fetchJson<unknown>(`/product/conversations?${productConversationQuery(scope)}`, { signal: options.signal });
  return { ...(isRecord(payload) ? payload as ProductConversationCollection : {}), items: productConversationSummaries(payload) };
}

export async function createProductConversation(scope: ProductScope, options: { title?: string; signal?: AbortSignal } = {}): Promise<ProductConversationDetail | ProductConversationSummary> {
  const body = options.title?.trim() ? { title: options.title.trim() } : {};
  return fetchJson<ProductConversationDetail | ProductConversationSummary>(`/product/conversations?${productConversationQuery(scope)}`, {
    method: "POST",
    signal: options.signal,
    body: JSON.stringify(body),
  });
}

export async function getProductConversation(conversationId: string, scope: ProductScope, options: { signal?: AbortSignal } = {}): Promise<ProductConversationDetail> {
  if (!conversationId.trim()) throw new Error("Conversation id is required");
  const payload = await fetchJson<unknown>(`/product/conversations/${encodeURIComponent(conversationId)}?${productConversationQuery(scope)}`, { signal: options.signal });
  if (!isRecord(payload)) return { messages: [] };
  return payload as ProductConversationDetail;
}

export async function sendMessage(text: string, userId: string, sessionId: string, messageId?: string, channel = "api", conversationId?: string): Promise<MessageResult> {
  return fetchJson<MessageResult>("/events/message", {
    method: "POST",
    body: JSON.stringify({ text, channel, user_id: userId, session_id: sessionId, ...(messageId ? { message_id: messageId } : {}), ...(conversationId ? { conversation_id: conversationId } : {}) })
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

export function streamEventConversationId(event: MessageStreamEvent): string {
  const payload = event.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return "";
  return safeText((payload as Record<string, JsonValue>).conversation_id, "").trim();
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
  conversationId?: string,
): Promise<MessageResult> {
  const response = await fetch(streamTarget(), {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ text, channel, user_id: userId, session_id: sessionId, message_id: messageId, ...(conversationId ? { conversation_id: conversationId } : {}) }),
  });
  // A desktop sidecar can briefly run an older bundle while it restarts. Keep
  // the conversation usable, but label this as a synchronous compatibility
  // path rather than pretending it is a token stream.
  if (response.status === 404 || response.status === 405) {
    onEvent?.({ type: "accepted", phase: "accepted", status: "compatibility" });
    onEvent?.({ type: "phase", phase: "processing", status: "synchronous" });
    const result = await sendMessage(text, userId, sessionId, messageId, channel, conversationId);
    onEvent?.({ type: "message", payload: result });
    onEvent?.({ type: "completed", status: safeText(result.status, "completed") });
    return result;
  }
  if (response.status === 403) {
    const body = await response.text();
    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.includes("json") && body.trim().toLowerCase() === "forbidden") {
      onEvent?.({ type: "accepted", phase: "accepted", status: "compatibility" });
      onEvent?.({ type: "phase", phase: "processing", status: "synchronous" });
      const result = await sendMessage(text, userId, sessionId, messageId, channel, conversationId);
      onEvent?.({ type: "message", payload: result });
      onEvent?.({ type: "completed", status: safeText(result.status, "completed") });
      return result;
    }
    throw new Error("请求被后端治理策略拒绝（403），不是前端流式显示问题。");
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = JSON.parse(await response.text()) as { detail?: JsonValue; message?: string };
      if (typeof payload.message === "string" && payload.message.trim()) detail = payload.message;
      else if (typeof payload.detail === "string" && payload.detail.trim()) detail = payload.detail;
      else if (payload.detail !== undefined) detail = `${response.status} ${response.statusText || "Request failed"}`;
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
      const message = payload && typeof payload === "object" && !Array.isArray(payload)
        ? safeText((payload as Record<string, JsonValue>).message, "Veyra could not finish this request")
        : safeText(payload, "Veyra could not finish this request");
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
