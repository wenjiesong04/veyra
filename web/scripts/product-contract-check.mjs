import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runProductProactiveContract } from "./product-proactive-contract.mjs";

const root = resolve(new URL("..", import.meta.url).pathname);
const read = (name) => readFileSync(resolve(root, name), "utf8");
const files = {
  api: read("src/api.ts"),
  main: read("src/main.tsx"),
  home: read("src/product_home.tsx"),
  situations: read("src/product_situations.tsx"),
  questions: read("src/product_questions.tsx"),
  reactions: read("src/product_reactions.tsx"),
  sources: read("src/product_sources.tsx"),
  sourceConsent: read("src/source_consent.ts"),
  sourceConsentDialog: read("src/source_consent_dialog.tsx"),
  conversation: read("src/conversation.tsx"),
  shared: read("src/shared.tsx"),
  productShared: read("src/product_shared.ts"),
  matters: read("src/matters.tsx"),
  settings: read("src/settings.tsx"),
  productCss: read("src/product.css"),
  firstMeeting: read("src/first-meeting.css"),
  vite: read("vite.config.ts"),
  desktopVite: read("vite.desktop.config.ts"),
  proxy: read("vite.proxy.ts"),
};

const must = (condition, message) => { if (!condition) throw new Error(message); };
must(runProductProactiveContract() === true, "proactive Product Conversation fixture contract failed");
must(files.proxy.includes('"/product": "http://127.0.0.1:8000"'), "Vite proxy does not forward /product");
for (const config of [files.vite, files.desktopVite]) must(/\bproxy\s*:\s*apiProxy\b/.test(config), "Vite proxy contract is incomplete");
must(files.api.includes("payload.detail === \"Not Found\"") && files.api.includes("Runtime version mismatch"), "generic FastAPI 404 does not identify a runtime-version mismatch");
must(files.api.includes(": payload.detail;"), "resource-specific API 404 details are not preserved");
must(files.api.includes("class HttpError") && files.api.includes("status: number") && files.api.includes("cache: options?.cache ?? \"no-store\""), "product reads do not expose typed HTTP status or disable stale browser caching");

