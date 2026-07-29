import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import http from "node:http";
import test from "node:test";

import {
  boundedDigest,
  createVeyraGovernancePlugin,
} from "../index.js";

const BINDING_DIGEST = "b".repeat(64);
const INVOCATION_DIGEST = "a".repeat(64);
const RESULT_DIGEST = "c".repeat(64);
const EFFECT_DIGEST = "d".repeat(64);
const GOVERNED_TOOLS = [
  "veyra_file_read",
  "veyra_file_write",
  "veyra_shell_probe",
];

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function jsonResponse(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

async function readJson(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function createMockVeyra(handler) {
  const requests = [];
  const server = http.createServer(async (request, response) => {
    try {
      const body = await readJson(request);
      const record = {
        path: request.url,
        headers: request.headers,
        body,
      };
      requests.push(record);
      await handler(record, response);
    } catch (error) {
      jsonResponse(response, 500, { detail: String(error) });
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    requests,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

function createFakeApi(pluginConfig, sharedRuntime = {}) {
  const hooks = new Map();
  const gateway = new Map();
  const gatewayScopes = new Map();
  const toolFactories = [];
  const lifecycles = [];
  const agentEventSubscriptions = [];
  const runContexts = sharedRuntime.runContexts ?? new Map();
  sharedRuntime.runContexts = runContexts;
  const runContextKey = (runId, namespace) =>
    `${runId.length}:${runId}${namespace.length}:${namespace}`;
  const api = {
    pluginConfig,
    on(name, handler) {
      hooks.set(name, handler);
    },
    registerTool(toolOrFactory) {
      toolFactories.push(toolOrFactory);
    },
    registerGatewayMethod(name, handler, options) {
      gateway.set(name, handler);
      gatewayScopes.set(name, options?.scope);
    },
    lifecycle: {
      registerRuntimeLifecycle(lifecycle) {
        lifecycles.push(lifecycle);
      },
    },
    agent: {
      events: {
        registerAgentEventSubscription(subscription) {
          agentEventSubscriptions.push(subscription);
        },
      },
    },
    runContext: {
      setRunContext({ runId, namespace, value, unset }) {
        if (sharedRuntime.rejectRunContextWrites === true) {
          return false;
        }
        const key = runContextKey(runId, namespace);
        if (unset === true) {
          runContexts.delete(key);
          return true;
        }
        runContexts.set(key, structuredClone(value));
        return true;
      },
      getRunContext({ runId, namespace }) {
        const value = runContexts.get(runContextKey(runId, namespace));
        return value === undefined ? undefined : structuredClone(value);
      },
      clearRunContext({ runId, namespace }) {
        if (namespace) {
          runContexts.delete(runContextKey(runId, namespace));
          return;
        }
        for (const key of runContexts.keys()) {
          if (key.startsWith(`${runId.length}:${runId}`)) {
            runContexts.delete(key);
          }
        }
      },
    },
  };
  return {
    api,
    hooks,
    gateway,
    gatewayScopes,
    toolFactories,
    lifecycles,
    agentEventSubscriptions,
    runContexts,
  };
}

function setupPlugin(baseUrl, config = {}, sharedRuntime = {}) {
  sharedRuntime.processContextStore ??= new Map();
  const fake = createFakeApi({
    baseUrl,
    requestTimeoutMs: 250,
    ...config,
  }, sharedRuntime);
  createVeyraGovernancePlugin({
    processContextStore: sharedRuntime.processContextStore,
  }).register(fake.api);
  return fake;
}

async function invokeGateway(fake, method, params = {}) {
  const handler = fake.gateway.get(method);
  assert.equal(typeof handler, "function");
  let result;
  await handler({
    params,
    respond(ok, payload, error) {
      result = { ok, payload, error };
    },
  });
  assert.ok(result);
  return result;
}

async function registerSession(fake, overrides = {}) {
  const result = await invokeGateway(
    fake,
    "veyra.governance.registerSession",
    {
      sessionKey: "agent-exec:test-task",
      runId: "run-test",
      dispatchToken: "dispatch-secret-not-persisted",
      expiresAt: new Date(Date.now() + 60_000).toISOString(),
      bindingDigest: BINDING_DIGEST,
      allowedTools: GOVERNED_TOOLS,
      ...overrides,
    },
  );
  assert.equal(result.ok, true);
  return result.payload;
}

function beforeEvent({
  toolName,
  params = {},
  sessionKey = "agent-exec:test-task",
  runId = "run-test",
  toolCallId = "call-test",
}) {
  return {
    event: {
      toolName,
      params,
      runId,
      toolCallId,
    },
    ctx: {
      sessionKey,
      runId,
      toolCallId,
      toolName,
    },
  };
}

function toolsForSession(fake, sessionKey = "agent-exec:test-task") {
  assert.equal(fake.toolFactories.length, 1);
  const tools = fake.toolFactories[0]({ sessionKey });
  assert.equal(tools.length, 3);
  return new Map(tools.map((tool) => [tool.name, tool]));
}

function cleanupPlugin(fake, context = { reason: "reset" }) {
  for (const lifecycle of fake.lifecycles) {
    lifecycle.cleanup(context);
  }
}

function containsSecret(value, secret, seen = new Set()) {
  if (typeof value === "string") {
    return value.includes(secret);
  }
  if (
    value === null ||
    value === undefined ||
    (typeof value !== "object" && typeof value !== "function") ||
    seen.has(value)
  ) {
    return false;
  }
  seen.add(value);
  if (value instanceof Map) {
    for (const [key, item] of value) {
      if (
        containsSecret(key, secret, seen) ||
        containsSecret(item, secret, seen)
      ) {
        return true;
      }
    }
    return false;
  }
  if (value instanceof Set || Array.isArray(value)) {
    for (const item of value) {
      if (containsSecret(item, secret, seen)) {
        return true;
      }
    }
    return false;
  }
  for (const item of Object.values(value)) {
    if (containsSecret(item, secret, seen)) {
      return true;
    }
  }
  return false;
}

function assertSecretsErased(runtime, secrets) {
  for (const secret of secrets) {
    assert.equal(
      containsSecret(runtime.processContextStore, secret),
      false,
      `process context retained ${secret}`,
    );
    assert.equal(
      containsSecret(runtime.runContexts, secret),
      false,
      `host run context retained ${secret}`,
    );
  }
}

test("registration accepts unique governed tool subsets including an empty set", async () => {
  const fake = setupPlugin("http://127.0.0.1:9");
  try {
    const empty = await invokeGateway(
      fake,
      "veyra.governance.registerSession",
      {
        sessionKey: "agent-exec:no-tools",
        runId: "run-no-tools",
        dispatchToken: "dispatch-no-tools",
        expiresAt: new Date(Date.now() + 60_000).toISOString(),
        bindingDigest: BINDING_DIGEST,
        allowedTools: [],
      },
    );
    assert.equal(empty.ok, true);

    const subset = await invokeGateway(
      fake,
      "veyra.governance.registerSession",
      {
        sessionKey: "agent-exec:read-only",
        runId: "run-read-only",
        dispatchToken: "dispatch-read-only",
        expiresAt: new Date(Date.now() + 60_000).toISOString(),
        bindingDigest: "e".repeat(64),
        allowedTools: ["veyra_file_read"],
      },
    );
    assert.equal(subset.ok, true);

    for (const allowedTools of [
      ["veyra_file_read", "veyra_file_read"],
      ["veyra_file_read", "openclaw_native_read"],
    ]) {
      const rejected = await invokeGateway(
        fake,
        "veyra.governance.registerSession",
        {
          sessionKey: `agent-exec:invalid-${allowedTools.length}`,
          runId: `run-invalid-${allowedTools.join("-")}`,
          dispatchToken: "dispatch-invalid",
          expiresAt: new Date(Date.now() + 60_000).toISOString(),
          bindingDigest: "f".repeat(64),
          allowedTools,
        },
      );
      assert.equal(rejected.ok, false);
      assert.equal(rejected.error.code, "invalid_registration");
    }
  } finally {
    cleanupPlugin(fake);
  }
});

test("an empty governed allowlist blocks every tool before preflight and direct execution", async () => {
  const mock = await createMockVeyra((request, response) => {
    assert.equal(request.path, "/tool-governance/hook/observe");
    assert.equal(request.body.outcome, "blocked");
    jsonResponse(response, 200, {
      status: "recorded",
      authoritative: false,
    });
  });
  const fake = setupPlugin(mock.baseUrl);
  try {
    await registerSession(fake, { allowedTools: [] });

    const custom = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "must-not-read.txt" },
      toolCallId: "no-tool-custom",
    });
    const customDecision = await fake.hooks.get("before_tool_call")(
      custom.event,
      custom.ctx,
    );
    assert.equal(customDecision.block, true);
    assert.match(customDecision.blockReason, /not allowed/);

    const native = beforeEvent({
      toolName: "read",
      params: { path: "must-not-read.txt" },
      toolCallId: "no-tool-native",
    });
    const nativeDecision = await fake.hooks.get("before_tool_call")(
      native.event,
      native.ctx,
    );
    assert.equal(nativeDecision.block, true);
    assert.match(nativeDecision.blockReason, /disabled/);

    await assert.rejects(
      toolsForSession(fake)
        .get("veyra_file_read")
        .execute("no-tool-direct", { path: "must-not-read.txt" }),
      (error) => error?.code === "tool_not_allowed",
    );

    assert.deepEqual(
      mock.requests.map((request) => request.path),
      [
        "/tool-governance/hook/observe",
        "/tool-governance/hook/observe",
      ],
    );
    const status = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.metrics.allowlist_blocks, 2);
    assert.equal(status.payload.metrics.disallowed_custom_blocks, 1);
    assert.equal(status.payload.metrics.native_blocks, 1);
    assert.equal(status.payload.metrics.preflight_allows, 0);
    assert.equal(status.payload.metrics.executions, 0);
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("governance unavailability fails closed and erases the dispatch token", async () => {
  const unavailable = await createMockVeyra((_request, response) => {
    response.destroy();
  });
  const baseUrl = unavailable.baseUrl;
  await unavailable.close();

  const fake = setupPlugin(baseUrl);
  try {
    await registerSession(fake);
    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
    });
    const decision = await fake.hooks.get("before_tool_call")(
      input.event,
      input.ctx,
    );
    assert.equal(decision.block, true);
    assert.match(decision.blockReason, /governance_unavailable/);

    const status = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.active_sessions, 0);
    assert.equal(status.payload.failed_sessions, 1);
    assert.equal(status.payload.metrics.preflight_failures, 1);
    assert.equal(
      JSON.stringify(status.payload).includes("dispatch-secret-not-persisted"),
      false,
    );
  } finally {
    cleanupPlugin(fake);
  }
});

test("registered runs block native and unknown tools before Veyra execution", async () => {
  const mock = await createMockVeyra((request, response) => {
    assert.equal(request.path, "/tool-governance/hook/observe");
    assert.equal(request.body.outcome, "blocked");
    assert.equal(
      request.body.paramsDigest,
      request.body.toolName === "write"
        ? sha256('{"content":"no","path":"/tmp/outside"}')
        : sha256("{}"),
    );
    assert.equal(
      Object.hasOwn(request.body, "dispatchToken"),
      false,
    );
    jsonResponse(response, 200, {
      status: "recorded",
      authoritative: false,
    });
  });
  const fake = setupPlugin(mock.baseUrl);
  try {
    await registerSession(fake);
    const native = beforeEvent({
      toolName: "write",
      params: { path: "/tmp/outside", content: "no" },
      toolCallId: "native-call",
    });
    const nativeDecision = await fake.hooks.get("before_tool_call")(
      native.event,
      native.ctx,
    );
    assert.equal(nativeDecision.block, true);
    assert.match(nativeDecision.blockReason, /Native OpenClaw tool/);

    const unknown = beforeEvent({
      toolName: "mystery_tool",
      toolCallId: "unknown-call",
    });
    const unknownDecision = await fake.hooks.get("before_tool_call")(
      unknown.event,
      unknown.ctx,
    );
    assert.equal(unknownDecision.block, true);
    assert.match(unknownDecision.blockReason, /Unknown tool/);
    assert.equal(mock.requests.length, 2);
    assert.deepEqual(
      mock.requests.map((request) => request.body.toolName),
      ["write", "mystery_tool"],
    );
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("matching governed run fails closed when the hook session key is missing", async () => {
  const fake = setupPlugin("http://127.0.0.1:9");
  try {
    await registerSession(fake);
    const input = beforeEvent({
      toolName: "write",
      params: { path: "/tmp/outside", content: "no" },
      toolCallId: "missing-session-call",
    });
    delete input.ctx.sessionKey;

    const decision = await fake.hooks.get("before_tool_call")(
      input.event,
      input.ctx,
    );
    assert.equal(decision.block, true);
    assert.match(decision.blockReason, /mismatched hook identity/);

    const status = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.active_sessions, 1);
    assert.equal(status.payload.failed_sessions, 0);
  } finally {
    cleanupPlugin(fake);
  }
});

test("matching governed run fails closed when the hook session key is mismatched", async () => {
  const fake = setupPlugin("http://127.0.0.1:9");
  try {
    await registerSession(fake);
    const input = beforeEvent({
      toolName: "mystery_tool",
      sessionKey: "ordinary-session",
      toolCallId: "mismatched-session-call",
    });

    const decision = await fake.hooks.get("before_tool_call")(
      input.event,
      input.ctx,
    );
    assert.equal(decision.block, true);
    assert.match(decision.blockReason, /mismatched hook identity/);

    const status = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.active_sessions, 1);
    assert.equal(status.payload.failed_sessions, 0);
  } finally {
    cleanupPlugin(fake);
  }
});

test("a governed session with no hook run id blocks native, unknown, and custom tools", async () => {
  const fake = setupPlugin("http://127.0.0.1:9");
  try {
    await registerSession(fake);
    for (const [toolName, toolCallId] of [
      ["write", "missing-run-native"],
      ["mystery_tool", "missing-run-unknown"],
      ["veyra_file_read", "missing-run-custom"],
    ]) {
      const input = beforeEvent({ toolName, toolCallId });
      delete input.event.runId;
      delete input.ctx.runId;
      const decision = await fake.hooks.get("before_tool_call")(
        input.event,
        input.ctx,
      );
      assert.equal(decision.block, true);
      assert.match(decision.blockReason, /mismatched hook identity/);
    }
  } finally {
    cleanupPlugin(fake);
  }
});

test("a completely identityless hook fails closed while any governed marker exists", async () => {
  const fake = setupPlugin("http://127.0.0.1:9");
  try {
    await registerSession(fake);
    for (const [toolName, toolCallId] of [
      ["write", "identityless-native"],
      ["mystery_tool", "identityless-unknown"],
      ["veyra_file_read", "identityless-custom"],
    ]) {
      const input = beforeEvent({ toolName, toolCallId });
      delete input.event.runId;
      delete input.ctx.runId;
      delete input.ctx.sessionKey;
      const decision = await fake.hooks.get("before_tool_call")(
        input.event,
        input.ctx,
      );
      assert.equal(decision.block, true);
      assert.match(decision.blockReason, /mismatched hook identity/);
    }
  } finally {
    cleanupPlugin(fake);
  }
});

test("an ordinary session with no hook run id keeps native behavior but cannot call Veyra tools", async () => {
  const fake = setupPlugin("http://127.0.0.1:9");
  try {
    const native = beforeEvent({
      toolName: "read",
      sessionKey: "ordinary-session",
      toolCallId: "ordinary-missing-run-native",
    });
    delete native.event.runId;
    delete native.ctx.runId;
    assert.equal(
      await fake.hooks.get("before_tool_call")(
        native.event,
        native.ctx,
      ),
      undefined,
    );

    const custom = beforeEvent({
      toolName: "veyra_file_read",
      sessionKey: "ordinary-session",
      toolCallId: "ordinary-missing-run-custom",
    });
    delete custom.event.runId;
    delete custom.ctx.runId;
    const blocked = await fake.hooks.get("before_tool_call")(
      custom.event,
      custom.ctx,
    );
    assert.equal(blocked.block, true);
    assert.match(blocked.blockReason, /registered governed session/);

    const identitylessNative = beforeEvent({
      toolName: "read",
      sessionKey: "ordinary-session",
      toolCallId: "ordinary-identityless-native",
    });
    delete identitylessNative.event.runId;
    delete identitylessNative.ctx.runId;
    delete identitylessNative.ctx.sessionKey;
    assert.equal(
      await fake.hooks.get("before_tool_call")(
        identitylessNative.event,
        identitylessNative.ctx,
      ),
      undefined,
    );
  } finally {
    cleanupPlugin(fake);
  }
});

test("governed hook call without a tool call id fails closed", async () => {
  const fake = setupPlugin("http://127.0.0.1:9");
  try {
    await registerSession(fake);
    const input = beforeEvent({
      toolName: "write",
    });
    delete input.event.toolCallId;
    delete input.ctx.toolCallId;

    const decision = await fake.hooks.get("before_tool_call")(
      input.event,
      input.ctx,
    );
    assert.equal(decision.block, true);
    assert.match(decision.blockReason, /malformed call/);
  } finally {
    cleanupPlugin(fake);
  }
});

test("an active governed run cannot be registered to another session", async () => {
  const fake = setupPlugin("http://127.0.0.1:9");
  try {
    await registerSession(fake);
    const conflict = await invokeGateway(
      fake,
      "veyra.governance.registerSession",
      {
        sessionKey: "agent-exec:conflicting-task",
        runId: "run-test",
        dispatchToken: "conflicting-dispatch-secret",
        expiresAt: new Date(Date.now() + 60_000).toISOString(),
        bindingDigest: "e".repeat(64),
        allowedTools: GOVERNED_TOOLS,
      },
    );
    assert.equal(conflict.ok, false);
    assert.equal(conflict.error.code, "run_binding_mismatch");

    const status = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.active_sessions, 1);
    assert.equal(status.payload.metrics.replacements, 0);
  } finally {
    cleanupPlugin(fake);
  }
});

test("an exact active registration is idempotent without replacing the session or clearing reservations", async () => {
  const mock = await createMockVeyra((request, response) => {
    assert.equal(request.path, "/tool-governance/hook/preflight");
    jsonResponse(response, 200, {
      allow: true,
      canonicalToolName: "file.read",
      invocationDigest: INVOCATION_DIGEST,
      reservationId: "reservation-idempotent-registration",
      reservationToken: "reservation-token-idempotent-registration",
      executionToken: "execution-token-idempotent-registration",
      expiresAt: new Date(Date.now() + 30_000).toISOString(),
    });
  });
  const fake = setupPlugin(mock.baseUrl);
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  const registration = {
    sessionKey: "agent-exec:idempotent-registration",
    runId: "run-idempotent-registration",
    dispatchToken: "dispatch-idempotent-registration",
    expiresAt,
    bindingDigest: "e".repeat(64),
    allowedTools: ["veyra_shell_probe", "veyra_file_read"],
  };
  try {
    const first = await invokeGateway(
      fake,
      "veyra.governance.registerSession",
      registration,
    );
    assert.equal(first.ok, true);
    assert.equal(first.payload.idempotent, false);

    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
      sessionKey: registration.sessionKey,
      runId: registration.runId,
      toolCallId: "idempotent-registration-call",
    });
    assert.equal(
      await fake.hooks.get("before_tool_call")(
        input.event,
        input.ctx,
      ),
      undefined,
    );

    const before = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(before.payload.metrics.registrations, 1);
    assert.equal(before.payload.metrics.replacements, 0);
    assert.equal(before.payload.pending_reservations, 1);

    const repeated = await invokeGateway(
      fake,
      "veyra.governance.registerSession",
      {
        ...registration,
        allowedTools: [...registration.allowedTools].reverse(),
      },
    );
    assert.equal(repeated.ok, true);
    assert.equal(repeated.payload.idempotent, true);

    const after = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(after.payload.metrics.registrations, 1);
    assert.equal(after.payload.metrics.replacements, 0);
    assert.equal(after.payload.pending_reservations, 1);

    for (const mismatch of [
      { bindingDigest: "f".repeat(64) },
      { dispatchToken: "different-dispatch-token" },
      { expiresAt: new Date(Date.now() + 90_000).toISOString() },
      { allowedTools: ["veyra_file_read"] },
    ]) {
      const rejected = await invokeGateway(
        fake,
        "veyra.governance.registerSession",
        {
          ...registration,
          ...mismatch,
        },
      );
      assert.equal(rejected.ok, false);
      assert.equal(rejected.error.code, "run_binding_mismatch");
    }

    const finalStatus = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(finalStatus.payload.metrics.registrations, 1);
    assert.equal(finalStatus.payload.metrics.replacements, 0);
    assert.equal(finalStatus.payload.pending_reservations, 1);
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("a cancelled session cannot be re-registered locally or from a shared tombstone", async () => {
  const sharedHostRuntime = {};
  const first = setupPlugin(
    "http://127.0.0.1:9",
    {},
    sharedHostRuntime,
  );
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  const registration = {
    sessionKey: "agent-exec:cancelled-registration",
    runId: "run-cancelled-registration",
    dispatchToken: "dispatch-cancelled-registration",
    expiresAt,
    bindingDigest: "f".repeat(64),
    allowedTools: [],
  };
  let restarted;
  try {
    const registered = await invokeGateway(
      first,
      "veyra.governance.registerSession",
      registration,
    );
    assert.equal(registered.ok, true);
    assert.equal(registered.payload.idempotent, false);

    const cancelled = await invokeGateway(
      first,
      "veyra.governance.cancelSession",
      {
        sessionKey: registration.sessionKey,
        runId: registration.runId,
        bindingDigest: registration.bindingDigest,
      },
    );
    assert.equal(cancelled.ok, true);

    const localReplay = await invokeGateway(
      first,
      "veyra.governance.registerSession",
      registration,
    );
    assert.equal(localReplay.ok, false);
    assert.equal(localReplay.error.code, "session_retired");

    const localStatus = await invokeGateway(
      first,
      "veyra.governance.status",
    );
    assert.equal(localStatus.payload.active_sessions, 0);
    assert.equal(localStatus.payload.metrics.registrations, 1);
    assert.equal(localStatus.payload.metrics.cancellations, 1);

    restarted = setupPlugin(
      "http://127.0.0.1:9",
      {},
      {
        runContexts: sharedHostRuntime.runContexts,
        processContextStore: new Map(),
      },
    );
    const sharedReplay = await invokeGateway(
      restarted,
      "veyra.governance.registerSession",
      registration,
    );
    assert.equal(sharedReplay.ok, false);
    assert.equal(sharedReplay.error.code, "session_retired");

    const restartedStatus = await invokeGateway(
      restarted,
      "veyra.governance.status",
    );
    assert.equal(restartedStatus.payload.active_sessions, 0);
  } finally {
    if (restarted) {
      cleanupPlugin(restarted);
    }
    cleanupPlugin(first);
  }
});

test("a delayed old registration cannot resurrect its run over a replacement", async () => {
  const fake = setupPlugin("http://127.0.0.1:9");
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  const oldRegistration = {
    sessionKey: "agent-exec:delayed-old-registration",
    runId: "run-delayed-old",
    dispatchToken: "dispatch-delayed-old",
    expiresAt,
    bindingDigest: "d".repeat(64),
    allowedTools: [],
  };
  const replacementRegistration = {
    ...oldRegistration,
    runId: "run-current-replacement",
    dispatchToken: "dispatch-current-replacement",
    bindingDigest: "e".repeat(64),
  };
  try {
    const old = await invokeGateway(
      fake,
      "veyra.governance.registerSession",
      oldRegistration,
    );
    assert.equal(old.ok, true);
    assert.equal(old.payload.idempotent, false);

    const replacement = await invokeGateway(
      fake,
      "veyra.governance.registerSession",
      replacementRegistration,
    );
    assert.equal(replacement.ok, true);
    assert.equal(replacement.payload.idempotent, false);

    const beforeReplay = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(beforeReplay.payload.active_sessions, 1);
    assert.equal(beforeReplay.payload.metrics.registrations, 2);
    assert.equal(beforeReplay.payload.metrics.replacements, 1);

    const delayedOld = await invokeGateway(
      fake,
      "veyra.governance.registerSession",
      oldRegistration,
    );
    assert.equal(delayedOld.ok, false);
    assert.equal(delayedOld.error.code, "session_retired");

    const afterReplay = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(afterReplay.payload.active_sessions, 1);
    assert.equal(afterReplay.payload.metrics.registrations, 2);
    assert.equal(afterReplay.payload.metrics.replacements, 1);

    const current = await invokeGateway(
      fake,
      "veyra.governance.registerSession",
      replacementRegistration,
    );
    assert.equal(current.ok, true);
    assert.equal(current.payload.idempotent, true);

    const finalStatus = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(finalStatus.payload.active_sessions, 1);
    assert.equal(finalStatus.payload.metrics.registrations, 2);
    assert.equal(finalStatus.payload.metrics.replacements, 1);
  } finally {
    cleanupPlugin(fake);
  }
});

test("registries with different Veyra authorities cannot reuse active credentials", async () => {
  const sharedRuntime = { rejectRunContextWrites: true };
  const first = setupPlugin(
    "http://127.0.0.1:9",
    {},
    sharedRuntime,
  );
  const second = setupPlugin(
    "http://127.0.0.1:10",
    {},
    sharedRuntime,
  );
  try {
    await registerSession(first);
    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
      toolCallId: "wrong-authority-call",
    });
    const decision = await second.hooks.get("before_tool_call")(
      input.event,
      input.ctx,
    );
    assert.equal(decision.block, true);
    assert.match(decision.blockReason, /mismatched hook identity/);

    const conflictingRegistration = await invokeGateway(
      second,
      "veyra.governance.registerSession",
      {
        sessionKey: "agent-exec:test-task",
        runId: "run-test",
        dispatchToken: "must-not-replace-authority",
        expiresAt: new Date(Date.now() + 60_000).toISOString(),
        bindingDigest: BINDING_DIGEST,
        allowedTools: GOVERNED_TOOLS,
      },
    );
    assert.equal(conflictingRegistration.ok, false);
    assert.equal(
      conflictingRegistration.error.code,
      "run_binding_mismatch",
    );
  } finally {
    cleanupPlugin(first);
    cleanupPlugin(second);
  }
});

test("allowed custom tool obtains preflight reservation and executes only through Veyra", async () => {
  const seenTokens = new Set();
  const mock = await createMockVeyra((request, response) => {
    assert.equal(
      request.headers["x-veyra-dispatch-token"],
      "dispatch-secret-not-persisted",
    );
    if (request.path === "/tool-governance/hook/preflight") {
      jsonResponse(response, 200, {
        allow: true,
        canonicalToolName: "file.read",
        invocationDigest: INVOCATION_DIGEST,
        reservationId: "reservation-1",
        reservationToken: "reservation-token-1",
        executionToken: "execution-token-1",
        expiresAt: new Date(Date.now() + 30_000).toISOString(),
      });
      return;
    }
    if (request.path === "/tool-governance/hook/execute") {
      assert.equal(request.body.executionToken, "execution-token-1");
      assert.equal(request.body.reservationToken, "reservation-token-1");
      assert.equal(seenTokens.has(request.body.executionToken), false);
      seenTokens.add(request.body.executionToken);
      assert.equal(Object.hasOwn(request.body, "dispatchToken"), false);
      jsonResponse(response, 200, {
        status: "ok",
        result: {
          status: "ok",
          path: "note.txt",
          content: "hello",
        },
        resultDigest: RESULT_DIGEST,
        receiptRef: {
          run_id: "run-test",
          tool_call_id: "read-call",
          invocation_digest: INVOCATION_DIGEST,
        },
        effectEvidenceDigest: EFFECT_DIGEST,
      });
      return;
    }
    if (request.path === "/tool-governance/hook/observe") {
      assert.equal(request.body.outcome, "completed");
      assert.equal(Object.hasOwn(request.body, "authoritative"), false);
      jsonResponse(response, 200, {
        status: "recorded",
        authoritative: false,
      });
      return;
    }
    jsonResponse(response, 404, { detail: "not found" });
  });
  const fake = setupPlugin(mock.baseUrl);
  try {
    await registerSession(fake);
    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
      toolCallId: "read-call",
    });
    const decision = await fake.hooks.get("before_tool_call")(
      input.event,
      input.ctx,
    );
    assert.equal(decision, undefined);

    const tool = toolsForSession(fake).get("veyra_file_read");
    const result = await tool.execute(
      "read-call",
      { path: "note.txt" },
    );
    assert.equal(result.details.veyra_governed, true);
    assert.deepEqual(result.details.receipt_ref, {
      run_id: "run-test",
      tool_call_id: "read-call",
      invocation_digest: INVOCATION_DIGEST,
    });
    assert.equal(result.details.result_digest, RESULT_DIGEST);
    assert.equal(result.details.effect_evidence_digest, EFFECT_DIGEST);
    assert.match(result.content[0].text, /hello/);

    await fake.hooks.get("after_tool_call")(
      {
        ...input.event,
        result,
        durationMs: 12,
      },
      input.ctx,
    );

    assert.deepEqual(
      mock.requests.map((request) => request.path),
      [
        "/tool-governance/hook/preflight",
        "/tool-governance/hook/execute",
        "/tool-governance/hook/observe",
      ],
    );
    const status = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.metrics.preflight_allows, 1);
    assert.equal(status.payload.metrics.executions, 1);
    assert.equal(status.payload.metrics.observations, 1);
    assert.equal(status.payload.pending_reservations, 0);
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("a denied custom preflight does not poison later governed calls", async () => {
  const mock = await createMockVeyra((request, response) => {
    if (request.path === "/tool-governance/hook/preflight") {
      jsonResponse(response, 200, {
        allow: false,
        reason: "sandbox traversal blocked",
      });
      return;
    }
    jsonResponse(response, 404, { detail: "not found" });
  });
  const fake = setupPlugin(mock.baseUrl);
  try {
    await registerSession(fake);
    const input = beforeEvent({
      toolName: "veyra_file_write",
      params: { path: "../escape.txt", content: "blocked" },
      toolCallId: "denied-call",
    });
    const blocked = await fake.hooks.get("before_tool_call")(
      input.event,
      input.ctx,
    );
    assert.equal(blocked.block, true);
    assert.match(blocked.blockReason, /traversal blocked/);

    await fake.hooks.get("after_tool_call")(
      {
        ...input.event,
        error: blocked.blockReason,
        durationMs: 0,
      },
      input.ctx,
    );

    const status = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.active_sessions, 1);
    assert.equal(status.payload.failed_sessions, 0);
    assert.equal(status.payload.metrics.preflight_denials, 1);
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("separate registries fail over to the process bridge when host run context rejects writes", async () => {
  const mock = await createMockVeyra((request, response) => {
    assert.equal(
      request.headers["x-veyra-dispatch-token"],
      "dispatch-secret-not-persisted",
    );
    if (request.path === "/tool-governance/hook/preflight") {
      jsonResponse(response, 200, {
        allow: true,
        canonicalToolName: "file.read",
        invocationDigest: INVOCATION_DIGEST,
        reservationId: "reservation-cross-registry",
        reservationToken: "reservation-token-cross-registry",
        executionToken: "execution-token-cross-registry",
        expiresAt: new Date(Date.now() + 30_000).toISOString(),
      });
      return;
    }
    if (request.path === "/tool-governance/hook/execute") {
      assert.equal(
        request.body.reservationToken,
        "reservation-token-cross-registry",
      );
      assert.equal(
        request.body.executionToken,
        "execution-token-cross-registry",
      );
      jsonResponse(response, 200, {
        status: "ok",
        result: { status: "ok", content: "cross-registry" },
        resultDigest: RESULT_DIGEST,
        receiptRef: {
          run_id: "run-test",
          tool_call_id: "cross-registry-call",
          invocation_digest: INVOCATION_DIGEST,
        },
        effectEvidenceDigest: EFFECT_DIGEST,
      });
      return;
    }
    if (request.path === "/tool-governance/hook/observe") {
      jsonResponse(response, 200, {
        status: "recorded",
        authoritative: false,
      });
      return;
    }
    jsonResponse(response, 404, { detail: "not found" });
  });
  const sharedRuntime = { rejectRunContextWrites: true };
  const gatewayFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const hookFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const toolFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const observeFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  try {
    await registerSession(gatewayFake);
    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
      toolCallId: "cross-registry-call",
    });
    assert.equal(
      await hookFake.hooks.get("before_tool_call")(
        input.event,
        input.ctx,
      ),
      undefined,
    );

    const tool = toolsForSession(toolFake).get("veyra_file_read");
    const result = await tool.execute(
      "cross-registry-call",
      { path: "note.txt" },
    );
    assert.equal(result.details.veyra_governed, true);

    await observeFake.hooks.get("after_tool_call")(
      {
        ...input.event,
        result,
        durationMs: 9,
      },
      input.ctx,
    );

    assert.deepEqual(
      mock.requests.map((request) => request.path),
      [
        "/tool-governance/hook/preflight",
        "/tool-governance/hook/execute",
        "/tool-governance/hook/observe",
      ],
    );
    assert.equal(
      JSON.stringify([
        await invokeGateway(
          gatewayFake,
          "veyra.governance.status",
        ),
        await invokeGateway(hookFake, "veyra.governance.status"),
        await invokeGateway(toolFake, "veyra.governance.status"),
        await invokeGateway(observeFake, "veyra.governance.status"),
      ]).includes("dispatch-secret-not-persisted"),
      false,
    );
  } finally {
    cleanupPlugin(gatewayFake);
    cleanupPlugin(hookFake);
    cleanupPlugin(toolFake);
    cleanupPlugin(observeFake);
    await mock.close();
  }
});

test("registry restart fails the observed governed run closed without erasing its marker", async () => {
  const sharedRuntime = { rejectRunContextWrites: true };
  const gatewayFake = setupPlugin(
    "http://127.0.0.1:9",
    {},
    sharedRuntime,
  );
  const hookFake = setupPlugin(
    "http://127.0.0.1:9",
    {},
    sharedRuntime,
  );
  try {
    await registerSession(gatewayFake);
    cleanupPlugin(gatewayFake, { reason: "restart" });

    for (const [toolName, toolCallId] of [
      ["write", "restart-native"],
      ["mystery_tool", "restart-unknown"],
      ["veyra_file_read", "restart-custom"],
    ]) {
      const input = beforeEvent({ toolName, toolCallId });
      const decision = await hookFake.hooks.get("before_tool_call")(
        input.event,
        input.ctx,
      );
      assert.equal(decision.block, true);
      assert.match(decision.blockReason, /fail-closed/);
    }

    const status = await invokeGateway(
      hookFake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.active_sessions, 0);
    assert.equal(status.payload.failed_sessions, 1);
    assertSecretsErased(sharedRuntime, [
      "dispatch-secret-not-persisted",
    ]);
  } finally {
    cleanupPlugin(hookFake);
    cleanupPlugin(gatewayFake);
  }
});

test("scoped reset preserves other observed bindings for a later restart", async () => {
  const sharedRuntime = {};
  const registryFake = setupPlugin(
    "http://127.0.0.1:9",
    {},
    sharedRuntime,
  );
  const laterFake = setupPlugin(
    "http://127.0.0.1:9",
    {},
    sharedRuntime,
  );
  try {
    await registerSession(registryFake, {
      sessionKey: "agent-exec:session-a",
      runId: "run-a",
      dispatchToken: "dispatch-secret-a",
    });
    await registerSession(registryFake, {
      sessionKey: "agent-exec:session-b",
      runId: "run-b",
      dispatchToken: "dispatch-secret-b",
    });

    cleanupPlugin(registryFake, {
      reason: "reset",
      sessionKey: "agent-exec:session-a",
    });
    cleanupPlugin(registryFake, { reason: "restart" });

    for (const [toolName, toolCallId] of [
      ["write", "session-b-native"],
      ["veyra_file_read", "session-b-custom"],
    ]) {
      const input = beforeEvent({
        toolName,
        toolCallId,
        sessionKey: "agent-exec:session-b",
        runId: "run-b",
      });
      const decision = await laterFake.hooks.get("before_tool_call")(
        input.event,
        input.ctx,
      );
      assert.equal(decision.block, true);
      assert.match(decision.blockReason, /fail-closed/);
    }

    const tool = toolsForSession(
      laterFake,
      "agent-exec:session-b",
    ).get("veyra_file_read");
    await assert.rejects(
      tool.execute("session-b-custom", { path: "note.txt" }),
      /session is unavailable/,
    );
    const status = await invokeGateway(
      laterFake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.active_sessions, 0);
    assert.equal(status.payload.failed_sessions, 2);
    assertSecretsErased(sharedRuntime, [
      "dispatch-secret-a",
      "dispatch-secret-b",
    ]);
  } finally {
    cleanupPlugin(laterFake);
    cleanupPlugin(registryFake);
  }
});

test("a stale writable host mirror cannot resurrect a process tombstone", async () => {
  let executeCalls = 0;
  const mock = await createMockVeyra((request, response) => {
    if (request.path === "/tool-governance/hook/execute") {
      executeCalls += 1;
    }
    jsonResponse(response, 500, { detail: "must not execute" });
  });
  const sharedRuntime = {};
  const gatewayFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const hookFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const toolFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  try {
    await registerSession(gatewayFake);
    sharedRuntime.rejectRunContextWrites = true;
    cleanupPlugin(gatewayFake, { reason: "restart" });

    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
      toolCallId: "split-brain-call",
    });
    const decision = await hookFake.hooks.get("before_tool_call")(
      input.event,
      input.ctx,
    );
    assert.equal(decision.block, true);
    assert.match(decision.blockReason, /mismatched hook identity/);

    const tool = toolsForSession(toolFake).get("veyra_file_read");
    await assert.rejects(
      tool.execute("split-brain-call", { path: "note.txt" }),
      /host\/process context is inconsistent/,
    );
    assert.equal(executeCalls, 0);
    assert.equal(
      containsSecret(
        sharedRuntime.processContextStore,
        "dispatch-secret-not-persisted",
      ),
      false,
    );
    assert.equal(
      containsSecret(
        sharedRuntime.runContexts,
        "dispatch-secret-not-persisted",
      ),
      true,
      "the fixture must retain the stale host credential",
    );
  } finally {
    cleanupPlugin(hookFake);
    cleanupPlugin(toolFake);
    cleanupPlugin(gatewayFake);
    await mock.close();
  }
});

