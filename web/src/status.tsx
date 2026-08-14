import { useEffect, useState } from "react";
import { Activity, ChevronRight, Eye, LockKeyhole, Network, RefreshCw, ShieldCheck } from "lucide-react";
import { getProductStatus, type ProductStatus } from "./api";
import { asRecord, ErrorBlock, Freshness, isEnglish, Language, LoadingBlock, OwnerScope, StatusBadge, Surface } from "./shared";

function value(data: unknown, fallback: string): string {
  return typeof data === "string" && data.trim() ? data : fallback;
}

function readiness(data: unknown): Record<string, unknown> {
  return asRecord(data) as Record<string, unknown>;
}

function shortRevision(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) return "unknown";
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

export function Status({ language = "zh" }: { scope?: OwnerScope; language?: Language }) {
  const [data, setData] = useState<ProductStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const en = isEnglish(language);
  const load = async () => {
    setLoading(true); setError(null);
    try { setData(await getProductStatus()); setUpdatedAt(new Date().toISOString()); }
    catch (caught) { setError(caught instanceof Error ? caught.message : (en ? "Status is temporarily unavailable" : "状态暂时不可用")); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);
  const copy = en ? { kicker: "Status", title: "How Veyra is doing", intro: "A curated product readout: source freshness, runtime readiness, and the boundary that remains in place.", refresh: "Refresh", source: "Sources", runtime: "Runtime", authority: "Boundary", implementation: "Implementation", configuration: "Configuration", automated: "Automated", live: "Live", production: "Production", agent: "Agent", integration: "Integrations", cognition: "Cognition", local: "Local-first preview", advanced: "Need full debug, rollback, or runtime matrix?", open: "Open Advanced console" } : { kicker: "状态", title: "Veyra 现在怎么样", intro: "这里看产品来源新鲜度、运行时准备情况，以及仍然保持的权限边界。", refresh: "刷新", source: "来源", runtime: "运行时", authority: "边界", implementation: "实现", configuration: "配置", automated: "自动化", live: "实时", production: "生产", agent: "Agent", integration: "集成", cognition: "认知", local: "本机优先预览", advanced: "需要完整调试、回滚或运行矩阵？", open: "打开 Advanced 旧控制台" };
  const productScope = asRecord(data?.product_scope); const sources = asRecord(productScope.sources); const runtime = asRecord(data?.runtime); const evidence = asRecord(data?.evidence); const authority = asRecord(data?.authority);
  const agent = readiness(runtime.agent); const integration = readiness(runtime.integration); const cognition = readiness(runtime.cognition); const build = readiness(runtime.build);
  const sourceItems = Object.entries(sources);
  return <div className="sectionPage statusPage">
    <div className="pageIntro"><div><span className="eyebrow">{copy.kicker}</span><h1>{copy.title}</h1><p>{copy.intro}</p></div><button className="ghostButton" onClick={() => void load()} disabled={loading}><RefreshCw size={15} className={loading ? "spinIcon" : ""} />{copy.refresh}</button></div>
    <div className="pageFreshness"><Freshness at={updatedAt} loading={loading} error={error} language={language} /><span>{value(productScope.mode, copy.local)} · {value(productScope.supported_boundary, "loopback/Tauri")}</span></div>
    {error ? <ErrorBlock message={error} onRetry={() => void load()} /> : null}
    {loading && !data ? <LoadingBlock label={en ? "Reading product status…" : "正在读取产品状态…"} /> : null}
    {data ? <>
      <div className="statusSummary"><div><span>{copy.source}</span><strong>{sourceItems.length ? (en ? "Read" : "已读取") : (en ? "Pending" : "待读取")}</strong></div><div><span>{copy.runtime}</span><strong>{value(build.status, "unknown")}</strong></div><div><span>{copy.agent}</span><strong>{value(agent.status, "unknown")}</strong></div><div><span>{copy.authority}</span><strong>{authority.external_delivery_allowed === false && authority.execution_allowed === false ? (en ? "Record only" : "仅记录") : (en ? "Unknown" : "未知")}</strong></div></div>
      <div className="evidenceStrip"><div className="projectionNote">{en ? "Evidence level is shown honestly; implemented does not mean production validated." : "证据等级保持诚实；已实现不等于生产已验证。"}</div>{(["implementation", "configuration", "automated", "live", "production"] as const).map((key) => <div key={key}><span>{copy[key]}</span><StatusBadge value={evidence[key] ?? "validation_pending"} label={value(evidence[key], "validation_pending").replaceAll("_", " ")} /></div>)}</div>
      <div className="statusSections"><Surface className="statusSection"><div className="statusSectionHeader"><div className="statusSectionTitle"><span className="sectionIcon"><Eye size={17} /></span><div><h2>{copy.source}</h2><p>{en ? "Freshness for the exact product read model" : "产品读模型的来源新鲜度"}</p></div></div></div><div className="statusFacts">{sourceItems.map(([key, source]) => <div key={key}><span>{key}</span><strong>{value(source, "unknown")}</strong></div>)}</div></Surface><Surface className="statusSection"><div className="statusSectionHeader"><div className="statusSectionTitle"><span className="sectionIcon"><Activity size={17} /></span><div><h2>{copy.runtime}</h2><p>{en ? "Readiness without raw runtime diagnostics" : "准备情况，不展开原始运行时诊断"}</p></div></div></div><div className="statusFacts"><div><span>{copy.agent}</span><StatusBadge value={agent.status} /></div><div><span>{copy.integration}</span><StatusBadge value={integration.status} /></div><div><span>{copy.cognition}</span><StatusBadge value={cognition.status} /></div><div><span>{en ? "Build" : "构建"}</span><strong>{shortRevision(build.revision ?? build.status)}</strong></div></div></Surface><Surface className="statusSection"><div className="statusSectionHeader"><div className="statusSectionTitle"><span className="sectionIcon"><LockKeyhole size={17} /></span><div><h2>{copy.authority}</h2><p>{en ? "Nothing in this product read model grants execution or delivery." : "产品读模型不会授予执行或交付权限。"}</p></div></div><ShieldCheck size={18} className="statusBoundaryIcon" /></div><div className="statusFacts"><div><span>{en ? "External delivery" : "外部交付"}</span><strong>{authority.external_delivery_allowed === false ? "none" : "unknown"}</strong></div><div><span>{en ? "Execution" : "执行"}</span><strong>{authority.execution_allowed === false ? "disabled" : "unknown"}</strong></div><div><span>{en ? "Advanced" : "Advanced"}</span><strong>{data.advanced_available === true ? (en ? "available" : "可进入") : "unknown"}</strong></div></div></Surface></div>
    </> : null}
    <div className="advancedHint"><span>{copy.advanced}</span><a href="#/advanced">{copy.open} <ChevronRight size={14} /></a></div>
  </div>;
}