// Home is the quiet first-meeting surface; Today is a separate projection.
must(files.main.includes('route.kind === "home" ? <HomePage'), "root does not mount the quiet HomePage");
must(files.main.includes('route.kind === "today" ? <ProductHome'), "Today is not mounted on its own route");
must(!files.home.includes("<HomePage"), "Today still replaces itself with HomePage");
must(!files.conversation.includes("export const Conversation = HomePage"), "Conversation still aliases the Home surface");
// Navigation labels are rendered from the typed navItems table and vary by
// language; route and surface contracts below intentionally do not assert
// a particular translated JSX label string.
must(files.main.includes("stableChatEntryId"), "chat entry route is not stable without an id");
must(files.home.includes("firstMeeting: true"), "Today request does not ask for first-meeting evaluation");
must(files.home.includes("30_000"), "Today refresh interval is not 30 seconds");
must(files.home.includes('addEventListener("focus"'), "Today does not refresh on window focus");
must(files.home.includes('veyra:refresh-product'), "Today does not refresh after a completed chat or mutation");
for (const field of ["attention", "recent_changes", "deadlines", "unknowns", "suggestions"]) must(files.home.includes(field), `Today is missing ${field}`);
must(files.home.includes("today.attention") && files.home.includes("productAttentionRow"), "Today does not render the server-ranked attention projection");
for (const field of ["rank", "why_now", "material_change", "recommendation"]) must(files.home.includes(field), `Today attention explanation is missing ${field}`);
must(files.home.includes("#/situations/") && files.home.includes("Open this Situation"), "Today attention does not link to the Situation detail");
must(files.home.includes("uniqueRecords") && files.productShared.includes("uniqueRecords"), "Today does not deduplicate repeated projections by stable identity");
for (const key of ["event_id", "observation_id", "record_id", "revision", "need_id", "reaction_id", "token"]) must(files.productShared.includes(key), `semantic identity is missing ${key}`);
for (const mode of ["situation", "change", "deadline", "unknown", "question", "reaction", "waiting"]) must(files.home.includes(`mode: "${mode}"`), `Today does not select a semantic identity for ${mode}`);
must(files.situations.includes('mode: "situation"') && files.situations.includes('mode: "timeline"'), "Situation pages do not use separate summary and timeline identities");
must(files.productShared.includes("mode === \"change\" || mode === \"event\" || mode === \"timeline\"") && files.productShared.includes("withRevision"), "event/change identity does not preserve revision boundaries");
must(files.productShared.includes("deadlineParts") && files.productShared.includes("deadline_at"), "deadline identity can collapse distinct deadlines in one Situation");
must(files.productShared.includes("mode: UniqueRecordMode") && !files.productShared.includes("mode?: UniqueRecordMode"), "semantic identity mode must be explicit at every call site");
for (const field of ["why_now", "what_changed", "recommendation"]) must(files.home.includes(field) || files.reactions.includes(field), `Today explanation is missing ${field}`);
must(files.home.includes("degraded") && files.home.includes("category={freshness"), "Today does not explicitly render degraded and freshness categories");
must(files.home.includes('.filter((item) => text(item.material_change, "").trim())'), "Recent changes are not guarded by material_change");
must(files.situations.includes("projectionStatus") && files.situations.includes("category={freshnessCategory}"), "Situation list does not explicitly render degraded and freshness categories");
must(files.shared.includes("freshnessLabel") && files.shared.includes("category?: unknown"), "Freshness category renderer is missing");
must(files.productShared.includes("serverFallbackText") && files.productShared.includes("SERVER_FALLBACK_COPY"), "server fallback copy is not bounded by a shared allow-list");
for (const fallback of ["The timing has changed and is worth checking while the signal is still timely.", "An unresolved unknown may affect the current understanding."]) must(files.productShared.includes(fallback), `known server fallback is missing from the product copy allow-list: ${fallback}`);
must(files.productShared.includes("?? raw"), "unknown product copy must remain unchanged");
for (const value of ["emerging", "local-first", "local_first", "none", "disabled"]) must(files.productShared.includes(value), `bounded status label is missing ${value}`);
for (const value of ["general", "personal", "work", "education", "health", "travel", "logistics", "finance", "other"]) must(files.productShared.includes(value), `bounded category label is missing ${value}`);
must(files.productShared.includes("export function categoryLabel") && files.situations.includes("categoryLabel(item.category"), "Situation cards do not use the bounded category label");
must(files.home.includes("serverFallbackText") && files.home.includes("progressLabel"), "Today does not use bounded fallback/status presentation");
must(files.situations.includes("progressLabel") && files.situations.includes("epistemicLabel"), "Situation detail does not use friendly progress/epistemic enum labels");
must(files.conversation.includes("needs_session_link") && files.conversation.includes("disabled={needsConnection}"), "session-link state does not disable First Meeting input");
must(files.conversation.includes("sessionLinkRequired") && files.conversation.includes("!inputScope || sessionLinkRequired"), "session-link state does not disable Chat input");
must(!files.conversation.includes("ask an Agent") && !files.conversation.includes("请 Agent"), "Product copy claims an unavailable Agent capability");

// Product navigation and semantic Situation detail.
for (const value of ["today", "situations", "situation", "chat", "status", "settings", "advanced"]) must(files.main.includes(`"${value}"`), `navigation is missing ${value}`);
must((files.main.match(/productContext=\{productContext\}/g) ?? []).length >= 2, "Product Context is not passed to Situation detail and Settings");
must(files.situations.includes("productContextReadiness") && files.situations.includes("!context.ready") && files.situations.includes("requestRef.current.controller?.abort"), "Situations reads are not fenced by exact Product Context and stale-request aborts");
must(files.sources.includes("productContextReadiness") && files.sources.includes("context.ready && data") && files.sources.includes("requestRef.current.controller?.abort"), "Product Sources reads are not fenced by exact Product Context and stale-request aborts");
for (const value of ["getProductSituations", "getProductSituation", "commandProductSituation", "expected_revision", "known", "unknown", "assumptions", "timeline", "evidence_refs", "evidence", "source", "kind", "status", "epistemic", "freshness", "fresh_until", "ttl_seconds", "snippet", "next_observation_at"]) must(files.situations.includes(value), `Situation contract is missing ${value}`);
must(files.situations.includes("strings(detail.unknown") && files.situations.includes("Unknown · open questions"), "Situation detail does not render unresolved unknowns");
must(files.situations.includes("evidenceProjection") && files.situations.includes("safeEvidenceText"), "Situation evidence is not projected through the safe renderer");
must(files.main.includes("lazy(() => import(\"./LegacyConsole\")"), "Advanced console is not lazy");

