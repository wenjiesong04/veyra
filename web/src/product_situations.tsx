import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, CalendarClock, Check, ChevronDown, CircleHelp, Eye, Flag, History, Pencil, RefreshCw, RotateCcw, StopCircle } from "lucide-react";
import { commandProductSituation, getProductSituation, getProductSituations, type ProductContext, type ProductQuestion, type ProductReaction, type ProductSituation, type SituationCommand } from "./api";
import { ProductQuestions } from "./product_questions";
import { ProductReactions, reactionRecords } from "./product_reactions";
import { categoryLabel, dateLabel, epistemicLabel, productContextReadiness, progressLabel, record, records, statusLabel, strings, text, uniqueRecords } from "./product_shared";
import { ErrorBlock, Freshness, isEnglish, Language, LoadingBlock, OwnerScope, safeText, StatusBadge, Surface } from "./shared";

type ScopeProps = { scope: OwnerScope; language?: Language; productContext?: ProductContext | null; onChanged?: () => void };

function scopeFor(scope: OwnerScope) { return { user_id: scope.userId, session_id: scope.sessionId }; }

function ContextGate({ loading, status, en, label }: { loading: boolean; status: string; en: boolean; label: string }) {
  if (loading) return <LoadingBlock label={en ? "Waiting for Product Context before reading " + label + "…" : "正在等待 Product Context，然后读取 " + label + "…"} />;
  return <Surface className="productEmptyPanel" role="status"><CircleHelp size={18} /><div><strong>{en ? label + " requires an exact local Product Context." : label + " 需要精确的本机 Product Context。"}</strong><p className="productMuted">{en ? "Veyra will not read this projection until the local context is ready or empty." : "在本机上下文就绪或明确为空之前，Veyra 不会读取这项内容。"}</p><StatusBadge value={status} label={statusLabel(status, en)} /></div></Surface>;
}

function safeEvidenceText(value: unknown, fallback: string, maxLength = 640): string {
  const selected = safeText(value, fallback, maxLength);
  const lowered = selected.toLowerCase();
  return ["/", "\\", "path", "token", "secret", "credential", "password"].some((marker) => lowered.includes(marker)) ? fallback : selected;
}

type EvidenceProjection = { label: string; fields: string[]; snippet: string; detail: string; epistemic: { label: string; tone: string } | null };

function evidenceProjection(value: unknown, en: boolean): EvidenceProjection {
  if (typeof value === "string") {
    const label = safeEvidenceText(value, en ? "Evidence" : "证据");
    return { label, fields: [], snippet: "", detail: label, epistemic: null };
  }
  const item = record(value);
  const label = safeEvidenceText(item.title ?? item.source ?? item.kind ?? item.summary, en ? "Evidence" : "证据", 240);
  const fields: string[] = [];
  const addField = (name: string, value: unknown, maxLength = 240) => {
    const selected = safeEvidenceText(value, "", maxLength);
    if (selected && !fields.includes(selected)) fields.push(`${name}: ${selected}`);
  };
  addField(en ? "source" : "来源", item.source);
  addField(en ? "kind" : "类型", item.kind);
  addField(en ? "status" : "状态", statusLabel(item.status, en));
  addField(en ? "freshness" : "新鲜度", statusLabel(item.freshness, en));
  addField(en ? "fresh until" : "有效至", item.fresh_until);
  if (typeof item.ttl_seconds === "number" && Number.isFinite(item.ttl_seconds) && item.ttl_seconds >= 0 && item.ttl_seconds <= 604800) {
    addField("TTL", `${Math.round(item.ttl_seconds)}s`);
  }
  addField(en ? "ref" : "引用", item.ref, 240);
  const snippet = safeEvidenceText(item.snippet ?? item.summary, "", 700);
  const detail = [...fields, snippet].join(" · ");
  const baseEpistemic = epistemicLabel(item.epistemic_status ?? item.status, en);
  const epistemic = baseEpistemic ? { ...baseEpistemic, label: detail ? `${baseEpistemic.label} · ${detail}` : baseEpistemic.label } : null;
  return { label, fields, snippet, detail, epistemic };
}

function DetailRows({ title, rows, en, empty }: { title: string; rows: Array<Record<string, unknown>>; en: boolean; empty: string }) {
  return <section className="situationDetailSection"><div className="productSectionHeading"><h2>{title}</h2></div>{rows.length ? <div className="situationDetailRows">{rows.map((row, index) => { const epistemic = epistemicLabel(row.epistemic_status, en); return <div className="situationDetailRow" key={`${title}-${index}`}><strong>{text(row.statement ?? row.value ?? row.kind, empty)}</strong>{epistemic ? <span className={`epistemicTag ${epistemic.tone}`}>{epistemic.label}</span> : null}{row.occurred_at ? <small>{dateLabel(row.occurred_at, en)}</small> : null}</div>; })}</div> : <p className="productMuted">{empty}</p>}</section>;
}

