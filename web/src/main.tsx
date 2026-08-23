import { lazy, StrictMode, Suspense, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  CalendarDays,
  ChevronLeft,
  Compass,
  History,
  House,
  Languages,
  Menu,
  MessagesSquare,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Sun,
  X,
  type LucideIcon,
} from "lucide-react";
import { ChatPage, conversationIdFor, HistoryDrawer, HomePage, type LocalConversation } from "./conversation";
import { ProductHome } from "./product_home";
import { SituationDetailPage, SituationsPage } from "./product_situations";
import { Settings as SettingsPage, type Language, type Theme } from "./settings";
import { Status } from "./status";
import { isEnglish, OwnerScope, sanitizeHistory } from "./shared";
import { SetupWizard } from "./SetupWizard";
import { getProductContext, getProductConversations, getSetupStatus, productConversationId, type MessageResult, fetchJson, type JsonValue, type ProductContext, type ProductConversationSummary, type ProductInputScope } from "./api";
import "./styles.css";
import "./first-meeting.css";
import "./product.css";
import "./app-shell.css";

type NavRoute = "home" | "today" | "situations" | "matters" | "status" | "settings" | "advanced";
type Route = { kind: NavRoute } | { kind: "chat"; id: string; isNew?: boolean } | { kind: "situation"; id: string };
const LegacyConsole = lazy(() => import("./LegacyConsole").then((module) => ({ default: module.LegacyConsole })));
const NEW_CHAT_PREFIX = "new-";

function newChatRouteId(): string {
  return `${NEW_CHAT_PREFIX}${newId()}`;
}

function stableChatEntryId(): string {
  const key = "veyra.chat-entry.v1";
  try {
    const existing = window.sessionStorage.getItem(key);
    if (existing?.startsWith(NEW_CHAT_PREFIX)) return existing;
    const id = newChatRouteId();
    window.sessionStorage.setItem(key, id);
    return id;
  } catch {
    // Private browsing can deny storage; a route-local id still keeps the
    // dedicated Chat surface usable.
    return newChatRouteId();
  }
}

function getRoute(): Route {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const head = parts[0] ?? "";
  if ((head === "situations" || head === "matters") && parts[1]) return { kind: "situation", id: decodeRoutePart(parts[1]) };
  if (head === "chat") {
    const id = parts[1] ? decodeRoutePart(parts[1]) : stableChatEntryId();
    return { kind: "chat", id, isNew: id.startsWith(NEW_CHAT_PREFIX) };
  }
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

function summaryText(value: ProductConversationSummary): string {
  for (const key of ["title", "preview", "last_message", "text"]) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return "新会话";
}

function serverHistoryRows(payload: { items?: ProductConversationSummary[] }, scope: ProductInputScope): LocalConversation[] {
  return (payload.items ?? []).flatMap((row) => {
    const id = productConversationId(row);
    if (!id) return [];
    const owner = typeof row.user_id === "string" ? row.user_id : typeof row.owner_id === "string" ? row.owner_id : "";
    const session = typeof row.session_id === "string" ? row.session_id : "";
    if ((owner && owner !== scope.user_id) || (session && session !== scope.session_id)) return [];
    const updated = typeof row.updated_at === "string" ? row.updated_at : typeof row.created_at === "string" ? row.created_at : new Date().toISOString();
    return [{ id, conversationId: id, text: summaryText(row), createdAt: updated, ownerId: scope.user_id, sessionId: scope.session_id, status: "completed" as const }];
  });
}

function mergedConversationHistory(serverRows: LocalConversation[] | null, localRows: LocalConversation[]): LocalConversation[] {
  // Browser rows are migration/offline history, not an alternative authority.
  // Preserve each one until the server projects the same conversation id, then
  // let the server summary win its identity and display content.
  const byConversationId = new Map<string, LocalConversation>();
  localRows.forEach((row) => byConversationId.set(conversationIdFor(row), row));
  (serverRows ?? []).forEach((row) => byConversationId.set(conversationIdFor(row), row));
  return Array.from(byConversationId.values()).sort((left, right) => String(right.createdAt).localeCompare(String(left.createdAt)));
}

function sameScope(left: OwnerScope, right: OwnerScope): boolean {
  return left.userId === right.userId && left.sessionId === right.sessionId;
}

function storedSidebarState(): boolean {
  try {
    return window.localStorage.getItem("veyra.sidebar-collapsed.v1") === "true";
  } catch {
    return false;
  }
}

function storedPreference<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const value = window.localStorage.getItem(key) as T | null;
    return value && allowed.includes(value) ? value : fallback;
  } catch {
    return fallback;
  }
}