// CAS-bound question and reaction feedback paths.
for (const value of ["answerProductQuestion", "deferProductQuestion", "dismissProductQuestion", "expected_generation", "onChanged"]) must(files.questions.includes(value), `Question contract is missing ${value}`);
for (const value of ["useful", "not_useful", "ignore", "resolved", "too_early", "too_late", "too_frequent", "remind_before", "remind_before_seconds", "remindButton", "feedbackProductReaction"]) must(files.reactions.includes(value), `Reaction feedback contract is missing ${value}`);
must(!files.reactions.includes("category: text(reaction.category") && !files.conversation.includes("category: metadata.category"), "feedback controls must not submit a client-owned category");
must(files.home.includes("<ProductQuestions") && files.home.includes("<ProductReactions"), "Today does not expose Questions and Suggestions");
must(files.home.includes("suggestions_boundary") && files.home.includes("other_ledger_recorded_count"), "an empty Suggestions card does not disclose the other record-only ledger");

// Sources remain status-first and honest about unsupported consent authority.
for (const value of ["getProductSources", "consentProductSource", "revokeProductSource", "expected_generation", "calendar", "weather", "public_web"]) must(files.sources.includes(value) || files.api.includes(value), `Sources contract is missing ${value}`);
must(files.sources.includes("Information source access") && files.sources.includes("信息来源授权"), "Source access card title does not state its consent boundary");
must(files.sources.includes("Configure first") && files.sources.includes("Permission denied") && files.sources.includes("aria-describedby"), "Disabled source consent lacks a direct label and accessible reason");
must(files.sources.includes("Agent research is disabled") || files.sources.includes("Agent research"), "Sources does not disclose Agent research boundary");
must(files.sources.includes("Email") && !files.sources.includes('id: "email"'), "Email must remain outside the supported source list");
for (const value of ["configured", "system_permission", "consented", "available", "can_request", "Permission denied; revoke consent"]) must(files.sources.includes(value), `Sources UI does not preserve ${value} boundary`);
must(files.sourceConsentDialog.includes('role="dialog"') && files.sourceConsentDialog.includes("aria-modal") && files.sourceConsentDialog.includes("Allow read-only") && files.sourceConsentDialog.includes("允许只读"), "Source consent is not a confirmable dialog");
must(files.sources.includes("Consent never grants execution") && files.sourceConsentDialog.includes("No execution or delivery"), "Source consent copy must keep the no-execution boundary");
must(files.sourceConsent.includes("macOS") && files.sourceConsent.includes("Calendar access"), "Calendar consent does not disclose the extra system permission");
must(files.questions.includes("SourceConsentDialog") && files.questions.includes("isReadableSource") && files.sourceConsent.includes("Let Veyra read") && files.sourceConsent.includes("让 Veyra 去看"), "Questions cannot request a governed source read from the same consent dialog");
must(files.home.includes("productContext={productContext}") && files.situations.includes("productContext={productContext}"), "Today and Situation detail do not pass Product Context into Questions");