test("reservation expiry independently erases bearer credentials and blocks execution", async () => {
  let executeCalls = 0;
  const mock = await createMockVeyra((request, response) => {
    if (request.path === "/tool-governance/hook/preflight") {
      jsonResponse(response, 200, {
        allow: true,
        canonicalToolName: "file.read",
        invocationDigest: INVOCATION_DIGEST,
        reservationId: "reservation-short-lived",
        reservationToken: "reservation-token-short-lived",
        executionToken: "execution-token-short-lived",
        expiresAt: new Date(Date.now() + 80).toISOString(),
      });
      return;
    }
    if (request.path === "/tool-governance/hook/execute") {
      executeCalls += 1;
    }
    jsonResponse(response, 500, { detail: "must not execute" });
  });
  const sharedRuntime = {};
  const hookFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const toolFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  try {
    await registerSession(hookFake);
    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
      toolCallId: "short-lived-call",
    });
    assert.equal(
      await hookFake.hooks.get("before_tool_call")(
        input.event,
        input.ctx,
      ),
      undefined,
    );
    assert.equal(
      containsSecret(
        sharedRuntime.processContextStore,
        "execution-token-short-lived",
      ),
      true,
    );

    await new Promise((resolve) => setTimeout(resolve, 140));

    assertSecretsErased(sharedRuntime, [
      "reservation-token-short-lived",
      "execution-token-short-lived",
    ]);
    const status = await invokeGateway(
      toolFake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.pending_reservations, 0);
    assert.equal(status.payload.metrics.reservation_expirations, 1);

    const tool = toolsForSession(toolFake).get("veyra_file_read");
    await assert.rejects(
      tool.execute("short-lived-call", { path: "note.txt" }),
      /execution token is unavailable/,
    );
    assert.equal(executeCalls, 0);
  } finally {
    cleanupPlugin(hookFake);
    cleanupPlugin(toolFake);
    await mock.close();
  }
});

