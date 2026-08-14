import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ArrowUpRight, Clock3, History, Info, MessageCircle, Paperclip, Plus, Send, ShieldCheck, Sparkles, X } from "lucide-react";
import { getProductToday, streamMessage, type MessageResult, type MessageStreamEvent, type ProductContext, type ProductInputScope, type ProductToday } from "./api";
import { asItems, asRecord, Disclosure, ErrorBlock, formatTime, isEnglish, Language, OwnerScope, sanitizeHistory, StatusBadge, Surface } from "./shared";

export type LocalConversation = {
  id: string;
  conversationId?: string;
  text: string;
  result?: MessageResult;
  createdAt: string;
  ownerId?: string;
  sessionId?: string;
  status?: "streaming" | "completed" | "failed";
  phase?: string;
  error?: string;
};

const HISTORY_KEY = "veyra.local-conversations.v1";

export function conversationIdFor(item: LocalConversation): string {
  return item.conversationId || item.id;
}

function readHistory(scope?: OwnerScope): LocalConversation[] {
  if (typeof window === "undefined") return [];
  try {
    const rows = sanitizeHistory(JSON.parse(window.localStorage.getItem(HISTORY_KEY) ?? "[]"));
    return rows.filter((item) => !scope || (item.ownerId === scope.userId && item.sessionId === scope.sessionId)) as LocalConversation[];
  } catch {
    return [];
  }
}

function readAllHistory(): LocalConversation[] {
  if (typeof window === "undefined") return [];
  try {
    return sanitizeHistory(JSON.parse(window.localStorage.getItem(HISTORY_KEY) ?? "[]")) as LocalConversation[];
  } catch {
    return [];
  }
}

function saveHistory(items: LocalConversation[]) {
  if (typeof window === "undefined") return;
  try {
    const all = readAllHistory();
    const scopeKeys = new Set(items.map((item) => `${item.ownerId ?? ""}:${item.sessionId ?? ""}`));
    const persisted = sanitizeHistory(items) as LocalConversation[];
    const merged = [...persisted, ...all.filter((item) => !scopeKeys.has(`${item.ownerId ?? ""}:${item.sessionId ?? ""}`))]
      .sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)))
      .slice(0, 60);
    window.localStorage.setItem(HISTORY_KEY, JSON.stringify(merged));
  } catch {
    // Browser history is optional; the server remains the source of truth.
  }
}

function recoverInterruptedTurns(items: LocalConversation[], en: boolean): LocalConversation[] {
  return items.map((item) => item.status === "streaming" ? {
    ...item,
    status: "failed" as const,
    phase: "interrupted",
    error: en ? "The previous connection ended before a verified response. You can send this again." : "上一次连接在得到已验证回应前结束了，可以重新发送。",
  } : item);
}

