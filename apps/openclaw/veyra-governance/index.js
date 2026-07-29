import { createHash } from "node:crypto";

const PLUGIN_ID = "veyra-governance";
const PROTOCOL_VERSION = "veyra.openclaw.governance.v1";
const IMPLEMENTATION_REVISION = "veyra.openclaw.governance.phase6.v1";
const RUN_CONTEXT_NAMESPACE = "session-v1";
const SESSION_INDEX_NAMESPACE = "session-index-v1";
const RESERVATION_INDEX_NAMESPACE = "reservation-index-v1";
const RESERVATION_NAMESPACE_PREFIX = "reservation-v1-";
const RESERVATION_INDEX_MAX_ENTRIES = 256;
const PROCESS_CONTEXT_STORE_SYMBOL = Symbol.for(
  "veyra.openclaw.governance.process-context.v1",
);
const PROCESS_RUNTIME_STATE_SYMBOL = Symbol.for(
  "veyra.openclaw.governance.runtime-state.v1",
);
const PROCESS_CONTEXT_KEY_PREFIX = `${PLUGIN_ID.length}:${PLUGIN_ID}`;
const CUSTOM_TOOLS = new Set([
  "veyra_file_read",
  "veyra_file_write",
  "veyra_shell_probe",
]);
const CANONICAL_TOOL_NAMES = Object.freeze({
  veyra_file_read: "file.read",
  veyra_file_write: "file.write",
  veyra_shell_probe: "shell.run",
});
const NATIVE_SIDE_EFFECT_TOOLS = new Set([
  "read",
  "write",
  "edit",
  "apply_patch",
  "exec",
  "process",
  "code_execution",
  "browser",
  "web_fetch",
  "web_search",
]);
const SESSION_TTL_MAX_MS = 15 * 60 * 1000;
const IDENTIFIER_MAX_BYTES = 512;
const TOKEN_MAX_BYTES = 4096;
const OBSERVATION_MAX_BYTES = 8192;
const OBSERVATION_MAX_DEPTH = 6;
const OBSERVATION_MAX_ENTRIES = 32;
const OBSERVATION_MAX_STRING_BYTES = 2048;

const DEFAULT_CONFIG = Object.freeze({
  baseUrl: "http://127.0.0.1:8000",
  requestTimeoutMs: 5000,
  maxRequestBytes: 1152 * 1024,
  maxResponseBytes: 512 * 1024,
  maxToolResultBytes: 128 * 1024,
});

function defaultProcessContextStore() {
  const existing = globalThis[PROCESS_CONTEXT_STORE_SYMBOL];
  if (existing instanceof Map) {
    return existing;
  }
  const store = new Map();
  Object.defineProperty(globalThis, PROCESS_CONTEXT_STORE_SYMBOL, {
    configurable: false,
    enumerable: false,
    writable: false,
    value: store,
  });
  return store;
}

function processContextKey(runId, namespace) {
  return `${PROCESS_CONTEXT_KEY_PREFIX}${runId.length}:${runId}${namespace.length}:${namespace}`;
}

function authorityFingerprint(config) {
  return createHash("sha256")
    .update(PROTOCOL_VERSION, "utf8")
    .update("\0", "utf8")
    .update(config.baseUrl, "utf8")
    .digest("hex");
}

function parseProcessContextKey(key) {
  if (
    typeof key !== "string" ||
    !key.startsWith(PROCESS_CONTEXT_KEY_PREFIX)
  ) {
    return undefined;
  }
  let offset = PROCESS_CONTEXT_KEY_PREFIX.length;
  const readPart = () => {
    const colon = key.indexOf(":", offset);
    if (colon < offset) {
      return undefined;
    }
    const rawLength = key.slice(offset, colon);
    if (!/^(0|[1-9][0-9]*)$/.test(rawLength)) {
      return undefined;
    }
    const length = Number(rawLength);
    if (!Number.isSafeInteger(length) || length < 1) {
      return undefined;
    }
    const start = colon + 1;
    const end = start + length;
    if (end > key.length) {
      return undefined;
    }
    offset = end;
    return key.slice(start, end);
  };
  const runId = readPart();
  const namespace = readPart();
  if (
    typeof runId !== "string" ||
    typeof namespace !== "string" ||
    offset !== key.length
  ) {
    return undefined;
  }
  return { runId, namespace };
}

function newMetrics() {
  return {
    registrations: 0,
    context_hydrations: 0,
    replacements: 0,
    cancellations: 0,
    expirations: 0,
    reservation_expirations: 0,
    native_blocks: 0,
    unknown_blocks: 0,
    unregistered_custom_blocks: 0,
    disallowed_custom_blocks: 0,
    allowlist_blocks: 0,
    malformed_blocks: 0,
    preflight_allows: 0,
    preflight_denials: 0,
    preflight_failures: 0,
    executions: 0,
    execution_failures: 0,
    observations: 0,
    observation_failures: 0,
    blocked_observations: 0,
  };
}

function processRuntimeState(processContextStore) {
  const existing = processContextStore.get(
    PROCESS_RUNTIME_STATE_SYMBOL,
  );
  if (
    existing &&
    existing.sessions instanceof Map &&
    existing.sessionsByRunId instanceof Map &&
    existing.reservations instanceof Map &&
    isRecord(existing.metrics)
  ) {
    return existing;
  }
  if (existing !== undefined) {
    throw new GovernanceError(
      "invalid_runtime",
      "process runtime state is invalid",
    );
  }
  const state = {
    sessions: new Map(),
    sessionsByRunId: new Map(),
    reservations: new Map(),
    metrics: newMetrics(),
  };
  processContextStore.set(PROCESS_RUNTIME_STATE_SYMBOL, state);
  return state;
}

const FILE_READ_SCHEMA = Object.freeze({
  type: "object",
  additionalProperties: false,
  required: ["path"],
  properties: {
    path: { type: "string", minLength: 1, maxLength: 4096 },
  },
});

const FILE_WRITE_SCHEMA = Object.freeze({
  type: "object",
  additionalProperties: false,
  required: ["path", "content"],
  properties: {
    path: { type: "string", minLength: 1, maxLength: 4096 },
    content: { type: "string", maxLength: 1048576 },
  },
});

const SHELL_PROBE_SCHEMA = Object.freeze({
  type: "object",
  additionalProperties: false,
  required: ["argv"],
  properties: {
    argv: {
      type: "array",
      minItems: 1,
      maxItems: 32,
      items: { type: "string", minLength: 1, maxLength: 4096 },
    },
  },
});

class GovernanceError extends Error {
  constructor(code, message = code) {
    super(message);
    this.name = "GovernanceError";
    this.code = code;
  }
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function byteLength(value) {
  return Buffer.byteLength(String(value), "utf8");
}

function requireBoundedString(value, field, maxBytes = IDENTIFIER_MAX_BYTES) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value !== value.trim() ||
    value.includes("\0") ||
    byteLength(value) > maxBytes
  ) {
    throw new GovernanceError("invalid_registration", `${field} is invalid`);
  }
  return value;
}

function boundedInteger(value, fallback, minimum, maximum) {
  if (value === undefined) {
    return fallback;
  }
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new GovernanceError("invalid_config", "integer config is out of range");
  }
  return value;
}

function normalizeBaseUrl(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new GovernanceError("invalid_config", "baseUrl is invalid");
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    throw new GovernanceError("invalid_config", "baseUrl must be an HTTP origin");
  }
  parsed.pathname = parsed.pathname.replace(/\/+$/, "") || "/";
  return parsed.toString().replace(/\/$/, "");
}

function resolveConfig(rawConfig) {
  const source = isRecord(rawConfig) ? rawConfig : {};
  return Object.freeze({
    baseUrl: normalizeBaseUrl(source.baseUrl ?? DEFAULT_CONFIG.baseUrl),
    requestTimeoutMs: boundedInteger(
      source.requestTimeoutMs,
      DEFAULT_CONFIG.requestTimeoutMs,
      100,
      30000,
    ),
    maxRequestBytes: boundedInteger(
      source.maxRequestBytes,
      DEFAULT_CONFIG.maxRequestBytes,
      4096,
      4 * 1024 * 1024,
    ),
    maxResponseBytes: boundedInteger(
      source.maxResponseBytes,
      DEFAULT_CONFIG.maxResponseBytes,
      4096,
      4 * 1024 * 1024,
    ),
    maxToolResultBytes: boundedInteger(
      source.maxToolResultBytes,
      DEFAULT_CONFIG.maxToolResultBytes,
      4096,
      1024 * 1024,
    ),
  });
}

function canonicalJson(value, maxBytes) {
  const seen = new Set();
  let estimatedBytes = 0;

  function addBudget(candidate) {
    estimatedBytes += byteLength(candidate) + 4;
    if (estimatedBytes > maxBytes) {
      throw new GovernanceError("payload_too_large", "JSON payload exceeds budget");
    }
  }

  function visit(candidate) {
    if (candidate === null || typeof candidate === "boolean") {
      addBudget(String(candidate));
      return candidate;
    }
    if (typeof candidate === "string") {
      addBudget(candidate);
      return candidate;
    }
    if (typeof candidate === "number") {
      if (!Number.isFinite(candidate)) {
        throw new GovernanceError("invalid_json", "non-finite number is not JSON");
      }
      addBudget(String(candidate));
      return candidate;
    }
    if (typeof candidate !== "object") {
      throw new GovernanceError("invalid_json", "unsupported JSON value");
    }
    if (seen.has(candidate)) {
      throw new GovernanceError("invalid_json", "cyclic JSON value");
    }
    seen.add(candidate);
    try {
      if (Array.isArray(candidate)) {
        addBudget(candidate.length);
        return candidate.map((entry) => visit(entry));
      }
      const output = {};
      for (const key of Object.keys(candidate).sort()) {
        addBudget(key);
        output[key] = visit(candidate[key]);
      }
      return output;
    } finally {
      seen.delete(candidate);
    }
  }

  const serialized = JSON.stringify(visit(value));
  if (byteLength(serialized) > maxBytes) {
    throw new GovernanceError("payload_too_large", "JSON payload exceeds budget");
  }
  return serialized;
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function exactDigest(value, maxBytes) {
  const serialized = canonicalJson(value, maxBytes);
  return {
    algorithm: "sha256",
    digest: sha256(serialized),
    serialized,
    serialized_bytes: byteLength(serialized),
  };
}

function truncateUtf8(value, maxBytes) {
  const input = String(value);
  if (byteLength(input) <= maxBytes) {
    return { value: input, truncated: false };
  }
  let low = 0;
  let high = input.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (byteLength(input.slice(0, middle)) <= maxBytes) {
      low = middle;
    } else {
      high = middle - 1;
    }
  }
  return { value: input.slice(0, low), truncated: true };
}

