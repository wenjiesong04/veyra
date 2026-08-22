import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, CalendarClock, CircleHelp, Eye, Flag, RefreshCw, Sparkles } from "lucide-react";
import { getProductToday, type JsonValue, type ProductContext, type ProductInputScope, type ProductQuestion, type ProductReaction, type ProductSituation, type ProductToday } from "./api";
import { ProductQuestions } from "./product_questions";
import { ProductReactions, reactionRecords } from "./product_reactions";
import { dateLabel, progressLabel, record, records, serverFallbackText, statusLabel, text, uniqueRecords } from "./product_shared";
import { ErrorBlock, Freshness, isEnglish, Language, LoadingBlock, OwnerScope, StatusBadge, Surface } from "./shared";

type Props = { scope: OwnerScope; productContext?: ProductContext | null; inputScope?: ProductInputScope | null; language?: Language; refreshKey?: number; onStartChat: (text: string) => void; onOpenHistory?: () => void; onChanged?: () => void };

function scopeFor(scope: OwnerScope) { return { user_id: scope.userId, session_id: scope.sessionId }; }

function TodayCard({ title, icon, children, className = "" }: { title: string; icon: React.ReactNode; children: React.ReactNode; className?: string }) {
  return <Surface className={`productTodayCard ${className}`}><div className="productTodayCardHeading"><span>{icon}</span><h2>{title}</h2></div>{children}</Surface>;
}

function SituationPreview({ item, en }: { item: ProductSituation; en: boolean }) {
  const id = text(item.situation_id, "");
  return <a className="productSituationPreview" href={id ? `#/situations/${encodeURIComponent(id)}` : "#"} onClick={(event) => { if (!id) event.preventDefault(); }}><div><strong>{text(item.title ?? item.label, en ? "A situation in progress" : "正在进行的 Situation")}</strong><p>{text(item.summary, en ? "Veyra is maintaining this context." : "Veyra 正在维护这条上下文。")}</p></div><span><StatusBadge value={item.status ?? "active"} label={statusLabel(item.status ?? "active", en)} /><ArrowRight size={15} /></span></a>;
}

function ChangeRow({ item, en }: { item: ProductSituation; en: boolean }) {
  return <div className="productChangeRow"><strong>{text(item.title ?? item.label, en ? "Situation updated" : "Situation 有了变化")}</strong><span>{serverFallbackText(item.material_change, en, en ? "A material change was observed." : "观察到一项重要变化。")}</span><small>{text(item.changed_at, en ? "Recently" : "最近")}</small></div>;
}

function AttentionRow({ item, index, en }: { item: Record<string, JsonValue>; index: number; en: boolean }) {
  const situationId = text(item.situation_id, "");
  const reaction = record(item.reaction);
  const rank = typeof item.rank === "number" && Number.isFinite(item.rank) ? item.rank : null;
  const whatChanged = serverFallbackText(item.material_change ?? item.what_changed ?? reaction.what_changed, en, en ? "A relevant signal was observed." : "观察到一项值得留意的变化。");
  const whyNow = serverFallbackText(item.why_now ?? reaction.why_now, en, en ? "This is the next useful point to review." : "这是现在值得重新查看的节点。");
  const recommendation = serverFallbackText(item.recommendation ?? item.suggested_next_step ?? reaction.recommendation, en, en ? "Review the Situation." : "查看这条 Situation。");
  const href = situationId ? `#/situations/${encodeURIComponent(situationId)}` : "#";
  return <article className="productAttentionRow">
    <div className="productAttentionTop"><span className="attentionRank">#{index + 1}</span><strong>{text(item.title, en ? "Situation" : "Situation")}</strong>{rank !== null ? <small>{rank.toFixed(2)}</small> : null}</div>
    <div className="productExplainGrid"><div><span>{en ? "What changed" : "发生了什么"}</span><strong>{whatChanged}</strong></div><div><span>{en ? "Why now" : "为什么是现在"}</span><strong>{whyNow}</strong></div><div><span>{en ? "Suggested next step" : "建议"}</span><strong>{recommendation}</strong></div></div>
    {situationId ? <a className="productAttentionLink" href={href}>{en ? "Open this Situation" : "查看这条 Situation"}<ArrowRight size={14} /></a> : null}
  </article>;
}

