import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ArrowUpRight, Clock3, History, Info, MessageCircle, Plus, Send, ShieldCheck, Sparkles, X } from "lucide-react";
import { feedbackProductReaction, getProductConversation, HttpError, productConversationId, productConversationMessages, streamEventConversationId, streamMessage, type MessageResult, type MessageStreamEvent, type ProductContext, type ProductConversationFactInference, type ProductConversationFeedbackLabel, type ProductConversationMetadata, type ProductInputScope, type ProductScope } from "./api";
import { Disclosure, formatTime, isEnglish, Language, noticeForImagePaste, OwnerScope, safeText, sanitizeHistory, Surface } from "./shared";

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

type ConversationMessageRole = "user" | "assistant" | "proactive";

type ConversationMessage = {
  messageId: string;
  conversationId: string;
  role: ConversationMessageRole;
  text: string;
  createdAt: string;
  ownerId?: string;
  sessionId?: string;
  kind?: string;
  source?: string;
  metadata?: ProductConversationMetadata;
  result?: MessageResult;
  status?: "streaming" | "completed" | "failed";
  error?: string;
};

const HISTORY_KEY = "veyra.local-conversations.v1";
const RUNTIME_VERSION_MISMATCH = "Runtime version mismatch:";

export function conversationIdFor(item: LocalConversation): string {
  return item.conversationId || item.id;
}

function conversationScope(scope: OwnerScope): ProductScope {
  return {
    user_id: scope.userId,
    session_id: scope.sessionId,
  };
}

function recordValue(value: unknown, ...keys: string[]): unknown {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  for (const key of keys) if (record[key] !== undefined && record[key] !== null) return record[key];
  return undefined;
}

function messageRole(value: unknown): ConversationMessageRole {
  const marker = safeText(value, "assistant").toLowerCase();
  if (marker === "user") return "user";
  if (marker === "proactive") return "proactive";
  return "assistant";
}

function messageResult(value: unknown): MessageResult | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  return {
    ...record,
    ...(typeof record.status === "string" ? { status: record.status } : {}),
    ...(typeof record.response === "string" ? { response: record.response } : {}),
  } as MessageResult;
}

function serverMessageRows(payload: unknown, conversationId: string, scope: ProductScope): ConversationMessage[] {
  const rows = productConversationMessages(payload);
  const seen = new Set<string>();
  const messages: ConversationMessage[] = [];
  rows.forEach((row, index) => {
    const messageId = safeText(recordValue(row, "message_id", "id"), "").trim();
    if (!messageId || seen.has(messageId)) return;
    const ownerId = safeText(recordValue(row, "user_id", "owner_id"), "").trim();
    const sessionId = safeText(recordValue(row, "session_id"), "").trim();
    if ((ownerId && ownerId !== scope.user_id) || (sessionId && sessionId !== scope.session_id)) return;
    const kind = safeText(recordValue(row, "kind", "message_type", "type"), "").toLowerCase();
    const source = safeText(recordValue(row, "source"), "").toLowerCase();
    const proactive = kind === "proactive" && source === "living_reaction";
    const role = proactive ? "proactive" : messageRole(recordValue(row, "role"));
    const nested = recordValue(row, "result", "payload");
    const hasResultFields = recordValue(row, "response", "artifacts", "risk_level", "route", "status") !== undefined;
    const result = messageResult(nested) ?? (role !== "user" && hasResultFields ? messageResult(row) : undefined);
    const text = safeText(recordValue(row, "text", "content", "response", "message"), result ? safeText(result.response, "") : "").trim();
    if (!text) return;
    seen.add(messageId);
    messages.push({
      messageId,
      conversationId,
      role,
      text,
      createdAt: safeText(recordValue(row, "created_at", "timestamp", "updated_at"), new Date(index).toISOString()),
      ownerId: ownerId || scope.user_id,
      sessionId: sessionId || scope.session_id,
      kind,
      source,
      metadata: row.metadata,
      result,
      status: "completed",
    });
  });
  return messages.sort((a, b) => {
    const left = Date.parse(a.createdAt);
    const right = Date.parse(b.createdAt);
    if (Number.isFinite(left) && Number.isFinite(right) && left !== right) return left - right;
    return 0;
  });
}

type ConversationFeedbackState = Record<string, ProductConversationFeedbackLabel | undefined>;

