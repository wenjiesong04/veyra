import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ArrowUpRight, Clock3, History, Info, MessageCircle, Paperclip, Plus, Send, ShieldCheck, Sparkles, X } from "lucide-react";
import { streamMessage, type MessageResult, type MessageStreamEvent, type ProductContext, type ProductInputScope } from "./api";
import { Disclosure, formatTime, isEnglish, Language, OwnerScope, safeText, sanitizeHistory, Surface } from "./shared";

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
    agent: ["等待可用能力", "Waiting for an available capability"],
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

/**
 * First meeting stays intentionally quiet. Product situations and runtime
 * summaries belong to Matters/Status; this route only helps a person begin.
 */
export function HomePage({ scope: _scope, productContext, inputScope, language = "zh", onOpenHistory, onStartChat }: { scope: OwnerScope; productContext?: ProductContext | null; inputScope?: ProductInputScope | null; language?: Language; onOpenHistory?: () => void; onStartChat: (text: string) => void }) {
  const [text, setText] = useState("");
  const en = isEnglish(language);
  const contextStatus = String(productContext?.status ?? "loading");
  const prompts = useMemo(
    () => en
      ? ["What deserves my attention?", "Help me set a goal", "Check the current runtime"]
      : ["看看最近有什么值得关注", "帮我建立一个目标", "检查当前运行状态"],
    [en],
  );
  const needsConnection = contextStatus === "loading" || contextStatus === "needs_session_link" || contextStatus === "degraded" || !inputScope;
  const connectionCopy = contextStatus === "loading"
    ? (en ? "Preparing the local connection…" : "正在准备本机连接…")
    : contextStatus === "needs_session_link"
      ? (en ? "This input is paused until the local session link is verified." : "本地 session link 完成验证前，输入已暂停。")
      : contextStatus === "degraded"
        ? (en ? "The local product state is degraded; input stays paused until scope can be verified." : "本机产品状态部分不可用；作用域验证完成前，输入会保持暂停。")
        : (en ? "Before we begin, Veyra needs a local connection." : "开始前，Veyra 需要完成本地连接。");
  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const value = text.trim();
    if (!value || !inputScope) return;
    setText("");
    onStartChat(value);
  };
  const iconPath = `${import.meta.env.BASE_URL}veyra-icon.png`;
  return <div className="conversationPage homePage">
    <div className="welcomeKicker"><span className="kickerLine" />{en ? "FIRST MEETING" : "初见"}<span className="kickerLine" /></div>
    <div className="welcomeMark" aria-hidden="true"><img src={iconPath} alt="" /><span /></div>
    <h1>{en ? "Hi, I’m Veyra." : "你好，我是 Veyra。"}</h1>
    <p className="welcomeLead">{en ? "I’ll first understand what you’re doing, then decide whether to answer, remember, remind you, or stay quiet." : "我会先了解你正在做什么，再决定是回答、记录、提醒，还是保持安静。"}</p>
    {needsConnection ? <div className="welcomeNote connectionNote"><Info size={15} /><span>{connectionCopy}</span>{contextStatus !== "loading" ? <a href="#/settings">{en ? "Open Settings" : "打开设置"}<ArrowUpRight size={13} /></a> : null}</div> : null}
    <form className="composer" onSubmit={submit}>
      <textarea value={text} onChange={(event) => setText(event.target.value)} placeholder={en ? "Tell me what you’re working on, or what I should keep in view…" : "告诉我你正在做什么，或希望我关注什么…"} aria-label={en ? "Message" : "输入消息"} rows={2} disabled={needsConnection} />
      <div className="composerBar"><span className="composerHint"><ShieldCheck size={14} />{inputScope ? (en ? "Local Veyra · actions remain reviewable" : "本机 Veyra · 行动始终可复核") : (en ? "Waiting for the local connection" : "等待本机连接")}</span><div className="composerActions"><button type="button" className="iconButton subtle" title={en ? "Attachments not enabled" : "附件暂未启用"} aria-label={en ? "Attachments not enabled" : "附件暂未启用"} disabled><Paperclip size={17} /></button><button className="sendButton" type="submit" disabled={!text.trim() || !inputScope}><Send size={17} /><span>{en ? "Start conversation" : "开始对话"}</span></button></div></div>
    </form>
    <div className="promptRow" aria-label={en ? "Suggestions" : "引导问题"}>{prompts.map((prompt) => <button key={prompt} type="button" onClick={() => setText(prompt)}><Sparkles size={14} />{prompt}</button>)}</div>
    <div className="conversationFooter"><span>{en ? "A quiet place to begin · local browser history" : "从这里安静地开始 · 历史仅保存在本机浏览器"}</span><button className="textButton" type="button" onClick={onOpenHistory}><History size={14} />{en ? "View history" : "查看会话历史"}<ArrowUpRight size={13} /></button></div>
  </div>;
}