function TodayView({ today, scope, language, readAt, onChanged, productContext }: { today: ProductToday; scope: OwnerScope; language: Language; readAt?: string | null; onChanged?: () => void; productContext?: ProductContext | null }) {
  const en = isEnglish(language);
  const situations = uniqueRecords(today.situations, { mode: "situation" });
  // The backend owns this ranking. Keep the received order intact: Today is
  // a projection of the server's attention decision, not a second ranker.
  const attention = records(today.attention, 20);
  const changes = uniqueRecords(today.recent_changes, { mode: "change" }).filter((item) => text(item.material_change, "").trim());
  const deadlines = uniqueRecords(today.deadlines, { mode: "deadline" });
  const unknowns = uniqueRecords(today.unknowns, { mode: "unknown" });
  const questionPayload = record(today.questions);
  const questions = uniqueRecords(questionPayload.items, { mode: "question" }) as ProductQuestion[];
  const suggestions = uniqueRecords(today.suggestions, { mode: "reaction" }) as ProductReaction[];
  const waiting = uniqueRecords(today.waiting, { mode: "waiting" });
  const freshness = record(today.freshness);
  const hasContent = attention.length || situations.length || changes.length || deadlines.length || unknowns.length || questions.length || suggestions.length || waiting.length;
  const degraded = String(today.status ?? "").toLowerCase() === "degraded";
  const summary = en ? "Veyra keeps the living context here: what is moving, what may be missed, and what deserves your attention now." : "Veyra 在这里维护生活上下文：什么在变化、什么可能被遗漏，以及现在最值得关注什么。";
  return <div className="productTodayPage sectionPage"><div className="productTodayIntro"><div><span className="eyebrow">TODAY</span><h1>{en ? "What is worth keeping in view" : "今天，什么值得关注"}</h1><p>{summary}</p></div><StatusBadge value={today.status ?? "unknown"} label={statusLabel(today.status ?? "unknown", en)} /></div><div className="productFreshness"><Freshness at={readAt} category={freshness.situations ?? freshness.semantic ?? "unknown"} language={language} /><span>{en ? "Exact owner/session context" : "精确 owner/session 上下文"}</span></div>
    {degraded ? <Surface className="productTodayQuiet degradedPanel"><AlertTriangle size={18} /><div><h2>{en ? "Today is degraded" : "Today 部分不可用"}</h2><p>{en ? "Some local context could not be verified. Veyra is showing only the evidence that remains trustworthy." : "部分本机上下文未能通过完整性检查。Veyra 只显示仍可信的证据。"}</p></div></Surface> : !hasContent ? <Surface className="productTodayQuiet"><Sparkles size={18} /><div><h2>{en ? "Nothing needs your attention yet" : "现在还没有需要你关注的事"}</h2><p>{en ? "Tell Veyra something real when you are ready. It will decide what needs to be remembered, asked, watched, or left quiet." : "准备好时告诉 Veyra 一件真实的事。它会判断什么需要记住、追问、观察，或保持安静。"}</p></div></Surface> : null}
    <div className="productTodayGrid">
      <TodayCard title={en ? "Worth your attention now" : "现在值得关注"} icon={<Flag size={17} />} className="spanWide">{attention.length ? attention.slice(0, 8).map((item, index) => <AttentionRow key={`${text(item.situation_id, "attention")}-${index}`} item={item} index={index} en={en} />) : <p className="productMuted">{en ? "Nothing has crossed Veyra's attention threshold." : "暂时没有事项越过 Veyra 的关注阈值。"}</p>}</TodayCard>
      <TodayCard title={en ? "In progress" : "正在关心"} icon={<Eye size={17} />} className="spanWide">{situations.length ? situations.slice(0, 6).map((item, index) => <SituationPreview key={text(item.situation_id, String(index))} item={item} en={en} />) : <p className="productMuted">{en ? "No active Situation." : "暂无进行中的 Situation。"}</p>}</TodayCard>
      <TodayCard title={en ? "Recent changes" : "最近变化"} icon={<Sparkles size={17} />}>{changes.length ? changes.slice(0, 4).map((item, index) => <ChangeRow key={`${text(item.situation_id, "change")}-${index}`} item={item} en={en} />) : <p className="productMuted">{en ? "No material change recorded." : "暂无重要变化记录。"}</p>}</TodayCard>
      <TodayCard title={en ? "Deadlines" : "临近时间"} icon={<CalendarClock size={17} />}>{deadlines.length ? deadlines.slice(0, 4).map((item, index) => { const progress = record(item.progress); const progressValue = Object.keys(progress).length ? progress.status : item.progress; return <div className="productDeadlineRow" key={`${text(item.situation_id, "deadline")}-${index}`}><strong>{text(item.title ?? item.label, en ? "Upcoming" : "即将到来")}</strong><span>{dateLabel(item.deadline_at, en)}</span><small>{progressLabel(progressValue, en)}</small></div>; }) : <p className="productMuted">{en ? "No deadline is close enough to surface." : "暂无需要现在提示的时间点。"}</p>}</TodayCard>
      <TodayCard title={en ? "May be missed" : "可能遗漏"} icon={<Flag size={17} />}>{unknowns.length ? unknowns.slice(0, 5).map((item, index) => <div className="productUnknownRow" key={`${text(item.situation_id, "unknown")}-${index}`}><strong>{text(item.statement, en ? "An unknown may matter." : "有一项未知可能影响判断。")}</strong><small>{en ? "Still unknown" : "仍未知"}</small></div>) : waiting.length ? waiting.slice(0, 4).map((item, index) => <div className="productUnknownRow" key={`waiting-${index}`}><strong>{text(item.what_changed ?? item.message, en ? "Veyra is waiting." : "Veyra 正在等待。")}</strong><small>{en ? "Waiting for a clearer signal" : "等待更清晰的信号"}</small></div>) : <p className="productMuted">{en ? "No likely omission surfaced." : "暂时没有发现可能遗漏。"}</p>}</TodayCard>
      <TodayCard title={en ? "Questions" : "还想问你"} icon={<CircleHelp size={17} />} className="spanWide"><ProductQuestions questions={questions} scope={scope} productContext={productContext} language={language} onChanged={onChanged} compact /></TodayCard>
      <TodayCard title={en ? "Suggestions" : "建议"} icon={<Sparkles size={17} />} className="spanWide"><ProductReactions reactions={suggestions} scope={scope} language={language} onChanged={onChanged} compact /></TodayCard>
    </div>
    <p className="productAuthorityNote">{en ? "Suggestions are record-only in this preview. Nothing is sent or executed automatically." : "这个预览中的建议只记录，不会自动发送或执行。"}</p>
  </div>;
}

