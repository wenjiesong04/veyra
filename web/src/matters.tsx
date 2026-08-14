import { useEffect, useState } from "react";
import { ArrowUpRight, ClipboardList, Clock3, Eye, Flag, MessageSquareText, RefreshCw, Sparkles } from "lucide-react";
import { getProductMatters, type ProductContext, type ProductMatters } from "./api";
import { asItems, asRecord, ErrorBlock, Freshness, isEnglish, Language, LoadingBlock, OwnerScope, StatusBadge, Surface } from "./shared";

type MatterSection = { status?: string; items?: unknown[]; count?: number };

function section(data: ProductMatters, key: string): MatterSection {
  const sections = asRecord(data.sections);
  const value = asRecord(sections[key]);
  return { status: String(value.status ?? (Array.isArray(value.items) ? "success" : "empty")), items: asItems(value.items), count: Number(value.count ?? 0) };
}

function text(value: unknown, fallback: string): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

const META = [
  { key: "situations", icon: Sparkles, zh: "正在发生", en: "Situations", hintZh: "Veyra 正在持续理解的情境", hintEn: "Situations Veyra is keeping in view" },
  { key: "attention", icon: Eye, zh: "关注假设", en: "Attention", hintZh: "值得继续留意，但不是事实", hintEn: "Worth keeping in view, not a fact" },
  { key: "suggestions", icon: MessageSquareText, zh: "建议预览", en: "Suggestion previews", hintZh: "仅记录，没有发送", hintEn: "Recorded only; nothing was sent" },
  { key: "commitments", icon: ClipboardList, zh: "承诺事项", en: "Commitments", hintZh: "只显示当前 owner/session", hintEn: "Exact owner/session only" },
  { key: "questions", icon: Flag, zh: "问题", en: "Questions", hintZh: "可信生产记录才会出现", hintEn: "Shown only when a trusted record exists" },
  { key: "waiting", icon: Clock3, zh: "正在等待", en: "Waiting", hintZh: "Veyra 暂时等待更多信号", hintEn: "Veyra is waiting for a clearer signal" },
] as const;

function rowTitle(item: Record<string, unknown>, key: string, en: boolean): string {
  if (key === "suggestions") return text(item.message, en ? "A recorded suggestion" : "一条已记录的建议");
  if (key === "attention") return text(item.title, en ? "Attention hypothesis" : "关注假设");
  if (key === "waiting") return text(item.message, en ? "Waiting for more evidence" : "等待更多证据");
  if (key === "questions") return text(item.question, en ? "No question is ready" : "还没有可信问题");
  return text(item.title, en ? "Current focus" : "当前关注");
}

function rowDetail(item: Record<string, unknown>, key: string, en: boolean): string {
  if (key === "situations") return text(item.summary, en ? "A situation is being observed." : "正在观察这一情境。");
  if (key === "attention") return text(item.why_now, en ? "Why now is still a hypothesis." : "为什么是现在仍属于假设。");
  if (key === "suggestions") return text(item.delivery, en ? "Delivery: none" : "交付：无");
  if (key === "commitments") return text(item.next_at, en ? "No next time set" : "尚未设置下一时间");
  if (key === "questions") return text(item.why_now, en ? "Why now is not available" : "暂时没有 why now");
  return text(item.status, en ? "Waiting" : "等待中");
}

