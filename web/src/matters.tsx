import { useEffect, useState } from "react";
import { ArrowUpRight, ChevronDown, ClipboardList, Clock3, Eye, Flag, MessageSquareText, RefreshCw, Sparkles } from "lucide-react";
import { getProductMatters, type ProductContext, type ProductMatters } from "./api";
import { asItems, asRecord, ErrorBlock, Freshness, isEnglish, Language, LoadingBlock, OwnerScope, StatusBadge, Surface } from "./shared";

type MatterSection = { status?: string; items?: unknown[]; count?: number };

function sectionCount(payload: MatterSection, items: unknown[]): number {
  return typeof payload.count === "number" && payload.count > 0 ? payload.count : items.length;
}

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

function userDetail(value: unknown, fallback: string, en: boolean): string {
  const result = text(value, fallback);
  if (en || !/[A-Za-z]/.test(result) || /[\u3400-\u9fff]/.test(result)) return result;
  return fallback;
}

function statusLabel(value: unknown, en: boolean): string {
  const normalized = String(value ?? "unknown").toLowerCase();
  const labels: Record<string, [string, string]> = {
    success: ["已有记录", "Recorded"], observed: ["已观察", "Observed"], active: ["进行中", "Active"],
    empty: ["暂无", "Empty"], unsupported: ["暂不可用", "Unavailable"], waiting: ["等待中", "Waiting"],
    stale: ["需要刷新", "Stale"], degraded: ["部分可用", "Degraded"], unknown: ["未知", "Unknown"],
  };
  return labels[normalized]?.[en ? 1 : 0] ?? (en ? normalized : "未知");
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
  if (key === "situations") return userDetail(item.summary, en ? "A situation is being observed." : "正在观察这一情境。", en);
  if (key === "attention") return userDetail(item.why_now, en ? "Why now is still a hypothesis." : "为什么是现在仍属于假设。", en);
  if (key === "suggestions") return userDetail(item.delivery, en ? "Delivery: none" : "交付：无", en);
  if (key === "commitments") return userDetail(item.next_at, en ? "No next time set" : "尚未设置下一时间", en);
  if (key === "questions") return userDetail(item.why_now, en ? "Why now is not available" : "暂时没有 why now", en);
  return userDetail(item.status, en ? "Waiting" : "等待中", en);
}

