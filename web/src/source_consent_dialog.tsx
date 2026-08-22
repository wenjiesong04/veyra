import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { Cloud, Globe2, Link2, LockKeyhole, ShieldCheck, X } from "lucide-react";
import { isEnglish, type Language } from "./shared";
import {
  SOURCE_CONSENT_COPY,
  isReadableSource,
  localeText,
  type ReadableSourceId,
  type SourceConsentMode,
} from "./source_consent";

const ICONS = {
  calendar: Link2,
  weather: Cloud,
  public_web: Globe2,
} as const;

type Props = {
  open: boolean;
  source: string;
  mode: SourceConsentMode;
  reason?: string;
  busy?: boolean;
  error?: string | null;
  language?: Language;
  onCancel: () => void;
  onConfirm: () => void;
};

export function SourceConsentDialog({ open, source, mode, reason, busy = false, error = null, language = "zh", onCancel, onConfirm }: Props) {
  const en = isEnglish(language);
  const titleId = useId();
  const bodyId = useId();
  const confirmRef = useRef<HTMLButtonElement>(null);
  const readable = isReadableSource(source);
  const copy = readable ? SOURCE_CONSENT_COPY[source as ReadableSourceId] : null;
  const Icon = readable ? ICONS[source as ReadableSourceId] : ShieldCheck;

  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => confirmRef.current?.focus());
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.cancelAnimationFrame(frame);
      document.body.style.overflow = previous;
      window.removeEventListener("keydown", onKey);
    };
  }, [open, busy, onCancel]);

  if (!open || !copy || typeof document === "undefined") return null;

  const grant = mode === "grant";
  const title = localeText(grant ? copy.title : copy.revokeTitle, en);
  const lead = localeText(grant ? copy.lead : copy.revokeLead, en);
  const confirmLabel = busy
    ? (en ? "Saving…" : "保存中…")
    : grant
      ? (en ? "Allow read-only" : "允许只读")
      : (en ? "Stop reading" : "停止读取");

  return createPortal(
    <div className="consentOverlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onCancel(); }}>
      <div className={`consentDialog ${grant ? "grant" : "revoke"}`} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={bodyId}>
        <div className="consentDialogAccent" aria-hidden="true" />
        <header className="consentDialogHeader">
          <span className="consentDialogIcon"><Icon size={18} /></span>
          <div>
            <small>{en ? "Read-only source access" : "只读来源授权"}</small>
            <h2 id={titleId}>{title}</h2>
          </div>
          <button className="iconButton subtle consentDialogClose" type="button" onClick={onCancel} disabled={busy} aria-label={en ? "Close" : "关闭"}>
            <X size={16} />
          </button>
        </header>
        <div className="consentDialogBody" id={bodyId}>
          <p>{lead}</p>
          {reason ? <p className="consentDialogReason"><strong>{en ? "Why now" : "为什么现在"}</strong>{reason}</p> : null}
          {grant ? (
            <>
              <ul className="consentChips" aria-label={en ? "Consent boundary" : "授权边界"}>
                <li>{en ? "Read-only" : "只读"}</li>
                <li>{en ? "No execution or delivery" : "不执行、不外发"}</li>
                <li>{en ? "Revocable anytime" : "随时可撤销"}</li>
              </ul>
              <div className="consentLists">
                <section>
                  <h3>{en ? "Veyra will" : "会做"}</h3>
                  <ul>{copy.will.map((item) => <li key={item.en}>{localeText(item, en)}</li>)}</ul>
                </section>
                <section>
                  <h3>{en ? "Veyra will not" : "不会做"}</h3>
                  <ul>{copy.wont.map((item) => <li key={item.en}>{localeText(item, en)}</li>)}</ul>
                </section>
              </div>
              {copy.extra ? <p className="consentDialogExtra"><LockKeyhole size={13} />{localeText(copy.extra, en)}</p> : null}
            </>
          ) : <p className="consentDialogExtra"><LockKeyhole size={13} />{en ? "Consent never granted execution or external delivery. Stopping the read only removes this source." : "这次授权从未赋予执行或外部交付权限。停止读取只关闭这一项来源。"}</p>}
          {error ? <p className="consentDialogError" role="alert">{error}</p> : null}
        </div>
        <footer className="consentDialogActions">
          <button className="ghostButton" type="button" onClick={onCancel} disabled={busy}>{en ? "Not now" : "暂时不"}</button>
          <button ref={confirmRef} className={grant ? "primaryButton" : "ghostButton consentRevokeButton"} type="button" onClick={onConfirm} disabled={busy}>
            <ShieldCheck size={15} />
            {confirmLabel}
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  );
}
