import React, { useEffect, useMemo, useState } from "react";
import { Bot, Brain, CheckCircle2, ChevronLeft, ChevronRight, Download, RefreshCw, Sparkles, X } from "lucide-react";
import { fetchJson, type JsonValue } from "./api";

type WizardStep = "welcome" | "bootstrap" | "core_model" | "agent" | "feishu" | "review";

type OpenClawDiagnostics = {
  installed?: boolean;
  executable?: string | null;
  npm_available?: boolean;
  port_listening?: boolean;
  connected?: boolean;
  agent_status?: string;
  base_url?: string;
  control_ui_url?: string;
  install_command?: string;
  start_command?: string;
  docs_hint?: string;
  ready?: boolean;
  needs_gateway?: boolean;
  needs_install?: boolean;
  needs_start?: boolean;
};

type FeishuDiagnostics = {
  configured?: boolean;
  enabled?: boolean;
  connection_mode?: string;
  status?: string;
  thread_alive?: boolean;
  last_event_after_start?: boolean;
  readiness?: string;
  app_id_set?: boolean;
  app_secret_set?: boolean;
  default_receive_id_set?: boolean;
  diagnostics?: JsonValue[];
};

type SetupWizardProps = {
  open: boolean;
  onClose: () => void;
  onComplete: () => Promise<void>;
  setupStatus: Record<string, JsonValue> | null;
  coreModelStatus: Record<string, JsonValue> | null;
  agentStatus: Record<string, JsonValue> | null;
};

const steps: Array<{ id: WizardStep; label: string }> = [
  { id: "welcome", label: "Welcome" },
  { id: "bootstrap", label: "Local setup" },
  { id: "core_model", label: "Core model" },
  { id: "agent", label: "Agent" },
  { id: "feishu", label: "Feishu" },
  { id: "review", label: "Finish" }
];
const CORE_MODEL_API_KEY_ENV = "VEYRA_CORE_MODEL_API_KEY";