// Browser history is sanitized and response cards use living_context artifacts.
must(files.shared.includes("sanitizeHistoryRecord") && files.shared.includes("sanitizeHistory("), "History sanitizer is not shared");
must(files.shared.includes("safeText") && files.conversation.includes("safeText"), "Conversation scalar sanitizer is not shared");
must(files.api.includes("safeText") && files.api.includes("parsed.type === \"failed\""), "Stream failure payload is not scalar-sanitized");
must(files.home.includes("AbortController") && files.home.includes("generation") && files.home.includes("controller?.abort"), "Today refresh does not fence stale responses");
must(files.conversation.includes("living_context") && files.conversation.includes("Still unknown"), "Chat response does not project living context");
must(!files.conversation.includes("JSON.stringify(result.artifacts"), "Chat response leaks raw artifacts");
must(!files.api.includes("JSON.stringify(payload.detail)"), "API error handling leaks raw server details");

// Product Conversation is a server-owned ledger. Browser history can only be
// used as a migration/offline fallback, never as the conversation identity.
for (const value of ["getProductConversations", "createProductConversation", "getProductConversation", "productConversationMessages", "/product/conversations?", "/product/conversations/${encodeURIComponent(conversationId)}"]) {
  must(files.api.includes(value), `conversation API contract is missing ${value}`);
}
must(files.api.includes("conversation_id: conversationId") && files.api.includes("conversation_id: conversationId }"), "message requests do not carry the bound conversation_id");
must(files.api.includes("user_id: scope.user_id") && files.api.includes("session_id: scope.session_id"), "conversation reads are not exact owner/session scoped");
for (const value of ["serverConversationIdRef", "streamMessage(value", "canonicalConversationId", "activeId", "messageId", "mergeMessages", "seen = new Set"]) {
  must(files.conversation.includes(value), `conversation ledger binding is missing ${value}`);
}
must(files.conversation.includes("setInterval(refresh, 12_000)"), "current conversation is not read-only refreshed every 10-15 seconds");
must(files.conversation.includes("role === \"proactive\"") && files.conversation.includes("assistantTurn") && files.conversation.includes("Veyra · proactive"), "proactive assistant messages are not rendered as assistant messages");
must(files.main.includes("getProductConversations") && files.main.includes("serverHistory") && files.main.includes("serverBacked"), "history drawer does not use server conversation summaries");
must(files.main.includes("setInterval(refresh, 12_000)") && files.main.includes("veyra:refresh-conversations"), "conversation summary list lacks read-only refresh");
must(!files.main.includes("redirect_single") && !files.main.includes("replaceChat"), "explicit chat routes must not redirect from an incomplete history list");
must(files.main.includes("scopedServerHistory") && files.main.includes("scopedLocalHistory") && files.main.includes("serverBacked={scopedServerHistory !== null}"), "history drawer is not scoped to the current owner/session");
must(files.main.includes('!["ready", "empty"].includes(status)') && files.main.includes('"veyra:refresh-product"') && files.main.includes('visibilitychange'), "Product Context does not retry bounded non-ready states or refresh when visible");
must(files.conversation.includes("cachedConversationMessages") && files.conversation.includes("streamEventConversationId") && files.conversation.includes("noticeForImagePaste"), "chat does not restore cached turns, bind admitted conversation ids, or reject image paste honestly");
must(files.conversation.includes("text only") && files.conversation.includes("只接受文字") && !files.conversation.includes("Paperclip"), "composer still pretends attachments exist");
must(files.api.includes("streamEventConversationId"), "message stream does not expose admitted conversation ids");
must(files.questions.includes("noticeForImagePaste"), "question answers do not reject image paste");
must(files.shared.includes("noticeForImagePaste") && files.shared.includes("image/"), "image paste notice is not shared");

