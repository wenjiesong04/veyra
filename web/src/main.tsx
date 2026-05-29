import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
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
  XCircle
} from "lucide-react";
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
  agent_memory?: Record<string, JsonValue>;
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

type WorkbenchSection = "awareness" | "governance" | "runtime" | "ops" | "audit" | "logs";

const initialMessage = "帮我看 18789 端口有没有被占用";

const workbenchSections: Array<{ id: WorkbenchSection; label: string; icon: React.ReactNode }> = [
  { id: "awareness", label: "Awareness", icon: <Gauge size={15} /> },
  { id: "governance", label: "Governance", icon: <Shield size={15} /> },
  { id: "runtime", label: "Runtime", icon: <Bot size={15} /> },
  { id: "ops", label: "Ops", icon: <Activity size={15} /> },
  { id: "audit", label: "Audit", icon: <History size={15} /> },
  { id: "logs", label: "Logs", icon: <ScrollText size={15} /> }
];

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (options?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(url, options ? { ...options, headers } : undefined);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

function StatusPill({ value }: { value: string }) {
  const tone = value === "R5" || value === "blocked" ? "danger" : value === "R1" || value === "success" ? "good" : "neutral";
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

function App() {
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
  const [activeSection, setActiveSection] = useState<WorkbenchSection>("awareness");

  const refresh = async () => {
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
      fetchJson<LogResponse>("/logs/memory?limit=20"),
      fetchJson<Record<string, JsonValue>>("/memory/providers/diagnostics?provider=all&session_id=console"),
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
    refresh().catch((caught: Error) => setError(caught.message));
  }, []);

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
          user_id: "console-user",
          session_id: "console-session"
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
    setLoading(true);
    setError(null);
    try {
      await fetchJson(`/agents/${selected}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: agentBaseUrl })
      });
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
        body: JSON.stringify({ target: watchTarget, reason: "console_watch", enabled: true })
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
      const response = await fetchJson<Record<string, JsonValue>>("/external/refresh?limit=5", { method: "POST" });
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
        body: JSON.stringify({ provider: "all", session_id: "console", write_probe: false })
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

  const latestClaims = useMemo(() => state?.belief_state.claims?.slice(-5).reverse() ?? [], [state]);
  const focus = state?.attention_state.focus ?? [];
  const currentRisk = String(state?.risk_state.current_risk ?? "R0");
  const connected = agentStatus?.connected === true ? "connected" : String(agentStatus?.status ?? "unconfigured");
  const snapshots = Array.isArray(state?.rollback_state?.snapshots) ? (state.rollback_state.snapshots as Array<Record<string, JsonValue>>) : [];
  const memoryItems = Array.isArray(state?.agent_memory?.items) ? (state.agent_memory.items as Array<Record<string, JsonValue>>) : [];
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
  const externalWatchlist = Array.isArray(state?.external_world?.watchlist) ? (state.external_world.watchlist as Array<JsonValue>) : [];
  const externalSummaries = Array.isArray(state?.external_world?.summaries) ? (state.external_world.summaries as Array<Record<string, JsonValue>>) : [];
  const coreModelConfigured = coreModelStatus?.configured === true ? "configured" : String(coreModelStatus?.status ?? "unconfigured");
  const opsStatus = String(opsHealth?.status ?? "unknown");
  const deploymentStatus = String(deploymentReadiness?.status ?? "unknown");
  const runtimeTelemetrySummary = asRecord(runtimeMetricsSummary);
  const runtimeTelemetryRouteDistribution = asRecord(runtimeTelemetrySummary.route_distribution);
  const runtimeTelemetryRoutesRaw = asRecord(runtimeMetricsRoutes).items;
  const runtimeTelemetryRoutes = Array.isArray(runtimeTelemetryRoutesRaw) ? (runtimeTelemetryRoutesRaw as Array<Record<string, JsonValue>>) : [];
  const runtimeTraceItems = runtimeTraceRecent.items.slice(-6).reverse();
  const runtimeFailureItems = runtimeMetricsFailures.items.slice(-6).reverse();

  return (
    <main className="appShell">
      <header className="topbar">
        <div>
          <div className="eyebrow">Awareness & Agent Control Console</div>
          <h1>Veyra</h1>
        </div>
        <div className="topbarRight">
          <StatusPill value={runtime?.lifecycle.status ?? "loading"} />
          <button className="iconButton" onClick={() => refresh()} title="Refresh state" aria-label="Refresh state">
            <RefreshCw size={18} />
          </button>
        </div>
      </header>

      {error ? (
        <div className="alert">
          <AlertTriangle size={18} />
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
        <Section title="Setup Wizard" icon={<Settings size={18} />}>
          <div className="setupGrid">
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
              <span>Current task</span>
              <strong>{String(state?.task_state.current_task ? "active" : "none")}</strong>
            </div>
            <div>
              <span>Executor</span>
              <strong>{String(state?.executor_state.selected_agent ?? "unknown")}</strong>
            </div>
          </div>
        </Section>
      </section>

      <section className={`workspaceGrid workbenchPane ${activeSection === "governance" ? "active" : ""}`}>
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
                  <span>{String(snapshot.source ?? "-")}</span>
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

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