function asRecord(value: JsonValue | null | undefined): Record<string, JsonValue> {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function editableOpenClawUrl(value: JsonValue | null | undefined): string {
  const raw = String(value ?? "").trim();
  if (!raw) return "http://127.0.0.1:18789";
  if (raw.startsWith("ws://")) return `http://${raw.slice(5)}`;
  if (raw.startsWith("wss://")) return `https://${raw.slice(6)}`;
  return raw;
}

export function SetupWizard({ open, onClose, onComplete, setupStatus, coreModelStatus, agentStatus }: SetupWizardProps) {
  const [step, setStep] = useState<WizardStep>("welcome");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openclaw, setOpenclaw] = useState<OpenClawDiagnostics | null>(null);

  const setupPaths = asRecord(setupStatus?.paths);

  const [coreEnabled, setCoreEnabled] = useState(false);
  const [coreBaseUrl, setCoreBaseUrl] = useState("");
  const [coreModel, setCoreModel] = useState("");
  const [coreApiKey, setCoreApiKey] = useState("");
  const [decisionMode, setDecisionMode] = useState("auto");

  const [useOpenClaw, setUseOpenClaw] = useState(true);
  const [openclawBaseUrl, setOpenclawBaseUrl] = useState("http://127.0.0.1:18789");
  const [openclawToken, setOpenclawToken] = useState("");
  const [skipOpenClaw, setSkipOpenClaw] = useState(false);
  const [skipCoreModel, setSkipCoreModel] = useState(false);
  const [useFeishu, setUseFeishu] = useState(false);
  const [skipFeishu, setSkipFeishu] = useState(true);
  const [feishuAppId, setFeishuAppId] = useState("");
  const [feishuAppSecret, setFeishuAppSecret] = useState("");
  const [feishuDefaultReceiveId, setFeishuDefaultReceiveId] = useState("");
  const [feishuReceiveIdType, setFeishuReceiveIdType] = useState("chat_id");
  const [feishuStatus, setFeishuStatus] = useState<FeishuDiagnostics | null>(null);
  const [installLog, setInstallLog] = useState("");

  useEffect(() => {
    if (!open) return;
    setStep("welcome");
    setError(null);
    setInstallLog("");
    const env = asRecord(asRecord(setupStatus?.env).values);
    setCoreEnabled(coreModelStatus?.enabled === true || String(env.VEYRA_CORE_MODEL_ENABLED ?? "") === "1");
    setCoreBaseUrl(String(coreModelStatus?.base_url ?? env.VEYRA_CORE_MODEL_BASE_URL ?? ""));
    setCoreModel(String(coreModelStatus?.model ?? env.VEYRA_CORE_MODEL ?? ""));
    setDecisionMode(String(coreModelStatus?.decision_mode ?? env.VEYRA_CORE_MODEL_DECISION_MODE ?? "auto"));
    setOpenclawBaseUrl(editableOpenClawUrl(env.OPENCLAW_BASE_URL ?? agentStatus?.base_url));
    setUseOpenClaw(String(asRecord(setupStatus?.agent).name ?? agentStatus?.name ?? "openclaw") === "openclaw");
    const feishu = asRecord(setupStatus?.feishu_setup);
    const feishuChannel = asRecord(asRecord(setupStatus?.feishu).channel_config);
    const feishuConfigured = feishu.configured === true;
    setUseFeishu(feishuConfigured);
    setSkipFeishu(!feishuConfigured);
    setFeishuStatus(feishu as FeishuDiagnostics);
    setFeishuAppId(String(feishuChannel.app_id && feishuChannel.app_id !== "<redacted>" ? feishuChannel.app_id : env.FEISHU_APP_ID ?? ""));
    setFeishuDefaultReceiveId(String(feishuChannel.default_receive_id && feishuChannel.default_receive_id !== "<redacted>" ? feishuChannel.default_receive_id : env.FEISHU_DEFAULT_RECEIVE_ID ?? ""));
    setFeishuReceiveIdType(String(feishuChannel.default_receive_id_type ?? "chat_id"));
    void refreshOpenClaw();
    void refreshFeishu();
  }, [open]);

  const refreshOpenClaw = async () => {
    try {
      const diagnostics = await fetchJson<OpenClawDiagnostics>("/setup/openclaw");
      setOpenclaw(diagnostics);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to load OpenClaw status");
    }
  };

  const refreshFeishu = async () => {
    try {
      const status = await fetchJson<Record<string, JsonValue>>("/integrations/feishu/ws/status");
      const setup = await fetchJson<Record<string, JsonValue>>("/setup/status");
      setFeishuStatus(asRecord(setup.feishu_setup) as FeishuDiagnostics);
      const channel = asRecord(status.channel_config);
      if (!feishuAppId && typeof channel.app_id === "string" && channel.app_id !== "<redacted>") {
        setFeishuAppId(channel.app_id);
      }
      if (!feishuDefaultReceiveId && typeof channel.default_receive_id === "string" && channel.default_receive_id !== "<redacted>") {
        setFeishuDefaultReceiveId(channel.default_receive_id);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to load Feishu status");
    }
  };

  const stepIndex = useMemo(() => steps.findIndex((item) => item.id === step), [step]);

  if (!open) return null;

  const bootstrapEnv = async () => {
    setBusy(true);
    setError(null);
    try {
      const normalizedOpenClawBaseUrl = editableOpenClawUrl(openclawBaseUrl);
      await fetchJson("/setup/env", {
        method: "POST",
        body: JSON.stringify({
          values: {
            VEYRA_STATE_ROOT: "state",
            VEYRA_AGENCY_ROOT: "agency",
            VEYRA_ACTION_RECORD_RETENTION_LIMIT: 10000,
            VEYRA_HOST: "127.0.0.1",
            VEYRA_PORT: 8000,
            VEYRA_SELECTED_AGENT: useOpenClaw ? "openclaw" : "custom",
            OPENCLAW_BASE_URL: normalizedOpenClawBaseUrl
          }
        })
      });
      setOpenclawBaseUrl(normalizedOpenClawBaseUrl);
      setStep("core_model");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to create local config");
    } finally {
      setBusy(false);
    }
  };

  const saveCoreModel = async () => {
    setBusy(true);
    setError(null);
    try {
      const envUpdates: Record<string, string | number | boolean | null> = {
        VEYRA_CORE_MODEL_ENABLED: coreEnabled,
        VEYRA_CORE_MODEL_PROVIDER: "openai_compatible",
        VEYRA_CORE_MODEL_BASE_URL: coreBaseUrl,
        VEYRA_CORE_MODEL: coreModel,
        VEYRA_CORE_MODEL_API_KEY_ENV: CORE_MODEL_API_KEY_ENV,
        VEYRA_CORE_MODEL_DECISION_MODE: decisionMode
      };
      if (coreApiKey.trim()) {
        envUpdates.VEYRA_CORE_MODEL_API_KEY = coreApiKey.trim();
      }
      await fetchJson("/setup/env", {
        method: "POST",
        body: JSON.stringify({ values: envUpdates })
      });
      await fetchJson("/core/model/config", {
        method: "POST",
        body: JSON.stringify({
          enabled: coreEnabled,
          provider: "openai_compatible",
          base_url: coreBaseUrl,
          model: coreModel,
          api_key_env: CORE_MODEL_API_KEY_ENV,
          decision_mode: decisionMode
        })
      });
      setStep("agent");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to save core model");
    } finally {
      setBusy(false);
    }
  };

  const saveAgent = async () => {
    setBusy(true);
    setError(null);
    try {
      if (useOpenClaw && !skipOpenClaw) {
        const normalizedBaseUrl = editableOpenClawUrl(openclawBaseUrl);
        const envUpdates: Record<string, string | number | boolean | null> = {
          VEYRA_SELECTED_AGENT: "openclaw",
          OPENCLAW_BASE_URL: normalizedBaseUrl,
          VEYRA_OPENCLAW_USE_LOCAL_CONFIG: 1
        };
        if (openclawToken.trim()) {
          envUpdates.OPENCLAW_GATEWAY_TOKEN = openclawToken.trim();
        }
        await fetchJson("/setup/env", {
          method: "POST",
          body: JSON.stringify({ values: envUpdates })
        });
        await fetchJson("/agents/openclaw/config", {
          method: "POST",
          body: JSON.stringify({
            kind: "openclaw",
            base_url: normalizedBaseUrl,
            api_key_env: "OPENCLAW_GATEWAY_TOKEN",
            enabled: true
          })
        });
        await fetchJson("/agents/select", {
          method: "POST",
          body: JSON.stringify({ name: "openclaw" })
        });
        setOpenclawBaseUrl(normalizedBaseUrl);
      }
      await refreshOpenClaw();
      setStep("feishu");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to save agent settings");
    } finally {
      setBusy(false);
    }
  };

  const saveFeishu = async () => {
    setBusy(true);
    setError(null);
    try {
      if (useFeishu && !skipFeishu) {
        const envUpdates: Record<string, string | number | boolean | null> = {
          FEISHU_APP_ID: feishuAppId.trim(),
          FEISHU_DEFAULT_RECEIVE_ID: feishuDefaultReceiveId.trim()
        };
        if (feishuAppSecret.trim()) {
          envUpdates.FEISHU_APP_SECRET = feishuAppSecret.trim();
        }
        await fetchJson("/setup/env", {
          method: "POST",
          body: JSON.stringify({ values: envUpdates })
        });
        await fetchJson("/channels/feishu/config", {
          method: "POST",
          body: JSON.stringify({
            enabled: true,
            delivery: "feishu",
            base_url: "https://open.feishu.cn",
            app_id: feishuAppId.trim(),
            app_id_env: "FEISHU_APP_ID",
            app_secret: feishuAppSecret.trim() || undefined,
            app_secret_env: "FEISHU_APP_SECRET",
            default_receive_id: feishuDefaultReceiveId.trim(),
            default_receive_id_env: "FEISHU_DEFAULT_RECEIVE_ID",
            default_receive_id_type: feishuReceiveIdType,
            connection_mode: "websocket",
            reply_to_session: true
          })
        });
        const status = await fetchJson<Record<string, JsonValue>>("/integrations/feishu/ws/start", { method: "POST" });
        const nextStatus = String(status.status ?? "");
        if (nextStatus === "not_configured") {
          setError(String(status.reason ?? "Feishu app id and app secret are required."));
          return;
        }
        await refreshFeishu();
      }
      setStep("review");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to save Feishu settings");
    } finally {
      setBusy(false);
    }
  };

  const installOpenClaw = async (startGateway: boolean) => {
    setBusy(true);
    setError(null);
    setInstallLog("");
    try {
      const response = await fetchJson<Record<string, JsonValue>>("/setup/openclaw/install", {
        method: "POST",
        body: JSON.stringify({ start_gateway: startGateway })
      });
      setInstallLog(String(response.install_output_tail ?? "OpenClaw install finished."));
      setOpenclaw(asRecord(response.openclaw) as OpenClawDiagnostics);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "OpenClaw install failed");
    } finally {
      setBusy(false);
    }
  };

  const finishWizard = async () => {
    setBusy(true);
    setError(null);
    try {
      await fetchJson("/setup/complete", {
        method: "POST",
        body: JSON.stringify({
          skipped_openclaw: skipOpenClaw || !useOpenClaw,
          skipped_feishu: skipFeishu || !useFeishu,
          skipped_core_model: skipCoreModel || !coreEnabled,
          notes: ""
        })
      });
      await onComplete();
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to finish setup");
    } finally {
      setBusy(false);
    }
  };

  const nextFromBootstrap = async () => {
    if (setupPaths.env_exists === true) {
      setStep("core_model");
      return;
    }
    await bootstrapEnv();
  };

  return (
    <div className="wizardOverlay" role="presentation">
      <div className="wizardModal" role="dialog" aria-modal="true" aria-labelledby="setup-wizard-title">
        <div className="wizardHeader">
          <div>
            <div className="eyebrow">First-run setup</div>
            <h2 id="setup-wizard-title">Configure Veyra</h2>
          </div>
          <button className="iconButton" type="button" onClick={onClose} aria-label="Close setup wizard">
            <X size={18} />
          </button>
        </div>

        <div className="wizardSteps" aria-label="Setup progress">
          {steps.map((item, index) => (
            <div key={item.id} className={`wizardStepMarker ${index <= stepIndex ? "active" : ""}`}>
              <span>{index + 1}</span>
              <small>{item.label}</small>
            </div>
          ))}
        </div>

        {error ? <div className="alert wizardAlert">{error}</div> : null}

        <div className="wizardBody">
          {step === "welcome" ? (
            <>
              <div className="wizardHero">
                <Sparkles size={28} />
                <p>
                  Welcome to Veyra. This wizard creates your local `.env`, optionally configures a Core model, and helps
                  you connect OpenClaw as the Agent runtime.
                </p>
              </div>
              <ul className="wizardList">
                <li>Configuration and runtime state stay local; enabled model and messaging integrations send the data required for their requests.</li>
                <li>Core model, OpenClaw, and Feishu are optional — you can skip and configure later.</li>
                <li>You can reopen this wizard anytime from the Runtime tab.</li>
              </ul>
            </>
          ) : null}

          {step === "bootstrap" ? (
            <>
              <p className="wizardIntro">Create the local configuration file Veyra uses on this machine.</p>
              <div className="wizardCardGrid">
                <div className="wizardCard">
                  <span>Environment file</span>
                  <strong>{setupPaths.env_exists === true ? "ready" : "will be created"}</strong>
                  <small>{String(setupPaths.env_file ?? ".env")}</small>
                </div>
                <div className="wizardCard">
                  <span>State root</span>
                  <strong>{String(setupPaths.state_root ?? "state")}</strong>
                  <small>Runtime logs, config, and local device material</small>
                </div>
              </div>
            </>
          ) : null}

          {step === "core_model" ? (
            <>
              <div className="wizardSectionTitle">
                <Brain size={18} />
                <span>Core model (optional)</span>
              </div>
              <label className="toggleRow">
                <input type="checkbox" checked={coreEnabled} onChange={(event) => setCoreEnabled(event.target.checked)} />
                <span>Use model inside Veyra Core</span>
              </label>
              <label className="toggleRow">
                <input type="checkbox" checked={skipCoreModel} onChange={(event) => setSkipCoreModel(event.target.checked)} />
                <span>Skip for now</span>
              </label>
              <div className="configGrid">
                <input value={coreBaseUrl} onChange={(event) => setCoreBaseUrl(event.target.value)} placeholder="https://api.moonshot.cn/v1" aria-label="Core model base URL" disabled={skipCoreModel} />
                <input value={coreModel} onChange={(event) => setCoreModel(event.target.value)} placeholder="moonshot-v1-auto" aria-label="Core model name" disabled={skipCoreModel} />
                <input value={CORE_MODEL_API_KEY_ENV} aria-label="Core model API key environment variable" disabled />
                <input type="password" autoComplete="new-password" value={coreApiKey} onChange={(event) => setCoreApiKey(event.target.value)} placeholder="API key (stored in .env)" aria-label="Core model API key" disabled={skipCoreModel} />
                <select value={decisionMode} onChange={(event) => setDecisionMode(event.target.value)} aria-label="Decision mode" disabled={skipCoreModel}>
                  <option value="auto">auto</option>
                  <option value="always">always</option>
                </select>
              </div>
            </>
          ) : null}

          {step === "agent" ? (
            <>
              <div className="wizardSectionTitle">
                <Bot size={18} />
                <span>Agent runtime (OpenClaw optional)</span>
              </div>
              <label className="toggleRow">
                <input type="checkbox" checked={useOpenClaw} onChange={(event) => setUseOpenClaw(event.target.checked)} />
                <span>Use OpenClaw as the selected Agent runtime</span>
              </label>
              <label className="toggleRow">
                <input type="checkbox" checked={skipOpenClaw} onChange={(event) => setSkipOpenClaw(event.target.checked)} />
                <span>Skip Agent setup for now</span>
              </label>
              <div className="wizardCardGrid">
                <div className="wizardCard">
                  <span>CLI installed</span>
                  <strong>{openclaw?.installed ? "yes" : "no"}</strong>
                  <small>{openclaw?.executable ?? "not found"}</small>
                </div>
                <div className="wizardCard">
                  <span>Gateway port 18789</span>
                  <strong>{openclaw?.port_listening ? "listening" : "not listening"}</strong>
                  <small>{openclaw?.agent_status ?? "unknown"}</small>
                </div>
                <div className="wizardCard">
                  <span>Agent connected</span>
                  <strong>{openclaw?.connected ? "connected" : "not connected"}</strong>
                  <small>{openclaw?.docs_hint ?? ""}</small>
                </div>
              </div>
              {!skipOpenClaw && useOpenClaw ? (
                <>
                  <div className="configGrid">
                    <input value={openclawBaseUrl} onChange={(event) => setOpenclawBaseUrl(event.target.value)} placeholder="http://127.0.0.1:18789" aria-label="OpenClaw base URL" />
                    <input type="password" autoComplete="new-password" value={openclawToken} onChange={(event) => setOpenclawToken(event.target.value)} placeholder="OPENCLAW gateway token (optional)" aria-label="OpenClaw gateway token" />
                  </div>
                  <div className="buttonRow compact wizardActionRow">
                    <button className="ghostButton" type="button" onClick={() => void refreshOpenClaw()} disabled={busy}>
                      <RefreshCw size={15} />
                      Recheck
                    </button>
                    <button className="ghostButton" type="button" onClick={() => void installOpenClaw(false)} disabled={busy || !openclaw?.npm_available}>
                      <Download size={15} />
                      Install OpenClaw CLI
                    </button>
                    <button className="primaryButton compactButton" type="button" onClick={() => void installOpenClaw(true)} disabled={busy || !openclaw?.npm_available}>
                      Install & start gateway
                    </button>
                  </div>
                  <div className="wizardHint">
                    Manual commands: <code>{openclaw?.install_command ?? "npm install -g openclaw@2026.6.11"}</code> then{" "}
                    <code>{openclaw?.start_command ?? "openclaw gateway"}</code>
                  </div>
                  {installLog ? <pre className="jsonBlock wizardLog">{installLog}</pre> : null}
                </>
              ) : null}
            </>
          ) : null}

          {step === "feishu" ? (
            <>
              <div className="wizardSectionTitle">
                <Bot size={18} />
                <span>Feishu intake (optional)</span>
              </div>
              <label className="toggleRow">
                <input type="checkbox" checked={useFeishu} onChange={(event) => {
                  setUseFeishu(event.target.checked);
                  setSkipFeishu(!event.target.checked);
                }} />
                <span>Use Feishu WebSocket intake</span>
              </label>
              <label className="toggleRow">
                <input type="checkbox" checked={skipFeishu} onChange={(event) => {
                  setSkipFeishu(event.target.checked);
                  if (event.target.checked) setUseFeishu(false);
                }} />
                <span>Skip Feishu setup for now</span>
              </label>
              <div className="wizardCardGrid">
                <div className="wizardCard">
                  <span>Configuration</span>
                  <strong>{feishuStatus?.configured ? "configured" : "not configured"}</strong>
                  <small>{feishuStatus?.connection_mode ?? "websocket"}</small>
                </div>
                <div className="wizardCard">
                  <span>WebSocket</span>
                  <strong>{feishuStatus?.thread_alive ? "running" : "not running"}</strong>
                  <small>{feishuStatus?.status ?? "unknown"}</small>
                </div>
                <div className="wizardCard">
                  <span>Inbound test</span>
                  <strong>{feishuStatus?.last_event_after_start ? "received" : feishuStatus?.thread_alive ? "waiting" : "pending"}</strong>
                  <small>{feishuStatus?.last_event_after_start ? "fresh event received" : "send a Feishu message to verify intake"}</small>
                </div>
              </div>
              {!skipFeishu && useFeishu ? (
                <>
                  <div className="configGrid">
                    <input value={feishuAppId} onChange={(event) => setFeishuAppId(event.target.value)} placeholder="FEISHU_APP_ID / cli_xxx" aria-label="Feishu app id" />
                    <input type="password" autoComplete="new-password" value={feishuAppSecret} onChange={(event) => setFeishuAppSecret(event.target.value)} placeholder="FEISHU_APP_SECRET (stored in .env)" aria-label="Feishu app secret" />
                    <input value={feishuDefaultReceiveId} onChange={(event) => setFeishuDefaultReceiveId(event.target.value)} placeholder="default chat_id or leave blank for replies" aria-label="Feishu default receive id" />
                    <select value={feishuReceiveIdType} onChange={(event) => setFeishuReceiveIdType(event.target.value)} aria-label="Feishu receive id type">
                      <option value="chat_id">chat_id</option>
                      <option value="open_id">open_id</option>
                      <option value="user_id">user_id</option>
                      <option value="union_id">union_id</option>
                      <option value="email">email</option>
                    </select>
                  </div>
                  <div className="buttonRow compact wizardActionRow">
                    <button className="ghostButton" type="button" onClick={() => void refreshFeishu()} disabled={busy}>
                      <RefreshCw size={15} />
                      Recheck
                    </button>
                  </div>
                </>
              ) : null}
            </>
          ) : null}

          {step === "review" ? (
            <>
              <div className="wizardHero">
                <CheckCircle2 size={28} />
                <p>Review your setup. You can change any of these later from the console tabs.</p>
              </div>
              <div className="wizardCardGrid">
                <div className="wizardCard">
                  <span>Local config</span>
                  <strong>{setupPaths.env_exists === true ? "ready" : "pending"}</strong>
                </div>
                <div className="wizardCard">
                  <span>Core model</span>
                  <strong>{skipCoreModel || !coreEnabled ? "skipped" : coreModel || "configured"}</strong>
                </div>
                <div className="wizardCard">
                  <span>Agent</span>
                  <strong>{skipOpenClaw || !useOpenClaw ? "skipped" : openclaw?.connected ? "connected" : openclaw?.port_listening ? "gateway up" : "needs setup"}</strong>
                </div>
                <div className="wizardCard">
                  <span>Feishu</span>
                  <strong>{skipFeishu || !useFeishu ? "skipped" : feishuStatus?.thread_alive ? "running" : "needs setup"}</strong>
                </div>
              </div>
            </>
          ) : null}
        </div>

        <div className="wizardFooter">
          <button className="ghostButton" type="button" onClick={() => setStep(steps[Math.max(stepIndex - 1, 0)].id)} disabled={busy || stepIndex === 0}>
            <ChevronLeft size={16} />
            Back
          </button>
          <div className="wizardFooterRight">
            {step === "welcome" ? (
              <button className="primaryButton" type="button" onClick={() => setStep("bootstrap")} disabled={busy}>
                Start
                <ChevronRight size={16} />
              </button>
            ) : null}
            {step === "bootstrap" ? (
              <button className="primaryButton" type="button" onClick={() => void nextFromBootstrap()} disabled={busy}>
                Continue
                <ChevronRight size={16} />
              </button>
            ) : null}
            {step === "core_model" ? (
              <button
                className="primaryButton"
                type="button"
                onClick={() => (skipCoreModel ? setStep("agent") : void saveCoreModel())}
                disabled={busy || (!skipCoreModel && coreEnabled && (!coreBaseUrl.trim() || !coreModel.trim()))}
              >
                Continue
                <ChevronRight size={16} />
              </button>
            ) : null}
            {step === "agent" ? (
              <button className="primaryButton" type="button" onClick={() => (skipOpenClaw || !useOpenClaw ? setStep("feishu") : void saveAgent())} disabled={busy}>
                Continue
                <ChevronRight size={16} />
              </button>
            ) : null}
            {step === "feishu" ? (
              <button
                className="primaryButton"
                type="button"
                onClick={() => (skipFeishu || !useFeishu ? setStep("review") : void saveFeishu())}
                disabled={busy || (!skipFeishu && useFeishu && (!feishuAppId.trim() || (!feishuAppSecret.trim() && feishuStatus?.app_secret_set !== true)))}
              >
                Continue
                <ChevronRight size={16} />
              </button>
            ) : null}
            {step === "review" ? (
              <button className="primaryButton" type="button" onClick={() => void finishWizard()} disabled={busy}>
                Finish setup
              </button>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