test("a restart during preflight cannot re-persist response bearer credentials", async () => {
  let markPreflightEntered;
  let releasePreflight;
  const preflightEntered = new Promise((resolve) => {
    markPreflightEntered = resolve;
  });
  const preflightRelease = new Promise((resolve) => {
    releasePreflight = resolve;
  });
  const mock = await createMockVeyra(async (request, response) => {
    if (request.path === "/tool-governance/hook/preflight") {
      markPreflightEntered();
      await preflightRelease;
      jsonResponse(response, 200, {
        allow: true,
        canonicalToolName: "file.read",
        invocationDigest: INVOCATION_DIGEST,
        reservationId: "reservation-restart-race",
        reservationToken: "reservation-token-restart-race",
        executionToken: "execution-token-restart-race",
        expiresAt: new Date(Date.now() + 30_000).toISOString(),
      });
      return;
    }
    jsonResponse(response, 500, { detail: "must not execute" });
  });
  const sharedRuntime = { rejectRunContextWrites: true };
  const gatewayFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const hookFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  try {
    await registerSession(gatewayFake);
    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
      toolCallId: "restart-preflight-race",
    });
    const pendingDecision = hookFake.hooks.get("before_tool_call")(
      input.event,
      input.ctx,
    );
    await preflightEntered;
    cleanupPlugin(gatewayFake, { reason: "restart" });
    releasePreflight();
    const decision = await pendingDecision;
    assert.equal(decision.block, true);
    assert.match(decision.blockReason, /changed during preflight/);
    assertSecretsErased(sharedRuntime, [
      "dispatch-secret-not-persisted",
      "reservation-token-restart-race",
      "execution-token-restart-race",
    ]);
    const status = await invokeGateway(
      hookFake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.pending_reservations, 0);
    assert.equal(status.payload.failed_sessions, 1);
  } finally {
    releasePreflight?.();
    cleanupPlugin(hookFake);
    cleanupPlugin(gatewayFake);
    await mock.close();
  }
});