const conversationFeedbackChoices: Array<{ label: ProductConversationFeedbackLabel; zh: string; en: string }> = [
  { label: "useful", zh: "有帮助", en: "Useful" },
  { label: "not_useful", zh: "没帮助", en: "Not useful" },
  { label: "too_early", zh: "太早", en: "Too early" },
  { label: "too_frequent", zh: "太频繁", en: "Too frequent" },
  { label: "resolved", zh: "已解决", en: "Resolved" },
];

function factInferenceValues(value: ProductConversationFactInference | undefined, key: "facts" | "inferences"): string[] {
  return value?.[key]?.filter((item) => typeof item === "string" && item.trim()).slice(0, 8) ?? [];
}

function FactInferenceDisclosure({ value, language }: { value?: ProductConversationFactInference; language: Language }) {
  const en = isEnglish(language);
  const facts = factInferenceValues(value, "facts");
  const inferences = factInferenceValues(value, "inferences");
  if (!facts.length && !inferences.length) return null;
  return <Disclosure title={en ? "Facts and inference" : "事实与推断"}><div className="proactiveEvidenceGrid">
    {facts.length ? <div><span>{en ? "Facts" : "事实"}</span><ul>{facts.map((item, index) => <li key={`fact-${index}`}>{item}</li>)}</ul></div> : null}
    {inferences.length ? <div><span>{en ? "Inference" : "推断"}</span><ul>{inferences.map((item, index) => <li key={`inference-${index}`}>{item}</li>)}</ul></div> : null}
  </div></Disclosure>;
}

function mergeMessages(current: ConversationMessage[], incoming: ConversationMessage[]): ConversationMessage[] {
  const byId = new Map<string, ConversationMessage>();
  incoming.forEach((message) => byId.set(message.messageId, message));
  current.forEach((message) => { if (!byId.has(message.messageId) && message.status === "streaming") byId.set(message.messageId, message); });
  return Array.from(byId.values()).sort((a, b) => {
    const left = Date.parse(a.createdAt);
    const right = Date.parse(b.createdAt);
    if (Number.isFinite(left) && Number.isFinite(right) && left !== right) return left - right;
    return 0;
  });
}