/** Dedicated conversation workspace. The landing page never renders a prior response. */
export function ChatPage({ scope, inputScope, inputStatus, language = "zh", conversationId, historyEpoch = 0, pendingText, onPendingConsumed, onOpenHistory, onNewConversation, onBackHome, onHistoryChange }: { scope: OwnerScope; inputScope?: ProductInputScope | null; inputStatus?: string; language?: Language; conversationId: string; historyEpoch?: number; pendingText?: string | null; onPendingConsumed?: () => void; onOpenHistory?: () => void; onNewConversation: () => void; onBackHome?: () => void; onHistoryChange?: (items: LocalConversation[]) => void }) {
  const en = isEnglish(language);
  const [turns, setTurns] = useState<LocalConversation[]>(() => recoverInterruptedTurns(readHistory(scope).filter((item) => conversationIdFor(item) === conversationId), en));
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [activePhase, setActivePhase] = useState<string | undefined>();
  const [error, setError] = useState<string | null>(null);
  const startedPending = useRef<string | null>(null);
  const sessionLinkRequired = inputStatus === "needs_session_link";

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
      if (typeof window !== "undefined") window.dispatchEvent(new Event("veyra:refresh-product"));
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
    <form className="composer chatComposer" onSubmit={(event) => { event.preventDefault(); void submitText(draft); }}><textarea value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={sessionLinkRequired ? (en ? "Waiting for the local session link…" : "等待本地 session link…") : (en ? "Continue the conversation…" : "继续告诉 Veyra…")} aria-label={en ? "Continue the conversation" : "继续对话"} rows={2} disabled={busy || !inputScope || sessionLinkRequired} /><div className="composerBar"><span className="composerHint"><ShieldCheck size={14} />{sessionLinkRequired ? (en ? "Input paused · local session link required" : "输入已暂停 · 需要本地 session link") : inputScope ? (en ? "Local Veyra · actions remain reviewable" : "本机 Veyra · 行动始终可复核") : (en ? "Waiting for the local connection" : "等待本机连接")}</span><div className="composerActions"><button type="button" className="iconButton subtle" disabled aria-label={en ? "Attachments not enabled" : "附件暂未启用"}><Paperclip size={17} /></button><button className="sendButton" type="submit" disabled={busy || !draft.trim() || !inputScope || sessionLinkRequired}>{busy ? <Clock3 size={17} className="spinIcon" /> : <Send size={17} />}<span>{busy ? (en ? "Working…" : "处理中…") : (en ? "Send" : "发送")}</span></button></div></div></form>
    <div className="chatFooter"><button className="textButton" type="button" onClick={onBackHome ?? onNewConversation}><ArrowLeft size={14} />{en ? "Back to Home" : "回到首页"}</button><span>{en ? "History is local to this browser" : "历史仅保存在当前浏览器"}</span></div>
  </div>;
}