export function SituationsPage({ scope, productContext, language = "zh", onChanged }: ScopeProps) {
  const en = isEnglish(language);
  const [items, setItems] = useState<ProductSituation[]>([]);
  const [terminal, setTerminal] = useState<ProductSituation[]>([]);
  const [projectionStatus, setProjectionStatus] = useState("loading");
  const [freshnessCategory, setFreshnessCategory] = useState<unknown>("unknown");
  const [loading, setLoading] = useState(true); const [error, setError] = useState<string | null>(null); const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const requestRef = useRef<{ generation: number; controller: AbortController | null }>({ generation: 0, controller: null });
  const load = useCallback(async () => {
    const generation = requestRef.current.generation + 1;
    requestRef.current.controller?.abort();
    const controller = new AbortController();
    requestRef.current = { generation, controller };
    const isCurrent = () => requestRef.current.generation === generation && requestRef.current.controller === controller && !controller.signal.aborted;
    const context = productContextReadiness(productContext, scope);
    setError(null);
    setProjectionStatus(context.status);
    if (!context.ready) {
      if (isCurrent()) {
        setItems([]); setTerminal([]); setUpdatedAt(null); setFreshnessCategory("unknown"); setLoading(context.loading); requestRef.current.controller = null;
      }
      return;
    }
    setLoading(true);
    try {
      const result = await getProductSituations(scopeFor(scope), { signal: controller.signal });
      if (!isCurrent()) return;
      const status = String(result.status ?? "unknown"); const freshness = record(result.freshness);
      setProjectionStatus(status); setFreshnessCategory(freshness.situations ?? "unknown"); setItems(uniqueRecords(result.items, { mode: "situation" }) as ProductSituation[]); setTerminal(uniqueRecords(result.recent_terminal, { mode: "situation" }) as ProductSituation[]); setUpdatedAt(new Date().toISOString());
      if (status === "degraded") setError(en ? "Situation data is degraded; only verified rows are shown." : "Situation 数据部分不可用，只显示通过校验的内容。");
    } catch (caught) {
      if (!isCurrent() || (caught instanceof DOMException && caught.name === "AbortError")) return;
      setProjectionStatus("unavailable"); setFreshnessCategory("unknown"); setError(caught instanceof Error ? caught.message : (en ? "Situations are unavailable" : "Situations 暂时不可用"));
    } finally {
      if (isCurrent()) { setLoading(false); requestRef.current.controller = null; }
    }
  }, [scope.userId, scope.sessionId, en, productContext]);
  useEffect(() => { void load(); return () => { requestRef.current.generation += 1; requestRef.current.controller?.abort(); requestRef.current.controller = null; }; }, [load]);
  const context = productContextReadiness(productContext, scope);
  if (!context.ready) return <div className="sectionPage situationsPage"><div className="pageIntro"><div><span className="eyebrow">SITUATIONS</span><h1>{en ? "What Veyra is carrying forward" : "Veyra 正在持续维护的事"}</h1><p>{en ? "Each Situation has a goal, current understanding, unknowns, and a next observation." : "每个 Situation 都有目标、当前理解、未知和下一次观察。"}</p></div></div><ContextGate loading={context.loading} status={context.status} en={en} label="Situations" /></div>;
  return <div className="sectionPage situationsPage"><div className="pageIntro"><div><span className="eyebrow">{en ? "SITUATIONS" : "SITUATIONS"}</span><h1>{en ? "What Veyra is carrying forward" : "Veyra 正在持续维护的事"}</h1><p>{en ? "Each Situation has a goal, current understanding, unknowns, and a next observation. Open one to correct or quiet it." : "每个 Situation 都有目标、当前理解、未知和下一次观察。打开后可以纠正或让它安静。"}</p></div><div className="pageIntroActions"><StatusBadge value={projectionStatus} label={statusLabel(projectionStatus, en)} /><button className="ghostButton" type="button" onClick={() => void load()} disabled={loading}><RefreshCw size={15} className={loading ? "spinIcon" : ""} />{en ? "Refresh" : "刷新"}</button></div></div><div className="productFreshness"><Freshness at={updatedAt} category={freshnessCategory} loading={loading} error={projectionStatus === "unavailable" ? error : null} language={language} /><span>{en ? "Exact owner/session only" : "只显示当前 owner/session"}</span></div>{error ? <ErrorBlock message={error} onRetry={() => void load()} /> : null}{loading && !items.length && !terminal.length ? <LoadingBlock label={en ? "Reading Situations…" : "正在读取 Situations…"} /> : null}<div className="situationListPage"><div className="situationListHeading"><h2>{en ? "In progress" : "进行中"}</h2><span>{items.length}</span></div>{items.length ? items.map((item, index) => <SituationListCard item={item} en={en} key={text(item.situation_id, String(index))} />) : !loading ? <Surface className="productEmptyPanel"><Eye size={18} /><span>{en ? "No active Situation yet. Tell Veyra something real in Chat." : "还没有进行中的 Situation。可以在 Chat 告诉 Veyra 一件真实的事。"}</span></Surface> : null}{terminal.length ? <><div className="situationListHeading terminal"><h2>{en ? "Recently closed" : "最近结束"}</h2><span>{terminal.length}</span></div><div className="situationTerminalList">{terminal.map((item, index) => <SituationListCard item={item} en={en} terminal key={text(item.situation_id, `terminal-${index}`)} />)}</div></> : null}</div><div className="advancedHint"><span>{en ? "Need runtime evidence?" : "需要运行时证据？"}</span><a href="#/advanced">{en ? "Open Advanced" : "打开 Advanced"} <ArrowRight size={14} /></a></div></div>;
}

