import { useCallback, useEffect, useRef, useState } from "react";
import { Check, CircleHelp, Cloud, Globe2, Link2, LockKeyhole, ShieldCheck } from "lucide-react";
import { consentProductSource, getProductSources, revokeProductSource, type ProductContext, type ProductSources } from "./api";
import { productContextReadiness, record, statusLabel, text } from "./product_shared";
import { ErrorBlock, Freshness, isEnglish, Language, LoadingBlock, OwnerScope, StatusBadge, Surface } from "./shared";

function sourceStatusLabel(value: unknown, en: boolean): string {
  const normalized = text(value, "not_configured").toLowerCase();
  const labels: Record<string, [string, string]> = {
    available: ["可用", "Available"],
    needs_consent: ["需要授权", "Needs consent"],
    permission_unknown: ["权限待确认", "Permission not confirmed"],
    permission_denied: ["权限被拒绝", "Permission denied"],
    unavailable: ["暂不可用", "Unavailable"],
    not_configured: ["尚未配置", "Not configured"],
  };
  return labels[normalized]?.[en ? 1 : 0] ?? statusLabel(normalized, en);
}

const sourceMeta = [
  { id: "calendar", icon: Link2, zh: "日历", en: "Calendar", hintZh: "用于理解时间冲突和临近承诺。", hintEn: "Used to understand timing conflicts and commitments." },
  { id: "weather", icon: Cloud, zh: "天气", en: "Weather", hintZh: "用于需要天气条件时的观察。", hintEn: "Used when weather conditions affect a Situation." },
  { id: "public_web", icon: Globe2, zh: "公共网页", en: "Public Web", hintZh: "只在需要外部事实时读取。", hintEn: "Read only when an external fact is needed." },
] as const;

function ContextGate({ loading, status, en }: { loading: boolean; status: string; en: boolean }) {
  if (loading) return <LoadingBlock label={en ? "Waiting for Product Context before reading source access…" : "正在等待 Product Context，然后读取来源授权…"} />;
  return <div className="sourceBoundary" role="status"><CircleHelp size={15} /><div><strong>{en ? "Information source access requires an exact local Product Context." : "信息来源授权需要精确的本机 Product Context。"}</strong><span>{en ? "Veyra will not request source status until the local context is ready or empty." : "在本机上下文就绪或明确为空之前，Veyra 不会请求来源状态。"}</span><StatusBadge value={status} label={statusLabel(status, en)} /></div></div>;
}