function boundedSnapshot(value, options = {}) {
  const maxBytes = options.maxBytes ?? OBSERVATION_MAX_BYTES;
  const maxDepth = options.maxDepth ?? OBSERVATION_MAX_DEPTH;
  const maxEntries = options.maxEntries ?? OBSERVATION_MAX_ENTRIES;
  const maxStringBytes =
    options.maxStringBytes ?? OBSERVATION_MAX_STRING_BYTES;
  const seen = new WeakSet();
  let truncated = false;
  let approximateBytes = 0;

  function charge(valueToCharge) {
    approximateBytes += byteLength(valueToCharge) + 4;
    if (approximateBytes > maxBytes) {
      truncated = true;
      return false;
    }
    return true;
  }

  function visit(candidate, depth) {
    if (candidate === null || typeof candidate === "boolean") {
      return charge(candidate) ? candidate : "[budget]";
    }
    if (typeof candidate === "number") {
      const normalized = Number.isFinite(candidate)
        ? candidate
        : `[number:${String(candidate)}]`;
      return charge(normalized) ? normalized : "[budget]";
    }
    if (typeof candidate === "string") {
      const clipped = truncateUtf8(candidate, maxStringBytes);
      truncated ||= clipped.truncated;
      return charge(clipped.value) ? clipped.value : "[budget]";
    }
    if (typeof candidate === "bigint") {
      truncated = true;
      const rendered = `[bigint:${candidate.toString()}]`;
      return charge(rendered) ? rendered : "[budget]";
    }
    if (typeof candidate === "undefined") {
      truncated = true;
      return "[undefined]";
    }
    if (typeof candidate === "function" || typeof candidate === "symbol") {
      truncated = true;
      return `[${typeof candidate}]`;
    }
    if (depth >= maxDepth) {
      truncated = true;
      return "[depth]";
    }
    if (seen.has(candidate)) {
      truncated = true;
      return "[cycle]";
    }
    seen.add(candidate);
    try {
      if (Array.isArray(candidate)) {
        const entries = candidate.slice(0, maxEntries);
        if (entries.length !== candidate.length) {
          truncated = true;
        }
        return entries.map((entry) => visit(entry, depth + 1));
      }
      const keys = Object.keys(candidate).sort().slice(0, maxEntries);
      if (keys.length !== Object.keys(candidate).length) {
        truncated = true;
      }
      const output = {};
      for (const key of keys) {
        if (!charge(key)) {
          output["[truncated]"] = true;
          break;
        }
        try {
          output[key] = visit(candidate[key], depth + 1);
        } catch {
          truncated = true;
          output[key] = "[unreadable]";
        }
      }
      return output;
    } finally {
      seen.delete(candidate);
    }
  }

  let snapshot = visit(value, 0);
  let serialized = JSON.stringify(snapshot);
  if (byteLength(serialized) > maxBytes) {
    truncated = true;
    snapshot = {
      truncated: true,
      type: Array.isArray(value) ? "array" : typeof value,
    };
    serialized = JSON.stringify(snapshot);
  }
  return Object.freeze({
    snapshot,
    serialized,
    serialized_bytes: byteLength(serialized),
    truncated,
  });
}

function boundedDigest(value, options = {}) {
  const bounded = boundedSnapshot(value, options);
  return Object.freeze({
    algorithm: "sha256",
    digest: sha256(bounded.serialized),
    serialized_bytes: bounded.serialized_bytes,
    truncated: bounded.truncated,
  });
}

async function readBoundedResponse(response, maxBytes) {
  const advertised = Number(response.headers?.get?.("content-length"));
  if (Number.isFinite(advertised) && advertised > maxBytes) {
    throw new GovernanceError(
      "invalid_response",
      "Veyra response exceeds byte budget",
    );
  }
  if (!response.body?.getReader) {
    const fallback = await response.text();
    if (byteLength(fallback) > maxBytes) {
      throw new GovernanceError(
        "invalid_response",
        "Veyra response exceeds byte budget",
      );
    }
    return fallback;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      size += value.byteLength;
      if (size > maxBytes) {
        await reader.cancel();
        throw new GovernanceError(
          "invalid_response",
          "Veyra response exceeds byte budget",
        );
      }
      chunks.push(Buffer.from(value));
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks, size).toString("utf8");
}

function safeReason(value, fallback) {
  if (typeof value !== "string" || value.length === 0) {
    return fallback;
  }
  return truncateUtf8(value.replace(/[\u0000-\u001f\u007f]/g, " "), 400).value;
}

function reservationKey(runId, toolCallId) {
  return `${runId.length}:${runId}${toolCallId.length}:${toolCallId}`;
}

function parseExpiry(value, field) {
  const parsed =
    typeof value === "number"
      ? value
      : typeof value === "string"
        ? Date.parse(value)
        : Number.NaN;
  if (!Number.isSafeInteger(parsed)) {
    throw new GovernanceError("invalid_registration", `${field} is invalid`);
  }
  return parsed;
}

function requireSha256(value, field) {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) {
    throw new GovernanceError("invalid_response", `${field} is invalid`);
  }
  return value;
}

function publicToolResult(result, maxBytes) {
  try {
    return {
      text: canonicalJson(result, maxBytes),
      truncated: false,
    };
  } catch (error) {
    if (!(error instanceof GovernanceError) || error.code !== "payload_too_large") {
      throw error;
    }
    const digest = boundedDigest(result);
    return {
      text: JSON.stringify({
        governed_result: "omitted",
        reason: "result exceeded the OpenClaw display budget",
        result_digest: digest.digest,
      }),
      truncated: true,
    };
  }
}

function createToolDefinition({
  name,
  label,
  description,
  parameters,
  sessionKey,
  executeGovernedTool,
}) {
  return {
    name,
    label,
    description,
    parameters,
    executionMode: "sequential",
    execute: async (toolCallId, params, signal) =>
      executeGovernedTool({
        sessionKey,
        toolName: name,
        toolCallId,
        params,
        signal,
      }),
  };
}

