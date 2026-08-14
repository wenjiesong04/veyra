import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  Bell,
  Bot,
  Brain,
  BookOpen,
  CheckCircle2,
  ClipboardList,
  Database,
  Eye,
  FileClock,
  FileText,
  Gauge,
  History,
  Layers,
  ListChecks,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  ScrollText,
  Settings,
  Shield,
  ThumbsDown,
  ThumbsUp,
  TerminalSquare,
  UserRound,
  XCircle
} from "lucide-react";
import { fetchJson, isDesktopRuntime, desktopApiBase } from "./api";
import { SetupWizard } from "./SetupWizard";
import "./styles.css";

type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

type RuntimeInfo = {
  identity: {
    name: string;
    full_name: string;
    selected_agent: string;
  };
  lifecycle: {
    status: string;
    started_at: string;
    last_heartbeat_at: string;
  };
  operational_mode: string[];
  core_model?: Record<string, JsonValue>;
};

type VeyraState = {
  user_world: Record<string, JsonValue>;
  local_world: Record<string, JsonValue>;
  external_world: Record<string, JsonValue>;
  executor_state: Record<string, JsonValue>;
  risk_state: Record<string, JsonValue>;
  belief_state: { claims?: Array<Record<string, JsonValue>> };
  task_state: Record<string, JsonValue>;
  attention_state: { focus?: string[]; ignored_noise?: string[] };
  review_queue?: { items?: Array<Record<string, JsonValue>> };
  rollback_state?: Record<string, JsonValue>;
};

type MessageResult = {
  event_id: string;
  route: string;
  status: string;
  response: string;
  risk_level: string;
  artifacts: Record<string, JsonValue>;
};

type LogResponse = {
  items: Array<Record<string, JsonValue>>;
};

type ArchitectureSnapshot = {
  blocks: Array<Record<string, JsonValue>>;
  core_modules: Array<Record<string, JsonValue>>;
  state_definitions: Array<Record<string, JsonValue>>;
  implementation_phases: Array<Record<string, JsonValue>>;
  risk_levels: Array<Record<string, JsonValue>>;
  lifecycle_statuses: string[];
  operational_modes: string[];
};

type Definitions = {
  risk_levels: Array<Record<string, JsonValue>>;
  lifecycle_statuses: string[];
  operational_modes: string[];
};

type OwnerScope = {
  userId: string;
  sessionId: string;
};

type ScopedCollection = {
  status?: JsonValue;
  count?: JsonValue;
  hypothesis_count?: JsonValue;
  items?: JsonValue;
  status_counts?: JsonValue;
  state_revision?: JsonValue;
  policy?: JsonValue;
  authority?: JsonValue;
};

type Phase6StageStatus = {
  id: string;
  label: string;
  access: "public_status" | "private_control";
  phase: string;
  health: string;
  status: string;
  scope: string;
};

type WorkbenchSection = "awareness" | "governance" | "runtime" | "ops" | "audit" | "logs";

type SuggestionFeedbackLabel =
  | "useful"
  | "not_useful"
  | "too_frequent"
  | "wrong_timing"
  | "wrong_evidence";

const initialMessage = "帮我看 18789 端口有没有被占用";
const defaultOwnerScope: OwnerScope = {
  userId: "console-user",
  sessionId: "console-session"
};
const ownerScopeStorageKey = "veyra.console.owner-scope.v1";

function readOwnerScope(): OwnerScope {
  if (typeof window === "undefined") return defaultOwnerScope;
  try {
    const stored = JSON.parse(window.localStorage.getItem(ownerScopeStorageKey) ?? "{}") as Partial<OwnerScope>;
    const userId = String(stored.userId ?? "").trim();
    const sessionId = String(stored.sessionId ?? "").trim();
    if (userId && sessionId) return { userId, sessionId };
  } catch {
    // Keep deterministic, non-secret Console defaults if local preferences are invalid.
  }
  return defaultOwnerScope;
}

function ownerQuery(scope: OwnerScope): string {
  return `user_id=${encodeURIComponent(scope.userId)}&session_id=${encodeURIComponent(scope.sessionId)}`;
}

function jsonItems(value: JsonValue | undefined): Array<Record<string, JsonValue>> {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is Record<string, JsonValue> => Boolean(item) && typeof item === "object" && !Array.isArray(item)
  );
}

function phase6StatusValue(payload: Record<string, JsonValue>, key: "phase" | "status" | "health"): string {
  if (key === "health") {
    return String(payload.operational_health ?? payload.availability ?? payload.status ?? "unknown");
  }
  return String(payload[key] ?? "unknown");
}

const workbenchSections: Array<{ id: WorkbenchSection; label: string; icon: React.ReactNode }> = [
  { id: "awareness", label: "Awareness", icon: <Gauge size={15} /> },
  { id: "governance", label: "Governance", icon: <Shield size={15} /> },
  { id: "runtime", label: "Runtime", icon: <Bot size={15} /> },
  { id: "ops", label: "Ops", icon: <Activity size={15} /> },
  { id: "audit", label: "Audit", icon: <History size={15} /> },
  { id: "logs", label: "Logs", icon: <ScrollText size={15} /> }
];

function StatusPill({ value }: { value: string }) {
  const normalized = value.trim().toLowerCase();
  const dangerStates = [
    "blocked",
    "critical",
    "configuration_missing",
    "degraded",
    "denied",
    "error",
    "fail_closed",
    "failed",
    "indeterminate",
    "not_certified",
    "not_configured",
    "not_ready",
    "overconservative_alert",
    "rejected",
    "timeout",
    "timed_out",
    "unconfigured",
    "unavailable"
  ];
  const goodStates = [
    "active",
    "available",
    "certified",
    "configured",
    "connected",
    "passed",
    "ready",
    "success"
  ];
  const matchesDanger = dangerStates.some((state) => normalized === state || normalized.includes(state));
  const tone = normalized === "r5" || matchesDanger ? "danger" : normalized === "r1" || goodStates.includes(normalized) ? "good" : "neutral";
  return <span className={`pill ${tone}`}>{value}</span>;
}