function uniqueItems(items: unknown[], key: string, en: boolean): unknown[] {
  const seen = new Set<string>();
  return items.filter((raw) => {
    const item = asRecord(raw);
    const signature = `${rowTitle(item, key, en)}\u0000${rowDetail(item, key, en)}`;
    if (seen.has(signature)) return false;
    seen.add(signature);
    return true;
  });
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
  const copy = en ? {
    kicker: "Matters", title: "What Veyra is keeping in view", intro: "One clear focus first. The rest stays available when you need it.", refresh: "Refresh", count: "items", empty: "Nothing is recorded for this scope yet.", unsupported: "No trusted production record is available yet.", current: "Current focus", other: "Other matters", expand: "Show details", collapse: "Hide details", records: "recorded", scope: "This local scope only", advanced: "Need deeper technical evidence?", open: "Open Advanced console"
  } : {
    kicker: "事项", title: "Veyra 正在关注什么", intro: "先看一件最值得知道的事，其余内容按需展开。", refresh: "刷新", count: "条", empty: "这个作用域还没有记录。", unsupported: "当前还没有可信的生产记录。", current: "当前关注", other: "其他事项", expand: "展开详情", collapse: "收起详情", records: "条记录", scope: "只显示当前本机范围", advanced: "需要更深的技术证据？", open: "打开 Advanced 旧控制台"
  };
  const goal = asRecord(data?.goal);
  const sectionData = data ? META.map((meta) => {
    const payload = section(data, meta.key);
    const items = uniqueItems(payload.items ?? [], meta.key, en);
    const status = payload.status ?? (items.length ? "success" : "empty");
    return { ...meta, payload, items, count: sectionCount(payload, items), status };
  }) : [];
  const primary = sectionData.find((item) => item.items.length > 0) ?? sectionData.find((item) => item.key === "situations") ?? sectionData[0];
  const secondary = primary ? sectionData.filter((item) => item.key !== primary.key) : sectionData;
  const PrimaryIcon = primary?.icon ?? Sparkles;
  return <div className="sectionPage mattersPage">
    <div className="pageIntro"><div><span className="eyebrow">{copy.kicker}</span><h1>{copy.title}</h1><p>{copy.intro}</p></div><button className="ghostButton" onClick={() => void load()} disabled={loading}><RefreshCw size={15} className={loading ? "spinIcon" : ""} />{copy.refresh}</button></div>
    <div className="pageFreshness"><Freshness at={updatedAt} loading={loading} error={error} language={language} /><span>{data?.status === "degraded" ? (en ? "Some sections are waiting for a fresh source." : "部分分区正在等待新鲜来源。") : copy.scope}</span></div>
    {error ? <ErrorBlock message={error} onRetry={() => void load()} /> : null}
    {loading && !data ? <LoadingBlock label={en ? "Reading product matters…" : "正在读取产品事项…"} /> : null}
    {!loading && !data && !error && contextStatus !== "loading" ? <Surface className="todayState"><StatusBadge value={contextStatus} /><span>{en ? "This product scope needs a local session link before Matters can be shown." : "事项需要本机 session link，之后才能显示。"}</span></Surface> : null}
    {data && primary ? <>
      <Surface className="matterHero">
        <div className="matterHeroTop">
          <div className="matterIcon matterHeroIcon"><PrimaryIcon size={18} /></div>
          <div className="matterHeroHeading"><span className="eyebrow">{copy.current}</span><h2>{en ? primary.en : primary.zh}</h2><p>{en ? primary.hintEn : primary.hintZh}</p></div>
          <StatusBadge value={primary.status} label={statusLabel(primary.status, en)} />
        </div>
        <div className="matterHeroBody">
          <div>
            <strong>{primary.items.length ? rowTitle(asRecord(primary.items[0]), primary.key, en) : (goal.title ? text(goal.title, copy.current) : (primary.status === "unsupported" ? copy.unsupported : copy.empty))}</strong>
            <p>{primary.items.length ? rowDetail(asRecord(primary.items[0]), primary.key, en) : (goal.description ? text(goal.description, copy.empty) : (primary.status === "unsupported" ? copy.unsupported : copy.empty))}</p>
          </div>
          <div className="matterHeroMeta"><strong>{primary.count}</strong><span>{copy.count}</span></div>
        </div>
        {goal.title ? <div className="matterGoal"><Flag size={14} /><span>{text(goal.title, copy.current)}</span><StatusBadge value={goal.status ?? "active"} label={statusLabel(goal.status ?? "active", en)} /></div> : null}
        {primary.items.length > 1 ? <details className="disclosure matterHeroDetails"><summary><ChevronDown size={14} />{copy.expand} · {primary.items.length} {copy.records}</summary><div className="matterRows">{primary.items.slice(0, 4).map((raw, index) => { const item = asRecord(raw); return <div className="matterRow" key={`${primary.key}-${index}`}><div><strong>{rowTitle(item, primary.key, en)}</strong><small>{rowDetail(item, primary.key, en)}</small></div><ArrowUpRight size={15} /></div>; })}</div></details> : null}
      </Surface>
      <div className="matterRailHeading"><div><span className="eyebrow">{copy.other}</span><h2>{en ? "More context, when you need it" : "其他信息，按需查看"}</h2></div><a href="#/status">{en ? "Open status" : "查看状态"} <ArrowUpRight size={14} /></a></div>
      <div className="matterRail">{secondary.map(({ key, icon: Icon, zh, en: enLabel, hintZh, hintEn, items, count, status }) => <Surface className="matterMini" key={key}><div className="matterMiniHeader"><div className="matterIcon"><Icon size={16} /></div><div className="matterMiniTitle"><h3>{en ? enLabel : zh}</h3><p>{en ? hintEn : hintZh}</p></div><StatusBadge value={status} label={statusLabel(status, en)} /></div><div className="matterMiniMeta"><strong>{count}</strong><span>{copy.count}</span></div>{items.length ? <details className="disclosure"><summary><ChevronDown size={13} />{copy.expand}</summary><div className="matterRows">{items.slice(0, 3).map((raw, index) => { const item = asRecord(raw); return <div className="matterRow" key={`${key}-${index}`}><div><strong>{rowTitle(item, key, en)}</strong><small>{rowDetail(item, key, en)}</small></div></div>; })}</div></details> : <p className="matterMiniEmpty">{status === "unsupported" ? copy.unsupported : copy.empty}</p>}</Surface>)}</div>
    </> : null}
    <div className="advancedHint"><span>{copy.advanced}</span><a href="#/advanced">{copy.open} <ArrowUpRight size={14} /></a></div>
  </div>;
}