export function createVeyraGovernancePlugin(runtimeOptions = {}) {
  const processContextStore =
    runtimeOptions.processContextStore ?? defaultProcessContextStore();
  if (
    !processContextStore ||
    typeof processContextStore.get !== "function" ||
    typeof processContextStore.set !== "function" ||
    typeof processContextStore.delete !== "function" ||
    typeof processContextStore.keys !== "function"
  ) {
    throw new GovernanceError(
      "invalid_runtime",
      "process context store is invalid",
    );
  }
  return {
    id: PLUGIN_ID,
    name: "Veyra Governance",
    description: "Fail-closed bridge for Veyra-governed OpenClaw tool calls.",
    register(api) {
      const config = resolveConfig({
        ...(isRecord(api.pluginConfig) ? api.pluginConfig : {}),
        ...(isRecord(runtimeOptions.config) ? runtimeOptions.config : {}),
      });
      const fetchImpl = runtimeOptions.fetchImpl ?? globalThis.fetch;
      if (typeof fetchImpl !== "function") {
        throw new GovernanceError(
          "invalid_runtime",
          "a WHATWG-compatible fetch implementation is required",
        );
      }
      const configuredAuthorityFingerprint =
        authorityFingerprint(config);

      const sharedRuntimeState = processRuntimeState(
        processContextStore,
      );
      const {
        sessions,
        sessionsByRunId,
        reservations,
        metrics,
      } = sharedRuntimeState;
      const observedSessionBindings = new Map();

      function rememberSessionBinding(session) {
        if (
          session &&
          typeof session.sessionKey === "string" &&
          typeof session.runId === "string" &&
          Number.isSafeInteger(session.generation)
        ) {
          observedSessionBindings.set(session.sessionKey, {
            runId: session.runId,
            generation: session.generation,
          });
        }
        return session;
      }

      const setRunContext =
        api.runContext?.setRunContext?.bind(api.runContext) ??
        api.setRunContext?.bind(api);
      const getRunContext =
        api.runContext?.getRunContext?.bind(api.runContext) ??
        api.getRunContext?.bind(api);
      const clearRunContext =
        api.runContext?.clearRunContext?.bind(api.runContext) ??
        api.clearRunContext?.bind(api);

      function sessionIndexRunId(sessionKey) {
        return `veyra-session-${createHash("sha256")
          .update(sessionKey, "utf8")
          .digest("hex")}`;
      }

      function reservationNamespace(sessionKey, toolCallId, toolName) {
        return `${RESERVATION_NAMESPACE_PREFIX}${createHash("sha256")
          .update(reservationKey(sessionKey, toolCallId), "utf8")
          .update(toolName, "utf8")
          .digest("hex")}`;
      }

      function setSharedContext(runId, namespace, value) {
        let stored = false;
        if (typeof setRunContext === "function") {
          try {
            stored =
              setRunContext({
                runId,
                namespace,
                value,
              }) === true;
          } catch {
            stored = false;
          }
        }
        try {
          processContextStore.set(
            processContextKey(runId, namespace),
            structuredClone(value),
          );
          stored = true;
        } catch {
          // The host-owned run context may still be sufficient. If neither
          // process-local bridge accepted the value, registration fails closed.
        }
        if (!stored) {
          throw new GovernanceError(
            "registration_unavailable",
            "OpenClaw rejected the governed run context",
          );
        }
      }

      function getSharedContext(runId, namespace) {
        let hostValue;
        if (typeof getRunContext === "function") {
          try {
            hostValue = getRunContext({
              runId,
              namespace,
            });
          } catch {
            hostValue = undefined;
          }
        }
        const processValue = processContextStore.get(
          processContextKey(runId, namespace),
        );
        if (
          hostValue !== undefined &&
          processValue !== undefined
        ) {
          let hostCanonical;
          let processCanonical;
          try {
            hostCanonical = canonicalJson(
              hostValue,
              config.maxResponseBytes,
            );
            processCanonical = canonicalJson(
              processValue,
              config.maxResponseBytes,
            );
          } catch {
            throw new GovernanceError(
              "run_binding_mismatch",
              "governed host/process context is not comparable",
            );
          }
          if (hostCanonical !== processCanonical) {
            throw new GovernanceError(
              "run_binding_mismatch",
              "governed host/process context is inconsistent",
            );
          }
          return structuredClone(processValue);
        }
        const value =
          processValue !== undefined ? processValue : hostValue;
        return value === undefined ? undefined : structuredClone(value);
      }

      function clearSharedContext(runId, namespace) {
        if (typeof clearRunContext === "function") {
          try {
            clearRunContext({ runId, namespace });
          } catch {
            // Always clear the compatibility bridge even if the host registry
            // has already retired its run-context writer.
          }
        }
        processContextStore.delete(processContextKey(runId, namespace));
      }

      function sharedSessionPayload(session, state = session.state) {
        const payload = {
          state,
          sessionKey: session.sessionKey,
          expiresAt: session.expiresAt,
          runId: session.runId,
          bindingDigest: session.bindingDigest,
          authorityFingerprint: session.authorityFingerprint,
          allowedTools: [...session.allowedTools],
          generation: session.generation,
        };
        if (state === "active") {
          payload.dispatchToken = session.dispatchToken;
        }
        return payload;
      }

      function sharedSessionIndexPayload(session, state = session.state) {
        return {
          state,
          sessionKey: session.sessionKey,
          runId: session.runId,
          bindingDigest: session.bindingDigest,
          authorityFingerprint: session.authorityFingerprint,
          expiresAt: session.expiresAt,
          generation: session.generation,
        };
      }

      function persistSharedSession(session, state = session.state) {
        const indexRunId = sessionIndexRunId(session.sessionKey);
        setSharedContext(
          session.runId,
          RUN_CONTEXT_NAMESPACE,
          sharedSessionPayload(session, state),
        );
        try {
          setSharedContext(
            indexRunId,
            SESSION_INDEX_NAMESPACE,
            sharedSessionIndexPayload(session, state),
          );
        } catch (error) {
          clearSharedContext(session.runId, RUN_CONTEXT_NAMESPACE);
          throw error;
        }
      }

      function persistSharedTombstone(session, state) {
        try {
          persistSharedSession(session, state);
        } catch {
          // Veyra's server-side revocation remains authoritative. A stale
          // active marker still blocks native tools and cannot bypass broker
          // cancellation, expiry, or one-use execution claims.
        }
      }

      function parseSharedSession(shared, expectedRunId) {
        if (!isRecord(shared)) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "governed run context is malformed",
          );
        }
        const sessionKey = requireBoundedString(
          shared.sessionKey,
          "session_key",
        );
        const sharedRunId = requireBoundedString(
          shared.runId,
          "run_id",
        );
        const bindingDigest = requireSha256(
          shared.bindingDigest,
          "binding_digest",
        );
        const sharedAuthorityFingerprint = requireSha256(
          shared.authorityFingerprint,
          "authority_fingerprint",
        );
        const allowedTools = shared.allowedTools;
        const state = shared.state;
        const generation = shared.generation;
        if (
          sharedRunId !== expectedRunId ||
          sharedAuthorityFingerprint !==
            configuredAuthorityFingerprint ||
          !["active", "cancelled", "expired", "failed"].includes(state) ||
          !Number.isSafeInteger(generation) ||
          generation < 1 ||
          !Array.isArray(allowedTools) ||
          new Set(allowedTools).size !== allowedTools.length ||
          allowedTools.some(
            (toolName) =>
              typeof toolName !== "string" || !CUSTOM_TOOLS.has(toolName),
          )
        ) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "governed run context identity is invalid",
          );
        }
        const expiresAt = parseExpiry(shared.expiresAt, "expires_at");
        const effectiveState =
          state === "active" && Date.now() >= expiresAt
            ? "expired"
            : state;
        const dispatchToken =
          effectiveState === "active"
            ? requireBoundedString(
                shared.dispatchToken,
                "dispatch_token",
                TOKEN_MAX_BYTES,
              )
            : undefined;
        return {
          sessionKey,
          dispatchToken,
          expiresAt,
          runId: sharedRunId,
          bindingDigest,
          authorityFingerprint: sharedAuthorityFingerprint,
          allowedTools: [...allowedTools].sort(),
          generation,
          state: effectiveState,
          timer: undefined,
        };
      }

      function hydrateSharedSession(runId) {
        const shared = getSharedContext(runId, RUN_CONTEXT_NAMESPACE);
        if (shared === undefined) {
          return undefined;
        }
        const hydrated = parseSharedSession(shared, runId);
        if (shared.state === "active" && hydrated.state === "expired") {
          persistSharedTombstone(hydrated, "expired");
        }
        const currentByRun = activeSessionForRun(runId, {
          hydrate: false,
        });
        if (currentByRun) {
          if (
            currentByRun.sessionKey !== hydrated.sessionKey ||
            currentByRun.bindingDigest !== hydrated.bindingDigest ||
            currentByRun.generation !== hydrated.generation
          ) {
            throw new GovernanceError(
              "run_binding_mismatch",
              "governed run context conflicts with local session state",
            );
          }
          currentByRun.state = hydrated.state;
          currentByRun.dispatchToken = hydrated.dispatchToken;
          if (hydrated.state !== "active") {
            clearSessionReservations(
              currentByRun.sessionKey,
              currentByRun,
            );
            clearSessionRunIndex(currentByRun);
          }
          return rememberSessionBinding(currentByRun);
        }
        const currentByKey = assertSessionAuthority(
          sessions.get(hydrated.sessionKey),
        );
        if (
          currentByKey &&
          currentByKey.runId !== runId &&
          currentByKey.state === "active"
        ) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "governed session key is already bound to another run",
          );
        }
        sessions.set(hydrated.sessionKey, hydrated);
        if (hydrated.state === "active") {
          sessionsByRunId.set(runId, hydrated.sessionKey);
          scheduleExpiry(hydrated);
        }
        metrics.context_hydrations += 1;
        return rememberSessionBinding(hydrated);
      }

      function parseSharedSessionIndex(index, expectedSessionKey) {
        if (!isRecord(index)) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "governed session index is malformed",
          );
        }
        const sessionKey = requireBoundedString(
          index.sessionKey,
          "session_key",
        );
        const runId = requireBoundedString(index.runId, "run_id");
        const bindingDigest = requireSha256(
          index.bindingDigest,
          "binding_digest",
        );
        const indexedAuthorityFingerprint = requireSha256(
          index.authorityFingerprint,
          "authority_fingerprint",
        );
        const expiresAt = parseExpiry(index.expiresAt, "expires_at");
        const generation = index.generation;
        const state = index.state;
        if (
          sessionKey !== expectedSessionKey ||
          indexedAuthorityFingerprint !==
            configuredAuthorityFingerprint ||
          !["active", "cancelled", "expired", "failed"].includes(state) ||
          !Number.isSafeInteger(generation) ||
          generation < 1
        ) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "governed session index identity is invalid",
          );
        }
        return {
          sessionKey,
          runId,
          bindingDigest,
          authorityFingerprint: indexedAuthorityFingerprint,
          expiresAt,
          generation,
          state,
        };
      }

      function hydrateSharedSessionForKey(sessionKey) {
        const index = getSharedContext(
          sessionIndexRunId(sessionKey),
          SESSION_INDEX_NAMESPACE,
        );
        if (index === undefined) {
          return undefined;
        }
        const parsedIndex = parseSharedSessionIndex(index, sessionKey);
        const session = hydrateSharedSession(parsedIndex.runId);
        if (
          !session ||
          session.sessionKey !== sessionKey ||
          session.bindingDigest !== parsedIndex.bindingDigest ||
          session.authorityFingerprint !==
            parsedIndex.authorityFingerprint ||
          session.expiresAt !== parsedIndex.expiresAt ||
          session.generation !== parsedIndex.generation ||
          session.state !== parsedIndex.state
        ) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "governed session index does not match run context",
          );
        }
        return rememberSessionBinding(session);
      }

      function sharedReservationPayload(reservation) {
        const payload = {
          state: reservation.state,
          sessionKey: reservation.sessionKey,
          runId: reservation.runId,
          toolCallId: reservation.toolCallId,
          toolName: reservation.toolName,
          paramsDigest: reservation.paramsDigest,
          reservationId: reservation.reservationId,
          canonicalToolName: reservation.canonicalToolName,
          invocationDigest: reservation.invocationDigest,
          expiresAt: reservation.expiresAt,
        };
        if (reservation.state === "reserved") {
          payload.reservationToken = reservation.reservationToken;
          payload.executionToken = reservation.executionToken;
        }
        if (reservation.receiptRef !== undefined) {
          payload.receiptRef = reservation.receiptRef;
        }
        if (reservation.resultDigest !== undefined) {
          payload.resultDigest = reservation.resultDigest;
        }
        return payload;
      }

      function reservationIndexPayload(sessionKey, runId, namespaces) {
        return {
          sessionKey,
          runId,
          namespaces: [...namespaces].sort(),
        };
      }

      function parseSharedReservationIndex(
        shared,
        expectedSessionKey,
        expectedRunId,
      ) {
        if (!isRecord(shared)) {
          throw new GovernanceError(
            "reservation_missing",
            "shared Veyra reservation index is malformed",
          );
        }
        const sessionKey = requireBoundedString(
          shared.sessionKey,
          "session_key",
        );
        const runId = requireBoundedString(shared.runId, "run_id");
        const namespaces = shared.namespaces;
        if (
          sessionKey !== expectedSessionKey ||
          runId !== expectedRunId ||
          !Array.isArray(namespaces) ||
          namespaces.length > RESERVATION_INDEX_MAX_ENTRIES ||
          new Set(namespaces).size !== namespaces.length ||
          namespaces.some(
            (namespace) =>
              typeof namespace !== "string" ||
              !/^reservation-v1-[0-9a-f]{64}$/.test(namespace),
          )
        ) {
          throw new GovernanceError(
            "reservation_missing",
            "shared Veyra reservation index identity is invalid",
          );
        }
        return { sessionKey, runId, namespaces: [...namespaces] };
      }

      function reservationIndexFor(sessionKey, runId) {
        const shared = getSharedContext(
          runId,
          RESERVATION_INDEX_NAMESPACE,
        );
        if (shared === undefined) {
          return {
            sessionKey,
            runId,
            namespaces: [],
          };
        }
        return parseSharedReservationIndex(
          shared,
          sessionKey,
          runId,
        );
      }

      function writeReservationIndex(index) {
        if (index.namespaces.length === 0) {
          clearSharedContext(
            index.runId,
            RESERVATION_INDEX_NAMESPACE,
          );
          return;
        }
        setSharedContext(
          index.runId,
          RESERVATION_INDEX_NAMESPACE,
          reservationIndexPayload(
            index.sessionKey,
            index.runId,
            index.namespaces,
          ),
        );
      }

      function persistSharedReservation(reservation) {
        const namespace = reservationNamespace(
          reservation.sessionKey,
          reservation.toolCallId,
          reservation.toolName,
        );
        const index = reservationIndexFor(
          reservation.sessionKey,
          reservation.runId,
        );
        if (!index.namespaces.includes(namespace)) {
          if (
            index.namespaces.length >=
            RESERVATION_INDEX_MAX_ENTRIES
          ) {
            throw new GovernanceError(
              "reservation_missing",
              "Veyra reservation index capacity was exceeded",
            );
          }
          index.namespaces.push(namespace);
          writeReservationIndex(index);
        }
        try {
          setSharedContext(
            reservation.runId,
            namespace,
            sharedReservationPayload(reservation),
          );
        } catch (error) {
          index.namespaces = index.namespaces.filter(
            (candidate) => candidate !== namespace,
          );
          try {
            writeReservationIndex(index);
          } catch {
            // The token-free index may be conservatively stale, but no raw
            // reservation was accepted by the compatibility bridge.
          }
          throw error;
        }
      }

      function parseSharedReservation(
        shared,
        session,
        toolCallId,
        toolName,
      ) {
        if (!isRecord(shared)) {
          throw new GovernanceError(
            "reservation_missing",
            "shared Veyra reservation is malformed",
          );
        }
        const state = shared.state;
        const sharedToolCallId = requireBoundedString(
          shared.toolCallId,
          "tool_call_id",
        );
        const sharedToolName = requireBoundedString(
          shared.toolName,
          "tool_name",
        );
        const reservation = {
          key: reservationKey(session.runId, sharedToolCallId),
          state,
          sessionKey: requireBoundedString(
            shared.sessionKey,
            "session_key",
          ),
          runId: requireBoundedString(shared.runId, "run_id"),
          toolCallId: sharedToolCallId,
          toolName: sharedToolName,
          paramsDigest: requireSha256(
            shared.paramsDigest,
            "params_digest",
          ),
          reservationId: requireBoundedString(
            shared.reservationId,
            "reservation_id",
          ),
          canonicalToolName: requireBoundedString(
            shared.canonicalToolName,
            "canonical_tool_name",
          ),
          invocationDigest: requireSha256(
            shared.invocationDigest,
            "invocation_digest",
          ),
          expiresAt: parseExpiry(shared.expiresAt, "expires_at"),
        };
        if (
          !["reserved", "consumed", "executed", "failed"].includes(state) ||
          reservation.sessionKey !== session.sessionKey ||
          reservation.runId !== session.runId ||
          reservation.toolCallId !== toolCallId ||
          reservation.toolName !== toolName ||
          reservation.canonicalToolName !==
            CANONICAL_TOOL_NAMES[toolName]
        ) {
          throw new GovernanceError(
            "reservation_missing",
            "shared Veyra reservation identity is invalid",
          );
        }
        if (state === "reserved") {
          reservation.reservationToken = requireBoundedString(
            shared.reservationToken,
            "reservation_token",
            TOKEN_MAX_BYTES,
          );
          reservation.executionToken = requireBoundedString(
            shared.executionToken,
            "execution_token",
            TOKEN_MAX_BYTES,
          );
        }
        if (isRecord(shared.receiptRef)) {
          reservation.receiptRef = shared.receiptRef;
        }
        if (typeof shared.resultDigest === "string") {
          reservation.resultDigest = requireSha256(
            shared.resultDigest,
            "result_digest",
          );
        }
        return reservation;
      }

      function hydrateSharedReservation(sessionKey, toolCallId, toolName) {
        const session = hydrateSharedSessionForKey(sessionKey);
        if (
          !session ||
          session.state !== "active" ||
          !session.dispatchToken
        ) {
          throw new GovernanceError(
            "session_unavailable",
            "Veyra governed session is unavailable",
          );
        }
        const namespace = reservationNamespace(
          sessionKey,
          toolCallId,
          toolName,
        );
        const shared = getSharedContext(session.runId, namespace);
        if (shared === undefined) {
          throw new GovernanceError(
            "reservation_missing",
            "exact Veyra reservation is unavailable",
          );
        }
        const hydrated = parseSharedReservation(
          shared,
          session,
          toolCallId,
          toolName,
        );
        const current = reservations.get(hydrated.key);
        if (current?.timer) {
          hydrated.timer = current.timer;
        }
        reservations.set(hydrated.key, hydrated);
        return { session, reservation: hydrated };
      }

      function clearSharedReservation(
        session,
        toolCallId,
        toolName,
      ) {
        const namespace = reservationNamespace(
          session.sessionKey,
          toolCallId,
          toolName,
        );
        clearSharedContext(session.runId, namespace);
        const key = reservationKey(session.runId, toolCallId);
        const reservation = reservations.get(key);
        if (reservation?.timer) {
          clearTimeout(reservation.timer);
          reservation.timer = undefined;
        }
        if (reservation) {
          reservation.reservationToken = undefined;
          reservation.executionToken = undefined;
        }
        reservations.delete(key);
        const sharedIndex = getSharedContext(
          session.runId,
          RESERVATION_INDEX_NAMESPACE,
        );
        if (sharedIndex === undefined) {
          return;
        }
        const index = parseSharedReservationIndex(
          sharedIndex,
          session.sessionKey,
          session.runId,
        );
        index.namespaces = index.namespaces.filter(
          (candidate) => candidate !== namespace,
        );
        writeReservationIndex(index);
      }

      function processReservationNamespaces(runId) {
        const namespaces = new Set();
        for (const key of processContextStore.keys()) {
          const parsed = parseProcessContextKey(key);
          if (
            parsed?.runId === runId &&
            parsed.namespace.startsWith(
              RESERVATION_NAMESPACE_PREFIX,
            )
          ) {
            namespaces.add(parsed.namespace);
          }
        }
        return namespaces;
      }

      function sessionIdentityForCleanup(sessionKey, sessionHint) {
        if (
          sessionHint &&
          sessionHint.sessionKey === sessionKey &&
          typeof sessionHint.runId === "string"
        ) {
          return sessionHint;
        }
        const current = sessions.get(sessionKey);
        if (current && typeof current.runId === "string") {
          return current;
        }
        const rawIndex = getSharedContext(
          sessionIndexRunId(sessionKey),
          SESSION_INDEX_NAMESPACE,
        );
        if (
          isRecord(rawIndex) &&
          rawIndex.sessionKey === sessionKey &&
          typeof rawIndex.runId === "string"
        ) {
          return {
            sessionKey,
            runId: rawIndex.runId,
          };
        }
        return undefined;
      }

      function clearSessionReservations(sessionKey, sessionHint) {
        const identity = sessionIdentityForCleanup(
          sessionKey,
          sessionHint,
        );
        const runIds = new Set();
        if (identity?.runId) {
          runIds.add(identity.runId);
        }
        for (const [key, reservation] of reservations) {
          if (reservation.sessionKey === sessionKey) {
            runIds.add(reservation.runId);
            if (reservation.timer) {
              clearTimeout(reservation.timer);
              reservation.timer = undefined;
            }
            reservation.reservationToken = undefined;
            reservation.executionToken = undefined;
            reservations.delete(key);
          }
        }
        for (const runId of runIds) {
          const namespaces = processReservationNamespaces(runId);
          const sharedIndex = getSharedContext(
            runId,
            RESERVATION_INDEX_NAMESPACE,
          );
          if (sharedIndex !== undefined) {
            try {
              const index = parseSharedReservationIndex(
                sharedIndex,
                sessionKey,
                runId,
              );
              for (const namespace of index.namespaces) {
                namespaces.add(namespace);
              }
            } catch {
              // The process mirror is authoritative for cleanup when the
              // token-free index is malformed. The session tombstone remains
              // available to fail closed.
            }
          }
          for (const namespace of namespaces) {
            clearSharedContext(runId, namespace);
          }
          clearSharedContext(
            runId,
            RESERVATION_INDEX_NAMESPACE,
          );
        }
      }

      function clearSessionRunIndex(session) {
        if (
          session &&
          typeof session.runId === "string" &&
          sessionsByRunId.get(session.runId) === session.sessionKey
        ) {
          sessionsByRunId.delete(session.runId);
        }
      }

      function expireSession(sessionKey, expectedGeneration) {
        const session =
          sessions.get(sessionKey) ??
          hydrateSharedSessionForKey(sessionKey);
        if (!session || session.generation !== expectedGeneration) {
          return;
        }
        if (Date.now() < session.expiresAt) {
          return;
        }
        session.dispatchToken = undefined;
        session.state = "expired";
        session.timer = undefined;
        clearSessionRunIndex(session);
        clearSessionReservations(sessionKey, session);
        persistSharedTombstone(session, "expired");
        metrics.expirations += 1;
      }

      function scheduleExpiry(session) {
        const delay = Math.max(0, session.expiresAt - Date.now());
        const timer = setTimeout(
          () => expireSession(session.sessionKey, session.generation),
          delay,
        );
        timer.unref?.();
        session.timer = timer;
      }

      function expireReservation(
        key,
        expectedRunId,
        expectedToolCallId,
        expectedToolName,
        expectedExpiresAt,
      ) {
        const reservation = reservations.get(key);
        if (
          !reservation ||
          reservation.runId !== expectedRunId ||
          reservation.toolCallId !== expectedToolCallId ||
          reservation.toolName !== expectedToolName ||
          reservation.expiresAt !== expectedExpiresAt ||
          reservation.state !== "reserved"
        ) {
          return;
        }
        if (Date.now() < reservation.expiresAt) {
          scheduleReservationExpiry(reservation);
          return;
        }
        if (reservation.timer) {
          clearTimeout(reservation.timer);
          reservation.timer = undefined;
        }
        reservation.reservationToken = undefined;
        reservation.executionToken = undefined;
        reservation.state = "failed";
        reservation.failureCode = "expired";
        try {
          persistSharedReservation(reservation);
        } catch {
          // The process tombstone is still authoritative inside this host
          // process. A stale host mirror will disagree and therefore fail
          // closed during the next read.
        }
        metrics.reservation_expirations += 1;
      }

      function scheduleReservationExpiry(reservation) {
        if (
          !reservation ||
          reservation.state !== "reserved" ||
          !Number.isSafeInteger(reservation.expiresAt)
        ) {
          return;
        }
        if (reservation.timer) {
          clearTimeout(reservation.timer);
        }
        const delay = Math.max(0, reservation.expiresAt - Date.now());
        const timer = setTimeout(
          () =>
            expireReservation(
              reservation.key,
              reservation.runId,
              reservation.toolCallId,
              reservation.toolName,
              reservation.expiresAt,
            ),
          delay,
        );
        timer.unref?.();
        reservation.timer = timer;
      }

      function failSession(session, reason) {
        if (session.timer) {
          clearTimeout(session.timer);
          session.timer = undefined;
        }
        session.dispatchToken = undefined;
        session.state = "failed";
        session.failureCode = reason;
        clearSessionRunIndex(session);
        clearSessionReservations(session.sessionKey, session);
        persistSharedTombstone(session, "failed");
      }

      function assertSessionAuthority(session) {
        if (
          session &&
          session.authorityFingerprint !==
            configuredAuthorityFingerprint
        ) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "governed session belongs to another Veyra authority",
          );
        }
        return session;
      }

      function activeSession(sessionKey) {
        if (typeof sessionKey !== "string") {
          return undefined;
        }
        const session = assertSessionAuthority(
          sessions.get(sessionKey),
        );
        if (
          session &&
          session.state === "active" &&
          Date.now() >= session.expiresAt
        ) {
          expireSession(sessionKey, session.generation);
        }
        return rememberSessionBinding(
          assertSessionAuthority(sessions.get(sessionKey)),
        );
      }

      function activeSessionForRun(runId, { hydrate = true } = {}) {
        if (typeof runId !== "string") {
          return undefined;
        }
        if (hydrate) {
          const shared = hydrateSharedSession(runId);
          return shared?.state === "active" ? shared : undefined;
        }
        const sessionKey = sessionsByRunId.get(runId);
        if (typeof sessionKey !== "string") {
          return undefined;
        }
        const session = activeSession(sessionKey);
        if (
          !session ||
          session.state !== "active" ||
          session.runId !== runId ||
          sessionsByRunId.get(runId) !== sessionKey
        ) {
          if (sessionsByRunId.get(runId) === sessionKey) {
            sessionsByRunId.delete(runId);
          }
          return undefined;
        }
        return session;
      }

      function hookRunIds(event, ctx) {
        const runIds = [];
        for (const value of [event?.runId, ctx?.runId]) {
          if (typeof value === "string" && !runIds.includes(value)) {
            runIds.push(value);
          }
        }
        return runIds;
      }

      function knownSessionsForRun(runId) {
        const active = activeSessionForRun(runId);
        if (active) {
          return [active];
        }
        return [...sessions.values()].filter(
          (session) =>
            session.runId === runId && session.state !== "active",
        );
      }

      function hasGovernedSessionMarker() {
        if (sessions.size > 0) {
          return true;
        }
        for (const key of processContextStore.keys()) {
          const parsed = parseProcessContextKey(key);
          if (parsed?.namespace === RUN_CONTEXT_NAMESPACE) {
            return true;
          }
        }
        return false;
      }

      function resolveHookSession(event, ctx) {
        const sessionKey =
          typeof ctx?.sessionKey === "string" ? ctx.sessionKey : undefined;
        const rawRunIds = [event?.runId, ctx?.runId];
        const malformedRunIdentity = rawRunIds.some(
          (value) => value !== undefined && typeof value !== "string",
        );
        const runIds = hookRunIds(event, ctx);
        const sessionsByRun = runIds.flatMap((runId) =>
          knownSessionsForRun(runId),
        );
        let sessionByKey;
        if (typeof sessionKey === "string") {
          sessionByKey =
            activeSession(sessionKey) ??
            hydrateSharedSessionForKey(sessionKey);
        }
        const matchedSessions = [
          ...new Set(
            [sessionByKey, ...sessionsByRun].filter(
              (session) => session !== undefined,
            ),
          ),
        ];

        if (matchedSessions.length === 0) {
          if (
            sessionKey === undefined &&
            runIds.length === 0 &&
            hasGovernedSessionMarker()
          ) {
            throw new GovernanceError(
              "run_binding_mismatch",
              "hook identity is absent while a governed marker is active",
            );
          }
          return { session: undefined, runId: runIds[0] };
        }

        const rejectIdentity = (message) => {
          throw new GovernanceError("run_binding_mismatch", message);
        };

        try {
          requireBoundedString(sessionKey, "session_key");
          if (malformedRunIdentity) {
            throw new GovernanceError(
              "run_binding_mismatch",
              "governed hook run identity is malformed",
            );
          }
          for (const runId of runIds) {
            requireBoundedString(runId, "run_id");
          }
        } catch {
          rejectIdentity("governed hook identity is missing or malformed");
        }
        if (
          !sessionByKey ||
          runIds.length === 0 ||
          runIds.some((runId) => runId !== sessionByKey.runId) ||
          sessionsByRun.length !== runIds.length ||
          sessionsByRun.some(
            (session) => session !== sessionByKey,
          )
        ) {
          rejectIdentity("governed hook session/run identity mismatch");
        }
        return { session: sessionByKey, runId: sessionByKey.runId };
      }

      async function postJson(endpoint, payload, dispatchToken, signal) {
        const body = canonicalJson(payload, config.maxRequestBytes);
        const controller = new AbortController();
        let externalAbort;
        if (signal) {
          if (signal.aborted) {
            controller.abort(signal.reason);
          } else {
            externalAbort = () => controller.abort(signal.reason);
            signal.addEventListener("abort", externalAbort, { once: true });
          }
        }
        const timeout = setTimeout(
          () => controller.abort(new Error("Veyra request timeout")),
          config.requestTimeoutMs,
        );
        timeout.unref?.();
        try {
          const response = await fetchImpl(`${config.baseUrl}${endpoint}`, {
            method: "POST",
            redirect: "error",
            headers: {
              accept: "application/json",
              "content-type": "application/json",
              "x-veyra-governance-protocol": PROTOCOL_VERSION,
              "x-veyra-dispatch-token": dispatchToken,
            },
            body,
            signal: controller.signal,
          });
          const responseText = await readBoundedResponse(
            response,
            config.maxResponseBytes,
          );
          let decoded;
          try {
            decoded = JSON.parse(responseText);
          } catch {
            throw new GovernanceError(
              "invalid_response",
              "Veyra returned invalid JSON",
            );
          }
          if (!response.ok) {
            throw new GovernanceError(
              "governance_rejected",
              safeReason(
                decoded?.detail ?? decoded?.reason,
                "Veyra rejected request",
              ),
            );
          }
          if (!isRecord(decoded)) {
            throw new GovernanceError(
              "invalid_response",
              "Veyra response must be an object",
            );
          }
          return decoded;
        } catch (error) {
          if (error instanceof GovernanceError) {
            throw error;
          }
          throw new GovernanceError(
            "governance_unavailable",
            "Veyra governance is unavailable",
          );
        } finally {
          clearTimeout(timeout);
          if (externalAbort) {
            signal.removeEventListener("abort", externalAbort);
          }
        }
      }

      function bindRun(session, runId) {
        requireBoundedString(runId, "run_id");
        if (
          session.runId !== runId ||
          sessionsByRunId.get(runId) !== session.sessionKey
        ) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "governed session is bound to another run",
          );
        }
      }

      async function reportBlockedTool({
        session,
        runId,
        toolCallId,
        toolName,
        paramsDigest,
        reason,
      }) {
        const response = await postJson(
          "/tool-governance/hook/observe",
          {
            runId,
            toolCallId,
            toolName,
            paramsDigest,
            outcome: "blocked",
            durationMs: 0,
            reason: safeReason(reason, "plugin policy blocked tool"),
          },
          session.dispatchToken,
        );
        if (
          response.status !== "recorded" ||
          response.authoritative !== false
        ) {
          throw new GovernanceError(
            "invalid_response",
            "Veyra blocked observation response is invalid",
          );
        }
        metrics.blocked_observations += 1;
        metrics.observations += 1;
      }

      async function beforeToolCall(event, ctx) {
        const toolName =
          typeof event?.toolName === "string"
            ? event.toolName
            : typeof ctx?.toolName === "string"
              ? ctx.toolName
              : "";
        let resolved;
        try {
          resolved = resolveHookSession(event, ctx);
        } catch (error) {
          metrics.malformed_blocks += 1;
          return {
            block: true,
            blockReason: `Veyra governance blocked mismatched hook identity (${error.code ?? "invalid"})`,
          };
        }
        const { session, runId } = resolved;
        if (!session) {
          if (CUSTOM_TOOLS.has(toolName)) {
            metrics.unregistered_custom_blocks += 1;
            return {
              block: true,
              blockReason: "Veyra tool requires a registered governed session",
            };
          }
          return undefined;
        }
        if (session.state !== "active" || !session.dispatchToken) {
          metrics.malformed_blocks += 1;
          return {
            block: true,
            blockReason: `Veyra governance is fail-closed (${session.state})`,
          };
        }
        const toolCallId = event?.toolCallId ?? ctx?.toolCallId;
        try {
          requireBoundedString(toolCallId, "tool_call_id");
          bindRun(session, runId);
        } catch (error) {
          metrics.malformed_blocks += 1;
          return {
            block: true,
            blockReason: `Veyra governance blocked malformed call (${error.code ?? "invalid"})`,
          };
        }

        if (!session.allowedTools.includes(toolName)) {
          const reason = NATIVE_SIDE_EFFECT_TOOLS.has(toolName)
            ? "Native OpenClaw tool is disabled for this Veyra-governed run"
            : CUSTOM_TOOLS.has(toolName)
              ? "Tool is not allowed for this Veyra-governed run"
              : "Unknown tool is disabled for this Veyra-governed run";
          try {
            const paramsDigest = exactDigest(
              event.params,
              config.maxRequestBytes,
            ).digest;
            await reportBlockedTool({
              session,
              runId,
              toolCallId,
              toolName,
              paramsDigest,
              reason,
            });
          } catch (error) {
            metrics.observation_failures += 1;
            failSession(session, error.code ?? "blocked_observation_failed");
            return {
              block: true,
              blockReason: `Veyra governance blocked tool and failed closed (${error.code ?? "observation_failed"})`,
            };
          }
          metrics.allowlist_blocks += 1;
          if (NATIVE_SIDE_EFFECT_TOOLS.has(toolName)) {
            metrics.native_blocks += 1;
            return {
              block: true,
              blockReason: reason,
            };
          }
          if (CUSTOM_TOOLS.has(toolName)) {
            metrics.disallowed_custom_blocks += 1;
            return {
              block: true,
              blockReason: reason,
            };
          }
          metrics.unknown_blocks += 1;
          return {
            block: true,
            blockReason: reason,
          };
        }
        if (!CUSTOM_TOOLS.has(toolName)) {
          metrics.malformed_blocks += 1;
          return {
            block: true,
            blockReason:
              "Veyra governance rejected an invalid tool allowlist",
          };
        }

        const key = reservationKey(runId, toolCallId);
        if (
          reservations.has(key) ||
          getSharedContext(
            runId,
            reservationNamespace(
              session.sessionKey,
              toolCallId,
              toolName,
            ),
          ) !== undefined
        ) {
          metrics.malformed_blocks += 1;
          return {
            block: true,
            blockReason: "Duplicate Veyra tool call was rejected",
          };
        }

        let paramsDigest;
        try {
          paramsDigest = exactDigest(
            event.params,
            config.maxRequestBytes,
          ).digest;
        } catch (error) {
          metrics.malformed_blocks += 1;
          return {
            block: true,
            blockReason: `Veyra tool arguments were rejected (${error.code ?? "invalid_json"})`,
          };
        }

        const expectedGeneration = session.generation;
        const expectedDispatchToken = session.dispatchToken;
        let response;
        try {
          response = await postJson(
            "/tool-governance/hook/preflight",
            {
              runId,
              sessionKey: session.sessionKey,
              toolCallId,
              toolName,
              params: event.params,
            },
            session.dispatchToken,
          );
        } catch (error) {
          metrics.preflight_failures += 1;
          failSession(session, error.code ?? "preflight_failed");
          return {
            block: true,
            blockReason: `Veyra governance preflight failed (${error.code ?? "failed"})`,
          };
        }

        if (
          sessions.get(session.sessionKey) !== session ||
          session.generation !== expectedGeneration ||
          session.state !== "active" ||
          session.dispatchToken !== expectedDispatchToken ||
          sessionsByRunId.get(runId) !== session.sessionKey ||
          Date.now() >= session.expiresAt
        ) {
          metrics.preflight_failures += 1;
          return {
            block: true,
            blockReason:
              "Veyra governance session changed during preflight",
          };
        }

        if (response.allow === false) {
          metrics.preflight_denials += 1;
          return {
            block: true,
            blockReason: safeReason(
              response.reason,
              "Veyra denied the tool invocation",
            ),
          };
        }
        try {
          if (response.allow !== true) {
            throw new GovernanceError(
              "invalid_response",
              "preflight allow flag is missing",
            );
          }
          const executionToken = requireBoundedString(
            response.executionToken,
            "execution_token",
            TOKEN_MAX_BYTES,
          );
          const reservationId = requireBoundedString(
            response.reservationId,
            "reservation_id",
          );
          const reservationToken = requireBoundedString(
            response.reservationToken,
            "reservation_token",
            TOKEN_MAX_BYTES,
          );
          const canonicalToolName = requireBoundedString(
            response.canonicalToolName,
            "canonical_tool_name",
          );
          if (canonicalToolName !== CANONICAL_TOOL_NAMES[toolName]) {
            throw new GovernanceError(
              "invalid_response",
              "canonical tool name does not match the host tool",
            );
          }
          const invocationDigest = requireSha256(
            response.invocationDigest,
            "invocation_digest",
          );
          const responseExpiry = parseExpiry(
            response.expiresAt,
            "expires_at",
          );
          if (
            responseExpiry <= Date.now() ||
            responseExpiry > session.expiresAt
          ) {
            throw new GovernanceError(
              "invalid_response",
              "preflight expiry is invalid",
            );
          }
          const reservation = {
            key,
            sessionKey: session.sessionKey,
            runId,
            toolCallId,
            toolName,
            paramsDigest,
            reservationId,
            reservationToken,
            executionToken,
            canonicalToolName,
            invocationDigest,
            expiresAt: responseExpiry,
            state: "reserved",
            timer: undefined,
          };
          persistSharedReservation(reservation);
          reservations.set(key, reservation);
          scheduleReservationExpiry(reservation);
        } catch (error) {
          metrics.preflight_failures += 1;
          failSession(session, error.code ?? "invalid_response");
          return {
            block: true,
            blockReason: `Veyra governance preflight failed (${error.code ?? "invalid_response"})`,
          };
        }
        metrics.preflight_allows += 1;
        return undefined;
      }

      function findReservation(sessionKey, toolCallId, toolName) {
        const matches = [];
        for (const reservation of reservations.values()) {
          if (
            reservation.sessionKey === sessionKey &&
            reservation.toolCallId === toolCallId &&
            reservation.toolName === toolName
          ) {
            matches.push(reservation);
          }
        }
        if (matches.length > 1) {
          throw new GovernanceError(
            "reservation_missing",
            "exact Veyra reservation is unavailable",
          );
        }
        // before_tool_call, the custom-tool factory, and after_tool_call can
        // run in separate OpenClaw registries. Always rehydrate the shared
        // state so a stale local "reserved" object cannot hide the tool
        // factory's authoritative consumed/executed transition.
        return hydrateSharedReservation(
          sessionKey,
          toolCallId,
          toolName,
        ).reservation;
      }

      async function executeGovernedTool({
        sessionKey,
        toolName,
        toolCallId,
        params,
        signal,
      }) {
        requireBoundedString(toolCallId, "tool_call_id");
        const governedSession =
          hydrateSharedSessionForKey(sessionKey) ??
          activeSession(sessionKey);
        if (
          !governedSession ||
          governedSession.state !== "active" ||
          !governedSession.dispatchToken
        ) {
          throw new GovernanceError(
            "session_unavailable",
            "Veyra governed session is unavailable",
          );
        }
        if (!governedSession.allowedTools.includes(toolName)) {
          throw new GovernanceError(
            "tool_not_allowed",
            "tool is not allowed for this Veyra-governed run",
          );
        }
        const hydrated = hydrateSharedReservation(
          sessionKey,
          toolCallId,
          toolName,
        );
        const { session, reservation } = hydrated;
        if (
          reservation.state === "reserved" &&
          Date.now() >= reservation.expiresAt
        ) {
          expireReservation(
            reservation.key,
            reservation.runId,
            reservation.toolCallId,
            reservation.toolName,
            reservation.expiresAt,
          );
        }
        if (reservation.state !== "reserved") {
          throw new GovernanceError(
            "execution_token_unavailable",
            "Veyra execution token is unavailable",
          );
        }
        const paramsDigest = exactDigest(params, config.maxRequestBytes).digest;
        if (paramsDigest !== reservation.paramsDigest) {
          failSession(session, "params_digest_mismatch");
          throw new GovernanceError(
            "params_digest_mismatch",
            "tool arguments changed after Veyra preflight",
          );
        }

        // Consume locally before crossing the network. An ambiguous network
        // failure cannot be retried with the same bearer credential.
        const executionToken = reservation.executionToken;
        const reservationToken = reservation.reservationToken;
        if (reservation.timer) {
          clearTimeout(reservation.timer);
          reservation.timer = undefined;
        }
        reservation.state = "consumed";
        reservation.executionToken = undefined;
        reservation.reservationToken = undefined;
        persistSharedReservation(reservation);
        let response;
        try {
          response = await postJson(
            "/tool-governance/hook/execute",
            {
              runId: reservation.runId,
              toolCallId: reservation.toolCallId,
              toolName: reservation.toolName,
              params,
              reservationToken,
              executionToken,
            },
            session.dispatchToken,
            signal,
          );
          if (
            typeof response.status !== "string" ||
            response.status.length === 0 ||
            !Object.hasOwn(response, "result")
          ) {
            throw new GovernanceError(
              "invalid_response",
              "Veyra execution response is invalid",
            );
          }
          const resultDigest = requireSha256(
            response.resultDigest,
            "result_digest",
          );
          if (!isRecord(response.receiptRef)) {
            throw new GovernanceError(
              "invalid_response",
              "receipt_ref is invalid",
            );
          }
          requireBoundedString(
            response.receiptRef.run_id,
            "receipt_ref.run_id",
          );
          requireBoundedString(
            response.receiptRef.tool_call_id,
            "receipt_ref.tool_call_id",
          );
          requireSha256(
            response.receiptRef.invocation_digest,
            "receipt_ref.invocation_digest",
          );
          if (
            response.receiptRef.run_id !== reservation.runId ||
            response.receiptRef.tool_call_id !== reservation.toolCallId ||
            response.receiptRef.invocation_digest !==
              reservation.invocationDigest
          ) {
            throw new GovernanceError(
              "invalid_response",
              "receipt_ref does not match the reserved invocation",
            );
          }
          if (
            response.effectEvidenceDigest !== null &&
            response.effectEvidenceDigest !== undefined
          ) {
            requireSha256(
              response.effectEvidenceDigest,
              "effect_evidence_digest",
            );
          }
          const rendered = publicToolResult(
            response.result,
            config.maxToolResultBytes,
          );
          reservation.state = "executed";
          reservation.receiptRef = response.receiptRef;
          reservation.resultDigest = resultDigest;
          persistSharedReservation(reservation);
          metrics.executions += 1;
          return {
            content: [{ type: "text", text: rendered.text }],
            details: {
              veyra_governed: true,
              outcome:
                response.status === "ok"
                  ? "server_reported_success"
                  : "server_reported_failure",
              status: response.status,
              receipt_ref: response.receiptRef,
              result_digest: resultDigest,
              effect_evidence_digest:
                response.effectEvidenceDigest ?? null,
              result_truncated: rendered.truncated,
            },
          };
        } catch (error) {
          reservation.state = "failed";
          try {
            persistSharedReservation(reservation);
          } catch {
            // The server-side one-use claim remains authoritative.
          }
          metrics.execution_failures += 1;
          failSession(session, error.code ?? "execution_failed");
          throw error instanceof GovernanceError
            ? error
            : new GovernanceError(
                "execution_failed",
                "Veyra tool execution failed",
              );
        }
      }

      async function afterToolCall(event, ctx) {
        let resolved;
        try {
          resolved = resolveHookSession(event, ctx);
        } catch (error) {
          metrics.observation_failures += 1;
          throw error instanceof GovernanceError
            ? error
            : new GovernanceError(
                "run_binding_mismatch",
                "Veyra after-tool hook identity mismatch",
              );
        }
        const { session, runId } = resolved;
        if (!session) {
          return;
        }
        const toolName =
          typeof event?.toolName === "string"
            ? event.toolName
            : typeof ctx?.toolName === "string"
              ? ctx.toolName
              : "";
        if (!CUSTOM_TOOLS.has(toolName)) {
          return;
        }
        const toolCallId = event?.toolCallId ?? ctx?.toolCallId;
        try {
          requireBoundedString(toolCallId, "tool_call_id");
          bindRun(session, runId);
          if (session.state !== "active" || !session.dispatchToken) {
            throw new GovernanceError(
              "session_unavailable",
              "governed session is unavailable",
            );
          }
          const reservation = findReservation(
            session.sessionKey,
            toolCallId,
            toolName,
          );
          if (
            !reservation ||
            reservation.sessionKey !== session.sessionKey ||
            reservation.toolName !== toolName ||
            !["executed", "failed"].includes(reservation.state)
          ) {
            throw new GovernanceError(
              "reservation_missing",
              "exact Veyra reservation is unavailable",
            );
          }
          const resultDigest = boundedDigest(
            event.error === undefined
              ? { result: event.result }
              : { error: event.error },
          );
          const paramsDigest = exactDigest(
            event.params,
            config.maxRequestBytes,
          ).digest;
          if (paramsDigest !== reservation.paramsDigest) {
            throw new GovernanceError(
              "params_digest_mismatch",
              "after-tool arguments do not match preflight",
            );
          }
          const response = await postJson(
            "/tool-governance/hook/observe",
            {
              runId,
              toolCallId,
              toolName,
              paramsDigest,
              outcome: event.error === undefined ? "completed" : "error",
              resultDigest: resultDigest.digest,
              durationMs:
                Number.isSafeInteger(event.durationMs) &&
                event.durationMs >= 0 &&
                event.durationMs <= 86400000
                  ? event.durationMs
                  : 0,
              reason:
                event.error === undefined
                  ? ""
                  : safeReason(event.error, "OpenClaw tool error"),
            },
            session.dispatchToken,
          );
          if (
            response.status !== "recorded" ||
            response.authoritative !== false
          ) {
            throw new GovernanceError(
              "invalid_response",
              "Veyra observation response is invalid",
            );
          }
          clearSharedReservation(session, toolCallId, toolName);
          metrics.observations += 1;
        } catch (error) {
          if (
            error instanceof GovernanceError &&
            error.code === "reservation_missing" &&
            typeof event?.error === "string" &&
            event.error.length > 0
          ) {
            // A host-blocked custom call never reached the tool factory, so
            // no execution reservation exists. The preflight denial is
            // already authoritative in Veyra; keep the session usable for
            // subsequent governed calls.
            return;
          }
          metrics.observation_failures += 1;
          failSession(session, error.code ?? "observation_failed");
          throw error instanceof GovernanceError
            ? error
            : new GovernanceError(
                "observation_failed",
                "Veyra observation failed",
              );
        }
      }

      function registerSession(params) {
        if (!isRecord(params)) {
          throw new GovernanceError(
            "invalid_registration",
            "registration payload must be an object",
          );
        }
        const sessionKey = requireBoundedString(
          params.session_key ?? params.sessionKey,
          "session_key",
        );
        const dispatchToken = requireBoundedString(
          params.dispatch_token ?? params.dispatchToken,
          "dispatch_token",
          TOKEN_MAX_BYTES,
        );
        const runId = requireBoundedString(
          params.run_id ?? params.runId,
          "run_id",
        );
        const bindingDigest = requireSha256(
          params.binding_digest ?? params.bindingDigest,
          "binding_digest",
        );
        const allowedTools = params.allowed_tools ?? params.allowedTools;
        if (
          !Array.isArray(allowedTools) ||
          new Set(allowedTools).size !== allowedTools.length ||
          allowedTools.some(
            (toolName) =>
              typeof toolName !== "string" || !CUSTOM_TOOLS.has(toolName),
          )
        ) {
          throw new GovernanceError(
            "invalid_registration",
            "allowed_tools must be a unique subset of the Veyra plugin tools",
          );
        }
        const normalizedAllowedTools = [...allowedTools].sort();
        const expiresAt = parseExpiry(
          params.expires_at ?? params.expiresAt,
          "expires_at",
        );
        const now = Date.now();
        if (
          expiresAt <= now ||
          expiresAt > now + SESSION_TTL_MAX_MS
        ) {
          throw new GovernanceError(
            "invalid_registration",
            "expires_at must be a near-future epoch millisecond",
          );
        }
        const indexedSessionKey = sessionsByRunId.get(runId);
        if (typeof indexedSessionKey === "string") {
          activeSession(indexedSessionKey);
        }
        const activeIndexedSessionKey = sessionsByRunId.get(runId);
        if (
          typeof activeIndexedSessionKey === "string" &&
          activeIndexedSessionKey !== sessionKey
        ) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "run_id is already bound to another active governed session",
          );
        }
        const sharedForRun = getSharedContext(
          runId,
          RUN_CONTEXT_NAMESPACE,
        );
        let sharedSessionForRun;
        if (sharedForRun !== undefined) {
          sharedSessionForRun = parseSharedSession(
            sharedForRun,
            runId,
          );
          if (
            sharedSessionForRun.state === "active" &&
            sharedSessionForRun.sessionKey !== sessionKey
          ) {
            throw new GovernanceError(
              "run_binding_mismatch",
              "run_id is already bound in OpenClaw run context",
            );
          }
          if (
            sharedSessionForRun.state !== "active" &&
            sharedSessionForRun.sessionKey === sessionKey
          ) {
            throw new GovernanceError(
              "session_retired",
              `governed session cannot be re-registered from state ${sharedSessionForRun.state}`,
            );
          }
        }
        const sharedIndex = getSharedContext(
          sessionIndexRunId(sessionKey),
          SESSION_INDEX_NAMESPACE,
        );
        let existing = sessions.get(sessionKey);
        if (
          existing &&
          existing.authorityFingerprint !==
            configuredAuthorityFingerprint
        ) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "governed session belongs to another Veyra authority",
          );
        }
        if (sharedIndex !== undefined) {
          existing = hydrateSharedSessionForKey(sessionKey);
        }
        if (
          !existing &&
          sharedSessionForRun?.sessionKey === sessionKey
        ) {
          existing = hydrateSharedSession(runId);
        }
        if (
          existing &&
          existing.runId === runId &&
          existing.state !== "active"
        ) {
          throw new GovernanceError(
            "session_retired",
            `governed session cannot be re-registered from state ${existing.state}`,
          );
        }
        if (existing?.state === "active" && existing.runId === runId) {
          const exactAllowedTools =
            existing.allowedTools.length ===
              normalizedAllowedTools.length &&
            existing.allowedTools.every(
              (toolName, index) =>
                toolName === normalizedAllowedTools[index],
            );
          if (
            existing.sessionKey !== sessionKey ||
            existing.bindingDigest !== bindingDigest ||
            existing.authorityFingerprint !==
              configuredAuthorityFingerprint ||
            !exactAllowedTools ||
            existing.dispatchToken !== dispatchToken ||
            existing.expiresAt !== expiresAt
          ) {
            throw new GovernanceError(
              "run_binding_mismatch",
              "active governed session registration does not exactly match its binding",
            );
          }
          return {
            status: "registered",
            registered: true,
            idempotent: true,
            sessionKey,
            runId,
            bindingDigest,
            expiresAt: new Date(expiresAt).toISOString(),
            runBound: true,
          };
        }
        if (existing?.state === "active" && existing.runId !== runId) {
          if (existing.timer) {
            clearTimeout(existing.timer);
            existing.timer = undefined;
          }
          existing.dispatchToken = undefined;
          existing.state = "cancelled";
          clearSessionRunIndex(existing);
          clearSessionReservations(sessionKey, existing);
          persistSharedTombstone(existing, "cancelled");
        }
        const session = {
          sessionKey,
          dispatchToken,
          expiresAt,
          runId,
          bindingDigest,
          authorityFingerprint: configuredAuthorityFingerprint,
          allowedTools: normalizedAllowedTools,
          generation: (existing?.generation ?? 0) + 1,
          state: "active",
          timer: undefined,
        };
        persistSharedSession(session);
        if (existing?.timer) {
          clearTimeout(existing.timer);
        }
        if (existing) {
          metrics.replacements += 1;
        }
        clearSessionRunIndex(existing);
        clearSessionReservations(sessionKey, existing);
        sessions.set(sessionKey, session);
        sessionsByRunId.set(runId, sessionKey);
        rememberSessionBinding(session);
        scheduleExpiry(session);
        metrics.registrations += 1;
        return {
          status: "registered",
          registered: true,
          idempotent: false,
          sessionKey,
          runId,
          bindingDigest,
          expiresAt: new Date(expiresAt).toISOString(),
          runBound: true,
        };
      }

      function cancelSession(params) {
        if (!isRecord(params)) {
          throw new GovernanceError(
            "invalid_cancellation",
            "cancellation payload must be an object",
          );
        }
        const sessionKey = requireBoundedString(
          params.session_key ?? params.sessionKey,
          "session_key",
        );
        const runId = requireBoundedString(
          params.run_id ?? params.runId,
          "run_id",
        );
        const bindingDigest =
          params.binding_digest ?? params.bindingDigest;
        if (
          typeof bindingDigest !== "string" ||
          !/^[0-9a-f]{64}$/.test(bindingDigest)
        ) {
          throw new GovernanceError(
            "invalid_cancellation",
            "binding_digest is invalid",
          );
        }
        const session =
          sessions.get(sessionKey) ??
          hydrateSharedSessionForKey(sessionKey);
        if (!session) {
          return {
            cancelled: false,
            idempotent: true,
            sessionKey,
            runId,
          };
        }
        if (
          session.runId !== runId ||
          session.bindingDigest !== bindingDigest
        ) {
          throw new GovernanceError(
            "run_binding_mismatch",
            "cancellation identity does not match the current governed session",
          );
        }
        if (session.state === "cancelled") {
          return {
            cancelled: true,
            idempotent: true,
            sessionKey,
            runId,
          };
        }
        if (
          session.state !== "active" ||
          sessionsByRunId.get(runId) !== sessionKey
        ) {
          throw new GovernanceError(
            "session_unavailable",
            `governed session cannot be cancelled from state ${session.state}`,
          );
        }
        if (session.timer) {
          clearTimeout(session.timer);
        }
        session.dispatchToken = undefined;
        session.timer = undefined;
        session.state = "cancelled";
        clearSessionRunIndex(session);
        clearSessionReservations(sessionKey, session);
        persistSharedTombstone(session, "cancelled");
        metrics.cancellations += 1;
        return {
          cancelled: true,
          idempotent: false,
          sessionKey,
          runId,
        };
      }

      async function attestCanary(params) {
        if (!isRecord(params)) {
          throw new GovernanceError(
            "invalid_attestation",
            "canary attestation payload must be an object",
          );
        }
        const sessionKey = requireBoundedString(
          params.session_key ?? params.sessionKey,
          "session_key",
        );
        const sentinelRelativePath = requireBoundedString(
          params.sentinel_relative_path ?? params.sentinelRelativePath,
          "sentinel_relative_path",
          4096,
        );
        const nativeBlockPath = requireBoundedString(
          params.native_block_path ?? params.nativeBlockPath,
          "native_block_path",
          4096,
        );
        const nativeBlockContent =
          params.native_block_content ?? params.nativeBlockContent;
        if (
          typeof nativeBlockContent !== "string" ||
          nativeBlockContent.length === 0 ||
          nativeBlockContent.includes("\0") ||
          byteLength(nativeBlockContent) > 1024
        ) {
          throw new GovernanceError(
            "invalid_attestation",
            "native_block_content must be bounded UTF-8 text",
          );
        }
        const session = activeSession(sessionKey);
        if (
          !session ||
          session.state !== "active" ||
          !session.dispatchToken ||
          !session.runId
        ) {
          throw new GovernanceError(
            "session_unavailable",
            "Veyra governed session is unavailable for canary attestation",
          );
        }
        bindRun(session, session.runId);
        const response = await postJson(
          "/tool-governance/hook/canary/attest",
          {
            runId: session.runId,
            sessionKey,
            sentinelRelativePath,
            nativeBlockPath,
            nativeBlockContent,
            pluginProtocol: PROTOCOL_VERSION,
            pluginImplementationRevision: IMPLEMENTATION_REVISION,
          },
          session.dispatchToken,
        );
        if (
          response.status !== "validated" ||
          !isRecord(response.implementation) ||
          response.implementation.plugin_protocol !== PROTOCOL_VERSION ||
          response.implementation.plugin_implementation_revision !==
            IMPLEMENTATION_REVISION
        ) {
          throw new GovernanceError(
            "invalid_response",
            "Veyra canary attestation response is invalid",
          );
        }
        const validatedAt =
          typeof response.validated_at === "string"
            ? safeReason(response.validated_at, "")
            : "";
        return {
          status: "validated",
          runId: session.runId,
          sessionKey,
          ...(validatedAt ? { validatedAt } : {}),
        };
      }

      function status() {
        let activeSessions = 0;
        let failedSessions = 0;
        let expiredSessions = 0;
        for (const session of sessions.values()) {
          activeSession(session.sessionKey);
          if (session.state === "active") {
            activeSessions += 1;
          } else if (session.state === "failed") {
            failedSessions += 1;
          } else if (session.state === "expired") {
            expiredSessions += 1;
          }
        }
        let pendingReservations = 0;
        for (const reservation of reservations.values()) {
          if (
            reservation.state === "reserved" &&
            Date.now() >= reservation.expiresAt
          ) {
            expireReservation(
              reservation.key,
              reservation.runId,
              reservation.toolCallId,
              reservation.toolName,
              reservation.expiresAt,
            );
          }
          if (reservation.state === "reserved") {
            pendingReservations += 1;
          }
        }
        return {
          status: "active",
          plugin_id: PLUGIN_ID,
          protocol_version: PROTOCOL_VERSION,
          implementation_revision: IMPLEMENTATION_REVISION,
          enforcement_scope: "registered_veyra_sessions_only",
          native_tools_blocked_for_governed_runs: true,
          custom_tools: [...CUSTOM_TOOLS].sort(),
          active_sessions: activeSessions,
          failed_sessions: failedSessions,
          expired_sessions: expiredSessions,
          pending_reservations: pendingReservations,
          metrics: { ...metrics },
        };
      }

      function eraseProcessContextsForRun(runId) {
        for (const key of [...processContextStore.keys()]) {
          const parsed = parseProcessContextKey(key);
          if (parsed?.runId === runId) {
            processContextStore.delete(key);
          }
        }
      }

      function terminalCleanupRun(runId, eventContext) {
        if (typeof runId !== "string" || runId.length === 0) {
          return;
        }
        const matchingSessions = [
          ...sessions.values(),
        ].filter((session) => session.runId === runId);
        let rawSharedSession = processContextStore.get(
          processContextKey(runId, RUN_CONTEXT_NAMESPACE),
        );
        if (
          rawSharedSession === undefined &&
          typeof getRunContext === "function"
        ) {
          try {
            rawSharedSession = getRunContext({
              runId,
              namespace: RUN_CONTEXT_NAMESPACE,
            });
          } catch {
            rawSharedSession = undefined;
          }
        }
        if (
          matchingSessions.length === 0 &&
          !(
            isRecord(rawSharedSession) &&
            rawSharedSession.runId === runId &&
            typeof rawSharedSession.sessionKey === "string"
          )
        ) {
          return;
        }
        const sessionKeys = new Set(
          matchingSessions.map((session) => session.sessionKey),
        );
        if (
          isRecord(rawSharedSession) &&
          rawSharedSession.runId === runId &&
          typeof rawSharedSession.sessionKey === "string"
        ) {
          sessionKeys.add(rawSharedSession.sessionKey);
        }
        for (const session of matchingSessions) {
          if (session.timer) {
            clearTimeout(session.timer);
            session.timer = undefined;
          }
          session.dispatchToken = undefined;
          clearSessionReservations(session.sessionKey, session);
          if (sessions.get(session.sessionKey) === session) {
            sessions.delete(session.sessionKey);
          }
          observedSessionBindings.delete(session.sessionKey);
        }
        for (const [key, reservation] of reservations) {
          if (
            reservation.runId === runId ||
            sessionKeys.has(reservation.sessionKey)
          ) {
            if (reservation.timer) {
              clearTimeout(reservation.timer);
              reservation.timer = undefined;
            }
            reservation.reservationToken = undefined;
            reservation.executionToken = undefined;
            reservations.delete(key);
          }
        }
        if (sessionsByRunId.get(runId) !== undefined) {
          sessionsByRunId.delete(runId);
        }
        for (const sessionKey of sessionKeys) {
          clearSharedContext(
            sessionIndexRunId(sessionKey),
            SESSION_INDEX_NAMESPACE,
          );
        }
        clearSharedContext(runId, RUN_CONTEXT_NAMESPACE);
        clearSharedContext(
          runId,
          RESERVATION_INDEX_NAMESPACE,
        );
        eraseProcessContextsForRun(runId);
        try {
          eventContext?.clearRunContext?.();
        } catch {
          // The host clears all run context after terminal handlers settle.
        }
      }

      function retireSession(session, reason) {
        if (!session || typeof session.sessionKey !== "string") {
          return;
        }
        if (session.timer) {
          clearTimeout(session.timer);
          session.timer = undefined;
        }
        session.dispatchToken = undefined;
        if (session.state === "active") {
          session.state = "failed";
          session.failureCode = `plugin_lifecycle_${reason}`;
        }
        clearSessionRunIndex(session);
        clearSessionReservations(session.sessionKey, session);
        persistSharedTombstone(session, session.state);
      }

      function runtimeLifecycleCleanup(context = {}) {
        const reason = [
          "disable",
          "reset",
          "delete",
          "restart",
        ].includes(context?.reason)
          ? context.reason
          : "reset";
        const targets = new Set();
        if (typeof context?.runId === "string") {
          for (const session of sessions.values()) {
            if (session.runId === context.runId) {
              targets.add(session);
            }
          }
        } else if (typeof context?.sessionKey === "string") {
          const session = sessions.get(context.sessionKey);
          if (session) {
            targets.add(session);
          }
        } else if (reason === "restart") {
          for (const [sessionKey, binding] of observedSessionBindings) {
            const session = sessions.get(sessionKey);
            if (
              session &&
              session.runId === binding.runId &&
              session.generation === binding.generation
            ) {
              targets.add(session);
            }
          }
        } else {
          for (const session of sessions.values()) {
            targets.add(session);
          }
        }
        for (const session of targets) {
          retireSession(session, reason);
          observedSessionBindings.delete(session.sessionKey);
        }
        const scoped =
          typeof context?.runId === "string" ||
          typeof context?.sessionKey === "string";
        if (!scoped && ["disable", "restart"].includes(reason)) {
          observedSessionBindings.clear();
        }
      }

      api.on("before_tool_call", beforeToolCall, {
        priority: 1000,
        timeoutMs: config.requestTimeoutMs + 1000,
      });
      api.on("after_tool_call", afterToolCall, {
        priority: 1000,
        timeoutMs: config.requestTimeoutMs + 1000,
      });

      api.registerTool(
        (ctx) => [
          createToolDefinition({
            name: "veyra_file_read",
            label: "Veyra File Read",
            description:
              "Read one sandboxed UTF-8 file through Veyra governance.",
            parameters: FILE_READ_SCHEMA,
            sessionKey: ctx.sessionKey,
            executeGovernedTool,
          }),
          createToolDefinition({
            name: "veyra_file_write",
            label: "Veyra File Write",
            description:
              "Write one sandboxed UTF-8 file through Veyra governance.",
            parameters: FILE_WRITE_SCHEMA,
            sessionKey: ctx.sessionKey,
            executeGovernedTool,
          }),
          createToolDefinition({
            name: "veyra_shell_probe",
            label: "Veyra Shell Probe",
            description:
              "Run one bounded, side-effect-free R0 argv probe through Veyra governance.",
            parameters: SHELL_PROBE_SCHEMA,
            sessionKey: ctx.sessionKey,
            executeGovernedTool,
          }),
        ],
        {
          names: [...CUSTOM_TOOLS],
          optional: false,
        },
      );

      api.registerGatewayMethod(
        "veyra.governance.registerSession",
        async ({ params, respond }) => {
          try {
            respond(true, registerSession(params));
          } catch (error) {
            respond(false, undefined, {
              code: error.code ?? "invalid_registration",
              message: safeReason(error.message, "session registration failed"),
            });
          }
        },
        { scope: "operator.write" },
      );
      api.registerGatewayMethod(
        "veyra.governance.attestCanary",
        async ({ params, respond }) => {
          try {
            respond(true, await attestCanary(params));
          } catch (error) {
            respond(false, undefined, {
              code: error.code ?? "invalid_attestation",
              message: safeReason(error.message, "canary attestation failed"),
            });
          }
        },
        { scope: "operator.write" },
      );
      api.registerGatewayMethod(
        "veyra.governance.status",
        async ({ respond }) => {
          respond(true, status());
        },
        { scope: "operator.read" },
      );
      api.registerGatewayMethod(
        "veyra.governance.cancelSession",
        async ({ params, respond }) => {
          try {
            respond(true, cancelSession(params));
          } catch (error) {
            respond(false, undefined, {
              code: error.code ?? "invalid_cancellation",
              message: safeReason(error.message, "session cancellation failed"),
            });
          }
        },
        { scope: "operator.write" },
      );

      const registerAgentEventSubscription =
        api.agent?.events?.registerAgentEventSubscription?.bind(
          api.agent.events,
        ) ??
        api.registerAgentEventSubscription?.bind(api);
      if (typeof registerAgentEventSubscription !== "function") {
        throw new GovernanceError(
          "invalid_runtime",
          "OpenClaw agent lifecycle subscriptions are required",
        );
      }
      registerAgentEventSubscription({
        id: "veyra-terminal-cleanup-v1",
        description:
          "Erase process-local Veyra bearer credentials at exact run terminal state.",
        streams: ["lifecycle"],
        handle(event, eventContext) {
          const phase = event?.data?.phase;
          if (
            event?.stream === "lifecycle" &&
            (phase === "end" || phase === "error")
          ) {
            terminalCleanupRun(event.runId, eventContext);
          }
        },
      });

      const lifecycle = {
        id: PLUGIN_ID,
        description:
          "Fail closed governed sessions owned by a retiring plugin registry.",
        cleanup: runtimeLifecycleCleanup,
      };
      if (api.lifecycle?.registerRuntimeLifecycle) {
        api.lifecycle.registerRuntimeLifecycle(lifecycle);
      } else if (api.registerRuntimeLifecycle) {
        api.registerRuntimeLifecycle(lifecycle);
      }
    },
  };
}

export { boundedDigest, boundedSnapshot, canonicalJson };

export default createVeyraGovernancePlugin();
