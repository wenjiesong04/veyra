import { useState } from "react";
import { Bell, Check, Clock3, Info, ThumbsDown, ThumbsUp } from "lucide-react";
import { feedbackProductReaction, type ProductReaction, type ReactionFeedbackLabel } from "./api";
import { record, text } from "./product_shared";
import { ErrorBlock, isEnglish, Language, OwnerScope } from "./shared";

type Props = { reactions: ProductReaction[]; scope: OwnerScope; language?: Language; onChanged?: () => void; compact?: boolean };

type ChoiceLabel = Exclude<ReactionFeedbackLabel, "remind_before" | "remind_offset">;
const labels: ChoiceLabel[] = ["useful", "not_useful", "ignore", "resolved", "too_early", "too_late", "too_frequent"];

function feedbackState(reaction: ProductReaction): { available: boolean; label: string } {
  const nested = record(reaction.feedback);
  const label = text(reaction.feedback_label ?? nested.label, "");
  const rawAvailable = typeof reaction.feedback_available === "boolean"
    ? reaction.feedback_available
    : nested.available;
  return { available: rawAvailable === true && !label, label };
}

export function ProductReactions({ reactions, scope, language = "zh", onChanged, compact = false }: Props) {
  const en = isEnglish(language);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reminder, setReminder] = useState<Record<string, number>>({});
  const [reminderOpen, setReminderOpen] = useState<string | null>(null);
  const copy = en ? { eyebrow: "SUGGESTIONS", title: "Worth telling you now", empty: "Veyra is staying quiet for now.", happened: "What happened", relevant: "Why it matters", now: "Why now", recommendation: "Suggested next step", evidence: "Facts and inference stay separate in the Situation detail.", feedback: "Was this useful?", saved: "Feedback saved", useful: "Useful", not_useful: "Not useful", ignore: "Ignore", resolved: "Resolved", too_early: "Too early", too_late: "Too late", too_frequent: "Too frequent", remind: "Remind before", remindButton: "Remind 1 day", days: "days", apply: "Apply", failed: "Feedback could not be saved." } : { eyebrow: "建议", title: "现在值得告诉你", empty: "Veyra 目前保持安静。", happened: "发生了什么", relevant: "为什么与你有关", now: "为什么是现在", recommendation: "我建议", evidence: "事实与推断会在 Situation 详情中分开显示。", feedback: "这条建议有帮助吗？", saved: "已记录反馈", useful: "有帮助", not_useful: "没帮助", ignore: "不用管", resolved: "已解决", too_early: "太早了", too_late: "太晚了", too_frequent: "太频繁", remind: "提前提醒", remindButton: "提前 1 天", days: "天", apply: "应用", failed: "反馈暂时没有保存。" };
  const send = async (reaction: ProductReaction, label: ReactionFeedbackLabel, seconds?: number) => {
    const id = text(reaction.reaction_id, "");
    const revision = Number(reaction.situation_revision);
    const feedback = feedbackState(reaction);
    if (!id || !Number.isFinite(revision) || revision < 1 || busy || !feedback.available) return;
    setBusy(`${id}:${label}`); setError(null);
    try {
      // The reaction row is the authority for category/learning semantics;
      // feedback only submits the user choice and the CAS revision.
      const result = await feedbackProductReaction(id, { user_id: scope.userId, session_id: scope.sessionId }, { label, situation_revision: revision, ...(seconds === undefined ? {} : { remind_before_seconds: Math.max(0, Math.min(30 * 86400, Math.round(seconds))) }) });
      const status = text(result.status, "").toLowerCase();
      if (!["recorded", "duplicate"].includes(status)) throw new Error(copy.failed);
      setReminderOpen(null); onChanged?.();
    } catch { setError(copy.failed); }
    finally { setBusy(null); }
  };
  const visible = reactions.filter((reaction) => text(reaction.disposition, "") === "suggest");
  return <section className={`productReactions ${compact ? "compact" : ""}`} aria-labelledby="product-reactions-title">
    <div className="productSectionHeading"><div><span className="eyebrow">{copy.eyebrow}</span><h2 id="product-reactions-title">{copy.title}</h2></div><Bell size={19} aria-hidden="true" /></div>
    {error ? <ErrorBlock message={error} onRetry={() => { setError(null); onChanged?.(); }} /> : null}
    {!visible.length ? <p className="productMuted">{copy.empty}</p> : <div className="productReactionList">{visible.slice(0, 8).map((reaction) => {
      const id = text(reaction.reaction_id, "reaction"); const busyThis = busy?.startsWith(`${id}:`) ?? false;
      const facts = text(reaction.fact_vs_inference ?? reaction.reason, "");
      const feedback = feedbackState(reaction);
      return <article className="productReaction" key={id}>
        <div className="productReactionTop"><span className="reactionRank">#{Math.max(1, Math.round(Number(reaction.rank) || 1))}</span><span className="productReactionScope">{text(reaction.category, en ? "general" : "一般")}</span></div>
        <div className="productExplainGrid"><div><span>{copy.happened}</span><strong>{text(reaction.what_changed, en ? "A material change was observed." : "观察到一项变化。")}</strong></div><div><span>{copy.relevant}</span><strong>{text(reaction.why_relevant, en ? "It may affect this Situation." : "它可能影响这条 Situation。")}</strong></div><div><span>{copy.now}</span><strong>{text(reaction.why_now, en ? "The timing crossed a useful threshold." : "现在是一个值得留意的时间点。")}</strong></div><div><span>{copy.recommendation}</span><strong>{text(reaction.recommendation, en ? "Review the linked Situation." : "查看关联 Situation。")}</strong></div></div>
        {facts ? <p className="productReactionEvidence"><Info size={13} />{facts}</p> : <p className="productReactionEvidence"><Info size={13} />{copy.evidence}</p>}
        <div className="productFeedback">{feedback.available ? <><span>{copy.feedback}</span><div className="productFeedbackButtons">{labels.map((label) => <button key={label} type="button" disabled={Boolean(busy)} className={label === "useful" ? "feedbackGood" : ""} onClick={() => void send(reaction, label)}>{label === "useful" ? <ThumbsUp size={13} /> : label === "not_useful" ? <ThumbsDown size={13} /> : null}{copy[label]}</button>)}<button type="button" disabled={Boolean(busy)} onClick={() => setReminderOpen(reminderOpen === id ? null : id)}><Clock3 size={13} />{copy.remindButton}</button></div></> : <span className="productFeedbackSaved">{copy.saved}{feedback.label ? `: ${feedback.label}` : ""}</span>}</div>
        {reminderOpen === id ? <div className="reminderEditor"><label htmlFor={`reminder-${id}`}>{copy.remind}</label><input id={`reminder-${id}`} type="number" min={0} max={30} step={1} value={reminder[id] ?? 1} onChange={(event) => setReminder((current) => ({ ...current, [id]: Math.max(0, Math.min(30, Number(event.target.value) || 0)) }))} /><span>{copy.days}</span><button className="primaryButton small" type="button" disabled={Boolean(busy)} onClick={() => void send(reaction, "remind_before", (reminder[id] ?? 1) * 86400)}>{busyThis ? <Clock3 size={13} className="spinIcon" /> : <Check size={13} />}{copy.apply}</button></div> : null}
      </article>;
    })}</div>}
  </section>;
}

export function reactionRecords(value: unknown): ProductReaction[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === "object" && !Array.isArray(item)).map((item) => record(item));
}