test("cross-registry cancellation clears reserved bearer credentials and blocks later execution", async () => {
  const mock = await createMockVeyra((request, response) => {
    if (request.path === "/tool-governance/hook/preflight") {
      jsonResponse(response, 200, {
        allow: true,
        canonicalToolName: "file.read",
        invocationDigest: INVOCATION_DIGEST,
        reservationId: "reservation-cancelled",
        reservationToken: "reservation-token-cancelled",
        executionToken: "execution-token-cancelled",
        expiresAt: new Date(Date.now() + 30_000).toISOString(),
      });
      return;
    }
    jsonResponse(response, 500, { detail: "must not execute" });
  });
  const sharedRuntime = { rejectRunContextWrites: true };
  const gatewayFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const hookFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const cancelFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const toolFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  try {
    await registerSession(gatewayFake);
    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
      toolCallId: "cancelled-reservation-call",
    });
    assert.equal(
      await hookFake.hooks.get("before_tool_call")(
        input.event,
        input.ctx,
      ),
      undefined,
    );

    const cancelled = await invokeGateway(
      cancelFake,
      "veyra.governance.cancelSession",
      {
        sessionKey: "agent-exec:test-task",
        runId: "run-test",
        bindingDigest: BINDING_DIGEST,
      },
    );
    assert.equal(cancelled.ok, true);
    assert.equal(cancelled.payload.cancelled, true);
    assertSecretsErased(sharedRuntime, [
      "dispatch-secret-not-persisted",
      "reservation-token-cancelled",
      "execution-token-cancelled",
    ]);

    const tool = toolsForSession(toolFake).get("veyra_file_read");
    await assert.rejects(
      tool.execute(
        "cancelled-reservation-call",
        { path: "note.txt" },
      ),
      /session is unavailable/,
    );
    assert.equal(
      mock.requests.filter(
        (request) =>
          request.path === "/tool-governance/hook/execute",
      ).length,
      0,
    );
  } finally {
    cleanupPlugin(gatewayFake);
    cleanupPlugin(hookFake);
    cleanupPlugin(cancelFake);
    cleanupPlugin(toolFake);
    await mock.close();
  }
});

