import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  ClipboardList,
  Database,
  Eye,
  FileClock,
  FileText,
  Gauge,
  History,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
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

const initialMessage = "帮我看 18789 端口有没有被占用";

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
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
  const [memoryLogs, setMemoryLogs] = useState<LogResponse>({ items: [] });
  const [agentStatus, setAgentStatus] = useState<Record<string, JsonValue> | null>(null);
  const [agentRegistry, setAgentRegistry] = useState<Record<string, JsonValue> | null>(null);
  const [mvpStatus, setMvpStatus] = useState<Record<string, JsonValue> | null>(null);
  const [rollbackLogs, setRollbackLogs] = useState<LogResponse>({ items: [] });
  const [diffStatus, setDiffStatus] = useState<Record<string, JsonValue> | null>(null);
  const [rollbackDiff, setRollbackDiff] = useState<Record<string, JsonValue> | null>(null);
  const [message, setMessage] = useState(initialMessage);
  const [snapshotPath, setSnapshotPath] = useState("README.md");
  const [agentBaseUrl, setAgentBaseUrl] = useState("");
  const [result, setResult] = useState<MessageResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    const [runtimeData, stateData, eventData, actionData, reviewData, toolData, memoryData, rollbackData, agentData, registryData, mvpData, diffData] = await Promise.all([
      fetchJson<RuntimeInfo>("/runtime"),
      fetchJson<VeyraState>("/state"),
      fetchJson<LogResponse>("/logs/events?limit=20"),
      fetchJson<LogResponse>("/logs/actions?limit=20"),
      fetchJson<LogResponse>("/reviews/actions?limit=20"),
      fetchJson<LogResponse>("/logs/tools?limit=20"),
      fetchJson<LogResponse>("/logs/memory?limit=20"),
      fetchJson<LogResponse>("/logs/rollback?limit=20"),
      fetchJson<Record<string, JsonValue>>("/agent/status"),
      fetchJson<Record<string, JsonValue>>("/agents"),
      fetchJson<Record<string, JsonValue>>("/mvp/status"),
      fetchJson<Record<string, JsonValue>>("/rollback/diff")
    ]);
    setRuntime(runtimeData);
    setState(stateData);
    setEvents(eventData);
    setActions(actionData);
    setReviews(reviewData);
    setToolLogs(toolData);
    setMemoryLogs(memoryData);
    setRollbackLogs(rollbackData);
    setAgentStatus(agentData);
    setAgentRegistry(registryData);
    setMvpStatus(mvpData);
    setDiffStatus(diffData);
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

  const latestClaims = useMemo(() => state?.belief_state.claims?.slice(-5).reverse() ?? [], [state]);
  const focus = state?.attention_state.focus ?? [];
  const currentRisk = String(state?.risk_state.current_risk ?? "R0");
  const connected = agentStatus?.connected === true ? "connected" : String(agentStatus?.status ?? "unconfigured");
  const snapshots = Array.isArray(state?.rollback_state?.snapshots) ? (state.rollback_state.snapshots as Array<Record<string, JsonValue>>) : [];
  const memoryItems = Array.isArray(state?.agent_memory?.items) ? (state.agent_memory.items as Array<Record<string, JsonValue>>) : [];
  const agents = asRecord(agentRegistry?.agents);
  const selectedAgent = String(agentRegistry?.selected_agent ?? runtime?.identity.selected_agent ?? "openclaw");

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
        <Metric label="Focus" value={focus.length ? focus.join(", ") : "none"} />
      </section>

      <section className="workspaceGrid">
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

      <section className="workspaceGrid">
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

      <section className="workspaceGrid">
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

        <Section title="Memory Bridge" icon={<Database size={18} />}>
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

      <section className="logGrid operationsGrid">
        <Section title="Tool Proxy Monitor" icon={<Shield size={18} />}>
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
                <span>{String(item.operation ?? (Array.isArray(item.command) ? "shell" : "policy"))}</span>
                <StatusPill value={String(item.status ?? "unknown")} />
                <code>{toolTarget(item)}</code>
                <span>{String(item.approved_by ?? "-")}</span>
                <code>{compactJson(item.returncode ?? item.reason ?? item.stderr, "ok")}</code>
                <small>{String(item.timestamp ?? "")}</small>
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
      <section className="logGrid single">
        <Section title="MVP Readiness" icon={<CheckCircle2 size={18} />}>
          <JsonBlock value={mvpStatus ?? { status: "not loaded" }} />
        </Section>
      </section>
      <section className="logGrid">
        <Section title="Event Log" icon={<FileClock size={18} />}>
          <JsonBlock value={events.items.slice(-4).reverse()} />
        </Section>
        <Section title="Action Record" icon={<ClipboardList size={18} />}>
          <JsonBlock value={actions.items.slice(-4).reverse()} />
        </Section>
        <Section title="Rollback Log" icon={<History size={18} />}>
          <JsonBlock value={rollbackLogs.items.slice(-4).reverse()} />
        </Section>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