// Proactive Product Conversation messages carry a bounded presentation
// envelope. Feedback remains on the existing CAS-bound reaction endpoint;
// raw metadata/tokens are never rendered.
for (const field of ["reaction_id", "situation_id", "situation_revision", "category", "fact_vs_inference", "feedback_available", "feedback_label", "feedback_at", "epistemic_status", "record_only"]) {
  must(files.api.includes(field), `conversation metadata is missing ${field}`);
}
must(!files.api.includes("feedback_token") && !files.api.includes("attention_candidate_id"), "conversation metadata retains raw token or duplicate candidate identity");
must(files.api.includes("normalizeProductConversationMetadata") && files.api.includes("PRODUCT_CONVERSATION_CATEGORIES"), "conversation metadata is not normalized through a bounded allow-list");
must(files.conversation.includes('message.kind === "proactive"') && files.conversation.includes('message.source === "living_reaction"'), "proactive feedback is not source-bound");
for (const label of ["useful", "not_useful", "too_early", "too_frequent", "resolved"]) must(files.conversation.includes(`label: "${label}"`), `conversation feedback is missing ${label}`);
must(files.conversation.includes("feedbackProductReaction") && files.conversation.includes("situation_revision") && files.conversation.includes("feedback_available"), "conversation feedback does not use the existing CAS-bound reaction endpoint");
must(files.conversation.includes("isRuntimeVersionMismatch") && files.conversation.includes("cachedConversationMessages(scope, id, language)") && files.conversation.includes("旧会话已失效"), "generic 404 must retain cached conversation while resource 404 remains bounded");
// A direct chat route must survive a failed/truncated summary list.  The exact
// owner/session GET owns both the successful load and a typed nonexistent-id
// 404; no list readiness gate or redirect may sit in front of it.
must(files.conversation.includes("getProductConversation") && files.conversation.includes("sole authority for valid vs 404") && !files.conversation.includes("serverHistory?.some") && !files.conversation.includes("serverHistoryReady") && !files.conversation.includes("serverHistoryScope") && files.conversation.includes("正在加载服务器会话"), "chat existence is still decided by the truncated history list");
must(files.conversation.includes("setFeedbackByMessage") && files.conversation.includes("veyra:refresh-product") && files.conversation.includes("refreshConversation"), "conversation feedback does not mark locally and refresh product surfaces");
must(files.conversation.includes("Feedback is unavailable for this message") && !files.conversation.includes('"Feedback already recorded"'), "feedback 404/409 may claim success without a proven label");
must(files.main.includes("mergedConversationHistory") && files.main.includes("byConversationId.set(conversationIdFor(row), row)"), "server-backed history does not retain and deduplicate local migration rows");
must(files.conversation.includes("Veyra 发现的可能变化") && files.conversation.includes("FactInferenceDisclosure") && files.conversation.includes("事实与推断"), "hypothesis and fact/inference presentation is missing");
must(!files.conversation.includes("JSON.stringify(message.metadata") && !files.conversation.includes("metadata.feedback_token"), "conversation UI leaks raw metadata or feedback tokens");
must(files.productCss.includes("@media (max-width:760px)"), "product CSS mobile contract is missing");
must(files.conversation.includes("proactiveFeedbackButtons") && files.firstMeeting.includes("proactiveFeedbackButtons"), "proactive feedback controls have no responsive styling");

for (const key of ["situations", "attention", "suggestions", "commitments", "questions", "waiting"]) must(files.matters.includes(`key: "${key}"`), `legacy Matters is missing ${key} section mapping`);
must(files.settings.includes("Promise.allSettled") && files.settings.includes("statusReadable"), "Settings does not separate setup/status failures");
must(files.settings.includes("Local product boundary") && files.settings.includes("本地产品边界") && files.settings.includes("statusLabel(agent.status") && files.settings.includes("statusLabel(feishu.readiness"), "Settings does not use the bounded local boundary and setup state labels");
must(files.settings.includes("statusLabel(productScope.mode") && files.settings.includes("statusLabel(externalDelivery") && files.settings.includes("statusLabel(execution"), "Settings exposes raw local boundary enum values");
for (const value of ["waiting_for_event", "processing_failed", "configured_not_running", "receiving"]) must(files.productShared.includes(value), `setup state allow-list is missing ${value}`);
must(files.questions.includes("encodeURIComponent(situationId)") && files.questions.includes("Linked Situation") && files.questions.includes("关联 Situation") && !files.questions.includes("text(question.situation_id, en ? \"Linked Situation\""), "Product question cards expose a raw Situation id instead of a neutral link");
must(files.productCss.includes("@media (max-width:760px)") && files.productCss.includes("grid-template-columns:repeat(6") && files.productCss.includes("productExplainGrid"), "390px product layout contract is incomplete");
console.log("product frontend contract checks passed");
