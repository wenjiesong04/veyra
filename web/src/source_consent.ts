import { consentProductSource, revokeProductSource, type JsonValue } from "./api";
import { text } from "./product_shared";

export const READABLE_SOURCE_IDS = ["calendar", "weather", "public_web"] as const;
export type ReadableSourceId = (typeof READABLE_SOURCE_IDS)[number];
export type SourceConsentMode = "grant" | "revoke";

export function isReadableSource(value: string): value is ReadableSourceId {
  return (READABLE_SOURCE_IDS as readonly string[]).includes(value);
}

type LocaleText = { zh: string; en: string };

export type SourceConsentCopy = {
  name: LocaleText;
  title: LocaleText;
  lead: LocaleText;
  will: LocaleText[];
  wont: LocaleText[];
  extra?: LocaleText;
  look: LocaleText;
  revokeTitle: LocaleText;
  revokeLead: LocaleText;
};

export const SOURCE_CONSENT_COPY: Record<ReadableSourceId, SourceConsentCopy> = {
  calendar: {
    name: { zh: "日历", en: "Calendar" },
    title: { zh: "允许读取日历", en: "Allow calendar reads" },
    lead: { zh: "只用来看时间冲突和临近承诺。授权不会改你的日程。", en: "Used only to see timing conflicts and nearby commitments. Consent does not change your events." },
    will: [
      { zh: "读取即将发生的日程标题与时间窗口", en: "Read upcoming event titles and time windows" },
      { zh: "只在某条 Situation 需要时间证据时读取", en: "Read only when a Situation needs timing evidence" },
    ],
    wont: [
      { zh: "不会新建、修改或删除任何日程", en: "Will not create, change, or delete events" },
      { zh: "不会发消息、不会外发、不会执行操作", en: "Will not message, deliver, or execute anything" },
    ],
    extra: {
      zh: "允许之后，macOS 仍可能再弹出一次系统日历权限。系统拒绝后，Veyra 不会继续读取。",
      en: "After this consent, macOS may still ask for Calendar access. If the system refuses, Veyra will not keep reading.",
    },
    look: { zh: "让 Veyra 去看日历", en: "Let Veyra read the calendar" },
    revokeTitle: { zh: "停止读取日历？", en: "Stop calendar reads?" },
    revokeLead: { zh: "之后需要时间证据时，会改回问你，而不是自己去看。", en: "When timing evidence is needed, Veyra will ask you again instead of reading on its own." },
  },
  weather: {
    name: { zh: "天气", en: "Weather" },
    title: { zh: "允许读取天气", en: "Allow weather reads" },
    lead: { zh: "只在天气会改变判断时读取公开天气。地点必须是你已经报告过的。", en: "Read public weather only when it changes a judgment. The place must already be reported by you." },
    will: [
      { zh: "读取公开天气，不登录任何账户", en: "Read public weather without signing into an account" },
      { zh: "只用已报告的地点，不会把推断地点当成授权依据", en: "Use a reported place only; inferred places are not enough" },
    ],
    wont: [
      { zh: "不会扩大到日历、网页或其他来源", en: "Will not expand into calendar, web, or other sources" },
      { zh: "不会发消息、不会外发、不会执行操作", en: "Will not message, deliver, or execute anything" },
    ],
    look: { zh: "让 Veyra 去看天气", en: "Let Veyra read the weather" },
    revokeTitle: { zh: "停止读取天气？", en: "Stop weather reads?" },
    revokeLead: { zh: "之后天气类问题会改回问你，而不是自己去看。", en: "Weather questions will be asked of you again instead of being read automatically." },
  },
  public_web: {
    name: { zh: "公共网页", en: "Public Web" },
    title: { zh: "允许读取公共网页", en: "Allow public web reads" },
    lead: { zh: "只在需要核对外部事实时读取公开页面。", en: "Read public pages only when an external fact has to be checked." },
    will: [
      { zh: "读取公开网页上的事实片段", en: "Read factual snippets from public pages" },
      { zh: "只在 Information Need 指定这个来源时读取", en: "Read only when an Information Need names this source" },
    ],
    wont: [
      { zh: "不会登录、发帖、下单或代表你操作", en: "Will not sign in, post, buy, or act on your behalf" },
      { zh: "不会发消息、不会外发、不会执行操作", en: "Will not message, deliver, or execute anything" },
    ],
    look: { zh: "让 Veyra 去看网页", en: "Let Veyra read the web" },
    revokeTitle: { zh: "停止读取公共网页？", en: "Stop public web reads?" },
    revokeLead: { zh: "之后外部事实会改回问你，而不是自己去核对。", en: "External facts will be asked of you again instead of being checked automatically." },
  },
};

export function localeText(value: LocaleText, en: boolean): string {
  return en ? value.en : value.zh;
}

export function applySourceConsent(options: {
  source: string;
  enabled: boolean;
  item: Record<string, JsonValue>;
  scope: { user_id: string; session_id: string };
  en: boolean;
}): Promise<Record<string, JsonValue>> {
  const generation = Number(options.item.generation ?? options.item.consent_generation ?? 0);
  const consentId = text(options.item.consent_id, "");
  if (!options.enabled && (!Number.isFinite(generation) || generation < 1)) {
    return Promise.reject(new Error(options.en ? "Refresh source status before revoking consent." : "请先刷新来源状态，再撤销授权。"));
  }
  if (options.enabled) {
    return consentProductSource(options.source, options.scope, {
      expected_generation: Number.isFinite(generation) && generation >= 0 ? generation : 0,
    });
  }
  return revokeProductSource(options.source, options.scope, {
    expected_generation: generation,
    ...(consentId ? { consent_id: consentId } : {}),
  });
}

export function notifyProductConsentChanged() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event("veyra:refresh-product"));
}