export function Matters({ scope, productContext, language = "zh" }: { scope: OwnerScope; productContext?: ProductContext | null; language?: Language }) {
  const [data, setData] = useState<ProductMatters | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const en = isEnglish(language);
  const contextStatus = String(productContext?.status ?? "loading");
  const load = async () => {
    setLoading(true); setError(null);
    if (!productContext || !["ready", "empty"].includes(contextStatus)) { setData(null); setLoading(false); return; }
    const internal = productContext.internal_read_scope;
    if (!internal) { setData(null); setLoading(false); return; }
    try { setData(await getProductMatters(internal)); setUpdatedAt(new Date().toISOString()); }
    catch (caught) { setError(caught instanceof Error ? caught.message : (en ? "Matters are temporarily unavailable" : "事项暂时不可用")); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [scope.userId, scope.sessionId, contextStatus, productContext?.internal_read_scope?.user_id, productContext?.internal_read_scope?.session_id]);
  const copy = en ? { kicker: "Matters", title: "What Veyra is keeping in view", intro: "A quiet projection of situations, suggestions, commitments, and waiting states. No operator-wide reviews or guessed tasks.", refresh: "Refresh", count: "items", empty: "Nothing is recorded for this scope yet.", unsupported: "No trusted production record is available yet.", advanced: "Need deeper technical evidence?", open: "Open Advanced console" } : { kicker: "事项", title: "Veyra 正在关注什么", intro: "这里展示情境、建议、承诺和等待状态；不混入全局 Review，也不猜测任务。", refresh: "刷新", count: "条", empty: "这个作用域还没有记录。", unsupported: "当前还没有可信的生产记录。", advanced: "需要更深的技术证据？", open: "打开 Advanced 旧控制台" };
  const goal = asRecord(data?.goal);
  return <div className="sectionPage mattersPage">
    <div className="pageIntro"><div><span className="eyebrow">{copy.kicker}</span><h1>{copy.title}</h1><p>{copy.intro}</p></div><button className="ghostButton" onClick={() => void load()} disabled={loading}><RefreshCw size={15} className={loading ? "spinIcon" : ""} />{copy.refresh}</button></div>
    <div className="pageFreshness"><Freshness at={updatedAt} loading={loading} error={error} language={language} /><span>{data?.status === "degraded" ? (en ? "Some sections are waiting for a fresh source." : "部分分区正在等待新鲜来源。") : (en ? "Exact owner/session product projection" : "精确 owner/session 产品投影")}</span></div>
    {error ? <ErrorBlock message={error} onRetry={() => void load()} /> : null}
    {loading && !data ? <LoadingBlock label={en ? "Reading product matters…" : "正在读取产品事项…"} /> : null}
    {!loading && !data && !error && contextStatus !== "loading" ? <Surface className="todayState"><StatusBadge value={contextStatus} /><span>{en ? "This product scope needs a local session link before Matters can be shown." : "事项需要本机 session link，之后才能显示。"}</span></Surface> : null}
    {data && goal.title ? <Surface className="matterFocus"><div className="matterFocusIcon"><Flag size={18} /></div><div><span className="eyebrow">{en ? "CURRENT FOCUS" : "当前关注"}</span><h2>{text(goal.title, en ? "Current focus" : "当前关注")}</h2><p>{text(goal.description, en ? "Veyra is keeping this local focus in view." : "Veyra 正在本机持续关注这一目标。")}</p></div><StatusBadge value={goal.status ?? "active"} /></Surface> : null}
    {data ? <div className="matterGrid">{META.map(({ key, icon: Icon, zh, en: enLabel, hintZh, hintEn }) => { const payload = section(data, key); const items = payload.items ?? []; const status = payload.status ?? (items.length ? "success" : "empty"); return <Surface className="matterCard" key={key}><div className="matterCardHeader"><div className="matterIcon"><Icon size={18} /></div><div><h2>{en ? enLabel : zh}</h2><p>{en ? hintEn : hintZh}</p></div><StatusBadge value={status} /></div><div className="matterCount">{items.length || payload.count || 0}<span>{copy.count}</span></div>{items.slice(0, 4).map((raw, index) => { const item = asRecord(raw); return <div className="matterRow" key={`${key}-${index}`}><div><strong>{rowTitle(item, key, en)}</strong><small>{rowDetail(item, key, en)}</small></div><ArrowUpRight size={15} /></div>; })}{!items.length ? <div className="matterEmpty">{status === "unsupported" ? copy.unsupported : copy.empty}</div> : null}<a className="cardLink" href="#/status">{en ? "See status" : "查看状态"} <ArrowUpRight size={14} /></a></Surface>; })}</div> : null}
    <div className="advancedHint"><span>{copy.advanced}</span><a href="#/advanced">{copy.open} <ArrowUpRight size={14} /></a></div>
  </div>;
}
