import type { JsonValue, ProductContext, ProductQuestion, ProductReaction, ProductSituation } from "./api";

export function record(value: unknown): Record<string, JsonValue> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, JsonValue> : {};
}

export function text(value: unknown, fallback = "—"): string {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

// These are server-owned fallback sentences, not a general translation layer.
// Keep this allow-list deliberately small: model/user-authored text must remain
// unchanged so the product never invents a translation for an unknown value.
const SERVER_FALLBACK_COPY: Record<string, [string, string]> = {
  "The relevant deadline is close enough that the next step may affect the outcome.": ["相关截止时间已经临近，下一步可能会影响结果。", "The relevant deadline is close enough that the next step may affect the outcome."],
  "The timing has changed and is worth checking while the signal is still timely.": ["时间情况已经变化，趁信号仍然及时，现在值得检查。", "The timing has changed and is worth checking while the signal is still timely."],
  "A material change was recorded and the current understanding should be kept aligned.": ["已记录重要变化，需要及时对齐当前理解。", "A material change was recorded and the current understanding should be kept aligned."],
  "There is no new material signal that needs an interruption right now.": ["目前没有需要打扰你的新重要信号。", "There is no new material signal that needs an interruption right now."],
  "A relevant deadline is close enough to affect the next step.": ["相关截止时间已临近，可能影响下一步。", "A relevant deadline is close enough to affect the next step."],
  "A material change was recorded for this Situation.": ["这条 Situation 已记录重要变化。", "A material change was recorded for this Situation."],
  "An unresolved unknown may affect the current understanding.": ["一项尚未解决的未知可能影响当前理解。", "An unresolved unknown may affect the current understanding."],
  "A fresh observation boundary has arrived.": ["新的观察节点已经到来。", "A fresh observation boundary has arrived."],
  "The timing crossed a useful threshold.": ["时间已经到了值得关注的节点。", "The timing crossed a useful threshold."],
  "A relevant signal was observed.": ["观察到一项相关信号。", "A relevant signal was observed."],
  "This is the next useful point to review.": ["这是现在值得重新查看的节点。", "This is the next useful point to review."],
  "This is the open question currently blocking the next step.": ["这是目前阻塞下一步的待回答问题。", "This is the open question currently blocking the next step."],
  "The relevant deadline is close enough that this open question now affects the outcome.": ["相关截止时间已经临近，这个待回答问题现在会影响结果。", "The relevant deadline is close enough that this open question now affects the outcome."],
  "The relevant deadline is approaching while this question is still open.": ["相关截止时间正在临近，而这个问题仍未回答。", "The relevant deadline is approaching while this question is still open."],
  "An authorised source can answer this open question without interrupting you.": ["已授权的来源可以回答这个问题，不需要打扰你。", "An authorised source can answer this open question without interrupting you."],
  "This question stays open until a better observation point arrives.": ["这个问题会保持开放，直到出现更合适的观察节点。", "This question stays open until a better observation point arrives."],
};

/** Translate only an exact, known server fallback; preserve all other text. */
export function serverFallbackText(value: unknown, en: boolean, fallback: string): string {
  const raw = text(value, "");
  if (!raw) return fallback;
  return SERVER_FALLBACK_COPY[raw]?.[en ? 1 : 0] ?? raw;
}

export function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function records(value: unknown, limit = 20): Array<Record<string, JsonValue>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, JsonValue> => Boolean(item) && typeof item === "object" && !Array.isArray(item)).slice(0, limit);
}

export type UniqueRecordMode = "situation" | "change" | "event" | "timeline" | "deadline" | "unknown" | "waiting" | "question" | "reaction";
export type UniqueRecordOptions = { limit?: number; mode: UniqueRecordMode };

/**
 * Product projections can contain the same observation more than once while
 * a background tick and a user turn converge. The identity is intentionally
 * selected by record semantics: a Situation summary is keyed by Situation,
 * while an event/change keeps its event/observation identity and revision.
 * This avoids hiding two real changes that happen in the same Situation.
 */
