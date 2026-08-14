import type { HTMLAttributes, ReactNode } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, LoaderCircle, RefreshCw } from "lucide-react";

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export type OwnerScope = { userId: string; sessionId: string };
export type Language = "system" | "zh" | "en";

export type LocalHistoryRecord = {
  id: string;
  conversationId?: string;
  text: string;
  result?: { status?: string; response?: string };
  createdAt: string;
  ownerId?: string;
  sessionId?: string;
  status?: "streaming" | "completed" | "failed";
  error?: string;
};

/** One parser for legacy and current browser history; never retains artifacts. */
export function sanitizeHistoryRecord(value: unknown): LocalHistoryRecord | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  const id = typeof item.id === "string" ? item.id.trim() : "";
  const text = typeof item.text === "string" ? item.text : "";
  const createdAt = typeof item.createdAt === "string" ? item.createdAt : "";
  if (!id || !text || !createdAt) return null;
  const rawResult = item.result && typeof item.result === "object" && !Array.isArray(item.result) ? item.result as Record<string, unknown> : null;
  const result = rawResult ? {
    status: typeof rawResult.status === "string" ? rawResult.status : undefined,
    response: typeof rawResult.response === "string" ? rawResult.response : undefined,
  } : undefined;
  return {
    id,
    conversationId: typeof item.conversationId === "string" ? item.conversationId : undefined,
    text,
    result: result && (result.status || result.response) ? result : undefined,
    createdAt,
    ownerId: typeof item.ownerId === "string" ? item.ownerId : undefined,
    sessionId: typeof item.sessionId === "string" ? item.sessionId : undefined,
    status: item.status === "completed" || item.status === "failed" || item.status === "streaming" ? item.status : undefined,
    error: typeof item.error === "string" ? item.error : undefined,
  };
}

export function sanitizeHistory(value: unknown): LocalHistoryRecord[] {
  if (!Array.isArray(value)) return [];
  return value.map(sanitizeHistoryRecord).filter((item): item is LocalHistoryRecord => item !== null);
}

export function isEnglish(language: Language): boolean {
  if (language === "en") return true;
  if (language === "zh") return false;
  return typeof navigator !== "undefined" && !navigator.language.toLowerCase().startsWith("zh");
}

export function asRecord(value: unknown): Record<string, JsonValue> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, JsonValue>) : {};
}

export function asItems(value: unknown): Array<Record<string, JsonValue>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, JsonValue> => Boolean(item) && typeof item === "object" && !Array.isArray(item));
}

export function displayValue(value: unknown, fallback = "—"): string {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return fallback;
  }
}

export function statusTone(value: unknown): "good" | "warn" | "bad" | "neutral" {
  const normalized = String(value ?? "").toLowerCase();
  if (["error", "failed", "blocked", "denied", "unavailable", "critical", "fail_closed", "unhealthy", "disconnected", "not_configured"].some((item) => normalized === item || normalized.includes(`${item}_`) || normalized.includes(`_${item}`))) return "bad";
  if (["degraded", "stale", "loading", "pending", "unknown", "waiting"].some((item) => normalized === item || normalized.includes(`${item}_`) || normalized.includes(`_${item}`))) return "warn";
  if (["ready", "active", "available", "connected", "configured", "success", "passed", "fresh", "healthy"].some((item) => normalized === item || normalized.includes(`${item}_`) || normalized.includes(`_${item}`))) return "good";
  return "neutral";
}

export function StatusBadge({ value, label }: { value: unknown; label?: string }) {
  const text = label ?? String(value ?? "unknown");
  return <span className={`statusBadge ${statusTone(value)}`}><span className="statusDot" />{text}</span>;
}

export function Freshness({ at, loading, error, language = "zh" }: { at?: string | null; loading?: boolean; error?: string | null; language?: Language }) {
  const en = isEnglish(language);
  if (loading) return <span className="freshness"><LoaderCircle size={13} className="spinIcon" />{en ? "Syncing" : "同步中"}</span>;
  if (error) return <span className="freshness bad"><AlertTriangle size={13} />{en ? "Unavailable" : "不可用"}</span>;
  if (!at) return <span className="freshness warn">{en ? "Not read" : "尚未读取"}</span>;
  const date = new Date(at);
  const label = Number.isNaN(date.valueOf()) ? "已读取" : `刚刚读取 · ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  return <span className="freshness"><CheckCircle2 size={13} />{en ? `Read just now · ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : label}</span>;
}

export function LoadingBlock({ label = "读取中…" }: { label?: string }) {
  return <div className="loadingBlock"><LoaderCircle size={17} className="spinIcon" /><span>{label}</span></div>;
}

export function ErrorBlock({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return <div className="errorBlock"><AlertTriangle size={17} /><span>{message}</span>{onRetry ? <button className="textButton" onClick={onRetry}><RefreshCw size={14} />重试</button> : null}</div>;
}

export function Surface({ children, className = "", ...props }: { children: ReactNode; className?: string } & HTMLAttributes<HTMLElement>) {
  return <section className={`surface ${className}`} {...props}>{children}</section>;
}

export function Disclosure({ title, children, open = false }: { title: string; children: ReactNode; open?: boolean }) {
  return <details className="disclosure" open={open}><summary><ChevronDown size={15} /><span>{title}</span></summary><div className="disclosureBody">{children}</div></details>;
}

export function formatTime(value: unknown): string {
  const date = new Date(String(value ?? ""));
  if (Number.isNaN(date.valueOf())) return "";
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
