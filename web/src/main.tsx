import { lazy, StrictMode, Suspense, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrainCircuit, CalendarClock, ChevronLeft, ChevronRight, History, MessageSquare, Settings, Sparkles } from "lucide-react";
import { ChatPage, conversationIdFor, HistoryDrawer, HomePage, type LocalConversation } from "./conversation";
import { ProductHome } from "./product_home";
import { SituationDetailPage, SituationsPage } from "./product_situations";
import { Settings as SettingsPage, type Language, type Theme } from "./settings";
import { Status } from "./status";
import { isEnglish, OwnerScope, sanitizeHistory } from "./shared";
import { SetupWizard } from "./SetupWizard";
import { getProductContext, getSetupStatus, type MessageResult, fetchJson, type JsonValue, type ProductContext, type ProductInputScope } from "./api";
import "./styles.css";
import "./first-meeting.css";
import "./product.css";

type NavRoute = "home" | "today" | "situations" | "matters" | "status" | "settings" | "advanced";
type Route = { kind: NavRoute } | { kind: "chat"; id: string } | { kind: "situation"; id: string };
const LegacyConsole = lazy(() => import("./LegacyConsole").then((module) => ({ default: module.LegacyConsole })));

function stableChatEntryId(): string {
  const key = "veyra.chat-entry.v1";
  try {
    const existing = window.sessionStorage.getItem(key);
    if (existing) return existing;
    const id = newId();
    window.sessionStorage.setItem(key, id);
    return id;
  } catch {
    // Private browsing can deny storage; a route-local id still keeps the
    // dedicated Chat surface usable.
    return newId();
  }
}

function getRoute(): Route {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const head = parts[0] ?? "";
  if ((head === "situations" || head === "matters") && parts[1]) return { kind: "situation", id: decodeRoutePart(parts[1]) };
  if (head === "chat") return { kind: "chat", id: parts[1] ? decodeRoutePart(parts[1]) : stableChatEntryId() };
  if (["today", "situations", "matters", "status", "settings", "advanced"].includes(head)) return { kind: head as NavRoute };
  return { kind: "home" };
}

