import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  ClipboardList,
  Database,
  FileClock,
  Gauge,
  Play,
  RefreshCw,
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
  const [message, setMessage] = useState(initialMessage);
  const [result, setResult] = useState<MessageResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    const [runtimeData, stateData, eventData, actionData, reviewData] = await Promise.all([
      fetchJson<RuntimeInfo>("/runtime"),
      fetchJson<VeyraState>("/state"),
      fetchJson<LogResponse>("/logs/events?limit=20"),
      fetchJson<LogResponse>("/logs/actions?limit=20"),
      fetchJson<LogResponse>("/reviews/actions?limit=20")
    ]);
    setRuntime(runtimeData);
    setState(stateData);
    setEvents(eventData);
    setActions(actionData);
    setReviews(reviewData);
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

  const latestClaims = useMemo(() => state?.belief_state.claims?.slice(-5).reverse() ?? [], [state]);
  const focus = state?.attention_state.focus ?? [];
  const currentRisk = String(state?.risk_state.current_risk ?? "R0");

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
        <Metric label="Runtime" value={runtime?.identity.selected_agent ?? "unknown"} />
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

      <section className="logGrid">
        <Section title="Event Log" icon={<FileClock size={18} />}>
          <JsonBlock value={events.items.slice(-6).reverse()} />
        </Section>
        <Section title="Action Record" icon={<ClipboardList size={18} />}>
          <JsonBlock value={actions.items.slice(-6).reverse()} />
        </Section>
        <Section title="Last Result" icon={<Activity size={18} />}>
          <JsonBlock value={result ?? { status: "no message submitted in this console session" }} />
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
