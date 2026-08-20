import type { JsonValue, ProductQuestion, ProductReaction, ProductSituation } from "./api";

export function record(value: unknown): Record<string, JsonValue> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, JsonValue> : {};
}

export function text(value: unknown, fallback = "—"): string {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
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

export function statusLabel(value: unknown, en: boolean): string {
  const normalized = text(value, "unknown").toLowerCase();
  const labels: Record<string, [string, string]> = {
    active: ["进行中", "Active"], observed: ["已观察", "Observed"], success: ["可用", "Available"],
    empty: ["暂无", "Empty"], waiting: ["等待中", "Waiting"], open: ["待补充", "Open"],
    asked: ["已询问", "Asked"], observing: ["观察中", "Observing"], resolved: ["已解决", "Resolved"],
    terminal: ["已结束", "Closed"], degraded: ["部分可用", "Degraded"], fail_closed: ["暂不可用", "Unavailable"],
  };
  return labels[normalized]?.[en ? 1 : 0] ?? (en ? normalized : "未知");
}

export type ProductViewSituation = ProductSituation;
export type ProductViewQuestion = ProductQuestion;
export type ProductViewReaction = ProductReaction;