function decodeRoutePart(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function newId(): string {
  return typeof crypto?.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function cleanLocalHistory(value: unknown, scope: OwnerScope): LocalConversation[] {
  return sanitizeHistory(value).filter((item) => item.ownerId === scope.userId && item.sessionId === scope.sessionId) as LocalConversation[];
}

function AppShell() {
  const [route, setRoute] = useState<Route>(() => getRoute());
  const [scope, setScope] = useState<OwnerScope>({ userId: "local-user", sessionId: "local-session" });
  const [productContext, setProductContext] = useState<ProductContext | null>(null);
  const [inputScope, setInputScope] = useState<ProductInputScope | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<LocalConversation[]>([]);
  const [historyEpoch, setHistoryEpoch] = useState(0);
  const [pendingChat, setPendingChat] = useState<{ id: string; text: string } | null>(null);
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem("veyra.theme") as Theme) || "system");
  const [language, setLanguage] = useState<Language>(() => (localStorage.getItem("veyra.language") as Language) || "system");
  const [setupOpen, setSetupOpen] = useState(false);
  const [setupStatus, setSetupStatus] = useState<Record<string, JsonValue> | null>(null);
  const [agentStatus, setAgentStatus] = useState<Record<string, JsonValue> | null>(null);
  const [coreModelStatus, setCoreModelStatus] = useState<Record<string, JsonValue> | null>(null);
  const setupLoaded = useRef(false);
  const productContextRef = useRef<ProductContext | null>(null);
  const productContextRequest = useRef(false);
  const productContextRetries = useRef(0);

  useEffect(() => { const onHash = () => setRoute(getRoute()); window.addEventListener("hashchange", onHash); return () => window.removeEventListener("hashchange", onHash); }, []);
  useEffect(() => { localStorage.setItem("veyra.theme", theme); document.documentElement.dataset.theme = theme; }, [theme]);
  useEffect(() => { localStorage.setItem("veyra.language", language); document.documentElement.lang = isEnglish(language) ? "en" : "zh-CN"; }, [language]);
  useEffect(() => { try { const value = JSON.parse(localStorage.getItem("veyra.local-conversations.v1") ?? "[]"); const cleaned = cleanLocalHistory(value, scope); setHistory(cleaned); localStorage.setItem("veyra.local-conversations.v1", JSON.stringify(sanitizeHistory(value).slice(0, 60))); } catch { /* optional local history */ } }, [scope.userId, scope.sessionId]);
  useEffect(() => {
    let cancelled = false;
    const activeController = { current: null as AbortController | null };
    const loadProductContext = async () => {
      if (cancelled || productContextRequest.current) return;
      productContextRequest.current = true;
      const controller = new AbortController();
      activeController.current = controller;
      const timeout = window.setTimeout(() => controller.abort(), 8000);
      try {
        const value = await getProductContext({ signal: controller.signal });
        if (cancelled) return;
        productContextRetries.current = 0;
        productContextRef.current = value;
        setProductContext(value);
        const internal = value.internal_read_scope;
        const external = value.external_input_scope;
        if (internal && typeof internal.user_id === "string" && internal.user_id.trim() && typeof internal.session_id === "string" && internal.session_id.trim()) setScope({ userId: internal.user_id, sessionId: internal.session_id });
        const inputAllowed = value.status === "ready" || value.status === "empty";
        setInputScope(inputAllowed && external && typeof external.user_id === "string" && typeof external.session_id === "string" && typeof external.channel === "string" ? external : null);
      } catch {
        if (!cancelled) {
          productContextRetries.current += 1;
          const unavailable: ProductContext = { status: "unavailable", reason: "product_context_unavailable" };
          productContextRef.current = unavailable;
          setProductContext(unavailable);
          setInputScope(null);
        }
      } finally {
        window.clearTimeout(timeout);
        if (activeController.current === controller) activeController.current = null;
        productContextRequest.current = false;
      }
    };
    void loadProductContext();
    const retry = window.setInterval(() => {
      const current = productContextRef.current;
      if (!cancelled && productContextRetries.current < 12 && (!current || current.status === "unavailable")) void loadProductContext();
    }, 5000);
    return () => { cancelled = true; activeController.current?.abort(); window.clearInterval(retry); };
  }, []);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const value = await getSetupStatus();
        if (cancelled) return;
        setSetupStatus(value);
        setupLoaded.current = true;
        const wizard = value.wizard && typeof value.wizard === "object" && !Array.isArray(value.wizard) ? value.wizard as Record<string, JsonValue> : {};
        if (wizard.should_show === true && wizard.completed !== true) setSetupOpen(true);
      } catch { /* a cold-start sidecar can be unavailable; Setup remains available in Settings */ }
      void fetchJson<Record<string, JsonValue>>("/agent/status").then((value) => { if (!cancelled) setAgentStatus(value); }).catch(() => undefined);
      void fetchJson<Record<string, JsonValue>>("/core/model/status").then((value) => { if (!cancelled) setCoreModelStatus(value); }).catch(() => undefined);
    };
    void load();
    const retry = window.setInterval(() => { if (!cancelled && !setupLoaded.current) void load(); }, 5000);
    return () => { cancelled = true; window.clearInterval(retry); };
  }, []);

  const openRoute = (next: NavRoute) => { window.location.hash = next === "home" ? "#/" : `#/${next}`; setRoute({ kind: next }); };
  const openChat = (id: string) => { window.location.hash = `#/chat/${encodeURIComponent(id)}`; setRoute({ kind: "chat", id }); };
  const startChat = (text: string) => { const id = newId(); setPendingChat({ id, text }); openChat(id); };
  const en = isEnglish(language);
  const activeNav: NavRoute | "chat" = route.kind === "chat" ? "chat" : route.kind === "situation" ? "situations" : route.kind === "matters" ? "situations" : route.kind;
  const navItems: Array<{ id: NavRoute | "chat"; label: string }> = [
    { id: "home", label: en ? "Home" : "首页" },
    { id: "today", label: en ? "Today" : "今天" },
    { id: "situations", label: "Situations" },
    { id: "chat", label: en ? "Chat" : "对话" },
    { id: "status", label: en ? "Status" : "状态" },
    { id: "settings", label: en ? "Settings" : "设置" },
  ];
  const navHref = (id: NavRoute | "chat") => id === "home" ? "#/" : `#/${id}`;
  const body = route.kind === "home" ? <HomePage scope={scope} productContext={productContext} inputScope={inputScope} language={language} onStartChat={startChat} onOpenHistory={() => setHistoryOpen(true)} />
    : route.kind === "today" ? <ProductHome scope={scope} productContext={productContext} inputScope={inputScope} language={language} onStartChat={startChat} onOpenHistory={() => setHistoryOpen(true)} />
    : route.kind === "chat" ? <ChatPage scope={scope} inputScope={inputScope} inputStatus={productContext?.status} language={language} conversationId={route.id} historyEpoch={historyEpoch} pendingText={pendingChat?.id === route.id ? pendingChat.text : null} onPendingConsumed={() => setPendingChat(null)} onOpenHistory={() => setHistoryOpen(true)} onNewConversation={() => openChat(newId())} onBackHome={() => openRoute("home")} onHistoryChange={setHistory} />
    : route.kind === "situation" ? <SituationDetailPage situationId={route.id} scope={scope} productContext={productContext} language={language} />
    : route.kind === "situations" || route.kind === "matters" ? <SituationsPage scope={scope} productContext={productContext} language={language} />
    : route.kind === "status" ? <Status scope={scope} language={language} />
    : route.kind === "settings" ? <SettingsPage scope={scope} productContext={productContext} theme={theme} language={language} onTheme={setTheme} onLanguage={setLanguage} onOpenSetup={() => setSetupOpen(true)} />
    : <div className="legacyPage"><div className="legacyHeader"><button className="textButton" onClick={() => openRoute("today")}><ChevronLeft size={15} />{en ? "Back to Today" : "返回 Today"}</button><span>Advanced · 旧控制台</span></div><Suspense fallback={<div className="loadingBlock">Loading Advanced…</div>}><LegacyConsole /></Suspense></div>;
  const iconPath = `${import.meta.env.BASE_URL}veyra-icon.png`;
  const clearCurrentHistory = () => { try { const value = JSON.parse(localStorage.getItem("veyra.local-conversations.v1") ?? "[]"); const rows = sanitizeHistory(value); localStorage.setItem("veyra.local-conversations.v1", JSON.stringify(rows.filter((item) => item.ownerId !== scope.userId || item.sessionId !== scope.sessionId))); } catch { /* optional local history */ } setHistory([]); setHistoryEpoch((value) => value + 1); };
  return <div className="veyraApp"><header className="appHeader"><a href="#/" className="brand" onClick={(event) => { event.preventDefault(); openRoute("home"); }}><span className="brandMark"><img src={iconPath} alt="" /></span><span>Veyra</span></a><nav className="desktopNav" aria-label={en ? "Main navigation" : "主导航"}>{navItems.map(({ id, label }) => <a key={id} className={activeNav === id ? "active" : ""} href={navHref(id)} onClick={(event) => { event.preventDefault(); id === "chat" ? openChat(newId()) : openRoute(id); }}>{label}</a>)}</nav><div className="headerActions"><span className="scopeHint" title="请求会携带精确 owner/session">{en ? "Local" : "本机"}</span><button className="iconButton subtle historyButton" onClick={() => setHistoryOpen(true)} aria-label={en ? "Conversation history" : "会话历史"}><History size={17} /></button></div></header><main className="appMain">{body}</main><nav className="mobileNav" aria-label={en ? "Mobile navigation" : "移动端主导航"}>{navItems.map(({ id, label }) => <a key={id} className={activeNav === id ? "active" : ""} href={navHref(id)} onClick={(event) => { event.preventDefault(); id === "chat" ? openChat(newId()) : openRoute(id); }}>{id === "home" ? <BrainCircuit size={18} /> : id === "today" ? <CalendarClock size={18} /> : id === "situations" ? <Sparkles size={18} /> : id === "chat" ? <MessageSquare size={18} /> : id === "status" ? <ChevronRight size={18} /> : <Settings size={18} />}<span>{label}</span></a>)}</nav><HistoryDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} items={history} language={language} onClear={clearCurrentHistory} onSelect={(item) => { setPendingChat(null); openChat(conversationIdFor(item)); }} /><SetupWizard open={setupOpen} onClose={() => setSetupOpen(false)} onComplete={async () => { setSetupOpen(false); setSetupStatus(await getSetupStatus()); }} setupStatus={setupStatus} coreModelStatus={coreModelStatus} agentStatus={agentStatus} /></div>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><AppShell /></StrictMode>);
