import { useEffect, useState } from "react";
import { Check, ExternalLink, Languages, LockKeyhole, Moon, Palette, Settings2, Sun, WandSparkles } from "lucide-react";
import { getProductStatus, getSetupStatus, type ProductStatus } from "./api";
import { asRecord, ErrorBlock, Freshness, isEnglish, LoadingBlock, Surface } from "./shared";

type Theme = "system" | "light" | "dark";
type Language = "system" | "zh" | "en";

export function Settings({ theme, language, onTheme, onLanguage, onOpenSetup }: { theme: Theme; language: Language; onTheme: (value: Theme) => void; onLanguage: (value: Language) => void; onOpenSetup: () => void }) {
  const [setup, setSetup] = useState<Record<string, unknown> | null>(null);
  const [productStatus, setProductStatus] = useState<ProductStatus | null>(null);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [setupUpdatedAt, setSetupUpdatedAt] = useState<string | null>(null);
  const [statusUpdatedAt, setStatusUpdatedAt] = useState<string | null>(null);
  const [setupLoading, setSetupLoading] = useState(true);
  const [statusLoading, setStatusLoading] = useState(true);
  const isEn = isEnglish(language);
  const load = async () => {
    setSetupLoading(true); setStatusLoading(true); setSetupError(null); setStatusError(null);
    const [setupResult, statusResult] = await Promise.allSettled([getSetupStatus(), getProductStatus()]);
    if (setupResult.status === "fulfilled") { setSetup(setupResult.value); setSetupUpdatedAt(new Date().toISOString()); }
    else setSetupError(setupResult.reason instanceof Error ? setupResult.reason.message : (isEn ? "Local setup is unavailable" : "本机设置暂时不可用"));
    if (statusResult.status === "fulfilled") { setProductStatus(statusResult.value); setStatusUpdatedAt(new Date().toISOString()); }
    else setStatusError(statusResult.reason instanceof Error ? statusResult.reason.message : (isEn ? "Product status is unavailable" : "产品状态暂时不可用"));
    setSetupLoading(false); setStatusLoading(false);
  };
  useEffect(() => { void load(); }, []);
  const desktop = asRecord(setup?.desktop); const agent = asRecord(setup?.agent); const feishu = asRecord(setup?.feishu_setup); const wizard = asRecord(setup?.wizard);
  const productScope = asRecord(productStatus?.product_scope); const authority = asRecord(productStatus?.authority); const sources = asRecord(productScope.sources);
  const statusReadable = Boolean(productStatus && !statusError);
  const unknown = isEn ? "unknown" : "未知";
  const externalDelivery = statusReadable && authority.external_delivery_allowed === false ? "none" : unknown;
  const execution = statusReadable && authority.execution_allowed === false ? "disabled" : unknown;
  const copy = isEn ? { kicker: "Settings", title: "Make Veyra yours", intro: "Appearance, language, and local connections live here. Deep controls remain in Advanced / Labs.", appearance: "Appearance", appearanceHint: "Choose light, dark, or follow the system.", language: "Language", languageHint: "Switch between Chinese and English.", local: "Local setup", localHint: "Connect Agent, finish first-run setup, and Feishu intake.", permissions: "Sources & permissions", permissionsHint: "Veyra reads only the local product sources shown here. Nothing is sent externally by this preview.", boundary: "This preview is local-only", boundaryHint: "Loopback and Tauri are supported. External delivery, execution, tool calls, and Agent dispatch remain off.", product: "Product", agent: "Agent", feishu: "Feishu", wizard: "Wizard", done: "Complete", pending: "Pending", openWizard: "Open setup wizard", advanced: "Advanced console", labs: "Labs", unknown } : { kicker: "设置", title: "让 Veyra 适合你", intro: "外观、语言和本机连接都在这里。深度能力仍保留在 Advanced / Labs。", appearance: "外观", appearanceHint: "选择浅色、深色或跟随系统。", language: "语言", languageHint: "中英文可切换，系统默认跟随浏览器。", local: "本机设置", localHint: "连接 Agent、完成首次启动和 Feishu 接入。", permissions: "来源与权限", permissionsHint: "Veyra 只读取这里展示的本机产品来源；这个预览不会向外部发送。", boundary: "本预览仅限本机", boundaryHint: "支持 loopback 与 Tauri；外部交付、执行、工具调用和 Agent 派发仍关闭。", product: "产品", agent: "Agent", feishu: "Feishu", wizard: "向导", done: "已完成", pending: "待完成", openWizard: "打开设置向导", advanced: "Advanced 旧控制台", labs: "Labs 实验能力", unknown };
  return <div className="sectionPage settingsPage"><div className="pageIntro"><div><span className="eyebrow">{copy.kicker}</span><h1>{copy.title}</h1><p>{copy.intro}</p></div></div><div className="settingsGrid"><Surface><div className="settingsHeading"><Palette size={18} /><div><h2>{copy.appearance}</h2><p>{copy.appearanceHint}</p></div></div><div className="choiceGroup">{(["system", "light", "dark"] as Theme[]).map((value) => <button key={value} className={theme === value ? "choice active" : "choice"} onClick={() => onTheme(value)}><span>{value === "system" ? <Settings2 size={16} /> : value === "light" ? <Sun size={16} /> : <Moon size={16} />}</span>{value === "system" ? (isEn ? "System" : "跟随系统") : value === "light" ? (isEn ? "Light" : "浅色") : (isEn ? "Dark" : "深色")}{theme === value ? <Check size={15} /> : null}</button>)}</div></Surface><Surface><div className="settingsHeading"><Languages size={18} /><div><h2>{copy.language}</h2><p>{copy.languageHint}</p></div></div><div className="choiceGroup">{(["system", "zh", "en"] as Language[]).map((value) => <button key={value} className={language === value ? "choice active" : "choice"} onClick={() => onLanguage(value)}>{value === "system" ? (isEn ? "System" : "跟随系统") : value === "zh" ? "中文" : "English"}{language === value ? <Check size={15} /> : null}</button>)}</div></Surface><Surface className="setupStatusCard"><div className="settingsHeading"><WandSparkles size={18} /><div><h2>{copy.local}</h2><p>{copy.localHint}</p></div><Freshness at={setupUpdatedAt} loading={setupLoading} error={setupError} language={language} /></div>{setupLoading ? <LoadingBlock /> : setupError ? <ErrorBlock message={setupError} onRetry={() => void load()} /> : <div className="setupFacts"><div><span>{copy.product}</span><strong>{String(desktop.product_name ?? "Veyra")}</strong></div><div><span>{copy.agent}</span><strong>{String(agent.status ?? (isEn ? "Not configured" : "未配置"))}</strong></div><div><span>{copy.feishu}</span><strong>{String(feishu.readiness ?? (isEn ? "Not configured" : "未配置"))}</strong></div><div><span>{copy.wizard}</span><strong>{wizard.completed === true ? copy.done : copy.pending}</strong></div></div>}<button className="primaryButton" onClick={onOpenSetup}><WandSparkles size={15} />{copy.openWizard}</button></Surface><Surface className="permissionsCard"><div className="settingsHeading"><LockKeyhole size={18} /><div><h2>{copy.permissions}</h2><p>{copy.permissionsHint}</p></div><Freshness at={statusUpdatedAt} loading={statusLoading} error={statusError} language={language} /></div>{statusError ? <ErrorBlock message={statusError} onRetry={() => void load()} /> : null}<div className="setupFacts"><div><span>{copy.product}</span><strong>{statusReadable ? String(productScope.mode ?? "local-first") : copy.unknown}</strong></div><div><span>{isEn ? "Sources" : "来源"}</span><strong>{statusReadable ? Object.keys(sources).length || "—" : copy.unknown}</strong></div><div><span>{isEn ? "External delivery" : "外部交付"}</span><strong>{externalDelivery}</strong></div><div><span>{isEn ? "Execution" : "执行"}</span><strong>{execution}</strong></div></div><p className="settingsBoundary"><strong>{copy.boundary}</strong><span>{copy.boundaryHint}</span></p></Surface></div><div className="settingsLinks"><a href="#/advanced"><ExternalLink size={15} />{copy.advanced}</a><a href="#/advanced"><ExternalLink size={15} />{copy.labs}</a></div></div>;
}

export type { Language, Theme };