function SituationListCard({ item, en, terminal = false }: { item: ProductSituation; en: boolean; terminal?: boolean }) {
  const id = text(item.situation_id, "");
  return <a className="situationListCard" href={id ? `#/situations/${encodeURIComponent(id)}` : "#"} onClick={(event) => { if (!id) event.preventDefault(); }}><div className="situationListIcon"><Eye size={17} /></div><div className="situationListBody"><div className="situationListTop"><h3>{text(item.title ?? item.label, en ? "Untitled Situation" : "未命名 Situation")}</h3><StatusBadge value={item.status ?? (terminal ? "resolved" : "active")} label={statusLabel(item.status ?? (terminal ? "resolved" : "active"), en)} /></div><p>{text(item.summary, en ? "Veyra is maintaining this context." : "Veyra 正在维护这条上下文。")}</p><div className="situationListMeta"><span>{categoryLabel(item.category, en)}</span>{item.deadline_at ? <span><CalendarClock size={12} />{dateLabel(item.deadline_at, en)}</span> : null}<span>{en ? "Revision" : "观察版本"} {text(item.revision, "1")}</span></div></div><ArrowRight size={16} /></a>;
}

export function SituationDetailPage({ situationId, scope, productContext, language = "zh", onChanged }: ScopeProps & { situationId: string }) {
  const en = isEnglish(language);
  const [detail, setDetail] = useState<ProductSituation | null>(null); const [questions, setQuestions] = useState<ProductQuestion[]>([]); const [reactions, setReactions] = useState<ProductReaction[]>([]);
  const [projectionStatus, setProjectionStatus] = useState("loading"); const [freshnessCategory, setFreshnessCategory] = useState<unknown>("unknown");
  const [loading, setLoading] = useState(true); const [error, setError] = useState<string | null>(null); const [commandBusy, setCommandBusy] = useState(false); const [commandError, setCommandError] = useState<string | null>(null); const [correction, setCorrection] = useState("");
  const requestRef = useRef<{ generation: number; controller: AbortController | null }>({ generation: 0, controller: null });
  const load = useCallback(async () => {
    const generation = requestRef.current.generation + 1;
    requestRef.current.controller?.abort();
    const controller = new AbortController();
    requestRef.current = { generation, controller };
    const isCurrent = () => requestRef.current.generation === generation && requestRef.current.controller === controller && !controller.signal.aborted;
    const context = productContextReadiness(productContext, scope);
    setError(null); setProjectionStatus(context.status);
    if (!context.ready) {
      if (isCurrent()) { setDetail(null); setQuestions([]); setReactions([]); setFreshnessCategory("unknown"); setLoading(context.loading); requestRef.current.controller = null; }
      return;
    }
    setLoading(true);
    try {
      const result = await getProductSituation(situationId, scopeFor(scope), { signal: controller.signal });
      if (!isCurrent()) return;
      const status = String(result.status ?? "unknown"); const freshness = record(result.freshness);
      setProjectionStatus(status); setFreshnessCategory(freshness.situations ?? "unknown");
      if (status === "degraded") { setDetail(null); setQuestions([]); setReactions([]); setError(en ? "This Situation is degraded; its state could not be verified." : "这条 Situation 部分不可用，状态未通过完整性检查。"); return; }
      if (!result.situation) throw new Error(en ? "Situation not found" : "Situation 不存在");
      setDetail(result.situation); setQuestions(Array.isArray(result.questions) ? result.questions : []); setReactions(Array.isArray(result.reactions) ? result.reactions : []);
    } catch (caught) {
      if (!isCurrent() || (caught instanceof DOMException && caught.name === "AbortError")) return;
      setProjectionStatus("unavailable"); setFreshnessCategory("unknown"); setError(caught instanceof Error ? caught.message : (en ? "Situation is unavailable" : "Situation 暂时不可用"));
    } finally {
      if (isCurrent()) { setLoading(false); requestRef.current.controller = null; }
    }
  }, [situationId, scope.userId, scope.sessionId, en, productContext]);
  useEffect(() => { void load(); return () => { requestRef.current.generation += 1; requestRef.current.controller?.abort(); requestRef.current.controller = null; }; }, [load]);
  const context = productContextReadiness(productContext, scope);
  const command = async (name: SituationCommand, patch: Record<string, string> = {}) => { if (!detail || commandBusy) return; const revision = Number(detail.revision); if (!Number.isFinite(revision) || revision < 1) return; if (name === "correct" && !correction.trim()) return; setCommandBusy(true); setCommandError(null); try { await commandProductSituation(situationId, scopeFor(scope), { command: name, expected_revision: revision, patch: name === "correct" ? { summary: correction.trim() } : patch, reason: name === "correct" ? correction.trim() : "" }); setCorrection(""); await load(); onChanged?.(); } catch (caught) { setCommandError(caught instanceof Error ? caught.message : (en ? "The Situation changed. Refresh and try again." : "Situation 已变化，请刷新后重试。")); } finally { setCommandBusy(false); } };
  if (!context.ready) return <div className="sectionPage situationDetailPage"><a className="textButton" href="#/situations"><ArrowLeft size={14} />{en ? "Back to Situations" : "返回 Situations"}</a><ContextGate loading={context.loading} status={context.status} en={en} label={en ? "Situation detail" : "Situation 详情"} /></div>;
  if (loading && !detail) return <div className="sectionPage"><LoadingBlock label={en ? "Opening Situation…" : "正在打开 Situation…"} /></div>;
  if (error && !detail) return <div className="sectionPage"><a className="textButton" href="#/situations"><ArrowLeft size={14} />{en ? "Back to Situations" : "返回 Situations"}</a><div className="productFreshness"><StatusBadge value={projectionStatus} label={statusLabel(projectionStatus, en)} /><Freshness at={null} category={freshnessCategory} language={language} /></div><ErrorBlock message={error} onRetry={() => void load()} /></div>;
  if (!detail) return null;
  const known = records(detail.known); const unknown = strings(detail.unknown, 12).map((statement) => ({ statement })); const assumptions = records(detail.assumptions); const timeline = uniqueRecords(detail.timeline, { mode: "timeline" }); const entities = records(detail.entities); const richEvidence = Array.isArray(detail.evidence) ? detail.evidence : []; const evidence = richEvidence.length ? richEvidence : (Array.isArray(detail.evidence_refs) ? detail.evidence_refs : []); const rawProgress = record(detail.progress); const progress = { ...rawProgress, status: progressLabel(rawProgress.status, en), value: rawProgress.value };
  const terminal = ["resolved", "expired", "contradicted", "archived", "completed", "closed", "terminal"].includes(text(detail.status, "").toLowerCase());
  return <div className="sectionPage situationDetailPage"><a className="textButton" href="#/situations"><ArrowLeft size={14} />{en ? "Back to Situations" : "返回 Situations"}</a><div className="situationDetailHero"><div className="situationDetailHeading"><span className="eyebrow">{en ? "SITUATION" : "SITUATION"}</span><h1>{text(detail.title ?? detail.label, en ? "Untitled Situation" : "未命名 Situation")}</h1><p>{text(detail.summary, en ? "Veyra is maintaining this context." : "Veyra 正在维护这条上下文。")}</p></div><StatusBadge value={detail.status ?? "unknown"} label={statusLabel(detail.status, en)} /></div><div className="situationDetailFacts"><div><span>{en ? "Goal" : "目标"}</span><strong>{text(detail.goal, en ? "Not stated" : "尚未明确")}</strong></div><div><span>{en ? "Progress" : "进展"}</span><strong>{text(progress.status, en ? "Unknown" : "未知")}{progress.value !== null && progress.value !== undefined ? ` · ${text(progress.value)}` : ""}</strong></div><div><span>{en ? "Deadline" : "时间点"}</span><strong>{detail.deadline_at ? dateLabel(detail.deadline_at, en) : (en ? "No deadline" : "没有明确时间")}</strong></div><div><span>{en ? "Next observation" : "下一观察点"}</span><strong>{detail.next_observation_at ? dateLabel(detail.next_observation_at, en) : (en ? "Not scheduled" : "尚未安排")}</strong></div></div><div className="situationCommandBar">{terminal ? <button className="ghostButton small" type="button" onClick={() => void command("reopen")} disabled={commandBusy}><RotateCcw size={14} />{en ? "Reopen" : "重新打开"}</button> : <><button className="ghostButton small" type="button" onClick={() => void command("resolve")} disabled={commandBusy}><Check size={14} />{en ? "Mark resolved" : "标记已解决"}</button><button className="ghostButton small" type="button" onClick={() => void command("quiet")} disabled={commandBusy}><StopCircle size={14} />{en ? "Keep quiet" : "保持安静"}</button></>}</div>{commandError ? <ErrorBlock message={commandError} onRetry={() => void load()} /> : null}<Surface className="situationCorrection"><div className="productSectionHeading"><div><h2>{en ? "Correct the understanding" : "纠正 Veyra 的理解"}</h2><p>{en ? "This correction is validated by the server and advances the Situation revision." : "纠正会由服务器校验，并推进 Situation 版本。"}</p></div><Pencil size={17} /></div><textarea value={correction} onChange={(event) => setCorrection(event.target.value)} rows={2} placeholder={en ? "What should be different?" : "哪里需要改？"} aria-label={en ? "Correct the Situation" : "纠正 Situation"} /><button className="primaryButton small" type="button" onClick={() => void command("correct")} disabled={commandBusy || !correction.trim()}><Pencil size={14} />{en ? "Save correction" : "保存纠正"}</button></Surface><div className="situationDetailColumns"><div><DetailRows title={en ? "Known · facts" : "已知 · 事实"} rows={known} en={en} empty={en ? "No reported facts yet." : "还没有已报告事实。"} /><DetailRows title={en ? "Unknown · open questions" : "未知 · 待确认"} rows={unknown} en={en} empty={en ? "No unresolved unknowns." : "暂无未解决的未知。"} /><DetailRows title={en ? "Assumptions · inference" : "假设 · 推断"} rows={assumptions} en={en} empty={en ? "No assumptions recorded." : "还没有记录假设。"} /><DetailRows title={en ? "Timeline" : "时间线"} rows={timeline} en={en} empty={en ? "No timeline entry yet." : "还没有时间线记录。"} /></div><div><Surface className="situationEvidence"><div className="productSectionHeading"><div><h2>{en ? "Evidence & freshness" : "证据与新鲜度"}</h2><p>{en ? "Handles are shown without paths or tokens." : "只展示安全的证据引用，不展示路径或 token。"}</p></div><History size={17} /></div><div className="evidenceList">{evidence.length ? evidence.slice(0, 12).map((ref, index) => { const projected = evidenceProjection(ref, en); return <span key={`${projected.label}-${index}`} title={projected.detail}>{projected.label}{projected.epistemic ? ` · ${projected.epistemic.label}` : projected.detail ? ` · ${projected.detail}` : ""}</span>; }) : <p className="productMuted">{en ? "No evidence reference is projected." : "暂无证据引用。"}</p>}</div><div className="detailFreshness"><span>{en ? "Last changed" : "最近变化"}</span><strong>{dateLabel(detail.changed_at, en)}</strong></div><div className="detailFreshness"><span>{en ? "Next step" : "下一步"}</span><strong>{text(detail.next_step, en ? "Wait for a clearer signal." : "等待更清晰的信号。")}</strong><small>{text(record(detail.epistemic).next_step, en ? "Inference" : "推断")}</small></div></Surface><DetailRows title={en ? "Entities" : "相关实体"} rows={entities} en={en} empty={en ? "No entities projected." : "暂无相关实体。"} /></div></div><div className="situationDetailBlocks"><ProductQuestions questions={questions} scope={scope} language={language} situationRevision={Number(detail.revision)} onChanged={() => { void load(); onChanged?.(); }} /><ProductReactions reactions={reactions} scope={scope} language={language} onChanged={() => { void load(); onChanged?.(); }} /></div><p className="productAuthorityNote">{en ? "Veyra can update understanding here, but this surface has no execution or external delivery authority." : "Veyra 可以在这里更新理解，但这个界面没有执行或外部交付权限。"}</p></div>;
}
