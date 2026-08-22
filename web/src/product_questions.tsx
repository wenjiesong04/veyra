import { useState } from "react";
import { Check, Clock3, MessageCircleQuestion, Send, X } from "lucide-react";
import { answerProductQuestion, deferProductQuestion, dismissProductQuestion, type ProductQuestion } from "./api";
import { dateLabel, record, statusLabel, text } from "./product_shared";
import { ErrorBlock, isEnglish, Language, LoadingBlock, OwnerScope, StatusBadge, Surface } from "./shared";

type Props = {
  questions: ProductQuestion[];
  scope: OwnerScope;
  language?: Language;
  onChanged?: () => void;
  compact?: boolean;
  situationRevision?: number;
};

function scopeFor(scope: OwnerScope) {
  return { user_id: scope.userId, session_id: scope.sessionId };
}

export function ProductQuestions({ questions, scope, language = "zh", onChanged, compact = false, situationRevision }: Props) {
  const en = isEnglish(language);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const copy = en ? {
    title: "Questions Veyra needs answered", empty: "Nothing needs your answer right now.", why: "Why now", answer: "Answer", defer: "Wait", dismiss: "Not relevant", placeholder: "Answer in your own words…", working: "Updating…", stale: "This question changed. Refresh and try again.", source: "This answer updates the linked Situation, not just Memory.",
  } : {
    title: "Veyra 还需要知道什么", empty: "现在没有需要你回答的问题。", why: "为什么现在", answer: "回答", defer: "稍后", dismiss: "不相关", placeholder: "用你的话补充…", working: "更新中…", stale: "这个问题已经变化，请刷新后重试。", source: "你的回答会更新关联 Situation，而不只是写进 Memory。",
  };
  const run = async (question: ProductQuestion, action: "answer" | "defer" | "dismiss") => {
    const id = text(question.need_id, "");
    const generation = Number(question.generation);
    if (!id || !Number.isFinite(generation) || generation < 1 || busy) return;
    if (action === "answer" && !text(drafts[id], "").trim()) return;
    setBusy(`${id}:${action}`); setError(null);
    try {
      const requestScope = scopeFor(scope);
      if (action === "answer") {
        const expectedRevision = Number(question.situation_revision ?? question.observation_revision ?? situationRevision);
        await answerProductQuestion(id, requestScope, { answer: text(drafts[id], "").trim(), expected_generation: generation, ...(Number.isFinite(expectedRevision) && expectedRevision > 0 ? { expected_revision: expectedRevision } : {}) });
      } else if (action === "defer") {
        await deferProductQuestion(id, requestScope, { expected_generation: generation });
      } else {
        await dismissProductQuestion(id, requestScope, { expected_generation: generation });
      }
      setDrafts((current) => { const next = { ...current }; delete next[id]; return next; });
      onChanged?.();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : copy.stale);
    } finally { setBusy(null); }
  };
  return <section className={`productQuestions ${compact ? "compact" : ""}`} aria-labelledby="product-questions-title">
    <div className="productSectionHeading"><div><span className="eyebrow">{en ? "UNKNOWN" : "仍未知"}</span><h2 id="product-questions-title">{copy.title}</h2></div><MessageCircleQuestion size={19} aria-hidden="true" /></div>
    {error ? <ErrorBlock message={error} onRetry={() => { setError(null); onChanged?.(); }} /> : null}
    {!questions.length ? <p className="productMuted">{copy.empty}</p> : <div className="productQuestionList">{questions.slice(0, 8).map((question) => {
      const id = text(question.need_id, "question");
      const situationId = text(question.situation_id, "");
      const itemBusy = busy?.startsWith(`${id}:`) ?? false;
      return <article className="productQuestion" key={id}>
        <div className="productQuestionTop"><StatusBadge value={question.status ?? "open"} label={statusLabel(question.status ?? "open", en)} />{situationId ? <a className="productQuestionSituation" href={`#/situations/${encodeURIComponent(situationId)}`}>{en ? "Linked Situation" : "关联 Situation"}</a> : null}</div>
        <h3>{text(question.question ?? question.blocked_judgment, en ? "What is still unclear?" : "还有什么没有弄清？")}</h3>
        <p className="productQuestionWhy"><strong>{copy.why}</strong>{text(question.why_now, en ? "The next observation depends on this." : "下一次观察依赖这条信息。")}</p>
        {question.expires_at ? <small className="productMuted">{en ? "Relevant until" : "关注到"} {dateLabel(question.expires_at, en)}</small> : null}
        <textarea value={drafts[id] ?? ""} onChange={(event) => setDrafts((current) => ({ ...current, [id]: event.target.value }))} placeholder={copy.placeholder} aria-label={text(question.question, copy.title)} rows={2} disabled={Boolean(busy)} />
        <div className="productQuestionActions"><button className="primaryButton small" type="button" onClick={() => void run(question, "answer")} disabled={Boolean(busy) || !text(drafts[id], "").trim()}>{itemBusy && busy?.endsWith(":answer") ? <Clock3 size={14} className="spinIcon" /> : <Send size={14} />}{itemBusy && busy?.endsWith(":answer") ? copy.working : copy.answer}</button><button className="ghostButton small" type="button" onClick={() => void run(question, "defer")} disabled={Boolean(busy)}><Clock3 size={14} />{copy.defer}</button><button className="textButton" type="button" onClick={() => void run(question, "dismiss")} disabled={Boolean(busy)}><X size={14} />{copy.dismiss}</button></div>
        <p className="productQuestionHint"><Check size={13} />{copy.source}</p>
      </article>;
    })}</div>}
  </section>;
}

export function QuestionsLoading({ language = "zh" }: { language?: Language }) {
  return <LoadingBlock label={isEnglish(language) ? "Reading questions…" : "正在读取问题…"} />;
}