function newId(): string {
  return typeof crypto?.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function phaseText(phase: string | undefined, en: boolean): string {
  const values: Record<string, [string, string]> = {
    accepted: ["已接收请求", "Request accepted"],
    queued: ["正在排队", "Queued"],
    understanding: ["正在理解", "Understanding"],
    processing: ["正在处理", "Processing"],
    agent: ["等待 Agent", "Waiting for Agent"],
    review_required: ["需要你确认", "Needs your confirmation"],
    verifying: ["正在验证", "Verifying"],
    message: ["正在整理回应", "Preparing the response"],
    completed: ["已完成", "Completed"],
  };
  return values[phase ?? ""]?.[en ? 1 : 0] ?? (en ? "Working" : "正在处理");
}

function eventPhase(event: MessageStreamEvent): string | undefined {
  if (typeof event.phase === "string" && event.phase) return event.phase;
  if (event.payload && typeof event.payload === "object" && !Array.isArray(event.payload)) {
    const value = (event.payload as Record<string, unknown>).phase;
    if (typeof value === "string" && value) return value;
  }
  if (event.type === "accepted") return "accepted";
  if (event.type === "message") return "message";
  if (event.type === "completed") return "completed";
  if (event.type === "review_required") return "review_required";
  return undefined;
}

function listText(value: unknown): string {
  if (!Array.isArray(value)) return "";
  return value.map((item) => {
    if (typeof item === "string" || typeof item === "number") return String(item);
    if (item && typeof item === "object" && !Array.isArray(item)) {
      const record = item as Record<string, unknown>;
      return typeof record.label === "string" ? record.label : typeof record.title === "string" ? record.title : "";
    }
    return "";
  }).filter(Boolean).slice(0, 5).join(" · ");
}

export function HomePage({ scope, productContext, inputScope, language = "zh", onOpenHistory, onStartChat }: { scope: OwnerScope; productContext?: ProductContext | null; inputScope?: ProductInputScope | null; language?: Language; onOpenHistory?: () => void; onStartChat: (text: string) => void }) {
  const [text, setText] = useState("");
  const [today, setToday] = useState<ProductToday | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const en = isEnglish(language);
  const contextStatus = String(productContext?.status ?? "loading");
  const load = async () => {
    if (!productContext || !["ready", "empty"].includes(contextStatus)) return;
    setLoading(true); setLoadError(null);
    try { setToday(await getProductToday({ user_id: scope.userId, session_id: scope.sessionId })); }
    catch (caught) { setLoadError(caught instanceof Error ? caught.message : (en ? "Today is temporarily unavailable" : "Today 暂时不可用")); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [scope.userId, scope.sessionId, contextStatus]);
  const prompts = useMemo(() => en ? ["What am I working on?", "Remember a task for me", "Is Agent online?"] : ["我正在做什么？", "帮我记下一个待办", "看看 Agent 是否在线"], [en]);
  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const value = text.trim();
    if (!value) return;
    setText("");
    if (!inputScope) return;
    onStartChat(value);
  };
  const iconPath = `${import.meta.env.BASE_URL}veyra-icon.png`;
  const goal = asRecord(today?.goal ?? productContext?.goal);
  const situations = asItems(today?.situations);
  const suggestions = asItems(today?.suggestions);
  const attention = asItems(today?.attention);
  const questions = asRecord(today?.questions);
  const freshness = asRecord(today?.freshness);
  const waiting = asItems(today?.waiting);
  const emptyContext = contextStatus === "empty" || today?.status === "empty";
  return <div className="conversationPage homePage todayPage">
    <div className="welcomeKicker"><span className="kickerLine" />{en ? "TODAY" : "今天"}<span className="kickerLine" /></div>
    <div className="welcomeMark" aria-hidden="true"><img src={iconPath} alt="" /><span /></div>
    <h1>{en ? "What Veyra is keeping in view" : "Veyra 正在关注什么"}</h1>
    <p className="welcomeLead">{goal.title ? String(goal.title) : en ? "Your local product context is ready when you are." : "你的本机产品上下文已准备好。"}</p>
    {contextStatus === "loading" ? <Surface className="todayState"><span>{en ? "Connecting to the local product context…" : "正在连接本机产品上下文…"}</span></Surface> : null}
    {!["loading", "ready", "empty"].includes(contextStatus) ? <Surface className="todayState"><StatusBadge value={contextStatus} /><span>{en ? "This product context needs a local session link before conversation can continue." : "产品上下文需要本机 session link，之后才能继续对话。"}</span></Surface> : null}
    {loadError ? <ErrorBlock message={loadError} onRetry={() => void load()} /> : null}
    {loading && !today ? <div className="todayLoading">{en ? "Reading Today…" : "正在读取 Today…"}</div> : null}
    {today && !emptyContext ? <><div className="todayFreshness"><span>{en ? "Sources" : "来源"}: {String(freshness.situations ?? "unknown")}</span><span>{en ? "Attention" : "关注"}: {String(freshness.attention ?? "unknown")}</span></div><div className="todayGrid"><Surface className="todayCard todayFocusCard"><div className="todayCardHeader"><span className="eyebrow">{en ? "CURRENT SITUATION" : "当前情境"}</span><StatusBadge value={situations[0]?.status ?? "observed"} /></div>{situations.length ? <><h2>{String(situations[0].title ?? goal.title ?? (en ? "Current focus" : "当前关注"))}</h2><p>{String(situations[0].summary ?? (en ? "Veyra is observing this focus." : "Veyra 正在观察这一关注。"))}</p><small>{en ? "Known" : "已知"}: {listText(situations[0].known) || "—"}</small><small>{en ? "Unknown" : "仍未知"}: {listText(situations[0].unknown) || "—"}</small><small>{en ? "Changed" : "变化"}: {String(situations[0].changed_at ?? "unknown")}</small></> : <div className="todayEmpty">{en ? "No current situation is recorded yet." : "当前还没有记录情境。"}</div>}{attention.length ? <div className="todayListItem"><strong>{en ? "Attention hypothesis" : "关注假设"}: {String(attention[0].title ?? "—")}</strong><small>{en ? "Why now" : "为什么现在"}: {String(attention[0].why_now ?? "unknown")}</small></div> : null}</Surface><Surface className="todayCard"><div className="todayCardHeader"><span className="eyebrow">{en ? "SUGGESTIONS" : "建议"}</span><StatusBadge value={suggestions.length ? "recorded" : "empty"} /></div>{suggestions.length ? suggestions.slice(0, 2).map((item, index) => <div className="todayListItem" key={index}><strong>{String(item.message ?? (en ? "Suggestion preview" : "建议预览"))}</strong><small>{en ? "Recorded only · delivery none" : "仅记录 · 不发送"}</small></div>) : <div className="todayEmpty">{en ? "No current suggestion is ready." : "当前没有可信建议。"}</div>}</Surface><Surface className="todayCard"><div className="todayCardHeader"><span className="eyebrow">{en ? "QUESTIONS" : "问题"}</span><StatusBadge value={questions.status ?? "unsupported"} /></div>{questions.status === "unsupported" ? <div className="todayEmpty">{en ? "No trusted production question is available yet." : "当前没有可信的生产问题记录。"}</div> : <div className="todayListItem"><strong>{String(questions.items ?? "—")}</strong></div>}</Surface><Surface className="todayCard"><div className="todayCardHeader"><span className="eyebrow">{en ? "WAITING" : "等待"}</span><StatusBadge value={waiting.length ? "waiting" : "quiet"} /></div>{waiting.length ? waiting.slice(0, 2).map((item, index) => <div className="todayListItem" key={index}><strong>{String(item.message ?? (en ? "Waiting for a clearer signal" : "等待更清晰的信号"))}</strong></div>) : <div className="todayEmpty">{en ? "Nothing is waiting right now." : "目前没有等待中的事项。"}</div>}</Surface></div></> : null}
    <form className="composer" onSubmit={submit}>
      <textarea value={text} onChange={(event) => setText(event.target.value)} placeholder={en ? "Tell me what you’re working on…" : "告诉我你正在做什么，或希望我关注什么…"} aria-label={en ? "Message" : "输入消息"} rows={2} />
      <div className="composerBar"><span className="composerHint"><ShieldCheck size={14} />{inputScope ? (en ? "Local Veyra · lifecycle events are real" : "本机 Veyra · 使用真实生命周期事件") : (en ? "Conversation waits for a local session link" : "对话等待本机 session link")}</span><div className="composerActions"><button type="button" className="iconButton subtle" title={en ? "Attachments not enabled" : "附件暂未启用"} aria-label={en ? "Attachments not enabled" : "附件暂未启用"} disabled><Paperclip size={17} /></button><button className="sendButton" type="submit" disabled={!text.trim() || !inputScope}><Send size={17} /><span>{en ? "Continue conversation" : "继续对话"}</span></button></div></div>
    </form>
    <div className="promptRow" aria-label={en ? "Suggestions" : "引导问题"}>{prompts.map((prompt) => <button key={prompt} type="button" onClick={() => setText(prompt)}><Sparkles size={14} />{prompt}</button>)}</div>
    <div className="welcomeNote"><Info size={15} /><span>{en ? "Boundary: this preview records suggestions only; important actions wait for your confirmation." : "边界：这个预览只记录建议；重要行动会显示依据并等待你的确认。"}</span></div>
    <div className="conversationFooter"><span>{en ? "A quiet place to begin · local browser history" : "从这里安静地开始 · 历史仅保存在本机浏览器"}</span><button className="textButton" type="button" onClick={onOpenHistory}><History size={14} />{en ? "View history" : "查看会话历史"}<ArrowUpRight size={13} /></button></div>
  </div>;
}

/** Dedicated conversation workspace. The landing page never renders a prior response. */
export function ChatPage({ scope, inputScope, language = "zh", conversationId, historyEpoch = 0, pendingText, onPendingConsumed, onOpenHistory, onNewConversation, onHistoryChange }: { scope: OwnerScope; inputScope?: ProductInputScope | null; language?: Language; conversationId: string; historyEpoch?: number; pendingText?: string | null; onPendingConsumed?: () => void; onOpenHistory?: () => void; onNewConversation: () => void; onHistoryChange?: (items: LocalConversation[]) => void }) {
  const en = isEnglish(language);
  const [turns, setTurns] = useState<LocalConversation[]>(() => recoverInterruptedTurns(readHistory(scope).filter((item) => conversationIdFor(item) === conversationId), en));
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [activePhase, setActivePhase] = useState<string | undefined>();
  const [error, setError] = useState<string | null>(null);
  const startedPending = useRef<string | null>(null);

  useEffect(() => {
    setTurns(recoverInterruptedTurns(readHistory(scope).filter((item) => conversationIdFor(item) === conversationId), en));
    setBusy(false); setActivePhase(undefined); setError(null); startedPending.current = null;
  }, [scope.userId, scope.sessionId, conversationId]);
  useEffect(() => {
    if (historyEpoch > 0) {
      setTurns([]); setBusy(false); setActivePhase(undefined); setError(null);
    }
  }, [historyEpoch]);

  const publish = (next: LocalConversation[]) => {
    setTurns(next);
    saveHistory(next.length ? [...readHistory(scope).filter((item) => conversationIdFor(item) !== conversationId), ...next] : readHistory(scope));
    onHistoryChange?.(readHistory(scope));
  };

  const updateTurn = (id: string, patch: Partial<LocalConversation>) => {
    setTurns((current) => {
      const next = current.map((item) => item.id === id ? { ...item, ...patch } : item);
      const others = readHistory(scope).filter((item) => item.id !== id && conversationIdFor(item) !== conversationId);
      saveHistory([...others, ...next]);
      onHistoryChange?.(readHistory(scope));
      return next;
    });
  };

  const submitText = async (raw: string) => {
    const value = raw.trim();
    if (!value || busy) return;
    if (!inputScope) {
      setError(en ? "Veyra is waiting for a local session link." : "Veyra 正在等待本机 session link。");
      return;
    }
    const item: LocalConversation = { id: newId(), conversationId, text: value, createdAt: new Date().toISOString(), ownerId: scope.userId, sessionId: scope.sessionId, status: "streaming", phase: "accepted" };
    publish([item, ...turns]);
    setDraft(""); setBusy(true); setError(null); setActivePhase("accepted");
    try {
      const result = await streamMessage(value, inputScope.user_id, inputScope.session_id, item.id, (event) => {
        const phase = eventPhase(event);
        if (phase) { setActivePhase(phase); updateTurn(item.id, { phase }); }
      }, inputScope.channel);
      updateTurn(item.id, { result, status: "completed", phase: "completed" });
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : (en ? "Veyra could not finish this request" : "Veyra 暂时没有完成这个请求");
      setError(message); updateTurn(item.id, { status: "failed", phase: "failed", error: message });
    } finally {
      setBusy(false); setActivePhase(undefined);
    }
  };

  useEffect(() => {
    if (!pendingText?.trim() || startedPending.current === pendingText) return;
    startedPending.current = pendingText;
    onPendingConsumed?.();
    void submitText(pendingText);
    // The pending value is intentionally consumed once per route entry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingText]);

  const title = turns[turns.length - 1]?.text || (en ? "New conversation" : "新会话");
  const orderedTurns = turns.slice().reverse();
  return <div className="chatPage">
    <div className="chatToolbar"><div><span className="eyebrow">{en ? "CONVERSATION" : "对话"}</span><h1>{title.length > 52 ? `${title.slice(0, 52)}…` : title}</h1><p>{en ? "Veyra keeps the conversation readable; execution details stay tucked below each verified response." : "Veyra 会保持对话清晰，执行详情收在每条已验证回应下方。"}</p></div><div className="chatToolbarActions"><button className="ghostButton" type="button" onClick={onNewConversation}><Plus size={15} />{en ? "New conversation" : "新会话"}</button><button className="textButton" type="button" onClick={onOpenHistory}><History size={15} />{en ? "History" : "历史"}</button></div></div>
    <div className="chatThread" aria-live="polite">
      {!orderedTurns.length && !busy ? <div className="chatEmpty"><MessageCircle size={21} /><p>{en ? "This is a new conversation. What should Veyra understand first?" : "这是一个新会话。你希望 Veyra 先了解什么？"}</p></div> : null}
      {orderedTurns.map((item) => <ChatTurn key={item.id} item={item} language={language} />)}
      {busy ? <div className="streamStatus"><Clock3 size={16} className="spinIcon" /><span>{phaseText(activePhase, en)}</span><small>{en ? "Live lifecycle event · no simulated typing" : "真实生命周期事件 · 不模拟逐字输出"}</small></div> : null}
    </div>
    <form className="composer chatComposer" onSubmit={(event) => { event.preventDefault(); void submitText(draft); }}><textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={en ? "Continue the conversation…" : "继续告诉 Veyra…"} aria-label={en ? "Continue the conversation" : "继续对话"} rows={2} disabled={busy} /><div className="composerBar"><span className="composerHint"><ShieldCheck size={14} />{en ? "Local Veyra · actions remain reviewable" : "本机 Veyra · 行动始终可复核"}</span><div className="composerActions"><button type="button" className="iconButton subtle" disabled aria-label={en ? "Attachments not enabled" : "附件暂未启用"}><Paperclip size={17} /></button><button className="sendButton" type="submit" disabled={busy || !draft.trim()}>{busy ? <Clock3 size={17} className="spinIcon" /> : <Send size={17} />}<span>{busy ? (en ? "Working…" : "处理中…") : (en ? "Send" : "发送")}</span></button></div></div></form>
    <div className="chatFooter"><button className="textButton" type="button" onClick={onNewConversation}><ArrowLeft size={14} />{en ? "Back to first meeting" : "回到初见"}</button><span>{en ? "History is local to this browser" : "历史仅保存在当前浏览器"}</span></div>
  </div>;
}

function ChatTurn({ item, language }: { item: LocalConversation; language: Language }) {
  const en = isEnglish(language);
  return <article className="chatTurn"><div className="userBubble"><span className="messageLabel">{en ? "You" : "你"}</span><p>{item.text}</p></div>{item.result ? <ResponseCard item={item} language={language} /> : item.status === "failed" ? <div className="conversationError turnError"><Info size={16} /><span>{item.error || (en ? "This turn did not complete." : "这一轮没有完成。")}</span></div> : item.status === "streaming" ? <div className="turnPending"><Clock3 size={15} className="spinIcon" />{phaseText(item.phase, en)}</div> : null}</article>;
}

function ResponseCard({ item, language = "zh" }: { item: LocalConversation; language?: Language }) {
  const result = item.result ?? {};
  const en = isEnglish(language);
  const response = typeof result.response === "string" && result.response.trim() ? result.response : typeof result.message === "string" && result.message.trim() ? result.message : en ? "The request completed, but no displayable response was returned." : "请求已完成，但服务没有返回可展示的回应。";
  return <Surface className="responseCard"><div className="responseHeader"><div><span className="responseEyebrow">{en ? "Veyra's response" : "Veyra 的回应"}</span><time>{formatTime(item.createdAt)}</time></div><span className="routeTag">{String(result.status ?? "—")}</span></div><p>{response}</p><Disclosure title={en ? "Response status" : "回应状态"}><div className="detailGrid"><div><span>{en ? "Status" : "状态"}</span><code>{String(result.status ?? "unknown")}</code></div><div><span>{en ? "Risk" : "风险"}</span><code>{String(result.risk_level ?? (en ? "Not assessed" : "未评估"))}</code></div></div><p className="smallNote">{en ? "Technical evidence remains available in Advanced." : "技术证据仍可在 Advanced 中查看。"}</p></Disclosure></Surface>;
}

export function HistoryDrawer({ open, onClose, items, onSelect, language = "zh", onClear }: { open: boolean; onClose: () => void; items: LocalConversation[]; onSelect?: (item: LocalConversation) => void; language?: Language; onClear?: () => void }) {
  const en = isEnglish(language);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  const summaries = useMemo(() => {
    const seen = new Set<string>();
    return items.filter((item) => { const key = conversationIdFor(item); if (seen.has(key)) return false; seen.add(key); return true; });
  }, [items]);
  if (!open) return null;
  return <div className="drawerBackdrop" role="presentation" onClick={onClose}><aside className="historyDrawer" role="dialog" aria-modal="true" aria-label={en ? "Conversation history" : "会话历史"} onClick={(event) => event.stopPropagation()}><div className="drawerHeader"><div><span className="eyebrow">{en ? "This browser" : "本机此浏览器"}</span><h2>{en ? "History" : "会话历史"}</h2></div><button className="iconButton" onClick={onClose} aria-label={en ? "Close history" : "关闭历史"}><X size={18} /></button></div>{summaries.length ? <div className="historyList">{summaries.map((item) => <button className="historyItem" key={conversationIdFor(item)} onClick={() => { onSelect?.(item); onClose(); }}><MessageCircle size={16} /><span><strong>{item.text}</strong><small>{formatTime(item.createdAt)} · {item.result ? (en ? "Responded" : "已回应") : item.status === "streaming" ? (en ? "In progress" : "进行中") : (en ? "Incomplete" : "未完成")}</small></span></button>)}</div> : <div className="emptyState"><History size={17} />{en ? "No local conversations yet." : "这里还没有本地会话。"}</div>}<div className="drawerFooter"><Info size={14} />{en ? "Stored only in this browser" : "仅保存在当前浏览器"}{summaries.length ? <button className="textButton clearHistory" onClick={() => { if (window.confirm(en ? "Clear local conversation history? This does not delete server records." : "清除本地会话历史？这不会删除服务器上的记录。")) onClear?.(); }}>{en ? "Clear" : "清除"}</button> : null}</div></aside></div>;
}

export function NewConversationButton({ onClick }: { onClick: () => void }) { return <button type="button" className="newConversation" onClick={onClick}><Plus size={15} />新会话</button>; }

// Backward-compatible name for any downstream imports; the shell now mounts HomePage explicitly.
export const Conversation = HomePage;