function ChatTurn({ item, language }: { item: LocalConversation; language: Language }) {
  const en = isEnglish(language);
  return <article className="chatTurn"><div className="userBubble"><span className="messageLabel">{en ? "You" : "你"}</span><p>{item.text}</p></div>{item.result ? <ResponseCard item={item} language={language} /> : item.status === "failed" ? <div className="conversationError turnError"><Info size={16} /><span>{item.error || (en ? "This turn did not complete." : "这一轮没有完成。")}</span></div> : item.status === "streaming" ? <div className="turnPending"><Clock3 size={15} className="spinIcon" />{phaseText(item.phase, en)}</div> : null}</article>;
}

function ResponseCard({ item, language = "zh" }: { item: LocalConversation; language?: Language }) {
  const result = item.result ?? {};
  const en = isEnglish(language);
  const fallback = en ? "The request completed, but no displayable response was returned." : "请求已完成，但服务没有返回可展示的回应。";
  const response = safeText(result.response, safeText(result.message, fallback));
  const artifacts = result.artifacts && typeof result.artifacts === "object" && !Array.isArray(result.artifacts) ? result.artifacts : {};
  const living = artifacts.living_context && typeof artifacts.living_context === "object" && !Array.isArray(artifacts.living_context) ? artifacts.living_context as Record<string, unknown> : null;
  const situation = living?.situation && typeof living.situation === "object" && !Array.isArray(living.situation) ? living.situation as Record<string, unknown> : null;
  const semantic = situation?.semantic && typeof situation.semantic === "object" && !Array.isArray(situation.semantic) ? situation.semantic as Record<string, unknown> : {};
  const needs = Array.isArray(living?.information_needs) ? living.information_needs.filter((need): need is Record<string, unknown> => Boolean(need) && typeof need === "object" && !Array.isArray(need)) : [];
  const openNeeds = needs.filter((need) => ["open", "asked", "observing", "waiting"].includes(safeText(need.status, "")));
  const livingStatus = safeText(living?.status, "");
  const semanticTitle = safeText(semantic.title, safeText(semantic.label, safeText(semantic.summary, livingStatus === "quiet" ? (en ? "No Situation changed" : "没有 Situation 变化") : (en ? "Context recorded" : "上下文已记录"))));
  const missingKnowledge = openNeeds.length ? safeText(openNeeds[0].question, safeText(openNeeds[0].blocked_judgment, en ? "More information" : "更多信息")) : (en ? "Nothing urgent" : "暂时没有紧要未知");
  const routeStatus = safeText(result.status, "—");
  const riskLevel = safeText(result.risk_level, en ? "Not assessed" : "未评估");
  return <Surface className="responseCard"><div className="responseHeader"><div><span className="responseEyebrow">{en ? "Veyra's response" : "Veyra 的回应"}</span><time>{formatTime(item.createdAt)}</time></div><span className="routeTag">{routeStatus}</span></div><p>{response}</p>{living ? <div className="responseLivingContext"><div><span>{en ? "Situation updated" : "已更新的 Situation"}</span><strong>{semanticTitle}</strong></div><div><span>{en ? "Still unknown" : "仍缺什么"}</span><strong>{missingKnowledge}</strong></div><div><span>{en ? "Why Veyra stayed quiet" : "为什么保持安静"}</span><strong>{livingStatus === "quiet" ? (en ? "The signal was not strong enough to interrupt you." : "信号还不够强，不值得打扰你。") : (en ? "The update was recorded without external delivery." : "这次更新只记录，不向外发送。")}</strong></div></div> : null}<Disclosure title={en ? "Response status" : "回应状态"}><div className="detailGrid"><div><span>{en ? "Status" : "状态"}</span><code>{safeText(result.status, "unknown")}</code></div><div><span>{en ? "Risk" : "风险"}</span><code>{riskLevel}</code></div></div><p className="smallNote">{en ? "Technical evidence remains available in Advanced." : "技术证据仍可在 Advanced 中查看。"}</p></Disclosure></Surface>;
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