function productContextCopy(status: string, en: boolean): { title: string; detail: string; tone: "ready" | "waiting" | "warn" } {
  if (status === "ready") return {
    title: en ? "Living Context ready" : "生活上下文已就绪",
    detail: en ? "Exact local scope connected" : "精确本机作用域已连接",
    tone: "ready",
  };
  if (status === "empty") return {
    title: en ? "Ready for first context" : "等待第一条真实上下文",
    detail: en ? "Nothing needs attention yet" : "目前没有需要关注的事",
    tone: "ready",
  };
  if (status === "degraded") return {
    title: en ? "Context partially available" : "上下文部分可用",
    detail: en ? "Only verified evidence is shown" : "只显示通过校验的证据",
    tone: "warn",
  };
  if (status === "reconnecting" || status === "unavailable") return {
    title: en ? "Reconnecting locally" : "正在重新连接本机状态",
    detail: en ? "Input remains paused" : "输入暂时保持暂停",
    tone: "warn",
  };
  return {
    title: en ? "Reading Living Context" : "正在读取生活上下文",
    detail: en ? "Checking the local scope" : "正在确认本机作用域",
    tone: "waiting",
  };
}

function AppShell() {
  const [route, setRoute] = useState<Route>(() => getRoute());
  const [scope, setScope] = useState<OwnerScope>({ userId: "local-user", sessionId: "local-session" });
  const [productContext, setProductContext] = useState<ProductContext | null>(null);
  const [inputScope, setInputScope] = useState<ProductInputScope | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [history, setHistory] = useState<LocalConversation[]>([]);
  const [serverHistory, setServerHistory] = useState<LocalConversation[] | null>(null);
  const [serverHistoryScope, setServerHistoryScope] = useState<OwnerScope | null>(null);
  const [historyEpoch, setHistoryEpoch] = useState(0);
  const [pendingChat, setPendingChat] = useState<{ id: string; text: string } | null>(null);
  const [theme, setTheme] = useState<Theme>(() => storedPreference("veyra.theme", ["system", "light", "dark"] as const, "system"));
  const [language, setLanguage] = useState<Language>(() => storedPreference("veyra.language", ["system", "zh", "en"] as const, "system"));
  const [sidebarCollapsed, setSidebarCollapsed] = useState(storedSidebarState);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [systemDark, setSystemDark] = useState(() => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false);
  const [setupOpen, setSetupOpen] = useState(false);
  const [setupStatus, setSetupStatus] = useState<Record<string, JsonValue> | null>(null);
  const [agentStatus, setAgentStatus] = useState<Record<string, JsonValue> | null>(null);
  const [coreModelStatus, setCoreModelStatus] = useState<Record<string, JsonValue> | null>(null);
  const setupLoaded = useRef(false);
  const productContextRef = useRef<ProductContext | null>(null);
  const productContextRetries = useRef(0);
  const productContextRequest = useRef<AbortController | null>(null);
  const productContextRequestSeq = useRef(0);
  const mobileSidebarButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => { const onHash = () => setRoute(getRoute()); window.addEventListener("hashchange", onHash); return () => window.removeEventListener("hashchange", onHash); }, []);
  useEffect(() => { try { localStorage.setItem("veyra.theme", theme); } catch { /* optional UI preference */ } document.documentElement.dataset.theme = theme; }, [theme]);
  useEffect(() => { try { localStorage.setItem("veyra.language", language); } catch { /* optional UI preference */ } document.documentElement.lang = isEnglish(language) ? "en" : "zh-CN"; }, [language]);
  useEffect(() => { try { localStorage.setItem("veyra.sidebar-collapsed.v1", String(sidebarCollapsed)); } catch { /* optional UI preference */ } }, [sidebarCollapsed]);
  useEffect(() => {
    setMobileSidebarOpen(false);
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    document.querySelector<HTMLElement>(".appMain")?.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [route]);
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    setSystemDark(media.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  useEffect(() => {
    if (!mobileSidebarOpen) return;
    const previousOverflow = document.body.style.overflow;
    if (window.matchMedia("(max-width: 960px)").matches) document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setMobileSidebarOpen(false);
      window.requestAnimationFrame(() => mobileSidebarButtonRef.current?.focus());
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [mobileSidebarOpen]);
  useEffect(() => { try { const value = JSON.parse(localStorage.getItem("veyra.local-conversations.v1") ?? "[]"); const cleaned = cleanLocalHistory(value, scope); setHistory(cleaned); localStorage.setItem("veyra.local-conversations.v1", JSON.stringify(sanitizeHistory(value).slice(0, 60))); } catch { /* optional local history */ } }, [scope.userId, scope.sessionId]);
  useEffect(() => {
    let cancelled = false;
    let controller: AbortController | null = null;
    setServerHistory(null);
    setServerHistoryScope(null);
    const load = async () => {
      controller?.abort();
      const activeController = new AbortController();
      controller = activeController;
      try {
        const conversationScope = { user_id: scope.userId, session_id: scope.sessionId, channel: "api" };
        const payload = await getProductConversations(conversationScope, { signal: activeController.signal });
        if (!cancelled && !activeController.signal.aborted) {
          setServerHistory(serverHistoryRows(payload, conversationScope));
          setServerHistoryScope({ userId: scope.userId, sessionId: scope.sessionId });
        }
      } catch (caught) {
        if (!cancelled && !activeController.signal.aborted) {
          setServerHistory(null);
          setServerHistoryScope(null);
        }
      }
    };
    const refresh = () => void load();
    void load();
    const timer = window.setInterval(refresh, 12_000);
    window.addEventListener("focus", refresh);
    window.addEventListener("veyra:refresh-conversations", refresh);
    return () => { cancelled = true; controller?.abort(); window.clearInterval(timer); window.removeEventListener("focus", refresh); window.removeEventListener("veyra:refresh-conversations", refresh); };
  }, [scope.userId, scope.sessionId]);
  useEffect(() => {
    let cancelled = false;
    const loadProductContext = async (force = false) => {
      if (cancelled) return;
      if (force && productContextRequest.current) {
        productContextRequest.current.abort();
        productContextRequest.current = null;
      }
      if (productContextRequest.current) return;
      const requestSeq = ++productContextRequestSeq.current;
      const controller = new AbortController();
      productContextRequest.current = controller;
      let timedOut = false;
      const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, 8000);
      try {
        const value = await getProductContext({ signal: controller.signal });
        if (cancelled || requestSeq !== productContextRequestSeq.current || controller.signal.aborted) return;
        const contextStatus = String(value.status ?? "").toLowerCase();
        if (["ready", "empty"].includes(contextStatus)) productContextRetries.current = 0;
        else productContextRetries.current += 1;
        productContextRef.current = value;
        setProductContext(value);
        const internal = value.internal_read_scope;
        const external = value.external_input_scope;
        if (internal && typeof internal.user_id === "string" && internal.user_id.trim() && typeof internal.session_id === "string" && internal.session_id.trim()) {
          const nextScope = { userId: internal.user_id, sessionId: internal.session_id };
          setScope((current) => sameScope(current, nextScope) ? current : nextScope);
        }
        const inputAllowed = value.status === "ready" || value.status === "empty";
        setInputScope(inputAllowed && external && typeof external.user_id === "string" && external.user_id.trim() && typeof external.session_id === "string" && external.session_id.trim() && typeof external.channel === "string" && external.channel.trim() ? external : null);
      } catch (caught) {
        if (!cancelled && requestSeq === productContextRequestSeq.current && (timedOut || !controller.signal.aborted)) {
          productContextRetries.current += 1;
          const reconnecting: ProductContext = { status: "reconnecting", reason: "product_context_unavailable" };
          productContextRef.current = reconnecting;
          setProductContext(reconnecting);
          setInputScope(null);
        }
      } finally {
        window.clearTimeout(timeout);
        if (productContextRequest.current === controller) productContextRequest.current = null;
      }
    };
    void loadProductContext();
    const retry = window.setInterval(() => {
      const current = productContextRef.current;
      const status = String(current?.status ?? "loading").toLowerCase();
      if (!cancelled && productContextRetries.current < 12 && !["ready", "empty"].includes(status)) void loadProductContext();
    }, 5000);
    const refresh = () => void loadProductContext(true);
    const onVisibility = () => { if (document.visibilityState === "visible") refresh(); };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("veyra:refresh-product", refresh);
    return () => { cancelled = true; productContextRequestSeq.current += 1; productContextRequest.current?.abort(); productContextRequest.current = null; window.clearInterval(retry); window.removeEventListener("focus", refresh); document.removeEventListener("visibilitychange", onVisibility); window.removeEventListener("veyra:refresh-product", refresh); };
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
  const openChat = (id: string) => { window.location.hash = `#/chat/${encodeURIComponent(id)}`; setRoute({ kind: "chat", id, isNew: id.startsWith(NEW_CHAT_PREFIX) }); };
  const startChat = (text: string) => { const id = newChatRouteId(); setPendingChat({ id, text }); openChat(id); };
  const en = isEnglish(language);
  const effectiveDark = theme === "dark" || (theme === "system" && systemDark);
  const activeNav: NavRoute | "chat" = route.kind === "chat" ? "chat" : route.kind === "situation" ? "situations" : route.kind === "matters" ? "situations" : route.kind;
  const navItems: Array<{ id: NavRoute | "chat"; label: string; icon: LucideIcon }> = [
    { id: "home", label: en ? "Home" : "首页", icon: House },
    { id: "today", label: en ? "Today" : "今天", icon: CalendarDays },
    { id: "situations", label: en ? "Situations" : "情境", icon: Compass },
    { id: "chat", label: en ? "Conversations" : "对话", icon: MessagesSquare },
    { id: "status", label: en ? "System status" : "系统状态", icon: Activity },
    { id: "settings", label: en ? "Settings" : "偏好设置", icon: SlidersHorizontal },
  ];
  const navHref = (id: NavRoute | "chat") => id === "home" ? "#/" : `#/${id}`;
  const routeTitles: Record<NavRoute | "chat" | "situation", string> = {
    home: en ? "Home" : "首页",
    today: en ? "Today" : "今天",
    situations: en ? "Situations" : "情境",
    situation: en ? "Situation detail" : "情境详情",
    matters: en ? "Situations" : "情境",
    chat: en ? "Conversation" : "对话",
    status: en ? "System status" : "系统状态",
    settings: en ? "Settings" : "偏好设置",
    advanced: en ? "Advanced console" : "高级控制台",
  };
  const pageTitle = routeTitles[route.kind];
  const contextStatus = String(productContext?.status ?? "loading").toLowerCase();
  const contextCopy = productContextCopy(contextStatus, en);
  const serverHistoryMatchesScope = Boolean(serverHistoryScope && sameScope(serverHistoryScope, scope));
  const scopedServerHistory = serverHistoryMatchesScope ? serverHistory : null;
  const scopedLocalHistory = history.filter((item) => item.ownerId === scope.userId && item.sessionId === scope.sessionId);
  const body = route.kind === "home" ? <HomePage scope={scope} productContext={productContext} inputScope={inputScope} language={language} onStartChat={startChat} onOpenHistory={() => setHistoryOpen(true)} />
    : route.kind === "today" ? <ProductHome scope={scope} productContext={productContext} inputScope={inputScope} language={language} onStartChat={startChat} onOpenHistory={() => setHistoryOpen(true)} />
    : route.kind === "chat" ? <ChatPage scope={scope} inputScope={inputScope} inputStatus={productContext?.status} language={language} conversationId={route.id} isNewConversation={route.isNew === true} historyEpoch={historyEpoch} pendingText={pendingChat?.id === route.id ? pendingChat.text : null} onPendingConsumed={() => setPendingChat(null)} onOpenHistory={() => setHistoryOpen(true)} onNewConversation={() => openChat(newChatRouteId())} onBackHome={() => openRoute("home")} onHistoryChange={setHistory} onConversationCreated={(id) => openChat(id)} />
    : route.kind === "situation" ? <SituationDetailPage situationId={route.id} scope={scope} productContext={productContext} language={language} />
    : route.kind === "situations" || route.kind === "matters" ? <SituationsPage scope={scope} productContext={productContext} language={language} />
    : route.kind === "status" ? <Status scope={scope} language={language} />
    : route.kind === "settings" ? <SettingsPage scope={scope} productContext={productContext} theme={theme} language={language} onTheme={setTheme} onLanguage={setLanguage} onOpenSetup={() => setSetupOpen(true)} />
    : <div className="legacyPage"><div className="legacyHeader"><button className="textButton" onClick={() => openRoute("today")}><ChevronLeft size={15} />{en ? "Back to Today" : "返回 Today"}</button><span>Advanced · 旧控制台</span></div><Suspense fallback={<div className="loadingBlock">Loading Advanced…</div>}><LegacyConsole /></Suspense></div>;
  const iconPath = `${import.meta.env.BASE_URL}veyra-icon.png`;
  const clearCurrentHistory = () => { try { const value = JSON.parse(localStorage.getItem("veyra.local-conversations.v1") ?? "[]"); const rows = sanitizeHistory(value); const scopes = [{ userId: scope.userId, sessionId: scope.sessionId }, ...(inputScope ? [{ userId: inputScope.user_id, sessionId: inputScope.session_id }] : [])]; localStorage.setItem("veyra.local-conversations.v1", JSON.stringify(rows.filter((item) => !scopes.some((candidate) => item.ownerId === candidate.userId && item.sessionId === candidate.sessionId)))); } catch { /* optional local history */ } setHistory([]); setHistoryEpoch((value) => value + 1); };
  const historyItems = mergedConversationHistory(scopedServerHistory, scopedLocalHistory);
  const navigateFromShell = (id: NavRoute | "chat") => id === "chat" ? openChat(newChatRouteId()) : openRoute(id);
  const closeMobileSidebar = () => {
    setMobileSidebarOpen(false);
    window.requestAnimationFrame(() => mobileSidebarButtonRef.current?.focus());
  };
  return <div className={`veyraApp${sidebarCollapsed ? " sidebarCollapsed" : ""}${mobileSidebarOpen ? " mobileSidebarOpen" : ""}`}>
    <button className="sidebarBackdrop" type="button" aria-label={en ? "Close navigation" : "关闭导航"} onClick={closeMobileSidebar} />
    <aside className="appSidebar" id="app-navigation">
      <div className="sidebarHeader">
        <a href="#/" className="brand" onClick={(event) => { event.preventDefault(); openRoute("home"); }}>
          <span className="brandMark"><img src={iconPath} alt="" /></span>
          <span className="brandCopy"><strong>Veyra</strong><small>{en ? "Local Living Context" : "本机生活上下文"}</small></span>
        </a>
        <button className="sidebarCollapseButton" type="button" aria-controls="app-navigation" aria-expanded={mobileSidebarOpen || !sidebarCollapsed} aria-label={en ? "Toggle sidebar" : "切换侧栏"} onClick={() => {
          if (window.matchMedia("(max-width: 960px)").matches) closeMobileSidebar();
          else setSidebarCollapsed((value) => !value);
        }}><span className="desktopSidebarToggleIcon">{sidebarCollapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}</span><span className="mobileSidebarCloseIcon"><X size={17} /></span></button>
      </div>
      <div className={`sidebarContext ${contextCopy.tone}`} title={`${contextCopy.title} · ${contextCopy.detail}`}>
        <span className="sidebarContextIcon"><Sparkles size={17} /><i /></span>
        <span className="sidebarContextCopy"><small>Living Context</small><strong>{contextCopy.title}</strong><em>{contextCopy.detail}</em></span>
      </div>
      <nav className="sidebarNav" aria-label={en ? "Main navigation" : "主导航"}>
        {navItems.map(({ id, label, icon: Icon }) => <a key={id} className={activeNav === id ? "active" : ""} href={navHref(id)} title={sidebarCollapsed ? label : undefined} aria-current={activeNav === id ? "page" : undefined} onClick={(event) => { event.preventDefault(); navigateFromShell(id); }}><Icon size={19} /><span>{label}</span></a>)}
      </nav>
      <div className="sidebarFooter" title={en ? "Local-only product boundary" : "仅限本机的产品边界"}><ShieldCheck size={18} /><span><strong>{en ? "Local only" : "仅限本机"}</strong><small>{en ? "No automatic execution" : "不会自动执行"}</small></span></div>
    </aside>
    <section className="appWorkspace">
      <header className="appHeader">
        <div className="topbarTitle">
          <button ref={mobileSidebarButtonRef} className="mobileSidebarButton" type="button" aria-controls="app-navigation" aria-expanded={mobileSidebarOpen} aria-label={mobileSidebarOpen ? (en ? "Close navigation" : "关闭导航") : (en ? "Open navigation" : "打开导航")} onClick={() => setMobileSidebarOpen((value) => !value)}><Menu size={19} /></button>
          <div><span>{en ? "VEYRA · LIVING CONTEXT" : "VEYRA · 生活上下文"}</span><strong>{pageTitle}</strong></div>
        </div>
        <div className="headerActions">
          <button className="headerControl historyButton" type="button" onClick={() => setHistoryOpen(true)} aria-label={en ? "Conversation history" : "会话历史"} title={en ? "Conversation history" : "会话历史"}><History size={17} /><span>{en ? "History" : "历史"}</span></button>
          <button className="headerControl languageControl" type="button" onClick={() => setLanguage(en ? "zh" : "en")} aria-label={en ? "切换到中文" : "Switch to English"} title={en ? "切换到中文" : "Switch to English"}><Languages size={17} /><span>{en ? "EN" : "中"}</span></button>
          <button className="headerControl themeControl" type="button" onClick={() => setTheme(effectiveDark ? "light" : "dark")} aria-label={effectiveDark ? (en ? "Switch to light mode" : "切换到白昼模式") : (en ? "Switch to dark mode" : "切换到暗夜模式")} title={effectiveDark ? (en ? "Light mode" : "白昼模式") : (en ? "Dark mode" : "暗夜模式")}>{effectiveDark ? <Sun size={17} /> : <Moon size={17} />}<span>{effectiveDark ? (en ? "Light" : "白昼") : (en ? "Dark" : "暗夜")}</span></button>
        </div>
      </header>
      <main className="appMain">{body}</main>
    </section>
    <HistoryDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} items={historyItems} serverBacked={scopedServerHistory !== null} language={language} onClear={clearCurrentHistory} onSelect={(item) => { setPendingChat(null); openChat(conversationIdFor(item)); }} />
    <SetupWizard open={setupOpen} onClose={() => setSetupOpen(false)} onComplete={async () => { setSetupOpen(false); setSetupStatus(await getSetupStatus()); }} setupStatus={setupStatus} coreModelStatus={coreModelStatus} agentStatus={agentStatus} />
  </div>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><AppShell /></StrictMode>);