export function uniqueRecords(value: unknown, options: UniqueRecordOptions): Array<Record<string, JsonValue>> {
  if (!Array.isArray(value)) return [];
  const limit = options.limit ?? 20;
  const mode = options.mode;
  const rows = value.filter((item): item is Record<string, JsonValue> => Boolean(item) && typeof item === "object" && !Array.isArray(item));
  const candidate = (item: Record<string, JsonValue>, keys: string[]): { key: string; value: string } | null => {
    for (const key of keys) {
      const value = text(item[key], "");
      if (value) return { key, value };
    }
    return null;
  };
  const revision = (item: Record<string, JsonValue>): string => text(item.revision ?? item.event_revision ?? item.observation_revision, "");
  const withRevision = (selected: { key: string; value: string }, item: Record<string, JsonValue>, includeRevision: boolean): string => {
    const suffix = includeRevision && revision(item) ? `:revision:${revision(item)}` : "";
    return `${selected.key}:${selected.value}${suffix}`;
  };
  const identity = (item: Record<string, JsonValue>): string => {
    if (mode === "situation") {
      const selected = candidate(item, ["situation_id"]);
      if (selected) return withRevision(selected, item, false);
    } else if (mode === "change" || mode === "event" || mode === "timeline") {
      const selected = candidate(item, ["event_id", "observation_id", "record_id"]);
      if (selected) return withRevision(selected, item, true);
    } else if (mode === "waiting" || mode === "question") {
      const selected = candidate(item, ["need_id", "token"]);
      if (selected) return withRevision(selected, item, false);
    } else if (mode === "reaction") {
      const selected = candidate(item, ["reaction_id", "token"]);
      if (selected) return withRevision(selected, item, false);
    } else if (mode === "deadline") {
      const selected = candidate(item, ["deadline_id", "event_id", "record_id", "token"]);
      if (selected) return withRevision(selected, item, true);
      const deadlineParts = [
        text(item.situation_id, ""),
        text(item.deadline_at, ""),
        text(item.title ?? item.label ?? item.summary, ""),
      ].filter(Boolean);
      if (deadlineParts.length) return `deadline:${deadlineParts.join("|")}`;
    }
    const fallbackKeys = mode === "change" || mode === "event" || mode === "timeline"
      ? ["what_changed", "material_change", "statement", "title", "label", "summary", "message"]
      : mode === "waiting" ? ["what_changed", "message", "statement", "question", "blocked_judgment"]
        : mode === "question" ? ["question", "blocked_judgment", "statement"]
          : mode === "reaction" ? ["what_changed", "recommendation", "message", "summary"]
            : mode === "deadline" ? ["deadline_at", "title", "label", "summary"]
              : mode === "unknown" ? ["unknown_id", "need_id", "token", "statement", "message"]
                : ["title", "label", "statement", "summary", "message"];
    const selected = candidate(item, fallbackKeys);
    if (!selected) return "";
    const timestamp = mode === "change" || mode === "event" || mode === "timeline" ? text(item.changed_at ?? item.observed_at ?? item.occurred_at ?? item.created_at, "") : "";
    return `${withRevision(selected, item, mode === "change" || mode === "event" || mode === "timeline")}${timestamp ? `:at:${timestamp}` : ""}`;
  };
  const freshness = (item: Record<string, JsonValue>): number => {
    for (const key of ["updated_at", "changed_at", "observed_at", "created_at", "occurred_at", "expires_at"]) {
      const timestamp = Date.parse(text(item[key], ""));
      if (Number.isFinite(timestamp)) return timestamp;
    }
    return 0;
  };
  const selected = new Map<string, { item: Record<string, JsonValue>; index: number; freshness: number }>();
  rows.forEach((item, index) => {
    const key = identity(item);
    if (!key) {
      selected.set(`anonymous:${index}`, { item, index, freshness: freshness(item) });
      return;
    }
    const current = selected.get(key);
    const next = { item, index, freshness: freshness(item) };
    if (!current || next.freshness >= current.freshness) selected.set(key, next);
  });
  return [...selected.values()].sort((left, right) => left.index - right.index).map(({ item }) => item).slice(0, limit);
}

export function strings(value: unknown, limit = 8): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => typeof item === "string" ? item.trim() : text(record(item).statement ?? record(item).label ?? record(item).title, "")).filter(Boolean).slice(0, limit);
}

export function situationId(value: ProductSituation | ProductQuestion | ProductReaction): string {
  return text(value.situation_id, "");
}

export function safeHref(value: unknown, prefix: string): string | null {
  const id = text(value, "");
  return id ? `${prefix}${encodeURIComponent(id)}` : null;
}