function Section({
  title,
  icon,
  children,
  action
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <section className="panel">
      <div className="panelHeader">
        <div className="panelTitle">
          {icon}
          <h2>{title}</h2>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="jsonBlock">{JSON.stringify(value, null, 2)}</pre>;
}

function asRecord(value: JsonValue | undefined): Record<string, JsonValue> {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function editableOpenClawUrl(value: string): string {
  const raw = value.trim();
  if (!raw) return "http://127.0.0.1:18789";
  if (raw.startsWith("ws://")) return `http://${raw.slice(5)}`;
  if (raw.startsWith("wss://")) return `https://${raw.slice(6)}`;
  return raw;
}

function compactJson(value: JsonValue | undefined, fallback = "-") {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function toolTarget(item: Record<string, JsonValue>) {
  if (Array.isArray(item.command)) return item.command.map(String).join(" ");
  return String(item.path ?? item.operation ?? "-");
}

function memoryPatch(item: Record<string, JsonValue>) {
  return asRecord(item.patch);
}

function traceTarget(item: Record<string, JsonValue>) {
  return compactJson(item.target ?? item.task_id ?? item.event_id);
}

function statusCounts(items: Array<Record<string, JsonValue>>, key = "status") {
  return items.reduce<Record<string, number>>((acc, item) => {
    const value = String(item[key] ?? "unknown");
    acc[value] = (acc[value] ?? 0) + 1;
    return acc;
  }, {});
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function LegacyConsole() {
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null);
  const [state, setState] = useState<VeyraState | null>(null);
  const [events, setEvents] = useState<LogResponse>({ items: [] });
  const [actions, setActions] = useState<LogResponse>({ items: [] });
  const [reviews, setReviews] = useState<LogResponse>({ items: [] });
  const [toolLogs, setToolLogs] = useState<LogResponse>({ items: [] });
  const [policyLogs, setPolicyLogs] = useState<LogResponse>({ items: [] });
  const [toolProxyStatus, setToolProxyStatus] = useState<Record<string, JsonValue> | null>(null);
  const [executionLogs, setExecutionLogs] = useState<LogResponse>({ items: [] });
  const [memoryLogs, setMemoryLogs] = useState<LogResponse>({ items: [] });
  const [memorySummary, setMemorySummary] = useState<Record<string, JsonValue> | null>(null);
  const [memoryDiagnostics, setMemoryDiagnostics] = useState<Record<string, JsonValue> | null>(null);
  const [coreModelLogs, setCoreModelLogs] = useState<LogResponse>({ items: [] });
  const [alertLogs, setAlertLogs] = useState<LogResponse>({ items: [] });
  const [auditJournal, setAuditJournal] = useState<Record<string, JsonValue> | null>(null);
  const [timeTravel, setTimeTravel] = useState<Record<string, JsonValue> | null>(null);
  const [agentStatus, setAgentStatus] = useState<Record<string, JsonValue> | null>(null);
  const [agentRegistry, setAgentRegistry] = useState<Record<string, JsonValue> | null>(null);
  const [agentContract, setAgentContract] = useState<Record<string, JsonValue> | null>(null);
  const [coreModelStatus, setCoreModelStatus] = useState<Record<string, JsonValue> | null>(null);
  const [mvpStatus, setMvpStatus] = useState<Record<string, JsonValue> | null>(null);
  const [opsHealth, setOpsHealth] = useState<Record<string, JsonValue> | null>(null);
  const [retentionStatus, setRetentionStatus] = useState<Record<string, JsonValue> | null>(null);
  const [soakStatus, setSoakStatus] = useState<Record<string, JsonValue> | null>(null);
  const [runtimeMatrix, setRuntimeMatrix] = useState<Record<string, JsonValue> | null>(null);
  const [deploymentReadiness, setDeploymentReadiness] = useState<Record<string, JsonValue> | null>(null);
  const [setupStatus, setSetupStatus] = useState<Record<string, JsonValue> | null>(null);
  const [alertingStatus, setAlertingStatus] = useState<Record<string, JsonValue> | null>(null);
  const [runtimeTraceRecent, setRuntimeTraceRecent] = useState<LogResponse>({ items: [] });
  const [runtimeMetricsSummary, setRuntimeMetricsSummary] = useState<Record<string, JsonValue> | null>(null);
  const [runtimeMetricsRoutes, setRuntimeMetricsRoutes] = useState<Record<string, JsonValue> | null>(null);
  const [runtimeMetricsModelCost, setRuntimeMetricsModelCost] = useState<Record<string, JsonValue> | null>(null);
  const [runtimeMetricsFailures, setRuntimeMetricsFailures] = useState<LogResponse>({ items: [] });
  const [runtimeSoakTelemetry, setRuntimeSoakTelemetry] = useState<Record<string, JsonValue> | null>(null);
  const [architecture, setArchitecture] = useState<ArchitectureSnapshot | null>(null);
  const [definitions, setDefinitions] = useState<Definitions | null>(null);
  const [heartbeat, setHeartbeat] = useState("");
  const [rollbackLogs, setRollbackLogs] = useState<LogResponse>({ items: [] });
  const [diffStatus, setDiffStatus] = useState<Record<string, JsonValue> | null>(null);
  const [rollbackDiff, setRollbackDiff] = useState<Record<string, JsonValue> | null>(null);
  const [isolatedRunnerStatus, setIsolatedRunnerStatus] = useState<Record<string, JsonValue> | null>(null);
  const [isolatedRunnerStatusError, setIsolatedRunnerStatusError] = useState<string | null>(null);
  const [ownerScope, setOwnerScope] = useState<OwnerScope>(() => readOwnerScope());
  const [ownerUserDraft, setOwnerUserDraft] = useState(() => readOwnerScope().userId);
  const [ownerSessionDraft, setOwnerSessionDraft] = useState(() => readOwnerScope().sessionId);
  const [scopedAttention, setScopedAttention] = useState<Record<string, JsonValue> | null>(null);
  const [cognitiveLoopStatus, setCognitiveLoopStatus] = useState<Record<string, JsonValue> | null>(null);
  const [generalSituations, setGeneralSituations] = useState<ScopedCollection>({});
  const [attentionHypotheses, setAttentionHypotheses] = useState<ScopedCollection>({});
  const [beliefStatus, setBeliefStatus] = useState<Record<string, JsonValue> | null>(null);
  const [externalScope, setExternalScope] = useState<Record<string, JsonValue> | null>(null);
  const [suggestionStatus, setSuggestionStatus] = useState<Record<string, JsonValue> | null>(null);
  const [suggestionInbox, setSuggestionInbox] = useState<ScopedCollection>({});
  const [suggestionFeedback, setSuggestionFeedback] = useState<ScopedCollection>({});
  const suggestionFeedbackCommandIds = useRef<Map<string, string>>(new Map());
  const [suggestionCalibration, setSuggestionCalibration] = useState<Record<string, JsonValue> | null>(null);
  const [awarenessPanelError, setAwarenessPanelError] = useState<string | null>(null);
  const [phase6Stages, setPhase6Stages] = useState<Phase6StageStatus[]>([]);
  const [phase6StatusUpdatedAt, setPhase6StatusUpdatedAt] = useState<string | null>(null);
  const [message, setMessage] = useState(initialMessage);
  const [snapshotPath, setSnapshotPath] = useState("README.md");
  const [agentBaseUrl, setAgentBaseUrl] = useState("");
  const [coreModelEnabled, setCoreModelEnabled] = useState(false);
  const [coreModelBaseUrl, setCoreModelBaseUrl] = useState("");
  const [coreModelName, setCoreModelName] = useState("");
  const [coreModelKeyEnv, setCoreModelKeyEnv] = useState("VEYRA_CORE_MODEL_API_KEY");
  const [coreModelDecisionMode, setCoreModelDecisionMode] = useState("auto");
  const [watchTarget, setWatchTarget] = useState("localhost");
  const [result, setResult] = useState<MessageResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [backendBooting, setBackendBooting] = useState(isDesktopRuntime);
  const [showSetupWizard, setShowSetupWizard] = useState(true);
  const [activeSection, setActiveSection] = useState<WorkbenchSection>("awareness");

  const applySetupStatus = (setup: Record<string, JsonValue>) => {
    setSetupStatus(setup);
    const wizard = asRecord(setup.wizard);
    if (wizard.should_show === true) {
      setShowSetupWizard(true);
      return;
    }
    if (wizard.completed === true) {
      setShowSetupWizard(false);
    }
  };

  const refreshScopedAwareness = async (scope: OwnerScope = ownerScope) => {
    const query = ownerQuery(scope);
    const results = await Promise.allSettled([
      fetchJson<Record<string, JsonValue>>(`/attention/active?${query}`),
      fetchJson<Record<string, JsonValue>>("/awareness/cognitive-loop/status"),
      fetchJson<ScopedCollection>(`/awareness/general-situations?${query}&limit=50`),
      fetchJson<ScopedCollection>(`/awareness/attention-hypotheses?${query}&limit=50`),
      fetchJson<Record<string, JsonValue>>(`/belief/status?${query}&limit=50`),
      fetchJson<Record<string, JsonValue>>(`/external/watchlist?${query}&limit=50`),
      fetchJson<Record<string, JsonValue>>("/awareness/suggestions/status"),
      fetchJson<ScopedCollection>(`/awareness/suggestions/inbox?${query}&limit=50`),
      fetchJson<ScopedCollection>(`/awareness/suggestions/feedback?${query}&active_only=true&limit=100`),
      fetchJson<Record<string, JsonValue>>(`/awareness/suggestions/calibration?${query}`)
    ]);
    const [
      attentionResult,
      cognitiveLoopResult,
      situationResult,
      hypothesisResult,
      beliefResult,
      externalResult,
      suggestionStatusResult,
      inboxResult,
      feedbackResult,
      calibrationResult
    ] = results;
    setScopedAttention(attentionResult.status === "fulfilled" ? attentionResult.value : null);
    setCognitiveLoopStatus(cognitiveLoopResult.status === "fulfilled" ? cognitiveLoopResult.value : null);
    setGeneralSituations(situationResult.status === "fulfilled" ? situationResult.value : {});
    setAttentionHypotheses(hypothesisResult.status === "fulfilled" ? hypothesisResult.value : {});
    setBeliefStatus(beliefResult.status === "fulfilled" ? beliefResult.value : null);
    setExternalScope(externalResult.status === "fulfilled" ? externalResult.value : null);
    setSuggestionStatus(suggestionStatusResult.status === "fulfilled" ? suggestionStatusResult.value : null);
    setSuggestionInbox(inboxResult.status === "fulfilled" ? inboxResult.value : {});
    setSuggestionFeedback(feedbackResult.status === "fulfilled" ? feedbackResult.value : {});
    setSuggestionCalibration(calibrationResult.status === "fulfilled" ? calibrationResult.value : null);
    const unavailable = [
      attentionResult.status === "rejected" ? "attention" : null,
      cognitiveLoopResult.status === "rejected" ? "cognitive loop" : null,
      situationResult.status === "rejected" ? "general situations" : null,
      hypothesisResult.status === "rejected" ? "attention hypotheses" : null,
      beliefResult.status === "rejected" ? "belief status" : null,
      externalResult.status === "rejected" ? "external world" : null,
      suggestionStatusResult.status === "rejected" ? "suggestion status" : null,
      inboxResult.status === "rejected" ? "suggestion inbox" : null,
      feedbackResult.status === "rejected" ? "suggestion feedback" : null,
      calibrationResult.status === "rejected" ? "suggestion calibration" : null
    ].filter(Boolean);
    setAwarenessPanelError(
      unavailable.length ? `${unavailable.join(", ")} unavailable for this exact owner/session scope` : null
    );
  };

  const refreshPhase6Overview = async () => {
    const probes = [
      {
        id: "spec",
        label: "Spec quarantine",
        endpoint: "/phase6/extensions/status",
        fallbackPhase: "6.2a",
        fallbackScope: "source-free specification quarantine"
      },
      {
        id: "generation",
        label: "Bounded generation",
        endpoint: "/phase6/extensions/generations/status",
        fallbackPhase: "6.2e",
        fallbackScope: "model output to private artifact quarantine"
      },
      {
        id: "artifact",
        label: "Artifact quarantine",
        endpoint: "/phase6/extensions/artifacts/status",
        fallbackPhase: "6.2b",
        fallbackScope: "bounded private source artifact quarantine"
      },
      {
        id: "source_check",
        label: "Static source gate",
        endpoint: "/phase6/extensions/source-checks/status",
        fallbackPhase: "6.2c",
        fallbackScope: "non-executing source policy check"
      },
      {
        id: "isolated_runner",
        label: "Isolated runner probe",
        endpoint: "/phase6/extensions/isolated-runs/status",
        fallbackPhase: "6.2d",
        fallbackScope: "trusted containment prerequisite probe"
      },
      {
        id: "dynamic_validation",
        label: "Dynamic validation",
        endpoint: "/phase6/extensions/dynamic-validations/status",
        fallbackPhase: "6.2e",
        fallbackScope: "fixed isolated unit, contract, security, fuzz and behavior validation"
      }
    ];
    const visibleStages = await Promise.all(
      probes.map(async (probe): Promise<Phase6StageStatus> => {
        try {
          const payload = await fetchJson<Record<string, JsonValue>>(probe.endpoint);
          return {
            id: probe.id,
            label: probe.label,
            access: "public_status",
            phase: phase6StatusValue(payload, "phase") === "unknown" ? probe.fallbackPhase : phase6StatusValue(payload, "phase"),
            health: phase6StatusValue(payload, "health"),
            status: phase6StatusValue(payload, "status"),
            scope: String(payload.completion_scope ?? probe.fallbackScope)
          };
        } catch {
          return {
            id: probe.id,
            label: probe.label,
            access: "public_status",
            phase: probe.fallbackPhase,
            health: "unavailable",
            status: "status_endpoint_unavailable",
            scope: probe.fallbackScope
          };
        }
      })
    );
    const privateStages: Phase6StageStatus[] = [
      {
        id: "capability_gap",
        label: "Capability-gap registry",
        access: "private_control",
        phase: "6.2h",
        health: "protected",
        status: "operator_auth_required",
        scope: "owner/session records are available only to authenticated control-plane clients"
      },
      {
        id: "signed_release",
        label: "Signed release registry",
        access: "private_control",
        phase: "6.2f",
        health: "protected",
        status: "operator_auth_required",
        scope: "signing identity and private release records are not exposed to the browser"
      },
      {
        id: "deployment",
        label: "Canary & promotion",
        access: "private_control",
        phase: "6.2g",
        health: "protected",
        status: "operator_auth_required",
        scope: "transitions remain review-bound and executable only in the trusted isolated runner"
      },
      {
        id: "governed_pipeline",
        label: "Governed extension pipeline",
        access: "private_control",
        phase: "6.2i",
        health: "protected",
        status: "operator_auth_required",
        scope: "explicit start/resume composes every gate but cannot approve scoped canary or promotion"
      }
    ];
    setPhase6Stages([privateStages[0], ...visibleStages, ...privateStages.slice(1)]);
    setPhase6StatusUpdatedAt(new Date().toISOString());
  };

  const refreshExtensionIsolation = async () => {
    try {
      const status = await fetchJson<Record<string, JsonValue>>("/phase6/extensions/isolated-runs/status");
      setIsolatedRunnerStatus(status);
      setIsolatedRunnerStatusError(null);
    } catch (caught) {
      setIsolatedRunnerStatus(null);
      setIsolatedRunnerStatusError(caught instanceof Error ? caught.message : "Status endpoint unavailable");
    }
  };

  const refresh = async (scope: OwnerScope = ownerScope) => {
    // Phase 6 is an additive, fail-closed surface. Its absence must not make the
    // established Console refresh fail or mask the rest of Veyra's state.
    void refreshExtensionIsolation();
    void refreshPhase6Overview();
    void refreshScopedAwareness(scope);
    const scopeQuery = ownerQuery(scope);
    const [
      runtimeData,
      stateData,
      eventData,
      actionData,
      reviewData,
      toolData,
      policyData,
      toolProxyData,
      executionData,
      memoryData,
      memorySummaryData,
      memoryDiagnosticsData,
      coreModelLogData,
      alertLogData,
      auditJournalData,
      timeTravelData,
      rollbackData,
      agentData,
      registryData,
      contractData,
      coreModelData,
      mvpData,
      opsHealthData,
      retentionData,
      soakData,
      runtimeMatrixData,
      deploymentData,
      setupData,
      alertingData,
      runtimeTraceData,
      runtimeMetricsSummaryData,
      runtimeMetricsRoutesData,
      runtimeMetricsCostData,
      runtimeMetricsFailureData,
      runtimeSoakTelemetryData,
      architectureData,
      definitionsData,
      heartbeatData,
      diffData
    ] = await Promise.all([
      fetchJson<RuntimeInfo>("/runtime"),
      fetchJson<VeyraState>("/state"),
      fetchJson<LogResponse>("/logs/events?limit=20"),
      fetchJson<LogResponse>("/logs/actions?limit=20"),
      fetchJson<LogResponse>("/reviews/actions?limit=20"),
      fetchJson<LogResponse>("/logs/tools?limit=20"),
      fetchJson<LogResponse>("/logs/policy?limit=20"),
      fetchJson<Record<string, JsonValue>>("/tool-proxy/status"),
      fetchJson<LogResponse>("/logs/execution?limit=20"),
      fetchJson<LogResponse>(
        `/logs/memory?limit=20&${scopeQuery}`
      ),
      fetchJson<Record<string, JsonValue>>(
        `/memory/summary?provider=local&${scopeQuery}`
      ),
      fetchJson<Record<string, JsonValue>>(
        `/memory/providers/diagnostics?provider=all&${scopeQuery}`
      ),
      fetchJson<LogResponse>("/logs/core-model?limit=20"),
      fetchJson<LogResponse>("/logs/alerts?limit=20"),
      fetchJson<Record<string, JsonValue>>("/audit/journal?limit=40"),
      fetchJson<Record<string, JsonValue>>("/audit/time-travel?limit=40"),
      fetchJson<LogResponse>("/logs/rollback?limit=20"),
      fetchJson<Record<string, JsonValue>>("/agent/status"),
      fetchJson<Record<string, JsonValue>>("/agents"),
      fetchJson<Record<string, JsonValue>>("/agent/contract"),
      fetchJson<Record<string, JsonValue>>("/core/model/status"),
      fetchJson<Record<string, JsonValue>>("/mvp/status"),
      fetchJson<Record<string, JsonValue>>("/ops/health"),
      fetchJson<Record<string, JsonValue>>("/ops/retention"),
      fetchJson<Record<string, JsonValue>>("/ops/soak/status"),
      fetchJson<Record<string, JsonValue>>("/ops/runtime-matrix"),
      fetchJson<Record<string, JsonValue>>("/ops/deployment"),
      fetchJson<Record<string, JsonValue>>("/setup/status"),
      fetchJson<Record<string, JsonValue>>("/ops/alerting"),
      fetchJson<LogResponse>("/runtime/traces/recent?limit=20"),
      fetchJson<Record<string, JsonValue>>("/runtime/metrics/summary?limit=1000"),
      fetchJson<Record<string, JsonValue>>("/runtime/metrics/routes?limit=1000"),
      fetchJson<Record<string, JsonValue>>("/runtime/metrics/model-cost?limit=1000"),
      fetchJson<LogResponse>("/runtime/metrics/failures?limit=20"),
      fetchJson<Record<string, JsonValue>>("/runtime/soak/status"),
      fetchJson<ArchitectureSnapshot>("/architecture"),
      fetchJson<Definitions>("/definitions"),
      fetchJson<{ heartbeat: string }>("/heartbeat"),
      fetchJson<Record<string, JsonValue>>("/rollback/diff")
    ]);
    setRuntime(runtimeData);
    setState(stateData);
    setEvents(eventData);
    setActions(actionData);
    setReviews(reviewData);
    setToolLogs(toolData);
    setPolicyLogs(policyData);
    setToolProxyStatus(toolProxyData);
    setExecutionLogs(executionData);
    setMemoryLogs(memoryData);
    setMemorySummary(memorySummaryData);
    setMemoryDiagnostics(memoryDiagnosticsData);
    setCoreModelLogs(coreModelLogData);
    setAlertLogs(alertLogData);
    setAuditJournal(auditJournalData);
    setTimeTravel(timeTravelData);
    setRollbackLogs(rollbackData);
    setAgentStatus(agentData);
    setAgentRegistry(registryData);
    setAgentContract(contractData);
    setCoreModelStatus(coreModelData);
    setMvpStatus(mvpData);
    setOpsHealth(opsHealthData);
    setRetentionStatus(retentionData);
    setSoakStatus(soakData);
    setRuntimeMatrix(runtimeMatrixData);
    setDeploymentReadiness(deploymentData);
    applySetupStatus(setupData);
    setAlertingStatus(alertingData);
    setRuntimeTraceRecent(runtimeTraceData);
    setRuntimeMetricsSummary(runtimeMetricsSummaryData);
    setRuntimeMetricsRoutes(runtimeMetricsRoutesData);
    setRuntimeMetricsModelCost(runtimeMetricsCostData);
    setRuntimeMetricsFailures(runtimeMetricsFailureData);
    setRuntimeSoakTelemetry(runtimeSoakTelemetryData);
    setArchitecture(architectureData);
    setDefinitions(definitionsData);
    setHeartbeat(heartbeatData.heartbeat);
    setDiffStatus(diffData);
    setCoreModelEnabled(coreModelData.enabled === true);
    setCoreModelBaseUrl(String(coreModelData.base_url ?? ""));
    setCoreModelName(String(coreModelData.model ?? ""));
    setCoreModelKeyEnv(String(coreModelData.api_key_env ?? "VEYRA_CORE_MODEL_API_KEY"));
    setCoreModelDecisionMode(String(coreModelData.decision_mode ?? "auto"));
  };

  useEffect(() => {
    let cancelled = false;
    const probeSetup = async (attempt = 0) => {
      try {
        const setup = await fetchJson<Record<string, JsonValue>>("/setup/status");
        if (!cancelled) {
          applySetupStatus(setup);
        }
      } catch {
        if (!cancelled && attempt < 40) {
          window.setTimeout(() => {
            if (!cancelled) void probeSetup(attempt + 1);
          }, 1000);
          return;
        }
        if (!cancelled) {
          setShowSetupWizard(true);
        }
      }
    };
    void probeSetup();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = async (attempt = 0) => {
      try {
        await refresh();
        if (!cancelled) {
          setError(null);
          setBackendBooting(false);
        }
      } catch (caught) {
        const message = caught instanceof Error ? caught.message : "Unknown error";
        if (isDesktopRuntime && attempt < 40) {
          if (!cancelled) {
            setBackendBooting(true);
            setError(`Starting local Veyra runtime... ${message}`);
          }
          window.setTimeout(() => {
            if (!cancelled) void load(attempt + 1);
          }, 1000);
          return;
        }
        if (!cancelled) {
          setBackendBooting(false);
          setError(message);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  const manualRefresh = async () => {
    setLoading(true);
    setError(null);
    try {
      await refresh();
      setBackendBooting(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const applyOwnerScope = async () => {
    const nextScope = {
      userId: ownerUserDraft.trim(),
      sessionId: ownerSessionDraft.trim()
    };
    if (!nextScope.userId || !nextScope.sessionId) {
      setError("Owner and session are both required; Veyra will not fall back to an unscoped read.");
      return;
    }
    if (nextScope.userId.length > 240 || nextScope.sessionId.length > 240) {
      setError("Owner and session identifiers must be 240 characters or fewer.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setOwnerScope(nextScope);
      window.localStorage.setItem(ownerScopeStorageKey, JSON.stringify(nextScope));
      await refresh(nextScope);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to load the selected owner scope");
    } finally {
      setLoading(false);
    }
  };

  const configureSuggestionMode = async (mode: string) => {
    const revision = Number(suggestionStatus?.ops_config_revision);
    if (!Number.isInteger(revision) || revision < 0) {
      setError("Suggestion configuration revision is unavailable; refresh before changing mode.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await fetchJson<Record<string, JsonValue>>("/awareness/suggestions/config", {
        method: "POST",
        body: JSON.stringify({ mode, expected_state_revision: revision })
      });
      await refreshScopedAwareness(ownerScope);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to update suggestion mode");
      await refreshScopedAwareness(ownerScope);
    } finally {
      setLoading(false);
    }
  };

  const configureSuggestionSandbox = async (sandboxEnabled: boolean) => {
    const revision = Number(suggestionInbox.state_revision);
    if (!Number.isInteger(revision) || revision < 0) {
      setError("Suggestion inbox revision is unavailable; refresh before changing the sandbox policy.");
      return;
    }
    const currentPolicy = asRecord(suggestionInbox.policy);
    const quietHours = currentPolicy.quiet_hours;
    const boundedNumber = (value: JsonValue | undefined, fallback: number) => {
      const selected = Number(value);
      return Number.isInteger(selected) && selected >= 0 ? selected : fallback;
    };
    setLoading(true);
    setError(null);
    try {
      await fetchJson<Record<string, JsonValue>>("/awareness/suggestions/policy", {
        method: "POST",
        body: JSON.stringify({
          user_id: ownerScope.userId,
          session_id: ownerScope.sessionId,
          sandbox_enabled: sandboxEnabled,
          daily_budget: boundedNumber(currentPolicy.daily_budget, 1),
          timezone: String(
            currentPolicy.timezone ??
              asRecord(currentPolicy.quiet_hours).timezone ??
              Intl.DateTimeFormat().resolvedOptions().timeZone ??
              "UTC"
          ),
          quiet_hours:
            quietHours && typeof quietHours === "object" && !Array.isArray(quietHours)
              ? quietHours
              : null,
          cooldown_seconds: boundedNumber(currentPolicy.cooldown_seconds, 3600),
          dismiss_cooldown_seconds: boundedNumber(currentPolicy.dismiss_cooldown_seconds, 86400),
          expected_state_revision: revision
        })
      });
      await refreshScopedAwareness(ownerScope);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to update the owner-scoped suggestion sandbox");
      await refreshScopedAwareness(ownerScope);
    } finally {
      setLoading(false);
    }
  };

  const respondWithSuggestionFeedback = async (
    proposal: Record<string, JsonValue>,
    label: SuggestionFeedbackLabel,
    existingFeedback?: Record<string, JsonValue>
  ) => {
    const proposalId = String(proposal.proposal_id ?? "");
    const proposalRevision = String(proposal.proposal_revision ?? "");
    const generalSituationId = String(proposal.general_situation_id ?? "");
    const parentRevision = Number(proposal.parent_revision);
    const outboxRevision = Number(suggestionInbox.state_revision);
    if (
      suggestionFeedback.status !== "success" ||
      String(proposal.schema_version ?? "") !== "veyra.informational_suggestion.v2" ||
      !proposalId ||
      !proposalRevision ||
      !generalSituationId ||
      !Number.isInteger(parentRevision) ||
      parentRevision < 1 ||
      !Number.isInteger(outboxRevision) ||
      outboxRevision < 0
    ) {
      setError("The feedback ledger or exact proposal revision is unavailable; feedback remains locked.");
      return;
    }
    if (String(existingFeedback?.label ?? "") === label) return;
    const supersedes = String(existingFeedback?.learning_id ?? "").trim();
    const commandKey = `${proposalRevision}:${label}:${supersedes || "initial"}`;
    let feedbackId = suggestionFeedbackCommandIds.current.get(commandKey);
    if (!feedbackId) {
      feedbackId = `sfb_${crypto.randomUUID()}`;
      suggestionFeedbackCommandIds.current.set(commandKey, feedbackId);
    }
    setLoading(true);
    setError(null);
    try {
      await fetchJson<Record<string, JsonValue>>(
        `/awareness/suggestions/${encodeURIComponent(proposalId)}/feedback`,
        {
          method: "POST",
          body: JSON.stringify({
            schema_version: "veyra.suggestion_feedback_command.v1",
            feedback_id: feedbackId,
            user_id: ownerScope.userId,
            session_id: ownerScope.sessionId,
            proposal_revision: proposalRevision,
            general_situation_id: generalSituationId,
            parent_revision: parentRevision,
            label,
            expected_outbox_state_revision: outboxRevision,
            supersedes_learning_id: supersedes || null
          })
        }
      );
      suggestionFeedbackCommandIds.current.delete(commandKey);
      await refreshScopedAwareness(ownerScope);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unable to record suggestion feedback");
      await refreshScopedAwareness(ownerScope);
    } finally {
      setLoading(false);
    }
  };

  const respondToSuggestion = async (proposalId: string, action: "ack" | "dismiss") => {
    const revision = Number(suggestionInbox.state_revision);
    if (!proposalId || !Number.isInteger(revision) || revision < 0) {
      setError("Suggestion inbox revision is unavailable; refresh before responding.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await fetchJson<Record<string, JsonValue>>(
        `/awareness/suggestions/${encodeURIComponent(proposalId)}/${action}`,
        {
          method: "POST",
          body: JSON.stringify({
            user_id: ownerScope.userId,
            session_id: ownerScope.sessionId,
            expected_state_revision: revision,
            reason: `console_${action}`
          })
        }
      );
      await refreshScopedAwareness(ownerScope);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : `Unable to ${action} suggestion`);
      await refreshScopedAwareness(ownerScope);
    } finally {
      setLoading(false);
    }
  };

  const submit = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<MessageResult>("/events/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: message,
          channel: "console",
          user_id: ownerScope.userId,
          session_id: ownerScope.sessionId
        })
      });
      setResult(response);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const decideReview = async (reviewId: string, decision: "approve" | "reject") => {
    setLoading(true);
    setError(null);
    try {
      await fetchJson(`/reviews/${reviewId}/${decision}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: `console_${decision}` })
      });
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const runProactiveCheck = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<MessageResult | Record<string, JsonValue>>("/proactive/check", {
        method: "POST"
      });
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const createSnapshot = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>("/rollback/snapshot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: snapshotPath })
      });
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const restoreSnapshot = async (snapshotId: string) => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>(`/rollback/${snapshotId}/restore`, {
        method: "POST"
      });
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const viewSnapshotDiff = async (snapshotId: string) => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>(`/rollback/${snapshotId}/diff`);
      setRollbackDiff(response);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const selectAgent = async (name: string) => {
    setLoading(true);
    setError(null);
    try {
      await fetchJson("/agents/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name })
      });
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const saveSelectedAgentUrl = async () => {
    const selected = String(agentRegistry?.selected_agent ?? runtime?.identity.selected_agent ?? "openclaw");
    const normalizedBaseUrl = selected === "openclaw" ? editableOpenClawUrl(agentBaseUrl) : agentBaseUrl.trim();
    setLoading(true);
    setError(null);
    try {
      if (selected === "openclaw") {
        await fetchJson("/setup/env", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            values: {
              VEYRA_SELECTED_AGENT: "openclaw",
              OPENCLAW_BASE_URL: normalizedBaseUrl,
              VEYRA_OPENCLAW_USE_LOCAL_CONFIG: 1
            }
          })
        });
      }
      await fetchJson(`/agents/${selected}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          selected === "openclaw"
            ? { kind: "openclaw", base_url: normalizedBaseUrl, api_key_env: "OPENCLAW_GATEWAY_TOKEN", enabled: true }
            : { base_url: normalizedBaseUrl }
        )
      });
      setAgentBaseUrl(normalizedBaseUrl);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const saveCoreModelConfig = async () => {
    setLoading(true);
    setError(null);
    try {
      await fetchJson("/core/model/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: coreModelEnabled,
          provider: "openai_compatible",
          base_url: coreModelBaseUrl,
          model: coreModelName,
          api_key_env: coreModelKeyEnv,
          decision_mode: coreModelDecisionMode
        })
      });
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const addWatchTarget = async () => {
    setLoading(true);
    setError(null);
    try {
      await fetchJson("/external/watchlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: ownerScope.userId,
          session_id: ownerScope.sessionId,
          target: watchTarget,
          reason: "console_watch",
          enabled: true
        })
      });
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const refreshExternalWorld = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>(`/external/refresh?limit=5&${ownerQuery(ownerScope)}`, { method: "POST" });
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const dispatchAlerts = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>("/ops/alerts/dispatch?min_severity=info", { method: "POST" });
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const enforceRetention = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>("/ops/retention/enforce", {
        method: "POST",
        body: JSON.stringify({ dry_run: false })
      });
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const startSoakSession = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>("/ops/soak/start", {
        method: "POST",
        body: JSON.stringify({ iterations: 60, interval_seconds: 60 })
      });
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const stopSoakSession = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>("/ops/soak/stop", { method: "POST" });
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const configureToolProxyExecutors = async (apiEnabled: boolean, browserEnabled: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>("/tool-proxy/config", {
        method: "POST",
        body: JSON.stringify({ api_executor_enabled: apiEnabled, browser_executor_enabled: browserEnabled })
      });
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const probeMemoryProviders = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>("/memory/providers/diagnostics", {
        method: "POST",
        body: JSON.stringify({
          provider: "all",
          user_id: ownerScope.userId,
          session_id: ownerScope.sessionId,
          write_probe: false
        })
      });
      setMemoryDiagnostics(response);
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const proposeReplayCompensation = async (traceId: string) => {
    if (!traceId) return;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>(`/audit/replay/${encodeURIComponent(traceId)}/propose`, { method: "POST" });
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const runRuntimeMatrix = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchJson<Record<string, JsonValue>>("/ops/runtime-matrix/run", { method: "POST" });
      setRuntimeMatrix(response);
      setResult(response as MessageResult);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  const latestClaims = useMemo(() => jsonItems(beliefStatus?.newest).slice(-5).reverse(), [beliefStatus]);
  const focus = Array.isArray(scopedAttention?.focus) ? scopedAttention.focus.map(String) : [];
  const situationItems = jsonItems(generalSituations.items);
  const hypothesisItems = jsonItems(attentionHypotheses.items);
  const hypothesisStatusCounts = asRecord(attentionHypotheses.status_counts);
  const beliefSummary = asRecord(beliefStatus?.summary);
  const beliefRefreshable = jsonItems(beliefStatus?.refreshable);
  const cognitiveLoopMetrics = asRecord(cognitiveLoopStatus?.metrics);
  const suggestionItems = jsonItems(suggestionInbox.items);
  const suggestionFeedbackItems = jsonItems(suggestionFeedback.items);
  const suggestionFeedbackAvailable = suggestionFeedback.status === "success";
  const suggestionMode = String(suggestionStatus?.mode ?? "unavailable");
  const allowedSuggestionModes = Array.isArray(suggestionStatus?.allowed_modes)
    ? suggestionStatus.allowed_modes.map(String)
    : ["disabled", "record_only", "shadow", "advise_only"];
  const suggestionAuthority = asRecord(suggestionStatus?.authority);
  const suggestionPolicy = asRecord(suggestionInbox.policy);
  const suggestionSandboxEnabled = suggestionPolicy.sandbox_enabled === true;
  const suggestionCalibrationCounts = asRecord(suggestionCalibration?.counts);
  const suggestionUsefulRate = suggestionCalibration?.useful_rate;
  const scopeChanged = ownerScope.userId !== ownerUserDraft.trim() || ownerScope.sessionId !== ownerSessionDraft.trim();
  const currentRisk = String(state?.risk_state.current_risk ?? "R0");
  const connected = agentStatus?.connected === true ? "connected" : String(agentStatus?.status ?? "unconfigured");
  const snapshots = useMemo(() => {
    const byId = new Map<string, Record<string, JsonValue>>();
    rollbackLogs.items.forEach((entry) => {
      const snapshot = asRecord(asRecord(entry).snapshot);
      const snapshotId = String(snapshot.snapshot_id ?? "");
      if (snapshotId) byId.set(snapshotId, snapshot);
    });
    return Array.from(byId.values());
  }, [rollbackLogs]);
  const memoryItems = Array.isArray(memorySummary?.summary) ? (memorySummary.summary as Array<Record<string, JsonValue>>) : [];
  const agents = asRecord(agentRegistry?.agents);
  const selectedAgent = String(agentRegistry?.selected_agent ?? runtime?.identity.selected_agent ?? "openclaw");
  const coreModules = architecture?.core_modules ?? [];
  const architectureBlocks = architecture?.blocks ?? [];
  const stateDefinitions = architecture?.state_definitions ?? [];
  const phases = architecture?.implementation_phases ?? [];
  const operationalModes = definitions?.operational_modes ?? architecture?.operational_modes ?? [];
  const activeModes = runtime?.operational_mode ?? [];
  const readiness = asRecord(mvpStatus?.core_loops);
  const executionStatusCounts = statusCounts(executionLogs.items);
  const policyDecisionCounts = statusCounts(policyLogs.items, "decision");
  const auditItems = Array.isArray(auditJournal?.items) ? (auditJournal.items as Array<Record<string, JsonValue>>) : [];
  const externalWatchlist = jsonItems(externalScope?.watchlist);
  const externalSummaries = jsonItems(externalScope?.summaries);
  const coreModelConfigured = coreModelStatus?.configured === true ? "configured" : String(coreModelStatus?.status ?? "unconfigured");
  const opsStatus = String(opsHealth?.status ?? "unknown");
  const deploymentStatus = String(deploymentReadiness?.status ?? "unknown");
  const runtimeTelemetrySummary = asRecord(runtimeMetricsSummary);
  const runtimeTelemetryRouteDistribution = asRecord(runtimeTelemetrySummary.route_distribution);
  const runtimeTelemetryRoutesRaw = asRecord(runtimeMetricsRoutes).items;
  const runtimeTelemetryRoutes = Array.isArray(runtimeTelemetryRoutesRaw) ? (runtimeTelemetryRoutesRaw as Array<Record<string, JsonValue>>) : [];
  const runtimeTraceItems = runtimeTraceRecent.items.slice(-6).reverse();
  const runtimeFailureItems = runtimeMetricsFailures.items.slice(-6).reverse();
  const setupDesktop = asRecord(setupStatus?.desktop);
  const setupPlatform = asRecord(setupStatus?.platform);
  const setupPaths = asRecord(setupStatus?.paths);
  const setupAgent = asRecord(setupStatus?.agent);
  const setupFeishu = asRecord(setupStatus?.feishu_setup);
  const isolatedRunnerBackend = asRecord(isolatedRunnerStatus?.backend);
  const isolatedRunnerAdmission = asRecord(isolatedRunnerStatus?.admission);
  const isolatedRunnerContract = asRecord(isolatedRunnerStatus?.isolation_contract);
  const isolatedRunnerLimits = asRecord(isolatedRunnerContract.limits);
  const isolatedRunnerAuthority = asRecord(isolatedRunnerStatus?.authority ?? isolatedRunnerBackend.authority);
  const isolatedRunnerUnavailable = isolatedRunnerStatusError !== null;
  const isolationStatus = isolatedRunnerUnavailable ? "unavailable" : String(isolatedRunnerStatus?.status ?? "loading");
  const isolationHealth = isolatedRunnerUnavailable ? "fail_closed" : String(isolatedRunnerStatus?.operational_health ?? "loading");
  const backendAvailability = isolatedRunnerUnavailable ? "unavailable" : String(isolatedRunnerBackend.availability ?? "loading");
  const backendCertification = isolatedRunnerUnavailable
    ? "not_certified"
    : isolatedRunnerBackend.conformance_certified === true
      ? "certified"
      : isolatedRunnerBackend.conformance_certified === false
        ? "not_certified"
        : "loading";
  const admissionEnabled = isolatedRunnerUnavailable
    ? "fail_closed"
    : isolatedRunnerAdmission.enabled === true
      ? "enabled"
      : isolatedRunnerAdmission.enabled === false
        ? "disabled"
        : "unknown";
  const admissionToken = isolatedRunnerAdmission.token_configured === true
    ? "configured"
    : isolatedRunnerAdmission.token_configured === false
      ? "not_configured"
      : "unknown";
  const admissionReady = isolatedRunnerUnavailable
    ? "fail_closed"
    : isolatedRunnerAdmission.start_ready === true
      ? "ready"
      : isolatedRunnerAdmission.start_ready === false
        ? "not_ready"
        : "unknown";
  const feishuReadiness = String(setupFeishu.readiness ?? "not_configured");
  const feishuReadinessLabels: Record<string, string> = {
    receiving: "receiving",
    processing_failed: "processing failed",
    waiting_for_event: "waiting for message",
    connecting: "connecting",
    not_ready: "not ready",
    configured_not_running: "configured, stopped",
    not_configured: "not configured"
  };
  const feishuReadinessLabel = feishuReadinessLabels[feishuReadiness] ?? feishuReadiness;

  return (
    <main className="appShell">
      <SetupWizard
        open={showSetupWizard}
        onClose={() => setShowSetupWizard(false)}
        onComplete={refresh}
        setupStatus={setupStatus}
        coreModelStatus={coreModelStatus}
        agentStatus={agentStatus}
      />
      <header className="topbar">
        <div>
          <div className="eyebrow">Awareness & Agent Control Console</div>
          <h1>Veyra</h1>
        </div>
        <div className="topbarRight">
          <StatusPill value={runtime?.lifecycle.status ?? "loading"} />
          <button className="iconButton" onClick={manualRefresh} title="Refresh state" aria-label="Refresh state">
            <RefreshCw size={18} />
          </button>
        </div>
      </header>

      {error ? (
        <div className={`alert ${backendBooting ? "booting" : ""}`}>
          {backendBooting ? <RefreshCw className="spinIcon" size={18} /> : <AlertTriangle size={18} />}
          {error}
        </div>
      ) : null}

      <section className="overviewGrid">
        <Metric label="Runtime" value={`${runtime?.identity.selected_agent ?? "unknown"} · ${connected}`} />
        <Metric label="Lifecycle" value={<StatusPill value={runtime?.lifecycle.status ?? "loading"} />} />
        <Metric label="Risk" value={<StatusPill value={currentRisk} />} />
        <Metric label="Ops" value={<StatusPill value={opsStatus} />} />
      </section>

      <section className="workbenchShell">
        <nav className="workbenchTabs" aria-label="Veyra console sections">
          {workbenchSections.map((section) => (
            <button
              key={section.id}
              className={`workbenchTab ${activeSection === section.id ? "active" : ""}`}
              onClick={() => setActiveSection(section.id)}
              type="button"
            >
              {section.icon}
              <span>{section.label}</span>
            </button>
          ))}
        </nav>
        <div className="workbenchBody">

      <section className={`workspaceGrid workbenchPane ${activeSection === "runtime" ? "active" : ""}`}>
        <Section
          title="Setup Wizard"
          icon={<Settings size={18} />}
          action={
            <button className="ghostButton compactButton" type="button" onClick={() => setShowSetupWizard(true)}>
              Open setup wizard
            </button>
          }
        >
          <div className="setupGrid">
            <div className="setupStep">
              <span>App</span>
              <strong>{String(setupDesktop.product_name ?? "Veyra")}</strong>
              <small>{String(setupDesktop.shell ?? "desktop")} · {String(setupDesktop.backend_mode ?? "local_api")}</small>
            </div>
            <div className="setupStep">
              <span>Platform</span>
              <strong>{String(setupPlatform.system ?? "unknown")}</strong>
              <small>{String(setupPlatform.machine ?? "")}</small>
            </div>
            <div className="setupStep">
              <span>Local config</span>
              <strong>{setupPaths.env_exists === true ? "configured" : "missing .env"}</strong>
              <small>{String(setupPaths.env_file ?? "")}</small>
            </div>
            <div className="setupStep">
              <span>Agent install</span>
              <strong>{setupAgent.connected === true ? "available" : "needs setup"}</strong>
              <small>{String(setupAgent.status ?? "unknown")}</small>
            </div>
            <div className="setupStep">
              <span>Feishu intake</span>
              <strong>{feishuReadinessLabel}</strong>
              <small>{String(setupFeishu.status ?? "unknown")}</small>
            </div>
            <div className="setupStep">
              <span>Selected runtime</span>
              <strong>{selectedAgent}</strong>
              <small>{connected}</small>
            </div>
            <div className="setupStep">
              <span>Safety boundary</span>
              <strong>Guardian enforced</strong>
              <small>{String(asRecord(mvpStatus?.agent_runtime).status ?? "unknown")}</small>
            </div>
            <div className="setupStep">
              <span>Autonomy</span>
              <strong>Read-only proactive checks</strong>
              <small>{String(readiness.proactive_read_only_checks ?? false)}</small>
            </div>
            <div className="setupStep">
              <span>Rollback / audit</span>
              <strong>{String(readiness.rollback_audit_depth ?? false)}</strong>
              <small>{snapshots.length} snapshots</small>
            </div>
            <div className="setupStep">
              <span>Deployment</span>
              <strong>{deploymentStatus}</strong>
              <small>{String(opsHealth?.alert_count ?? 0)} alerts</small>
            </div>
          </div>
        </Section>

        <Section title="Architecture Coverage" icon={<Layers size={18} />}>
          <div className="coverageGrid">
            {architectureBlocks.map((block) => (
              <div className="coverageItem" key={String(block.id)}>
                <span>{String(block.name)}</span>
                <StatusPill value={String(block.status ?? "unknown")} />
              </div>
            ))}
          </div>
        </Section>
      </section>

      <section className={`logGrid single workbenchPane ${activeSection === "awareness" ? "active" : ""}`}>
        <Section
          title="Owner / Session Scope"
          icon={<UserRound size={18} />}
          action={<StatusPill value={scopeChanged ? "draft" : "exact_scope"} />}
        >
          <div className="ownerScopePanel">
            <div className="ownerScopeGrid">
              <label>
                <span>Owner</span>
                <input
                  value={ownerUserDraft}
                  onChange={(event) => setOwnerUserDraft(event.target.value)}
                  aria-label="Console owner ID"
                  autoComplete="off"
                />
              </label>
              <label>
                <span>Session</span>
                <input
                  value={ownerSessionDraft}
                  onChange={(event) => setOwnerSessionDraft(event.target.value)}
                  aria-label="Console session ID"
                  autoComplete="off"
                />
              </label>
              <button
                className="primaryButton"
                onClick={applyOwnerScope}
                disabled={loading || !scopeChanged || !ownerUserDraft.trim() || !ownerSessionDraft.trim()}
              >
                <RefreshCw size={15} />
                Apply & refresh
              </button>
            </div>
            <div className="scopeBoundary">
              <Shield size={15} />
              <span>
                Attention, memory, General Situations and suggestions use this exact owner/session pair. The Console stores only these non-secret IDs locally and never falls back to an ownerless read.
              </span>
            </div>
            {awarenessPanelError ? (
              <div className="isolationNotice">
                <AlertTriangle size={16} />
                <span>{awarenessPanelError}</span>
              </div>
            ) : null}
          </div>
        </Section>
      </section>

      <section className={`workspaceGrid workbenchPane ${activeSection === "awareness" ? "active" : ""}`}>
        <Section
          title="Belief Console"
          icon={<Database size={18} />}
          action={<StatusPill value={beliefStatus ? String(beliefStatus.status ?? "unknown") : "unavailable"} />}
        >
          {beliefStatus ? (
            <div className="situationSummary">
              <Metric label="Fresh" value={String(beliefSummary.fresh ?? 0)} />
              <Metric label="Stale" value={String(beliefSummary.stale ?? 0)} />
              <Metric label="Conflict" value={String(beliefSummary.conflict ?? 0)} />
              <Metric label="Refreshable" value={String(beliefRefreshable.length)} />
            </div>
          ) : (
            <div className="emptyState">
              <AlertTriangle size={18} />
              Belief status is unavailable for this exact owner/session scope.
            </div>
          )}
          {beliefStatus ? (
            <div className="evidenceRefs">
              <span>Refreshable claims (next_action = refresh_probe)</span>
              {beliefRefreshable.slice(0, 8).map((claim, index) => (
                <code key={`${String(claim.key ?? index)}`}>
                  {String(claim.key ?? "claim")} · {String(claim.status ?? "unknown")}
                </code>
              ))}
              {!beliefRefreshable.length ? <small>No refreshable Belief claim exists for this scope.</small> : null}
            </div>
          ) : null}
        </Section>
      </section>

      <section className={`workspaceGrid workbenchPane ${activeSection === "awareness" ? "active" : ""}`}>
        <Section title="Core Model" icon={<Brain size={18} />}>
          <div className="configPanel">
            <div className="configSummary">
              <Metric label="Status" value={<StatusPill value={coreModelConfigured} />} />
              <Metric label="Mode" value={String(coreModelStatus?.decision_mode ?? "auto")} />
            </div>
            <label className="toggleRow">
              <input type="checkbox" checked={coreModelEnabled} onChange={(event) => setCoreModelEnabled(event.target.checked)} />
              <span>Use model inside Veyra Core</span>
            </label>
            <div className="configGrid">
              <input value={coreModelBaseUrl} onChange={(event) => setCoreModelBaseUrl(event.target.value)} placeholder="http://127.0.0.1:11434/v1" aria-label="Core model base URL" />
              <input value={coreModelName} onChange={(event) => setCoreModelName(event.target.value)} placeholder="model name" aria-label="Core model name" />
              <input value={coreModelKeyEnv} onChange={(event) => setCoreModelKeyEnv(event.target.value)} placeholder="VEYRA_CORE_MODEL_API_KEY" aria-label="Core model API key environment variable" />
              <select value={coreModelDecisionMode} onChange={(event) => setCoreModelDecisionMode(event.target.value)} aria-label="Core model decision mode">
                <option value="auto">auto</option>
                <option value="always">always</option>
              </select>
            </div>
            <div className="buttonRow compact">
              <button className="primaryButton compactButton" onClick={saveCoreModelConfig} disabled={loading || (coreModelEnabled && (!coreModelBaseUrl.trim() || !coreModelName.trim()))}>
                <Save size={14} />
                Save Core Model
              </button>
            </div>
            <JsonBlock value={coreModelStatus ?? { status: "not loaded" }} />
          </div>
        </Section>

        <Section title="External World" icon={<Eye size={18} />}>
          <div className="configPanel">
            <div className="agentUrlRow">
              <input value={watchTarget} onChange={(event) => setWatchTarget(event.target.value)} placeholder="https://example.com or host" aria-label="External watch target" />
              <button className="primaryButton compactButton" onClick={addWatchTarget} disabled={loading || !watchTarget.trim()}>
                <Save size={14} />
                Watch
              </button>
            </div>
            <div className="buttonRow compact">
              <button className="ghostButton" onClick={refreshExternalWorld} disabled={loading || !externalWatchlist.length}>
                <RefreshCw size={15} />
                Refresh Watchlist
              </button>
            </div>
            <div className="watchList">
              {externalWatchlist.slice(-5).reverse().map((item, index) => {
                const record = typeof item === "object" && item !== null && !Array.isArray(item) ? (item as Record<string, JsonValue>) : { target: item };
                return (
                  <div className="watchItem" key={index}>
                    <strong>{String(record.target ?? "-")}</strong>
                    <small>{String(record.reason ?? record.kind ?? "watch target")}</small>
                  </div>
                );
              })}
              {!externalWatchlist.length ? <div className="emptyState"><Eye size={18} />No watch targets yet.</div> : null}
            </div>
            <JsonBlock value={externalSummaries.slice(-3).reverse()} />
          </div>
        </Section>
      </section>

      <section className={`workspaceGrid proactiveGrid workbenchPane ${activeSection === "awareness" ? "active" : ""}`}>
        <Section
          title="General Situations"
          icon={<Layers size={18} />}
          action={<StatusPill value={String(generalSituations.status ?? "loading")} />}
        >
          <div className="situationSummary">
            <Metric label="Aggregates" value={String(generalSituations.count ?? situationItems.length)} />
            <Metric label="State revision" value={String(generalSituations.state_revision ?? "-")} />
          </div>
          <div className="hypothesisPanel">
            <div className="hypothesisHeader">
              <div>
                <strong>Attention Hypotheses</strong>
                <span>Candidate → accumulating → confirmed; readiness is not factual probability.</span>
              </div>
              <div className="hypothesisStageCounts">
                <span>candidate {String(hypothesisStatusCounts.candidate ?? 0)}</span>
                <span>accumulating {String(hypothesisStatusCounts.accumulating ?? 0)}</span>
                <span>confirmed {String(hypothesisStatusCounts.confirmed ?? 0)}</span>
              </div>
            </div>
            <div className="cognitiveBalanceRow">
              <span>model cycles {String(cognitiveLoopMetrics.cycle_count ?? 0)}</span>
              <span>observed {String(cognitiveLoopMetrics.observed_count ?? 0)}</span>
              <span>model candidates {String(cognitiveLoopMetrics.candidate_count ?? 0)}</span>
              <span>candidate rate {String(cognitiveLoopMetrics.candidate_rate ?? 0)}</span>
              <StatusPill
                value={
                  cognitiveLoopMetrics.overconservative_alert === true
                    ? "overconservative_alert"
                    : "within_observed_window"
                }
              />
            </div>
            <div className="hypothesisList">
              {hypothesisItems.slice(0, 8).map((hypothesis, index) => {
                const readiness = asRecord(hypothesis.attention_readiness);
                return (
                  <article className="hypothesisCard" key={String(hypothesis.hypothesis_id ?? index)}>
                    <div className="hypothesisTopline">
                      <StatusPill value={String(hypothesis.status ?? "candidate")} />
                      <code>{String(hypothesis.hypothesis_id ?? "hypothesis")}</code>
                    </div>
                    <div className="hypothesisFacts">
                      <span>readiness {String(readiness.value ?? "-")}</span>
                      <span>evidence {String(hypothesis.evidence_count ?? 0)}</span>
                      <span>revision {String(hypothesis.hypothesis_revision ?? "-")}</span>
                    </div>
                    <small>{String(readiness.semantics ?? "attention_policy_readiness_not_factual_probability")}</small>
                  </article>
                );
              })}
              {!hypothesisItems.length ? (
                <div className="emptyState compactEmpty">
                  <Brain size={17} />
                  No durable attention hypothesis exists for this exact scope yet.
                </div>
              ) : null}
            </div>
          </div>
          <div className="situationList">
            {situationItems.map((situation, index) => {
              const childRefs = jsonItems(situation.child_refs);
              const anchors = Array.isArray(situation.common_anchor_keys) ? situation.common_anchor_keys.map(String) : [];
              return (
                <article className="situationCard" key={String(situation.general_situation_id ?? index)}>
                  <div className="situationTopline">
                    <StatusPill value={String(situation.status ?? "unknown")} />
                    <code>{String(situation.general_situation_id ?? "general situation")}</code>
                  </div>
                  <div className="situationFacts">
                    <div><span>Anchor</span><strong>{String(situation.primary_anchor_key ?? anchors[0] ?? "-")}</strong></div>
                    <div><span>Distinct events</span><strong>{String(situation.distinct_event_count ?? childRefs.length)}</strong></div>
                    <div><span>Parent revision</span><strong>{String(situation.parent_revision ?? "-")}</strong></div>
                    <div><span>Aggregation</span><strong>{String(situation.aggregation_scope ?? "exact_owner_session")}</strong></div>
                  </div>
                  <div className="evidenceRefs">
                    <span>Immutable child evidence</span>
                    {childRefs.map((ref, refIndex) => (
                      <code key={`${String(ref.source_event_id ?? refIndex)}:${String(ref.observation_revision ?? "")}`}>
                        {String(ref.source_event_id ?? "event")} · r{String(ref.observation_revision ?? "?")}
                      </code>
                    ))}
                  </div>
                  <div className="boundaryLine">
                    <span>Causality asserted: {String(situation.causality_asserted === true)}</span>
                    <span>Model merge: {String(situation.model_similarity_used_for_merge === true)}</span>
                  </div>
                </article>
              );
            })}
            {!situationItems.length ? (
              <div className="emptyState">
                <Layers size={18} />
                No multi-event aggregate exists for this exact owner/session yet.
              </div>
            ) : null}
          </div>
        </Section>

        <Section
          title="Proactive Suggestions"
          icon={<Bell size={18} />}
          action={<StatusPill value={suggestionMode} />}
        >
          <div className="suggestionPanel">
            <div className="modeLegend" aria-label="Suggestion delivery modes">
              {allowedSuggestionModes.map((mode) => (
                <button
                  type="button"
                  key={mode}
                  className={mode === suggestionMode ? "active" : ""}
                  onClick={() => configureSuggestionMode(mode)}
                  disabled={loading || mode === suggestionMode || suggestionStatus === null}
                  title={
                    mode === "record_only"
                      ? "Persist eligible proposals without surfacing them"
                      : mode === "shadow"
                        ? "Record what would have been suggested without delivery"
                        : mode === "advise_only"
                          ? "Surface informational proposals only in this owner-scoped Console inbox"
                          : "Do not create proposals"
                  }
                >
                  {mode}
                </button>
              ))}
            </div>
            <div className="suggestionSummary">
              <Metric label="Recorded proposals" value={String(suggestionStatus?.proposal_count ?? 0)} />
              <Metric label="Inbox" value={String(suggestionInbox.count ?? suggestionItems.length)} />
              <Metric label="Daily budget" value={String(suggestionPolicy.daily_budget ?? 1)} />
            </div>
            <div className="sandboxControl">
              <div>
                <strong>Owner-scoped Suggestion Sandbox</strong>
                <span>
                  {suggestionSandboxEnabled
                    ? "Enabled for this exact owner/session. advise_only is still required before a proposal can enter the Console inbox."
                    : "Closed by default. Eligible hypotheses remain private until this exact owner/session opts in."}
                </span>
              </div>
              <button
                type="button"
                className={suggestionSandboxEnabled ? "rejectButton" : "approveButton"}
                onClick={() => configureSuggestionSandbox(!suggestionSandboxEnabled)}
                disabled={loading || suggestionInbox.status !== "success"}
              >
                {suggestionSandboxEnabled ? "Close sandbox" : "Enable sandbox"}
              </button>
            </div>
            <div className="authorityNotice">
              <Shield size={16} />
              <div>
                <strong>Informational only</strong>
                <span>
                  No execution, tool, Agent, capability-grant or route-change authority. External and Feishu delivery remain disabled.
                </span>
              </div>
            </div>
            <div className="calibrationPanel">
              <div className="calibrationHeader">
                <div>
                  <strong>Descriptive self-calibration</strong>
                  <span>Explicit feedback only; no automatic policy or promotion effect.</span>
                </div>
                <StatusPill value={String(suggestionCalibration?.support ?? "insufficient_data")} />
              </div>
              <div className="calibrationMetrics">
                <Metric label="Feedback" value={String(suggestionCalibration?.active_feedback_count ?? 0)} />
                <Metric
                  label="Useful rate"
                  value={
                    typeof suggestionUsefulRate === "number"
                      ? `${Math.round(suggestionUsefulRate * 100)}%`
                      : "unavailable"
                  }
                />
                <Metric label="Useful" value={String(suggestionCalibrationCounts.useful ?? 0)} />
                <Metric label="Not useful" value={String(suggestionCalibrationCounts.not_useful ?? 0)} />
                <Metric label="Too frequent" value={String(suggestionCalibrationCounts.too_frequent ?? 0)} />
                <Metric
                  label="Wrong timing / evidence"
                  value={`${String(suggestionCalibrationCounts.wrong_timing ?? 0)} / ${String(suggestionCalibrationCounts.wrong_evidence ?? 0)}`}
                />
              </div>
              <small>
                Accuracy: {String(suggestionCalibration?.accuracy ?? "unavailable_without_verified_outcomes")}
              </small>
            </div>
            <div className="suggestionList">
              {suggestionItems.map((proposal, index) => {
                const whyNow = jsonItems(proposal.why_now);
                const evidence = jsonItems(proposal.evidence);
                const proposalId = String(proposal.proposal_id ?? "");
                const proposalState = String(proposal.status ?? "unknown");
                const existingFeedback = suggestionFeedbackItems.find(
                  (item) =>
                    String(item.proposal_id ?? "") === proposalId &&
                    String(item.proposal_revision ?? "") === String(proposal.proposal_revision ?? "")
                );
                const selectedFeedback = String(existingFeedback?.label ?? "");
                const proposalFeedbackEligible =
                  suggestionFeedbackAvailable &&
                  String(proposal.schema_version ?? "") === "veyra.informational_suggestion.v2" &&
                  /^sugr_[0-9a-f]{24}$/.test(String(proposal.proposal_revision ?? ""));
                const feedbackChoices: Array<{ label: SuggestionFeedbackLabel; title: string }> = [
                  { label: "useful", title: "有用" },
                  { label: "not_useful", title: "无关" },
                  { label: "too_frequent", title: "太频繁" },
                  { label: "wrong_timing", title: "时机不对" },
                  { label: "wrong_evidence", title: "判断/证据错误" }
                ];
                return (
                  <article className="suggestionCard" key={proposalId || index}>
                    <div className="suggestionTopline">
                      <StatusPill value={proposalState} />
                      <strong>Why now · score {String(proposal.score ?? "-")}</strong>
                      <code>{proposalId || "proposal"}</code>
                    </div>
                    <div className="whyNowList">
                      {whyNow.map((component, componentIndex) => (
                        <div key={String(component.component ?? componentIndex)}>
                          <strong>{String(component.component ?? "signal")}</strong>
                          <span>
                            value {String(component.value ?? "-")} · weight {String(component.weight ?? "-")} · {String(component.source ?? "structured evidence")}
                          </span>
                        </div>
                      ))}
                    </div>
                    <div className="boundaryLine">
                      <span>{evidence.length} immutable evidence reference(s)</span>
                      <span>Execution allowed: false</span>
                    </div>
                    <div className="feedbackPrompt">
                      <span>这条建议对你有帮助吗？</span>
                      <div className="feedbackChoices">
                        {feedbackChoices.map((choice) => (
                          <button
                            type="button"
                            key={choice.label}
                            className={selectedFeedback === choice.label ? "selected" : ""}
                            onClick={() => respondWithSuggestionFeedback(proposal, choice.label, existingFeedback)}
                            disabled={
                              loading ||
                              !proposalFeedbackEligible ||
                              selectedFeedback === choice.label
                            }
                          >
                            {choice.title}
                          </button>
                        ))}
                      </div>
                      <small>
                        {!suggestionFeedbackAvailable
                          ? "Feedback ledger unavailable; feedback controls are locked."
                          : !proposalFeedbackEligible
                          ? "This proposal has no current feedback-safe revision and remains read-only."
                          : selectedFeedback
                          ? `Current explicit label: ${selectedFeedback}. Choosing another label records an auditable correction.`
                          : "Acknowledge and Dismiss remain separate inbox controls and are never inferred as usefulness."}
                      </small>
                    </div>
                    {proposalState === "pending" || proposalState === "acknowledged" ? (
                      <div className="buttonRow compact suggestionActions">
                        <button
                          className="approveButton"
                          onClick={() => respondToSuggestion(proposalId, "ack")}
                          disabled={loading || proposalState === "acknowledged"}
                        >
                          <ThumbsUp size={15} />
                          Acknowledge
                        </button>
                        <button
                          className="rejectButton"
                          onClick={() => respondToSuggestion(proposalId, "dismiss")}
                          disabled={loading}
                        >
                          <ThumbsDown size={15} />
                          Dismiss
                        </button>
                      </div>
                    ) : null}
                  </article>
                );
              })}
              {!suggestionItems.length ? (
                <div className="emptyState suggestionEmpty">
                  <Bell size={18} />
                  {suggestionMode === "record_only"
                    ? "Record-only keeps eligible proposals in audit state; it does not place them in the Console inbox."
                    : suggestionMode === "shadow"
                      ? "Shadow records would-suggest decisions without delivery."
                      : suggestionMode === "advise_only"
                        ? "No pending owner-scoped suggestions right now."
                        : "Suggestion generation is disabled."}
                </div>
              ) : null}
            </div>
            {Object.keys(suggestionAuthority).length ? (
              <div className="boundaryLine authorityProjection">
                {Object.entries(suggestionAuthority).map(([name, value]) => (
                  <span key={name}>{name.replaceAll("_", " ")}: {String(value)}</span>
                ))}
              </div>
            ) : null}
          </div>
        </Section>
      </section>

      <section className={`workspaceGrid workbenchPane ${activeSection === "awareness" ? "active" : ""}`}>
        <Section title="Message Console" icon={<TerminalSquare size={18} />}>
          <textarea
            className="messageInput"
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            rows={4}
            placeholder="Send an event into Veyra..."
          />
          <div className="buttonRow">
            <button className="primaryButton" onClick={submit} disabled={loading || !message.trim()}>
              <Play size={16} />
              {loading ? "Running" : "Run"}
            </button>
            <button className="ghostButton" onClick={() => setMessage("请执行 rm -rf /")}>
              <Shield size={16} />
              Test Block
            </button>
            <button className="ghostButton" onClick={() => setMessage("帮我重启 OpenClaw 服务")}>
              <AlertTriangle size={16} />
              Test Review
            </button>
            <button className="ghostButton" onClick={() => setMessage("帮我调试 OpenClaw 为什么没响应")}>
              <Bot size={16} />
              Test Agent
            </button>
            <button className="ghostButton" onClick={runProactiveCheck} disabled={loading}>
              <RefreshCw size={16} />
              Proactive Check
            </button>
          </div>
          {result ? (
            <div className="resultStrip">
              <StatusPill value={result.route} />
              <StatusPill value={result.risk_level} />
              <span>{result.response}</span>
            </div>
          ) : null}
        </Section>

        <Section title="Awareness State" icon={<Gauge size={18} />}>
          <div className="stateRows">
            <div>
              <span>Attention</span>
              <strong>{focus.length ? focus.join(", ") : "none"}</strong>
            </div>
            <div>
              <span>Attention scope</span>
              <strong>{String(scopedAttention?.scope_status ?? "not_loaded")}</strong>
            </div>
            <div>
              <span>Owner / session</span>
              <strong>{ownerScope.userId} / {ownerScope.sessionId}</strong>
            </div>
            <div>
              <span>Current task</span>
              <strong>{String(state?.task_state.current_task ? "active" : "none")}</strong>
            </div>
            <div>
              <span>Executor</span>
              <strong>{selectedAgent}</strong>
            </div>
          </div>
        </Section>
      </section>

      <section className={`logGrid single workbenchPane ${activeSection === "governance" ? "active" : ""}`}>
        <Section
          title="Phase 6 Extension Lifecycle"
          icon={<ListChecks size={18} />}
          action={
            <button className="ghostButton compactButton" onClick={refreshPhase6Overview} disabled={loading}>
              <RefreshCw size={14} />
              Refresh public status
            </button>
          }
        >
          <div className="phase6LifecyclePanel">
            <div className="scopeBoundary phase6Boundary">
              <Shield size={15} />
              <span>
                This Console is an observability surface. It reads public, zero-secret stage status only; private capability-gap, release and deployment state stays in the authenticated control plane. No control token is requested, stored or rendered in the browser.
              </span>
            </div>
            <div className="phase6StageList">
              {phase6Stages.map((stage, index) => (
                <div className="phase6Stage" key={stage.id}>
                  <div className="phase6StageIndex">{index + 1}</div>
                  <div className="phase6StageBody">
                    <div className="phase6StageTopline">
                      <strong>{stage.label}</strong>
                      <code>{stage.phase}</code>
                      <StatusPill value={stage.health} />
                      <StatusPill value={stage.access} />
                    </div>
                    <span>{stage.scope}</span>
                    <small>{stage.status}</small>
                  </div>
                </div>
              ))}
              {!phase6Stages.length ? (
                <div className="emptyState"><ListChecks size={18} />Phase 6 public status is loading.</div>
              ) : null}
            </div>
            <div className="boundaryLine">
              <span>Private transitions: explicit review + CAS + authenticated operator only</span>
              <span>Last public refresh: {phase6StatusUpdatedAt ?? "not loaded"}</span>
            </div>
          </div>
        </Section>
      </section>

      <section className={`workspaceGrid workbenchPane ${activeSection === "governance" ? "active" : ""}`}>
        <Section title="Extension Isolation" icon={<Shield size={18} />}>
          <div className="isolationPanel">
            {isolatedRunnerStatusError ? (
              <div className="isolationNotice">
                <AlertTriangle size={16} />
                <span>Status unavailable; isolated-run admission remains fail closed.</span>
              </div>
            ) : null}

            <div className="isolationSummary">
              <Metric label="Phase" value={String(isolatedRunnerStatus?.phase ?? "6.2d")} />
              <Metric label="Status" value={<StatusPill value={isolationStatus} />} />
              <Metric label="Operational health" value={<StatusPill value={isolationHealth} />} />
              <Metric label="Completion scope" value={String(isolatedRunnerStatus?.completion_scope ?? "isolated runner only")} />
            </div>

            <div className="isolationGroup">
              <div className="isolationGroupTitle">Trusted backend</div>
              <div className="isolationStatusRows">
                <div><span>Backend</span><strong>{String(isolatedRunnerBackend.backend_kind ?? "unknown")}</strong></div>
                <div><span>Availability</span><StatusPill value={backendAvailability} /></div>
                <div><span>Conformance</span><StatusPill value={backendCertification} /></div>
                <div><span>Reason</span><StatusPill value={String(isolatedRunnerBackend.reason_code ?? "unknown")} /></div>
              </div>
            </div>

            <div className="isolationGroup">
              <div className="isolationGroupTitle">Control admission</div>
              <div className="isolationStatusRows">
                <div><span>Admission</span><StatusPill value={admissionEnabled} /></div>
                <div><span>Control token</span><StatusPill value={admissionToken} /></div>
                <div><span>Start readiness</span><StatusPill value={admissionReady} /></div>
                <div><span>Loopback bypass</span><StatusPill value={isolatedRunnerAdmission.loopback_bypass_allowed === false ? "false" : "critical"} /></div>
              </div>
            </div>

            <div className="isolationGroup">
              <div className="isolationGroupTitle">Fixed isolation contract</div>
              <div className="isolationStatusRows isolationContractRows">
                <div><span>Network</span><strong>{String(isolatedRunnerContract.network ?? "unknown")}</strong></div>
                <div><span>Root filesystem</span><strong>{String(isolatedRunnerContract.rootfs ?? "unknown")}</strong></div>
                <div><span>Candidate mount</span><strong>{String(isolatedRunnerContract.candidate_mount ?? "unknown")}</strong></div>
                <div><span>Fixed harness</span><strong>{String(isolatedRunnerContract.fixed_harness ?? "unknown")}</strong></div>
                <div><span>Host workspace</span><strong>{String(isolatedRunnerContract.host_workspace ?? "unknown")}</strong></div>
                <div><span>Host state</span><strong>{String(isolatedRunnerContract.host_state ?? "unknown")}</strong></div>
                <div><span>Host secrets</span><strong>{String(isolatedRunnerContract.host_secrets ?? "unknown")}</strong></div>
                <div><span>Candidate executed</span><strong>{String(isolatedRunnerContract.candidate_executed ?? "unknown")}</strong></div>
              </div>
              <div className="isolationIdentityRow">
                <span>Limits</span>
                <code>{Object.keys(isolatedRunnerLimits).length ? compactJson(isolatedRunnerLimits) : "unknown"}</code>
              </div>
            </div>

            <div className="isolationGroup isolationIdentity">
              <div className="isolationGroupTitle">Pinned trust identity</div>
              <div className="isolationIdentityRow"><span>Image</span><code>{String(isolatedRunnerBackend.image_id ?? "unavailable")}</code></div>
              <div className="isolationIdentityRow"><span>Image conformance</span><code>{String(isolatedRunnerBackend.image_conformance_digest ?? "unavailable")}</code></div>
              <div className="isolationIdentityRow"><span>Engine identity</span><code>{String(isolatedRunnerBackend.engine_identity_digest ?? "unavailable")}</code></div>
              <div className="isolationIdentityRow"><span>Harness</span><code>{String(isolatedRunnerBackend.harness_revision ?? "unknown")}</code></div>
              <div className="isolationIdentityRow"><span>Harness digest</span><code>{String(isolatedRunnerBackend.harness_digest ?? "unknown")}</code></div>
              <div className="isolationIdentityRow"><span>Policy</span><code>{String(isolatedRunnerBackend.runner_policy_revision ?? "unknown")}</code></div>
              <div className="isolationIdentityRow"><span>Policy digest</span><code>{String(isolatedRunnerBackend.runner_policy_digest ?? "unknown")}</code></div>
            </div>

            <div className="isolationGroup">
              <div className="isolationGroupTitle">Authority locks</div>
              <div className="authorityGrid">
                {Object.keys(isolatedRunnerAuthority).length ? Object.entries(isolatedRunnerAuthority).map(([name, value]) => (
                  <div key={name}>
                    <span>{name.replaceAll("_", " ")}</span>
                    <StatusPill value={value === false ? "false" : "critical"} />
                  </div>
                )) : <div className="emptyState"><Shield size={16} />Authority status unavailable.</div>}
              </div>
            </div>
          </div>
        </Section>

        <Section title="Action Review" icon={<Shield size={18} />}>
          <div className="reviewList">
            {reviews.items.length ? (
              reviews.items.map((item, index) => {
                const reviewId = String(item.review_id ?? "");
                const status = String(item.status ?? "unknown");
                const foresight = item.foresight as Record<string, JsonValue> | undefined;
                const sideEffects = Array.isArray(foresight?.side_effects) ? foresight.side_effects.map(String).join(", ") : "none";
                const alternatives = Array.isArray(foresight?.safer_alternatives) ? foresight.safer_alternatives.map(String).join(", ") : "none";
                return (
                  <div className="reviewItem" key={reviewId || index}>
                    <div className="reviewTopline">
                      <StatusPill value={status} />
                      <StatusPill value={String(item.risk_level ?? "R?")} />
                      <code>{reviewId || String(item.event_id ?? "unknown")}</code>
                    </div>
                    <strong>{String(item.task_text ?? "Review item")}</strong>
                    <small>Side effects: {sideEffects}</small>
                    <small>Safer alternatives: {alternatives}</small>
                    {item.execution_result ? (
                      <div className="executionResult">
                        <span>Execution</span>
                        <JsonBlock value={item.execution_result} />
                      </div>
                    ) : null}
                    {status === "pending" ? (
                      <div className="buttonRow compact">
                        <button className="approveButton" onClick={() => decideReview(reviewId, "approve")} disabled={loading}>
                          <ThumbsUp size={15} />
                          Approve
                        </button>
                        <button className="rejectButton" onClick={() => decideReview(reviewId, "reject")} disabled={loading}>
                          <ThumbsDown size={15} />
                          Reject
                        </button>
                      </div>
                    ) : null}
                  </div>
                );
              })
            ) : (
              <div className="emptyState">
                <CheckCircle2 size={18} />
                No blocked or pending review actions.
              </div>
            )}
          </div>
        </Section>

        <Section title="Belief / Uncertainty" icon={<Database size={18} />}>
          <div className="claimList">
            {latestClaims.length ? (
              latestClaims.map((claim, index) => (
                <div className="claimItem" key={index}>
                  <span>{String(claim.claim ?? "claim")}</span>
                  <small>{String(claim.status ?? "unknown")} · confidence {String(claim.confidence ?? "-")}</small>
                </div>
              ))
            ) : (
              <div className="emptyState">
                <XCircle size={18} />
                No belief claims yet.
              </div>
            )}
          </div>
        </Section>
      </section>

      <section className={`workspaceGrid workbenchPane ${activeSection === "runtime" ? "active" : ""}`}>
        <Section title="Persona Manager" icon={<Brain size={18} />}>
          <div className="personaGrid">
            {operationalModes.map((mode) => (
              <div className={`personaItem ${activeModes.includes(mode) ? "active" : ""}`} key={mode}>
                <strong>{mode}</strong>
                <StatusPill value={activeModes.includes(mode) ? "active" : "available"} />
              </div>
            ))}
          </div>
        </Section>

        <Section title="Core Module Status" icon={<ListChecks size={18} />}>
          <div className="moduleList">
            {coreModules.map((module) => (
              <div className="moduleItem" key={String(module.id)}>
                <span>{String(module.id)}</span>
                <StatusPill value={String(module.status ?? "unknown")} />
              </div>
            ))}
          </div>
        </Section>
      </section>

      <section className={`workspaceGrid workbenchPane ${activeSection === "audit" ? "active" : ""}`}>
        <Section title="Rollback Viewer" icon={<RotateCcw size={18} />}>
          <div className="rollbackControls">
            <input value={snapshotPath} onChange={(event) => setSnapshotPath(event.target.value)} aria-label="Snapshot path" />
            <button className="primaryButton" onClick={createSnapshot} disabled={loading || !snapshotPath.trim()}>
              <Save size={15} />
              Snapshot
            </button>
          </div>
          <div className="dataTable">
            <div className="dataTableHead">
              <span>ID</span>
              <span>Source</span>
              <span>Status</span>
              <span>Actions</span>
            </div>
            {snapshots.slice(-5).reverse().map((snapshot, index) => {
              const id = String(snapshot.snapshot_id ?? "");
              const canRestore = id && snapshot.status === "created";
              return (
                <div className="dataTableRow" key={id || index}>
                  <code>{id || "-"}</code>
                  <span>{String(snapshot.source_scope ?? "workspace-scoped")}</span>
                  <StatusPill value={String(snapshot.status ?? "unknown")} />
                  <div className="inlineActions">
                    <button className="iconButton smallIconButton" onClick={() => viewSnapshotDiff(id)} disabled={loading || !canRestore} title="View diff" aria-label="View snapshot diff">
                      <Eye size={14} />
                    </button>
                    <button className="iconButton smallIconButton" onClick={() => restoreSnapshot(id)} disabled={loading || !canRestore} title="Restore snapshot" aria-label="Restore snapshot">
                      <RotateCcw size={14} />
                    </button>
                  </div>
                </div>
              );
            })}
            {!snapshots.length ? <div className="emptyState"><History size={18} />No snapshots yet.</div> : null}
          </div>
          {rollbackDiff ? (
            <div className="diffPreview">
              <div className="diffPreviewHeader">
                <FileText size={15} />
                <strong>{String(rollbackDiff.snapshot_id ?? "Snapshot diff")}</strong>
                <StatusPill value={String(rollbackDiff.status ?? "unknown")} />
              </div>
              <pre>{String(rollbackDiff.diff ?? rollbackDiff.reason ?? "No diff available.")}</pre>
            </div>
          ) : null}
          <div className="miniBlock">
            <span>Git diff</span>
            <code>{String(diffStatus?.diff_stat ?? "No diff loaded.")}</code>
          </div>
        </Section>

        <Section title="Action Journal / Replay" icon={<History size={18} />}>
          <div className="traceSummary">
            {Object.entries(asRecord(asRecord(auditJournal?.summary).by_source)).map(([source, count]) => (
              <Metric key={source} label={source} value={String(count)} />
            ))}
          </div>
          <div className="dataTable auditTable">
            <div className="dataTableHead">
              <span>Source</span>
              <span>Status</span>
              <span>Route</span>
              <span>Summary</span>
              <span>Time</span>
              <span>Actions</span>
            </div>
            {auditItems.slice(-6).reverse().map((item, index) => {
              const traceId = String(item.trace_id ?? "");
              return (
                <div className="dataTableRow" key={String(item.journal_id ?? index)}>
                  <span>{String(item.source ?? "-")}</span>
                  <StatusPill value={String(item.status ?? "unknown")} />
                  <span>{String(item.route ?? "-")}</span>
                  <code>{String(item.summary ?? "-")}</code>
                  <small>{String(item.timestamp ?? "")}</small>
                  <button className="iconButton smallIconButton" onClick={() => proposeReplayCompensation(traceId)} disabled={loading || !traceId} title="Propose replay compensation" aria-label="Propose replay compensation">
                    <RotateCcw size={14} />
                  </button>
                </div>
              );
            })}
            {!auditItems.length ? <div className="emptyState"><History size={18} />No journal entries yet.</div> : null}
          </div>
          <JsonBlock value={timeTravel ?? { status: "not loaded" }} />
        </Section>
      </section>

      <section className={`workspaceGrid workbenchPane ${activeSection === "runtime" ? "active" : ""}`}>
        <Section title="Memory Bridge" icon={<Database size={18} />}>
          <div className="buttonRow compact">
            <button className="ghostButton" onClick={probeMemoryProviders} disabled={loading}>
              <Database size={15} />
              Probe Providers
            </button>
          </div>
          <JsonBlock value={memoryDiagnostics ?? { status: "not loaded" }} />
          <div className="dataTable memoryTable">
            <div className="dataTableHead">
              <span>Task</span>
              <span>Executor</span>
              <span>Status</span>
              <span>Written</span>
              <span>Patch</span>
            </div>
            {memoryItems.slice(-6).reverse().map((item, index) => {
              const patch = memoryPatch(item);
              return (
                <div className="dataTableRow" key={index}>
                  <strong>{String(patch.task ?? "Memory patch")}</strong>
                  <span>{String(patch.executor ?? "-")}</span>
                  <StatusPill value={String(patch.status ?? item.status ?? "written")} />
                  <small>{String(item.created_at ?? item.timestamp ?? "")}</small>
                  <code>{compactJson(patch.result ?? item.patch)}</code>
                </div>
              );
            })}
            {!memoryItems.length ? <div className="emptyState"><Database size={18} />No memory patches yet.</div> : null}
          </div>
          <div className="writeLogList">
            {memoryLogs.items.slice(-4).reverse().map((item, index) => (
              <div className="writeLogItem" key={index}>
                <StatusPill value={String(item.status ?? "unknown")} />
                <span>{String(item.reason ?? memoryPatch(asRecord(item.item)).task ?? "memory write")}</span>
                <small>{String(item.timestamp ?? "")}</small>
              </div>
            ))}
          </div>
        </Section>
      </section>

      <section className={`logGrid operationsGrid workbenchPane ${activeSection === "governance" ? "active" : ""}`}>
        <Section title="Execution Trace" icon={<ScrollText size={18} />}>
          <div className="traceSummary">
            {Object.entries(executionStatusCounts).map(([status, count]) => (
              <Metric key={status} label={status} value={count} />
            ))}
          </div>
          <div className="dataTable traceTable">
            <div className="dataTableHead">
              <span>Trace</span>
              <span>Route</span>
              <span>Status</span>
              <span>Executor</span>
              <span>Target</span>
              <span>Verifier</span>
            </div>
            {executionLogs.items.slice(-6).reverse().map((item, index) => {
              const verification = asRecord(item.verification);
              return (
                <div className="dataTableRow" key={String(item.trace_id ?? index)}>
                  <code>{String(item.trace_id ?? "-")}</code>
                  <span>{String(item.route ?? "-")}</span>
                  <StatusPill value={String(item.status ?? "unknown")} />
                  <span>{String(item.executor ?? "-")}</span>
                  <code>{traceTarget(item)}</code>
                  <span>{String(verification.verdict ?? "-")}</span>
                </div>
              );
            })}
            {!executionLogs.items.length ? <div className="emptyState"><ScrollText size={18} />No execution traces yet.</div> : null}
          </div>
        </Section>

        <Section title="Policy Trace" icon={<Shield size={18} />}>
          <div className="traceSummary">
            {Object.entries(policyDecisionCounts).map(([decision, count]) => (
              <Metric key={decision} label={decision} value={count} />
            ))}
          </div>
          <div className="dataTable policyTable">
            <div className="dataTableHead">
              <span>Tool</span>
              <span>Action</span>
              <span>Decision</span>
              <span>Risk</span>
              <span>Target</span>
              <span>Reason</span>
            </div>
            {policyLogs.items.slice(-6).reverse().map((item, index) => (
              <div className="dataTableRow" key={index}>
                <span>{String(item.tool ?? "-")}</span>
                <span>{String(item.action_type ?? "-")}</span>
                <StatusPill value={String(item.decision ?? "unknown")} />
                <StatusPill value={String(item.risk_level ?? "R?")} />
                <code>{traceTarget(item)}</code>
                <span>{String(item.reason ?? "-")}</span>
              </div>
            ))}
            {!policyLogs.items.length ? <div className="emptyState"><Shield size={18} />No policy traces yet.</div> : null}
          </div>
        </Section>

        <Section title="Tool Proxy Monitor" icon={<Shield size={18} />}>
          <div className="buttonRow compact">
            <button className="ghostButton" onClick={() => configureToolProxyExecutors(true, false)} disabled={loading}>
              <Settings size={15} />
              Enable API
            </button>
            <button className="ghostButton" onClick={() => configureToolProxyExecutors(false, false)} disabled={loading}>
              <XCircle size={15} />
              Disable Executors
            </button>
          </div>
          <JsonBlock value={toolProxyStatus ?? { status: "not loaded" }} />
          <div className="dataTable toolTable">
            <div className="dataTableHead">
              <span>Tool</span>
              <span>Operation</span>
              <span>Status</span>
              <span>Target</span>
              <span>Approval</span>
              <span>Result</span>
              <span>Time</span>
            </div>
            {toolLogs.items.slice(-8).reverse().map((item, index) => (
              <div className="dataTableRow" key={index}>
                <span>{String(item.tool ?? "-")}</span>
                <span>{String(item.action_type ?? item.operation ?? (Array.isArray(item.command) ? "shell" : "policy"))}</span>
                <StatusPill value={String(item.status ?? "unknown")} />
                <code>{String(item.target ?? toolTarget(item))}</code>
                <span>{String(item.approved_by ?? "-")}</span>
                <code>{compactJson(item.result_summary ?? item.returncode ?? item.reason ?? item.stderr, "ok")}</code>
                <small>{String(item.recorded_at ?? item.timestamp ?? "")}</small>
              </div>
            ))}
            {!toolLogs.items.length ? <div className="emptyState"><Shield size={18} />No tool calls yet.</div> : null}
          </div>
        </Section>
        <Section title="Agent Runtime" icon={<Bot size={18} />}>
          <div className="agentRuntimePanel">
            <div className="segmentedControl" role="group" aria-label="Select agent runtime">
              {Object.keys(agents).map((name) => {
                const item = asRecord(agents[name]);
                return (
                  <button
                    key={name}
                    className={name === selectedAgent ? "selected" : ""}
                    onClick={() => selectAgent(name)}
                    disabled={loading}
                    title={String(item.status ?? "unknown")}
                  >
                    {name}
                  </button>
                );
              })}
            </div>
            <div className="agentUrlRow">
              <input
                value={agentBaseUrl}
                onChange={(event) => setAgentBaseUrl(event.target.value)}
                placeholder={String(asRecord(agents[selectedAgent])?.base_url ?? "http://127.0.0.1:18789")}
                aria-label="Agent base URL"
              />
              <button className="primaryButton compactButton" onClick={saveSelectedAgentUrl} disabled={loading || !agentBaseUrl.trim()}>
                <Save size={14} />
                Save URL
              </button>
            </div>
            <JsonBlock value={agentRegistry ?? agentStatus ?? { status: "not loaded" }} />
          </div>
        </Section>
        <Section title="Last Result" icon={<Activity size={18} />}>
          <JsonBlock value={result ?? { status: "no message submitted in this console session" }} />
        </Section>
      </section>
      <section className={`logGrid single workbenchPane ${activeSection === "ops" ? "active" : ""}`}>
        <Section title="MVP Readiness" icon={<CheckCircle2 size={18} />}>
          <div className="readinessGrid">
            <JsonBlock value={mvpStatus ?? { status: "not loaded" }} />
            <JsonBlock value={opsHealth ?? { status: "not loaded" }} />
            <JsonBlock value={retentionStatus ?? { status: "not loaded" }} />
            <JsonBlock value={soakStatus ?? { status: "not loaded" }} />
            <JsonBlock value={runtimeMatrix ?? { status: "not loaded" }} />
            <JsonBlock value={deploymentReadiness ?? { status: "not loaded" }} />
          </div>
          <div className="buttonRow compact">
            <button className="ghostButton" onClick={runRuntimeMatrix} disabled={loading}>
              <ListChecks size={15} />
              Runtime Matrix
            </button>
            <button className="ghostButton" onClick={startSoakSession} disabled={loading || soakStatus?.status === "running"}>
              <Play size={15} />
              Start Soak
            </button>
            <button className="ghostButton" onClick={stopSoakSession} disabled={loading || soakStatus?.status !== "running"}>
              <XCircle size={15} />
              Stop Soak
            </button>
            <button className="ghostButton" onClick={enforceRetention} disabled={loading}>
              <FileClock size={15} />
              Enforce Retention
            </button>
            <button className="ghostButton" onClick={dispatchAlerts} disabled={loading}>
              <AlertTriangle size={15} />
              Dispatch Alerts
            </button>
          </div>
          <JsonBlock value={{ alerting: alertingStatus ?? { status: "not loaded" }, recent_alerts: alertLogs.items.slice(-5).reverse() }} />
        </Section>
        <Section title="Runtime Telemetry" icon={<ScrollText size={18} />}>
          <div className="traceSummary">
            <Metric label="Trace Window" value={String(runtimeTelemetrySummary.window_size ?? runtimeTraceRecent.items.length)} />
            <Metric label="Avg Latency" value={`${String(runtimeTelemetrySummary.avg_latency_ms ?? 0)} ms`} />
            <Metric label="Agent Calls" value={String(runtimeTelemetrySummary.agent_calls ?? 0)} />
            <Metric label="Blocks" value={String(runtimeTelemetrySummary.block_count ?? 0)} />
          </div>
          <div className="traceSummary">
            {Object.entries(runtimeTelemetryRouteDistribution).slice(0, 6).map(([route, count]) => (
              <Metric key={route} label={route} value={String(count)} />
            ))}
          </div>
          <div className="dataTable traceTable">
            <div className="dataTableHead">
              <span>Trace</span>
              <span>Channel</span>
              <span>Route</span>
              <span>Status</span>
              <span>Latency</span>
              <span>Failure</span>
            </div>
            {runtimeTraceItems.map((item, index) => (
              <div className="dataTableRow" key={String(item.trace_id ?? index)}>
                <code>{String(item.trace_id ?? "-")}</code>
                <span>{String(item.channel ?? "-")}</span>
                <span>{String(item.final_route ?? item.route ?? "-")}</span>
                <StatusPill value={String(item.status ?? "unknown")} />
                <span>{String(item.latency_ms ?? "-")} ms</span>
                <code>{String(item.failure_reason ?? "-")}</code>
              </div>
            ))}
            {!runtimeTraceItems.length ? <div className="emptyState"><ScrollText size={18} />No runtime traces yet.</div> : null}
          </div>
          <div className="dataTable traceTable">
            <div className="dataTableHead">
              <span>Route</span>
              <span>Count</span>
              <span>Share</span>
            </div>
            {runtimeTelemetryRoutes.slice(-6).map((item, index) => (
              <div className="dataTableRow" key={index}>
                <span>{String(item.route ?? "-")}</span>
                <strong>{String(item.count ?? 0)}</strong>
                <span>{String(item.share ?? 0)}</span>
              </div>
            ))}
            {!runtimeTelemetryRoutes.length ? <div className="emptyState"><ListChecks size={18} />No route metrics yet.</div> : null}
          </div>
          <div className="dataTable traceTable">
            <div className="dataTableHead">
              <span>Trace</span>
              <span>Route</span>
              <span>Failure</span>
              <span>Completed</span>
            </div>
            {runtimeFailureItems.map((item, index) => (
              <div className="dataTableRow" key={String(item.trace_id ?? index)}>
                <code>{String(item.trace_id ?? "-")}</code>
                <span>{String(item.final_route ?? item.route ?? "-")}</span>
                <code>{String(item.failure_reason ?? "-")}</code>
                <small>{String(item.completed_at ?? "-")}</small>
              </div>
            ))}
            {!runtimeFailureItems.length ? <div className="emptyState"><CheckCircle2 size={18} />No failures in current window.</div> : null}
          </div>
          <JsonBlock
            value={{
              summary: runtimeMetricsSummary ?? { status: "not loaded" },
              routes: runtimeMetricsRoutes ?? { status: "not loaded" },
              model_cost: runtimeMetricsModelCost ?? { status: "not loaded" },
              soak: runtimeSoakTelemetry ?? { status: "not loaded" },
            }}
          />
        </Section>
      </section>

      <section className={`workspaceGrid workbenchPane ${activeSection === "runtime" ? "active" : ""}`}>
        <Section title="State / Heartbeat" icon={<BookOpen size={18} />}>
          <div className="stateDefinitionGrid">
            {stateDefinitions.map((definition) => (
              <div className="stateDefinitionItem" key={String(definition.id)}>
                <strong>{String(definition.id)}</strong>
                <span>{String(definition.owner ?? "-")}</span>
                <small>{String(definition.freshness ?? "-")}</small>
              </div>
            ))}
          </div>
          <pre className="heartbeatBlock">{heartbeat || "heartbeat not loaded"}</pre>
        </Section>

        <Section title="Agent Contract" icon={<Bot size={18} />}>
          <JsonBlock value={agentContract ?? { status: "not loaded" }} />
        </Section>
      </section>

      <section className={`logGrid single workbenchPane ${activeSection === "ops" ? "active" : ""}`}>
        <Section title="Implementation Phases" icon={<ListChecks size={18} />}>
          <div className="phaseGrid">
            {phases.map((phase) => (
              <div className="phaseItem" key={String(phase.phase)}>
                <strong>{String(phase.phase)}</strong>
                <span>{String(phase.name)}</span>
                <StatusPill value={String(phase.status ?? "unknown")} />
              </div>
            ))}
          </div>
        </Section>
      </section>

      <section className={`logGrid workbenchPane ${activeSection === "logs" ? "active" : ""}`}>
        <Section title="Event Log" icon={<FileClock size={18} />}>
          <JsonBlock value={events.items.slice(-4).reverse()} />
        </Section>
        <Section title="Action Record" icon={<ClipboardList size={18} />}>
          <JsonBlock value={actions.items.slice(-4).reverse()} />
        </Section>
        <Section title="Core Model Trace" icon={<Brain size={18} />}>
          <JsonBlock value={coreModelLogs.items.slice(-4).reverse()} />
        </Section>
        <Section title="Rollback Log" icon={<History size={18} />}>
          <JsonBlock value={rollbackLogs.items.slice(-4).reverse()} />
        </Section>
      </section>
        </div>
      </section>
    </main>
  );
}