function cacheMessages(messages: ConversationMessage[], scope: OwnerScope, conversationId: string, onHistoryChange?: (items: LocalConversation[]) => void) {
  const users = messages.filter((message) => message.role === "user");
  if (!users.length) return;
  const cached = users.map((message) => {
    const nextAssistant = messages.find((candidate) => candidate.role !== "user" && Date.parse(candidate.createdAt) >= Date.parse(message.createdAt));
    return {
      id: message.messageId,
      conversationId,
      text: message.text,
      result: nextAssistant?.result,
      createdAt: message.createdAt,
      ownerId: scope.userId,
      sessionId: scope.sessionId,
      status: nextAssistant ? "completed" as const : message.status,
    } satisfies LocalConversation;
  });
  saveHistory([...readHistory(scope).filter((item) => conversationIdFor(item) !== conversationId), ...cached]);
  onHistoryChange?.(readHistory(scope));
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

function cachedConversationMessages(scope: OwnerScope, conversationId: string, language: Language): ConversationMessage[] {
  const scoped = conversationScope(scope);
  const en = isEnglish(language);
  const cached = recoverInterruptedTurns(readHistory(scope).filter((item) => conversationIdFor(item) === conversationId), en);
  return cached.flatMap((item) => {
    const user: ConversationMessage = { messageId: item.id, conversationId, role: "user", text: item.text, createdAt: item.createdAt, ownerId: scoped.user_id, sessionId: scoped.session_id, status: item.status, error: item.error };
    const assistant = item.result ? [{ messageId: `${item.id}:response`, conversationId, role: "assistant" as const, text: safeText(item.result.response, safeText(item.result.message, "")), createdAt: item.createdAt, ownerId: scoped.user_id, sessionId: scoped.session_id, result: item.result, status: "completed" as const }] : [];
    return [user, ...assistant];
  });
}

function isRuntimeVersionMismatch(error: unknown): boolean {
  return error instanceof HttpError
    && error.status === 404
    && error.message.startsWith(RUNTIME_VERSION_MISMATCH);
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
  const [pasteHint, setPasteHint] = useState<string | null>(null);
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
        : contextStatus === "reconnecting"
          ? (en ? "Reconnecting to the local product state…" : "正在重新连接本机产品状态…")
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
      <textarea value={text} onChange={(event) => { setText(event.target.value); if (pasteHint) setPasteHint(null); }} onPaste={(event) => { const notice = noticeForImagePaste(event, en); if (notice) setPasteHint(notice); }} placeholder={en ? "Tell me what you’re working on, or what I should keep in view…" : "告诉我你正在做什么，或希望我关注什么…"} aria-label={en ? "Message" : "输入消息"} rows={2} disabled={needsConnection} />
      {pasteHint ? <p className="composerPasteHint"><Info size={14} />{pasteHint}</p> : null}
      <div className="composerBar"><span className="composerHint"><ShieldCheck size={14} />{inputScope ? (en ? "Local Veyra · text only · actions remain reviewable" : "本机 Veyra · 只接受文字 · 行动始终可复核") : (en ? "Waiting for the local connection" : "等待本机连接")}</span><div className="composerActions"><button className="sendButton" type="submit" disabled={!text.trim() || !inputScope}><Send size={17} /><span>{en ? "Start conversation" : "开始对话"}</span></button></div></div>
    </form>
    <div className="promptRow" aria-label={en ? "Suggestions" : "引导问题"}>{prompts.map((prompt) => <button key={prompt} type="button" onClick={() => setText(prompt)}><Sparkles size={14} />{prompt}</button>)}</div>
    <div className="conversationFooter"><span>{en ? "A quiet place to begin · server conversation ledger" : "从这里安静地开始 · 会话由服务器账本保存"}</span><button className="textButton" type="button" onClick={onOpenHistory}><History size={14} />{en ? "View history" : "查看会话历史"}<ArrowUpRight size={13} /></button></div>
  </div>;
}

/** Dedicated conversation workspace. The landing page never renders a prior response. */
export function ChatPage({ scope, inputScope, inputStatus, language = "zh", conversationId, isNewConversation = false, historyEpoch = 0, pendingText, onPendingConsumed, onOpenHistory, onNewConversation, onBackHome, onHistoryChange, onConversationCreated }: { scope: OwnerScope; inputScope?: ProductInputScope | null; inputStatus?: string; language?: Language; conversationId: string; isNewConversation?: boolean; historyEpoch?: number; pendingText?: string | null; onPendingConsumed?: () => void; onOpenHistory?: () => void; onNewConversation: () => void; onBackHome?: () => void; onHistoryChange?: (items: LocalConversation[]) => void; onConversationCreated?: (conversationId: string) => void }) {
  const en = isEnglish(language);
  const [messages, setMessages] = useState<ConversationMessage[]>(() => []);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [activePhase, setActivePhase] = useState<string | undefined>();
  const [error, setError] = useState<string | null>(null);
  const [pasteHint, setPasteHint] = useState<string | null>(null);
  const [ledgerState, setLedgerState] = useState<"loading" | "ready" | "expired" | "error">(isNewConversation ? "ready" : "loading");
  const [feedbackByMessage, setFeedbackByMessage] = useState<ConversationFeedbackState>({});
  const [serverConversationId, setServerConversationId] = useState(isNewConversation ? "" : conversationId);
  const messagesRef = useRef<ConversationMessage[]>([]);
  const serverConversationIdRef = useRef(serverConversationId);
  const startedPending = useRef<string | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const conversationRequestSeq = useRef(0);
  const sessionLinkRequired = inputStatus === "needs_session_link";
  const scoped = conversationScope(scope);

  const setMessageState = (next: ConversationMessage[] | ((current: ConversationMessage[]) => ConversationMessage[])) => {
    setMessages((current) => {
      const value = typeof next === "function" ? next(current) : next;
      messagesRef.current = value;
      return value;
    });
  };

  const refreshConversation = async () => {
    const id = serverConversationIdRef.current;
    if (!id) return;
    requestRef.current?.abort();
    const requestSeq = ++conversationRequestSeq.current;
    const controller = new AbortController();
    requestRef.current = controller;
    try {
      const payload = await getProductConversation(id, scoped, { signal: controller.signal });
      if (controller.signal.aborted || requestSeq !== conversationRequestSeq.current) return;
      const envelopeId = productConversationId(payload.conversation) || productConversationId(payload);
      if (envelopeId && envelopeId !== id) throw new Error(en ? "This conversation belongs to another scope." : "这个会话不属于当前 owner/session。");
      const envelope = payload.conversation ?? payload;
      const envelopeOwner = safeText(recordValue(envelope, "user_id", "owner_id"), "").trim();
      const envelopeSession = safeText(recordValue(envelope, "session_id"), "").trim();
      if ((envelopeOwner && envelopeOwner !== scoped.user_id) || (envelopeSession && envelopeSession !== scoped.session_id)) throw new Error(en ? "This conversation belongs to another scope." : "这个会话不属于当前 owner/session。");
      const serverMessages = serverMessageRows(payload, id, scoped);
      setMessageState((current) => mergeMessages(current, serverMessages));
      cacheMessages(serverMessages, scope, id, onHistoryChange);
      setLedgerState("ready");
      setError(null);
    } catch (caught) {
      if (controller.signal.aborted || requestSeq !== conversationRequestSeq.current) return;
      // A generic FastAPI 404 means this frontend and backend are different
      // revisions, not that this particular conversation has expired. Keep
      // the scoped browser recovery visible while showing a bounded warning.
      if (isRuntimeVersionMismatch(caught)) {
        setMessageState((current) => current.length ? current : cachedConversationMessages(scope, id, language));
        setLedgerState("error");
        setError(en ? "The local Veyra frontend and backend are on different versions. Cached conversation history is shown when available." : "本机 Veyra 前端与后端版本不匹配；如有可用内容，正在显示本机缓存的会话记录。");
        return;
      }
      if (caught instanceof HttpError && caught.status === 404) {
        setMessageState([]);
        setLedgerState("expired");
        setError(null);
        if (typeof window !== "undefined") window.dispatchEvent(new Event("veyra:refresh-conversations"));
        return;
      }
      const message = caught instanceof Error ? caught.message : (en ? "Conversation could not be loaded." : "会话暂时无法加载。");
      setLedgerState("error");
      setError(message);
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
    }
  };

  useEffect(() => {
    requestRef.current?.abort();
    if (isNewConversation) {
      serverConversationIdRef.current = "";
      setServerConversationId("");
      setMessageState([]);
      setLedgerState("ready");
      setBusy(false); setActivePhase(undefined); setError(null); setPasteHint(null); setFeedbackByMessage({}); startedPending.current = null;
      return () => requestRef.current?.abort();
    }
    // The summary list is intentionally not consulted for existence: it is a
    // history projection and may be paginated, delayed, or unavailable.  The
    // exact owner/session GET below is the sole authority for valid vs 404.
    serverConversationIdRef.current = conversationId;
    setServerConversationId(conversationId);
    // The exact server ledger remains authoritative.  This only keeps a
    // readable scoped migration/offline copy visible until that read settles.
    setMessageState(cachedConversationMessages(scope, conversationId, language));
    setLedgerState("loading");
    void refreshConversation();
    return () => requestRef.current?.abort();
    // The refresh function intentionally reads refs so route changes cannot
    // apply an old owner/session response to the current conversation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope.userId, scope.sessionId, inputScope?.user_id, inputScope?.session_id, conversationId, isNewConversation]);

  useEffect(() => {
    if (!serverConversationId || ledgerState === "expired") return;
    const refresh = () => void refreshConversation();
    const timer = window.setInterval(refresh, 12_000);
    window.addEventListener("focus", refresh);
    window.addEventListener("veyra:refresh-conversations", refresh);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", refresh); window.removeEventListener("veyra:refresh-conversations", refresh); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverConversationId, scope.userId, scope.sessionId, ledgerState]);

  useEffect(() => {
    if (historyEpoch > 0) void refreshConversation();
    // Local history clearing never clears server messages.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyEpoch]);

  const submitText = async (raw: string) => {
    const value = raw.trim();
    if (!value || busy) return;
    if (!inputScope) {
      setError(en ? "Veyra is waiting for a local session link." : "Veyra 正在等待本机 session link。");
      return;
    }
    setDraft(""); setBusy(true); setError(null); setPasteHint(null); setActivePhase("accepted");
    const optimisticId = newId();
    let activeId = serverConversationIdRef.current;
    const optimisticConversationId = activeId || conversationId;
    const bindConversation = (nextId: string) => {
      const id = nextId.trim();
      if (!id || id === activeId) return;
      activeId = id;
      serverConversationIdRef.current = id;
      setServerConversationId(id);
      setMessageState((current) => current.map((message) => ({ ...message, conversationId: id })));
      cacheMessages(messagesRef.current, scope, id, onHistoryChange);
      onConversationCreated?.(id);
    };
    try {
      const userMessage: ConversationMessage = { messageId: optimisticId, conversationId: optimisticConversationId, role: "user", text: value, createdAt: new Date().toISOString(), ownerId: scoped.user_id, sessionId: scoped.session_id, status: "streaming" };
      setMessageState((current) => mergeMessages(current, [userMessage]));
      cacheMessages([...messagesRef.current, userMessage], scope, optimisticConversationId, onHistoryChange);
      const result = await streamMessage(value, inputScope.user_id, inputScope.session_id, optimisticId, (event) => {
        const phase = eventPhase(event);
        if (phase) setActivePhase(phase);
        const admitted = streamEventConversationId(event);
        if (admitted) bindConversation(admitted);
      }, inputScope.channel, activeId || undefined);
      const canonicalConversationId = safeText(result.conversation_id, "").trim();
      if (canonicalConversationId) bindConversation(canonicalConversationId);
      const assistantText = safeText(result.response, safeText(result.message, ""));
      const assistantId = safeText(result.message_id, safeText(result.event_id, "")) || newId();
      setMessageState((current) => {
        const completed = current.map((message) => message.messageId === optimisticId ? { ...message, conversationId: activeId || optimisticConversationId, status: "completed" as const } : message);
        return assistantText ? mergeMessages(completed, [{ messageId: assistantId, conversationId: activeId || optimisticConversationId, role: "assistant", text: assistantText, createdAt: new Date().toISOString(), ownerId: scoped.user_id, sessionId: scoped.session_id, result, status: "completed" }]) : completed;
      });
      cacheMessages(messagesRef.current, scope, activeId || optimisticConversationId, onHistoryChange);
      if (typeof window !== "undefined") {
        window.dispatchEvent(new Event("veyra:refresh-product"));
        window.dispatchEvent(new Event("veyra:refresh-conversations"));
      }
      if (activeId) void refreshConversation();
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : (en ? "Veyra could not finish this request" : "Veyra 暂时没有完成这个请求");
      setError(message);
      setMessageState((current) => current.map((item) => item.messageId === optimisticId ? { ...item, status: "failed" as const, error: message } : item));
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

  const handleFeedback = (messageId: string, label: ProductConversationFeedbackLabel, updatedMessage?: ConversationMessage) => {
    setFeedbackByMessage((current) => ({ ...current, [messageId]: label }));
    if (updatedMessage) {
      setMessageState((current) => current.map((item) => item.messageId === messageId ? { ...item, ...updatedMessage, metadata: updatedMessage.metadata ?? item.metadata, status: "completed" as const } : item));
    } else {
      void refreshConversation();
    }
    if (typeof window !== "undefined") {
      window.dispatchEvent(new Event("veyra:refresh-product"));
      window.dispatchEvent(new Event("veyra:refresh-conversations"));
    }
  };

  const conversationLoading = !isNewConversation && ledgerState === "loading";
  const title = messages.find((message) => message.role === "user")?.text || (conversationLoading ? (en ? "Loading conversation…" : "正在加载会话…") : (en ? "New conversation" : "新会话"));
  return <div className="chatPage">
    <div className="chatToolbar"><div><span className="eyebrow">{en ? "CONVERSATION" : "对话"}</span><h1>{title.length > 52 ? `${title.slice(0, 52)}…` : title}</h1><p>{en ? "Veyra keeps the conversation readable; execution details stay tucked below each verified response." : "Veyra 会保持对话清晰，执行详情收在每条已验证回应下方。"}</p></div><div className="chatToolbarActions"><button className="ghostButton" type="button" onClick={onNewConversation}><Plus size={15} />{en ? "New conversation" : "新会话"}</button><button className="textButton" type="button" onClick={onOpenHistory}><History size={15} />{en ? "History" : "历史"}</button></div></div>
    {conversationLoading ? <div className="conversationNotice chatLoadError"><Clock3 size={16} className="spinIcon" /><span>{en ? "Loading the server conversation…" : "正在加载服务器会话…"}</span></div> : null}
    {ledgerState === "expired" ? <div className="conversationError chatLoadError"><Info size={16} /><span>{en ? "This old conversation is no longer available. Open History or start a new conversation." : "旧会话已失效，请打开历史或新建会话。"}</span></div> : null}
    {error && ledgerState !== "expired" && !conversationLoading ? <div className="conversationError chatLoadError"><Info size={16} /><span>{error}</span></div> : null}
    {pasteHint ? <div className="conversationError chatLoadError"><Info size={16} /><span>{pasteHint}</span></div> : null}
    <div className="chatThread" aria-live="polite">
      {!conversationLoading && ledgerState === "ready" && !messages.length && !busy ? <div className="chatEmpty"><MessageCircle size={21} /><p>{en ? "This is a new conversation. What should Veyra understand first?" : "这是一个新会话。你希望 Veyra 先了解什么？"}</p></div> : null}
      {messages.map((message) => <ConversationMessageView key={message.messageId} message={message} language={language} scope={scoped} feedbackLabel={feedbackByMessage[message.messageId]} onFeedback={(label, updatedMessage) => handleFeedback(message.messageId, label, updatedMessage)} onRefresh={refreshConversation} />)}
      {busy ? <div className="streamStatus"><Clock3 size={16} className="spinIcon" /><span>{phaseText(activePhase, en)}</span><small>{en ? "Live lifecycle event · no simulated typing" : "真实生命周期事件 · 不模拟逐字输出"}</small></div> : null}
    </div>
    <form className="composer chatComposer" onSubmit={(event) => { event.preventDefault(); void submitText(draft); }}><textarea value={draft} onChange={(event) => { setDraft(event.target.value); if (pasteHint) setPasteHint(null); }} onPaste={(event) => { const notice = noticeForImagePaste(event, en); if (notice) setPasteHint(notice); }} placeholder={sessionLinkRequired ? (en ? "Waiting for the local session link…" : "等待本地 session link…") : (en ? "Continue the conversation…" : "继续告诉 Veyra…")} aria-label={en ? "Continue the conversation" : "继续对话"} rows={2} disabled={busy || !inputScope || sessionLinkRequired} /><div className="composerBar"><span className="composerHint"><ShieldCheck size={14} />{sessionLinkRequired ? (en ? "Input paused · local session link required" : "输入已暂停 · 需要本地 session link") : inputScope ? (en ? "Local Veyra · text only · actions remain reviewable" : "本机 Veyra · 只接受文字 · 行动始终可复核") : (en ? "Waiting for the local connection" : "等待本机连接")}</span><div className="composerActions"><button className="sendButton" type="submit" disabled={busy || !draft.trim() || !inputScope || sessionLinkRequired}>{busy ? <Clock3 size={17} className="spinIcon" /> : <Send size={17} />}<span>{busy ? (en ? "Working…" : "处理中…") : (en ? "Send" : "发送")}</span></button></div></div></form>
    <div className="chatFooter"><button className="textButton" type="button" onClick={onBackHome ?? onNewConversation}><ArrowLeft size={14} />{en ? "Back to Home" : "回到首页"}</button><span>{en ? "Server conversation ledger · local browser cache is fallback only" : "服务器会话账本为准 · 本机缓存仅作回退"}</span></div>
  </div>;
}

function ConversationMessageView({ message, language, scope, feedbackLabel, onFeedback, onRefresh }: { message: ConversationMessage; language: Language; scope: ProductScope; feedbackLabel?: ProductConversationFeedbackLabel; onFeedback: (label: ProductConversationFeedbackLabel, updatedMessage?: ConversationMessage) => void; onRefresh?: () => void }) {
  const en = isEnglish(language);
  if (message.role === "user") return <article className="chatTurn"><div className="userBubble"><span className="messageLabel">{en ? "You" : "你"}</span><p>{message.text}</p></div>{message.status === "failed" ? <div className="conversationError turnError"><Info size={16} /><span>{message.error || (en ? "This turn did not complete." : "这一轮没有完成。")}</span></div> : message.status === "streaming" ? <div className="turnPending"><Clock3 size={15} className="spinIcon" />{en ? "Working…" : "处理中…"}</div> : null}</article>;
  const proactive = message.role === "proactive";
  const metadata = message.metadata;
  const hypothesis = proactive && metadata?.epistemic_status === "hypothesis";
  const serverFeedbackLabel = metadata?.feedback_label;
  const selectedFeedback = feedbackLabel ?? serverFeedbackLabel;
  return <article className={`chatTurn assistantTurn ${proactive ? "proactiveTurn" : ""}`}><div className="assistantMessage"><span className="messageLabel">{proactive ? (en ? "Veyra · proactive" : "Veyra · 主动关注") : (en ? "Veyra" : "Veyra")}</span>{message.result ? <ResponseCard result={message.result} createdAt={message.createdAt} language={language} /> : <p>{message.text}</p>}{hypothesis ? <div className="proactiveHypothesis">{en ? "Veyra noticed a possible change" : "Veyra 发现的可能变化"}</div> : null}{proactive ? <FactInferenceDisclosure value={metadata?.fact_vs_inference} language={language} /> : null}{proactive ? <ProactiveFeedback message={message} language={language} scope={scope} selected={selectedFeedback} onFeedback={onFeedback} onRefresh={onRefresh} /> : null}</div></article>;
}

function feedbackResponseMessage(payload: unknown, current: ConversationMessage, scope: ProductScope): ConversationMessage | undefined {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return undefined;
  const root = payload as Record<string, unknown>;
  const candidates: unknown[] = [];
  const addMessage = (value: unknown) => { if (value && typeof value === "object" && !Array.isArray(value)) candidates.push(value); };
  const updated = root.updated;
  if (updated && typeof updated === "object" && !Array.isArray(updated)) addMessage((updated as Record<string, unknown>).message);
  addMessage(root.message);
  const conversation = root.conversation;
  if (conversation && typeof conversation === "object" && !Array.isArray(conversation)) {
    const conversationRecord = conversation as Record<string, unknown>;
    addMessage(conversationRecord.message);
    if (Array.isArray(conversationRecord.messages)) conversationRecord.messages.forEach(addMessage);
  }
  if (Array.isArray(root.messages)) root.messages.forEach(addMessage);
  for (const candidate of candidates) {
    const normalized = serverMessageRows({ messages: [candidate] }, current.conversationId, scope)[0];
    if (normalized && (normalized.messageId === current.messageId || normalized.metadata?.reaction_id === current.metadata?.reaction_id)) return normalized;
  }
  return undefined;
}

function ProactiveFeedback({ message, language, scope, selected, onFeedback, onRefresh }: { message: ConversationMessage; language: Language; scope: ProductScope; selected?: ProductConversationFeedbackLabel; onFeedback: (label: ProductConversationFeedbackLabel, updatedMessage?: ConversationMessage) => void; onRefresh?: () => void }) {
  const en = isEnglish(language);
  const metadata = message.metadata;
  const reactionId = metadata?.reaction_id?.trim() ?? "";
  const situationId = metadata?.situation_id?.trim() ?? "";
  const revision = metadata?.situation_revision;
  const validRevision = typeof revision === "number" && Number.isSafeInteger(revision) && revision >= 1 ? revision : null;
  const [unavailable, setUnavailable] = useState(false);
  const available = message.kind === "proactive"
    && message.source === "living_reaction"
    && metadata?.feedback_available === true
    && Boolean(reactionId && situationId)
    && validRevision !== null
    && !selected
    && !unavailable;
  const choice = conversationFeedbackChoices.find((item) => item.label === selected);
  // A closed control is only an "already recorded" result when a bounded
  // label proves it. A stale/missing reaction has no such proof.
  const feedbackRecorded = Boolean(choice);
  const feedbackClosed = metadata?.feedback_available === false || feedbackRecorded || unavailable;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const send = async (label: ProductConversationFeedbackLabel) => {
    if (busy || selected || validRevision === null) return;
    setBusy(true); setError(null);
    try {
      // The current product endpoint binds owner/session through the scoped
      // query, reaction through the path, and the Situation CAS through
      // situation_revision.  Category and learning semantics remain
      // server-owned; this control submits only the user's choice.
      const result = await feedbackProductReaction(reactionId, scope, {
        label,
        situation_revision: validRevision,
      });
      const status = safeText(result.status, "recorded").toLowerCase();
      if (["failed", "error", "unavailable"].includes(status)) throw new Error(en ? "Feedback is not available for this message." : "这条消息暂时不能记录反馈。");
      onFeedback(label, feedbackResponseMessage(result, message, scope));
    } catch (caught) {
      if (caught instanceof HttpError && (caught.status === 404 || caught.status === 409)) {
        setUnavailable(true);
        onRefresh?.();
        setError(en ? "Feedback is no longer available for this message." : "这条消息的反馈入口已不可用。");
      } else {
        setError(safeText(caught instanceof Error ? caught.message : "", en ? "Feedback could not be saved. Try again." : "反馈未保存，请稍后重试。", 240));
      }
    } finally {
      setBusy(false);
    }
  };
  if (!available && !feedbackClosed) return null;
  return <div className="proactiveFeedback" aria-label={en ? "Feedback on this proactive message" : "对这条主动消息的反馈"}>
    {feedbackRecorded && choice ? <span className="proactiveFeedbackSaved">{en ? `Feedback saved: ${choice.en}` : `已反馈：${choice.zh}`}</span> : feedbackClosed ? <span className="proactiveFeedbackSaved">{en ? "Feedback is unavailable for this message" : "这条消息暂时无法反馈"}</span> : <><span className="proactiveFeedbackPrompt">{en ? "Was this useful?" : "这条提醒有帮助吗？"}</span><div className="proactiveFeedbackButtons">{conversationFeedbackChoices.map((item) => <button key={item.label} type="button" disabled={busy} onClick={() => void send(item.label)}>{en ? item.en : item.zh}</button>)}</div></>}
    {error ? <span className="proactiveFeedbackError" role="status">{error}</span> : null}
  </div>;
}

function ResponseCard({ result, createdAt, language = "zh" }: { result: MessageResult; createdAt: string; language?: Language }) {
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
  return <Surface className="responseCard"><div className="responseHeader"><div><span className="responseEyebrow">{en ? "Veyra's response" : "Veyra 的回应"}</span><time>{formatTime(createdAt)}</time></div><span className="routeTag">{routeStatus}</span></div><p>{response}</p>{living ? <div className="responseLivingContext"><div><span>{en ? "Situation updated" : "已更新的 Situation"}</span><strong>{semanticTitle}</strong></div><div><span>{en ? "Still unknown" : "仍缺什么"}</span><strong>{missingKnowledge}</strong></div><div><span>{en ? "Why Veyra stayed quiet" : "为什么保持安静"}</span><strong>{livingStatus === "quiet" ? (en ? "The signal was not strong enough to interrupt you." : "信号还不够强，不值得打扰你。") : (en ? "The update was recorded without external delivery." : "这次更新只记录，不向外发送。")}</strong></div></div> : null}<Disclosure title={en ? "Response status" : "回应状态"}><div className="detailGrid"><div><span>{en ? "Status" : "状态"}</span><code>{safeText(result.status, "unknown")}</code></div><div><span>{en ? "Risk" : "风险"}</span><code>{riskLevel}</code></div></div><p className="smallNote">{en ? "Technical evidence remains available in Advanced." : "技术证据仍可在 Advanced 中查看。"}</p></Disclosure></Surface>;
}

export function HistoryDrawer({ open, onClose, items, onSelect, language = "zh", onClear, serverBacked = false }: { open: boolean; onClose: () => void; items: LocalConversation[]; onSelect?: (item: LocalConversation) => void; language?: Language; onClear?: () => void; serverBacked?: boolean }) {
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
  return <div className="drawerBackdrop" role="presentation" onClick={onClose}><aside className="historyDrawer" role="dialog" aria-modal="true" aria-label={en ? "Conversation history" : "会话历史"} onClick={(event) => event.stopPropagation()}><div className="drawerHeader"><div><span className="eyebrow">{serverBacked ? (en ? "Server ledger" : "服务器账本") : (en ? "Offline cache" : "离线缓存")}</span><h2>{en ? "History" : "会话历史"}</h2></div><button className="iconButton" onClick={onClose} aria-label={en ? "Close history" : "关闭历史"}><X size={18} /></button></div>{summaries.length ? <div className="historyList">{summaries.map((item) => <button className="historyItem" key={conversationIdFor(item)} onClick={() => { onSelect?.(item); onClose(); }}><MessageCircle size={16} /><span><strong>{item.text}</strong><small>{formatTime(item.createdAt)} · {item.result ? (en ? "Responded" : "已回应") : item.status === "streaming" ? (en ? "In progress" : "进行中") : (en ? "Server conversation" : "服务器会话")}</small></span></button>)}</div> : <div className="emptyState"><History size={17} />{serverBacked ? (en ? "No server conversations yet." : "服务器上还没有会话。") : (en ? "No cached conversations yet." : "这里还没有离线缓存会话。")}</div>}<div className="drawerFooter"><Info size={14} />{serverBacked ? (en ? "Server ledger is authoritative; browser cache is fallback only" : "服务器账本为准；浏览器缓存仅作回退") : (en ? "Offline cache only; server will be used when available" : "仅为离线缓存；服务可用后以服务器为准")}{summaries.length && !serverBacked ? <button className="textButton clearHistory" onClick={() => { if (window.confirm(en ? "Clear local conversation history?" : "清除本地会话历史？")) onClear?.(); }}>{en ? "Clear" : "清除"}</button> : null}</div></aside></div>;
}

export function NewConversationButton({ onClick }: { onClick: () => void }) { return <button type="button" className="newConversation" onClick={onClick}><Plus size={15} />新会话</button>; }