test("terminal lifecycle cleanup erases exact run context and all bearer credentials", async () => {
  const mock = await createMockVeyra((request, response) => {
    if (request.path === "/tool-governance/hook/preflight") {
      jsonResponse(response, 200, {
        allow: true,
        canonicalToolName: "file.read",
        invocationDigest: INVOCATION_DIGEST,
        reservationId: "reservation-terminal",
        reservationToken: "reservation-token-terminal",
        executionToken: "execution-token-terminal",
        expiresAt: new Date(Date.now() + 30_000).toISOString(),
      });
      return;
    }
    jsonResponse(response, 500, { detail: "must not execute" });
  });
  const sharedRuntime = {};
  const gatewayFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const hookFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  const terminalFake = setupPlugin(mock.baseUrl, {}, sharedRuntime);
  try {
    await registerSession(gatewayFake);
    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
      toolCallId: "terminal-reservation-call",
    });
    assert.equal(
      await hookFake.hooks.get("before_tool_call")(
        input.event,
        input.ctx,
      ),
      undefined,
    );

    assert.equal(terminalFake.agentEventSubscriptions.length, 1);
    await terminalFake.agentEventSubscriptions[0].handle(
      {
        runId: "run-test",
        seq: 1,
        stream: "lifecycle",
        ts: Date.now(),
        data: { phase: "end" },
        sessionKey: "agent-exec:test-task",
      },
      {
        clearRunContext() {
          terminalFake.api.runContext.clearRunContext({
            runId: "run-test",
          });
        },
      },
    );

    assertSecretsErased(sharedRuntime, [
      "dispatch-secret-not-persisted",
      "reservation-token-terminal",
      "execution-token-terminal",
    ]);
    assert.equal(
      [...sharedRuntime.processContextStore.keys()].some(
        (key) => typeof key === "string",
      ),
      false,
    );
    assert.equal(sharedRuntime.runContexts.size, 0);
    const status = await invokeGateway(
      terminalFake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.active_sessions, 0);
    assert.equal(status.payload.pending_reservations, 0);
  } finally {
    cleanupPlugin(gatewayFake);
    cleanupPlugin(hookFake);
    cleanupPlugin(terminalFake);
    await mock.close();
  }
});