export function dateLabel(value: unknown, en: boolean): string {
  const raw = text(value, "");
  if (!raw) return en ? "No date set" : "尚未设置时间";
  const date = new Date(raw);
  if (Number.isNaN(date.valueOf())) return raw;
  return date.toLocaleString(en ? "en-US" : "zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

const STATUS_LABELS: Record<string, [string, string]> = {
  active: ["进行中", "Active"], observed: ["已观察", "Observed"], success: ["可用", "Available"],
  available: ["可用", "Available"], configured: ["已配置", "Configured"], connected: ["已连接", "Connected"],
  emerging: ["新出现", "Emerging"], "local-first": ["本地优先", "Local-first"], local_first: ["本地优先", "Local-first"],
  receiving: ["接收中", "Receiving"], processing_failed: ["处理失败", "Processing failed"],
  waiting_for_event: ["等待消息", "Waiting for message"], waiting_for_message: ["等待消息", "Waiting for message"],
  connecting: ["连接中", "Connecting"], not_ready: ["未就绪", "Not ready"],
  configured_not_running: ["已配置但未运行", "Configured, stopped"], gateway_up: ["网关已启动", "Gateway up"],
  needs_setup: ["需要设置", "Needs setup"], skipped: ["已跳过", "Skipped"], unconfigured: ["尚未配置", "Not configured"],
  validated: ["已验证", "Validated"], validation_pending: ["验证中", "Validation pending"],
  not_certified: ["未通过验证", "Not certified"], already_running: ["已在运行", "Already running"],
  empty: ["暂无", "Empty"], waiting: ["等待中", "Waiting"], pending: ["待处理", "Pending"], open: ["待补充", "Open"],
  asked: ["已询问", "Asked"], answered: ["已回答", "Answered"], observing: ["观察中", "Observing"], resolved: ["已解决", "Resolved"],
  terminal: ["已结束", "Closed"], closed: ["已关闭", "Closed"], degraded: ["部分可用", "Degraded"], fail_closed: ["暂不可用", "Unavailable"],
  unavailable: ["暂不可用", "Unavailable"], not_configured: ["尚未配置", "Not configured"], unsupported: ["不支持", "Unsupported"],
  denied: ["已拒绝", "Denied"], error: ["错误", "Error"], timeout: ["已超时", "Timed out"], timed_out: ["已超时", "Timed out"],
  stale: ["已过期", "Stale"], fresh: ["新鲜", "Fresh"], expired: ["已过期", "Expired"], dismissed: ["已忽略", "Dismissed"],
  loading: ["加载中", "Loading"], ready: ["就绪", "Ready"], none: ["无", "None"], disabled: ["已停用", "Disabled"], unknown: ["未知", "Unknown"],
};

export function statusLabel(value: unknown, en: boolean): string {
  const raw = text(value, "unknown");
  const normalized = raw.toLowerCase();
  return STATUS_LABELS[normalized]?.[en ? 1 : 0] ?? raw;
}

const CATEGORY_LABELS: Record<string, [string, string]> = {
  general: ["一般", "General"], personal: ["个人", "Personal"], work: ["工作", "Work"],
  education: ["教育", "Education"], health: ["健康", "Health"], travel: ["旅行", "Travel"],
  logistics: ["物流", "Logistics"], finance: ["财务", "Finance"], other: ["其他", "Other"],
};

/** Translate only the bounded Situation category vocabulary; preserve unknown values. */
export function categoryLabel(value: unknown, en: boolean): string {
  const raw = text(value, "general");
  return CATEGORY_LABELS[raw.toLowerCase()]?.[en ? 1 : 0] ?? raw;
}

export type ProductContextReadiness = {
  status: string;
  loading: boolean;
  ready: boolean;
  internalScope: { user_id: string; session_id: string } | null;
};

/** A product read is legal only after Context has named one exact scope. */
export function productContextReadiness(context: ProductContext | null | undefined, scope: { userId: string; sessionId: string }): ProductContextReadiness {
  const status = text(context?.status, "loading").toLowerCase();
  const raw = context?.internal_read_scope;
  const internalScope = raw && typeof raw.user_id === "string" && raw.user_id.trim() && typeof raw.session_id === "string" && raw.session_id.trim()
    ? { user_id: raw.user_id, session_id: raw.session_id }
    : null;
  const ready = Boolean(internalScope && ["ready", "empty"].includes(status) && internalScope.user_id === scope.userId && internalScope.session_id === scope.sessionId);
  return { status, loading: !context || status === "loading", ready, internalScope };
}

const PROGRESS_LABELS: Record<string, [string, string]> = {
  unknown: ["未知", "Unknown"], not_started: ["未开始", "Not started"], in_progress: ["进行中", "In progress"],
  blocked: ["受阻", "Blocked"], waiting: ["等待中", "Waiting"], completed: ["已完成", "Completed"],
};

export function progressLabel(value: unknown, en: boolean): string {
  const raw = text(value, "unknown");
  return PROGRESS_LABELS[raw.toLowerCase()]?.[en ? 1 : 0] ?? raw;
}

const EPISTEMIC_LABELS: Record<string, [string, string, string]> = {
  reported: ["事实", "Fact", "fact"], observed: ["已观察", "Observed", "observed"],
  inferred: ["推断", "Inference", "inference"], verified: ["已验证", "Verified", "verified"],
  unknown: ["未知", "Unknown", "unknown"],
};

export function epistemicLabel(value: unknown, en: boolean): { label: string; tone: string } | null {
  const raw = text(value, "").toLowerCase();
  const selected = EPISTEMIC_LABELS[raw];
  return selected ? { label: selected[en ? 1 : 0], tone: selected[2] } : null;
}

export type ProductViewSituation = ProductSituation;
export type ProductViewQuestion = ProductQuestion;
export type ProductViewReaction = ProductReaction;