export function ProductHome({ scope, productContext, inputScope, language = "zh", refreshKey = 0, onStartChat, onOpenHistory, onChanged }: Props) {
  const [today, setToday] = useState<ProductToday | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const refreshRef = useRef<{ generation: number; controller: AbortController | null }>({ generation: 0, controller: null });
  const load = useCallback(async () => {
    const generation = refreshRef.current.generation + 1;
    refreshRef.current.controller?.abort();
    const controller = new AbortController();
    refreshRef.current = { generation, controller };
    const isCurrent = () => refreshRef.current.generation === generation && refreshRef.current.controller === controller && !controller.signal.aborted;
    const contextStatus = String(productContext?.status ?? "loading");
    const internal = productContext?.internal_read_scope;
    if (contextStatus === "loading" || !productContext) {
      if (isCurrent()) refreshRef.current.controller = null;
      return;
    }
    if (!internal || !["ready", "empty"].includes(contextStatus) || internal.user_id !== scope.userId || internal.session_id !== scope.sessionId) {
      if (isCurrent()) {
        setToday(null);
        setError(contextStatus === "unavailable" ? (isEnglish(language) ? "Today is waiting for the local product scope." : "Today 正在等待本机产品作用域。") : null);
        setLoading(false);
        refreshRef.current.controller = null;
      }
      return;
    }
    if (!scope.userId || !scope.sessionId) {
      if (isCurrent()) refreshRef.current.controller = null;
      return;
    }
    setLoading(true); setError(null);
    try {
      const nextToday = await getProductToday(scopeFor(scope), { firstMeeting: true, signal: controller.signal });
      if (!isCurrent()) return;
      setToday(nextToday);
      setUpdatedAt(new Date().toISOString());
    } catch (caught) {
      if (!isCurrent() || (caught instanceof DOMException && caught.name === "AbortError")) return;
      setError(caught instanceof Error ? caught.message : (isEnglish(language) ? "Today is temporarily unavailable" : "Today 暂时不可用"));
    } finally {
      if (isCurrent()) {
        setLoading(false);
        refreshRef.current.controller = null;
      }
    }
  }, [scope.userId, scope.sessionId, language, productContext]);
  useEffect(() => {
    void load();
    return () => {
      refreshRef.current.generation += 1;
      refreshRef.current.controller?.abort();
      refreshRef.current.controller = null;
    };
  }, [load, refreshKey, productContext?.status]);
  useEffect(() => {
    let active = true;
    const refresh = () => { if (active) void load(); };
    const timer = window.setInterval(refresh, 30_000);
    window.addEventListener("focus", refresh);
    window.addEventListener("veyra:refresh-product", refresh);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener("focus", refresh); window.removeEventListener("veyra:refresh-product", refresh); };
  }, [load]);
  const en = isEnglish(language);
  if (loading && !today) return <div className="sectionPage productLoadingPage"><LoadingBlock label={en ? "Reading Today…" : "正在读取 Today…"} /></div>;
  if (error && !today) return <div className="sectionPage"><ErrorBlock message={error} onRetry={() => void load()} /></div>;
  if (!today) return null;
  return <><TodayView today={today} scope={scope} language={language} readAt={updatedAt} productContext={productContext} onChanged={() => { onChanged?.(); void load(); }} /><button className="productRefreshButton" type="button" onClick={() => void load()} disabled={loading} aria-label={en ? "Refresh Today" : "刷新 Today"}><RefreshCw size={15} className={loading ? "spinIcon" : ""} /></button></>;
}

export function refreshProductSurfaces() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event("veyra:refresh-product"));
}