test("tampered execution token is rejected and the governed session fails closed", async () => {
  let executeCalls = 0;
  const mock = await createMockVeyra((request, response) => {
    if (request.path === "/tool-governance/hook/preflight") {
      jsonResponse(response, 200, {
        allow: true,
        canonicalToolName: "file.write",
        invocationDigest: INVOCATION_DIGEST,
        reservationId: "reservation-tampered",
        reservationToken: "reservation-token-tampered",
        executionToken: "tampered-token",
        expiresAt: new Date(Date.now() + 30_000).toISOString(),
      });
      return;
    }
    if (request.path === "/tool-governance/hook/execute") {
      executeCalls += 1;
      assert.equal(request.body.executionToken, "tampered-token");
      jsonResponse(response, 409, { detail: "invalid execution token" });
      return;
    }
    jsonResponse(response, 500, { detail: "unexpected" });
  });
  const fake = setupPlugin(mock.baseUrl);
  try {
    await registerSession(fake);
    const input = beforeEvent({
      toolName: "veyra_file_write",
      params: { path: "note.txt", content: "hello" },
      toolCallId: "tampered-call",
    });
    assert.equal(
      await fake.hooks.get("before_tool_call")(input.event, input.ctx),
      undefined,
    );
    const tool = toolsForSession(fake).get("veyra_file_write");
    await assert.rejects(
      tool.execute(
        "tampered-call",
        { path: "note.txt", content: "hello" },
      ),
      /invalid execution token/,
    );
    assert.equal(executeCalls, 1);

    const next = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "note.txt" },
      toolCallId: "after-tamper",
    });
    const blocked = await fake.hooks.get("before_tool_call")(
      next.event,
      next.ctx,
    );
    assert.equal(blocked.block, true);
    assert.match(blocked.blockReason, /fail-closed/);
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("execution token cannot be replayed even after a successful server call", async () => {
  let executeCalls = 0;
  const mock = await createMockVeyra((request, response) => {
    if (request.path === "/tool-governance/hook/preflight") {
      jsonResponse(response, 200, {
        allow: true,
        canonicalToolName: "shell.run",
        invocationDigest: INVOCATION_DIGEST,
        reservationId: "reservation-once",
        reservationToken: "reservation-token-once",
        executionToken: "one-time-token",
        expiresAt: new Date(Date.now() + 30_000).toISOString(),
      });
      return;
    }
    if (request.path === "/tool-governance/hook/execute") {
      executeCalls += 1;
      jsonResponse(response, 200, {
        status: "ok",
        result: { status: "ok" },
        resultDigest: RESULT_DIGEST,
        receiptRef: {
          run_id: "run-test",
          tool_call_id: "once-call",
          invocation_digest: INVOCATION_DIGEST,
        },
        effectEvidenceDigest: EFFECT_DIGEST,
      });
      return;
    }
    jsonResponse(response, 200, {
      status: "recorded",
      authoritative: false,
    });
  });
  const fake = setupPlugin(mock.baseUrl);
  try {
    await registerSession(fake);
    const input = beforeEvent({
      toolName: "veyra_shell_probe",
      params: { argv: ["true"] },
      toolCallId: "once-call",
    });
    assert.equal(
      await fake.hooks.get("before_tool_call")(input.event, input.ctx),
      undefined,
    );
    const tool = toolsForSession(fake).get("veyra_shell_probe");
    await tool.execute("once-call", { argv: ["true"] });
    await assert.rejects(
      tool.execute("once-call", { argv: ["true"] }),
      /execution token is unavailable/,
    );
    assert.equal(executeCalls, 1);
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("non-governed runs keep native behavior but cannot call Veyra tools", async () => {
  const mock = await createMockVeyra((_request, response) => {
    jsonResponse(response, 500, { detail: "must not be called" });
  });
  const fake = setupPlugin(mock.baseUrl);
  try {
    const native = beforeEvent({
      toolName: "read",
      sessionKey: "ordinary-session",
      toolCallId: "ordinary-read",
    });
    assert.equal(
      await fake.hooks.get("before_tool_call")(native.event, native.ctx),
      undefined,
    );

    const custom = beforeEvent({
      toolName: "veyra_file_read",
      sessionKey: "ordinary-session",
      toolCallId: "ordinary-custom",
    });
    const blocked = await fake.hooks.get("before_tool_call")(
      custom.event,
      custom.ctx,
    );
    assert.equal(blocked.block, true);
    assert.match(blocked.blockReason, /registered governed session/);
    assert.equal(mock.requests.length, 0);
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("after-tool observation serializes only a bounded non-authoritative digest", async () => {
  let observation;
  const mock = await createMockVeyra((request, response) => {
    if (request.path === "/tool-governance/hook/preflight") {
      jsonResponse(response, 200, {
        allow: true,
        canonicalToolName: "file.read",
        invocationDigest: INVOCATION_DIGEST,
        reservationId: "reservation-observe",
        reservationToken: "reservation-token-observe",
        executionToken: "execution-observe",
        expiresAt: new Date(Date.now() + 30_000).toISOString(),
      });
      return;
    }
    if (request.path === "/tool-governance/hook/execute") {
      jsonResponse(response, 200, {
        status: "ok",
        result: { status: "ok" },
        resultDigest: RESULT_DIGEST,
        receiptRef: {
          run_id: "run-test",
          tool_call_id: "bounded-call",
          invocation_digest: INVOCATION_DIGEST,
        },
        effectEvidenceDigest: EFFECT_DIGEST,
      });
      return;
    }
    if (request.path === "/tool-governance/hook/observe") {
      observation = request;
      jsonResponse(response, 200, {
        status: "recorded",
        authoritative: false,
      });
      return;
    }
    jsonResponse(response, 404, { detail: "not found" });
  });
  const fake = setupPlugin(mock.baseUrl);
  try {
    await registerSession(fake);
    const input = beforeEvent({
      toolName: "veyra_file_read",
      params: { path: "bounded.txt" },
      toolCallId: "bounded-call",
    });
    await fake.hooks.get("before_tool_call")(input.event, input.ctx);
    const result = await toolsForSession(fake)
      .get("veyra_file_read")
      .execute("bounded-call", { path: "bounded.txt" });

    const cyclic = {
      result,
      huge_secret: "TOP_SECRET_".repeat(200_000),
    };
    cyclic.self = cyclic;
    await fake.hooks.get("after_tool_call")(
      {
        ...input.event,
        result: cyclic,
        durationMs: 9,
      },
      input.ctx,
    );

    assert.ok(observation);
    const serialized = JSON.stringify(observation.body);
    assert.ok(Buffer.byteLength(serialized) < 4096);
    assert.equal(serialized.includes("TOP_SECRET_"), false);
    assert.equal(Object.hasOwn(observation.body, "authoritative"), false);
    assert.equal(observation.body.outcome, "completed");
    assert.equal(observation.body.resultDigest.length, 64);
    assert.equal(
      observation.body.paramsDigest,
      sha256('{"path":"bounded.txt"}'),
    );

    const directDigest = boundedDigest(cyclic);
    assert.equal(directDigest.algorithm, "sha256");
    assert.equal(directDigest.digest.length, 64);
    assert.equal(directDigest.truncated, true);
    assert.ok(directDigest.serialized_bytes <= 8192);
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("gateway canary attestation uses the active session token without exposing it", async () => {
  const mock = await createMockVeyra((request, response) => {
    assert.equal(
      request.path,
      "/tool-governance/hook/canary/attest",
    );
    assert.equal(
      request.headers["x-veyra-dispatch-token"],
      "dispatch-secret-not-persisted",
    );
    assert.deepEqual(request.body, {
      runId: "run-test",
      sessionKey: "agent-exec:test-task",
      sentinelRelativePath: "sentinel.txt",
      nativeBlockPath: "native-block.txt",
      nativeBlockContent: "must-not-be-written",
      pluginProtocol: "veyra.openclaw.governance.v1",
      pluginImplementationRevision:
        "veyra.openclaw.governance.phase6.v1",
    });
    jsonResponse(response, 200, {
      status: "validated",
      validated_at: "2026-07-27T08:00:00+00:00",
      implementation: {
        plugin_protocol: "veyra.openclaw.governance.v1",
        plugin_implementation_revision:
          "veyra.openclaw.governance.phase6.v1",
      },
      dispatchToken: "server-response-token-must-be-ignored",
    });
  });
  const fake = setupPlugin(mock.baseUrl);
  try {
    await registerSession(fake);
    assert.equal(
      fake.gatewayScopes.get("veyra.governance.attestCanary"),
      "operator.write",
    );
    const attested = await invokeGateway(
      fake,
      "veyra.governance.attestCanary",
      {
        sessionKey: "agent-exec:test-task",
        sentinelRelativePath: "sentinel.txt",
        nativeBlockPath: "native-block.txt",
        nativeBlockContent: "must-not-be-written",
      },
    );
    assert.equal(attested.ok, true);
    assert.deepEqual(attested.payload, {
      status: "validated",
      runId: "run-test",
      sessionKey: "agent-exec:test-task",
      validatedAt: "2026-07-27T08:00:00+00:00",
    });
    const serialized = JSON.stringify(attested);
    assert.equal(
      serialized.includes("dispatch-secret-not-persisted"),
      false,
    );
    assert.equal(
      serialized.includes("server-response-token-must-be-ignored"),
      false,
    );
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("gateway methods use write/read scopes and cancellation removes authority", async () => {
  const mock = await createMockVeyra((_request, response) => {
    jsonResponse(response, 500, { detail: "must not be called" });
  });
  const fake = setupPlugin(mock.baseUrl);
  try {
    assert.equal(
      fake.gatewayScopes.get("veyra.governance.registerSession"),
      "operator.write",
    );
    assert.equal(
      fake.gatewayScopes.get("veyra.governance.status"),
      "operator.read",
    );
    assert.equal(
      fake.gatewayScopes.get("veyra.governance.cancelSession"),
      "operator.write",
    );
    await registerSession(fake);
    const cancelled = await invokeGateway(
      fake,
      "veyra.governance.cancelSession",
      {
        sessionKey: "agent-exec:test-task",
        runId: "run-test",
        bindingDigest: BINDING_DIGEST,
      },
    );
    assert.equal(cancelled.ok, true);
    assert.deepEqual(cancelled.payload, {
      cancelled: true,
      idempotent: false,
      sessionKey: "agent-exec:test-task",
      runId: "run-test",
    });
    const repeated = await invokeGateway(
      fake,
      "veyra.governance.cancelSession",
      {
        sessionKey: "agent-exec:test-task",
        runId: "run-test",
        bindingDigest: BINDING_DIGEST,
      },
    );
    assert.equal(repeated.ok, true);
    assert.deepEqual(repeated.payload, {
      cancelled: true,
      idempotent: true,
      sessionKey: "agent-exec:test-task",
      runId: "run-test",
    });
    const status = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(status.payload.metrics.cancellations, 1);

    const call = beforeEvent({
      toolName: "read",
      toolCallId: "after-cancel",
    });
    const decision = await fake.hooks.get("before_tool_call")(
      call.event,
      call.ctx,
    );
    assert.equal(decision.block, true);
    assert.match(decision.blockReason, /fail-closed/);
  } finally {
    cleanupPlugin(fake);
    await mock.close();
  }
});

test("a delayed cancellation cannot cancel a replacement run on the same session key", async () => {
  const fake = setupPlugin("http://127.0.0.1:9");
  const replacementDigest = "f".repeat(64);
  try {
    await registerSession(fake);
    await registerSession(fake, {
      runId: "run-replacement",
      dispatchToken: "replacement-dispatch-secret",
      bindingDigest: replacementDigest,
    });

    const stale = await invokeGateway(
      fake,
      "veyra.governance.cancelSession",
      {
        sessionKey: "agent-exec:test-task",
        runId: "run-test",
        bindingDigest: BINDING_DIGEST,
      },
    );
    assert.equal(stale.ok, false);
    assert.equal(stale.error.code, "run_binding_mismatch");

    const beforeExactCancel = await invokeGateway(
      fake,
      "veyra.governance.status",
    );
    assert.equal(beforeExactCancel.payload.active_sessions, 1);
    assert.equal(beforeExactCancel.payload.metrics.replacements, 1);
    assert.equal(beforeExactCancel.payload.metrics.cancellations, 0);

    const exact = await invokeGateway(
      fake,
      "veyra.governance.cancelSession",
      {
        sessionKey: "agent-exec:test-task",
        runId: "run-replacement",
        bindingDigest: replacementDigest,
      },
    );
    assert.equal(exact.ok, true);
    assert.equal(exact.payload.cancelled, true);
    assert.equal(exact.payload.idempotent, false);
  } finally {
    cleanupPlugin(fake);
  }
});