export function ProductSources({ scope, productContext, language = "zh" }: { scope: OwnerScope; productContext?: ProductContext | null; language?: Language }) {
  const en = isEnglish(language);
  const [data, setData] = useState<ProductSources | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [action, setAction] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const requestRef = useRef<{ generation: number; controller: AbortController | null }>({ generation: 0, controller: null });
  const load = useCallback(async () => {
    const generation = requestRef.current.generation + 1;
    requestRef.current.controller?.abort();
    const controller = new AbortController();
    requestRef.current = { generation, controller };
    const isCurrent = () => requestRef.current.generation === generation && requestRef.current.controller === controller && !controller.signal.aborted;
    const context = productContextReadiness(productContext, scope);
    setError(null);
    if (!context.ready) {
      if (isCurrent()) {
        setData(null); setUpdatedAt(null); setAction(null); setLoading(context.loading); requestRef.current.controller = null;
      }
      return;
    }
    setLoading(true);
    try {
      const readScope = context.internalScope;
      if (!readScope) return;
      const nextData = await getProductSources(readScope, { signal: controller.signal });
      if (!isCurrent()) return;
      setData(nextData);
      setUpdatedAt(new Date().toISOString());
    } catch (caught) {
      if (!isCurrent() || (caught instanceof DOMException && caught.name === "AbortError")) return;
      setError(caught instanceof Error ? caught.message : (en ? "Source status is unavailable" : "来源状态暂时不可用"));
    } finally {
      if (isCurrent()) { setLoading(false); requestRef.current.controller = null; }
    }
  }, [scope.userId, scope.sessionId, en, productContext]);
  useEffect(() => { void load(); return () => { requestRef.current.generation += 1; requestRef.current.controller?.abort(); requestRef.current.controller = null; }; }, [load]);
  const context = productContextReadiness(productContext, scope);
  const items = record(data?.items);
  const changeConsent = async (source: string, enabled: boolean) => {
    const currentContext = productContextReadiness(productContext, scope);
    if (!currentContext.ready || !data || !currentContext.internalScope) return;
    setAction(source); setActionError(null);
    try {
      const item = record(items[source]);
      const generation = Number(item.generation ?? item.consent_generation ?? 0);
      const consentId = text(item.consent_id, "");
      if (!enabled && (!Number.isFinite(generation) || generation < 1)) throw new Error(en ? "Refresh source status before revoking consent." : "请先刷新来源状态，再撤销授权。");
      if (enabled) await consentProductSource(source, currentContext.internalScope, { expected_generation: Number.isFinite(generation) && generation >= 0 ? generation : 0 });
      else await revokeProductSource(source, currentContext.internalScope, { expected_generation: generation, ...(consentId ? { consent_id: consentId } : {}) });
      await load();
    } catch (caught) {
      setActionError(caught instanceof Error ? (en ? "Consent is not available yet: " + caught.message : "授权入口暂不可用：" + caught.message) : (en ? "Consent is not available yet" : "授权入口暂不可用"));
    } finally {
      setAction(null);
    }
  };
  return <Surface className="productSourcesCard"><div className="settingsHeading"><ShieldCheck size={18} /><div><h2>{en ? "Information source access" : "信息来源授权"}</h2><p>{en ? "Configuration, system permission, consent, and current availability are shown separately. Consent never grants execution or external delivery." : "配置、系统权限、用户授权和当前可用性分别展示。授权不会赋予执行或外部交付权限。"}</p></div><Freshness at={updatedAt} loading={loading} error={error} language={language} /></div>{actionError ? <ErrorBlock message={actionError} onRetry={() => { setActionError(null); void load(); }} /> : null}{!context.ready ? <ContextGate loading={context.loading} status={context.status} en={en} /> : null}{context.ready && loading && !data ? <LoadingBlock label={en ? "Reading source status…" : "正在读取来源状态…"} /> : null}{context.ready && error && !data ? <ErrorBlock message={error} onRetry={() => void load()} /> : null}{context.ready && data ? <div className="sourceList">{sourceMeta.map(({ id, icon: Icon, zh, en: labelEn, hintZh, hintEn }) => { const item = record(items[id]); const available = item.available === true; const canRequest = item.can_request === true || item.attemptable === true; const configured = item.configured === true; const permission = text(item.system_permission, "unknown").toLowerCase(); const permissionDenied = permission === "denied" || permission === "permission_denied"; const consented = item.consented === true; const stateHint = permissionDenied ? (consented ? (en ? "Permission denied; revoke consent to stop future reads" : "权限被拒绝；可撤销授权以停止后续读取") : (en ? "Permission denied" : "权限被拒绝")) : available ? (en ? "Available for governed reads" : "可用于受治理读取") : consented && canRequest ? (en ? "Consented; ready to check system permission" : "已授权；可以检查系统权限") : consented ? (en ? "Consented, waiting for availability" : "已授权，等待来源可用") : permission === "unknown" ? (en ? "System permission is not confirmed" : "系统权限尚未确认") : configured ? (en ? "Configured, consent required" : "已配置，需要授权") : (en ? "Not configured" : "尚未配置"); const disabledReason = !consented && (permissionDenied ? (en ? "Permission denied" : "权限被拒绝") : !configured ? (en ? "Configure first" : "请先配置") : ""); const disabled = action === id || Boolean(disabledReason); const buttonLabel = action === id ? (en ? "Saving…" : "保存中…") : consented ? (en ? "Revoke" : "撤销授权") : disabledReason || (en ? "Consent" : "授权"); const hintId = "source-" + id + "-hint"; return <div className="sourceRow" key={id}><div className="sourceIcon"><Icon size={17} /></div><div className="sourceInfo"><div><h3>{en ? labelEn : zh}</h3><StatusBadge value={item.status ?? "not_configured"} label={sourceStatusLabel(item.status ?? "not_configured", en)} /></div><p>{en ? hintEn : hintZh}</p><small id={hintId}>{stateHint}</small></div><button className={consented ? "ghostButton small" : "primaryButton small"} type="button" disabled={disabled} aria-label={buttonLabel} aria-describedby={hintId} title={disabledReason || undefined} onClick={() => void changeConsent(id, !consented)}>{consented ? <LockKeyhole size={14} /> : disabledReason ? <LockKeyhole size={14} /> : <Check size={14} />}{buttonLabel}</button></div>; })}</div> : null}<div className="sourceBoundary"><LockKeyhole size={15} /><span>{en ? "Agent research is disabled in V1. Email is not presented as a supported source yet." : "Agent research 在 V1 中关闭；Email 目前不作为已支持来源展示。"}</span></div></Surface>;
}
