// Behavioural contract for the server-owned proactive conversation envelope.
// Keep this fixture independent from source-text/includes checks: it exercises
// the user-visible state transitions that the React projection must preserve.

const supportedLabels = new Set(["useful", "not_useful", "too_early", "too_frequent", "resolved"]);
const buttonLabels = ["useful", "not_useful", "too_early", "too_frequent", "resolved"];

function projectFeedbackState(message, localLabel) {
  const metadata = message?.metadata && typeof message.metadata === "object" ? message.metadata : {};
  const selected = supportedLabels.has(localLabel)
    ? localLabel
    : supportedLabels.has(metadata.feedback_label)
      ? metadata.feedback_label
      : undefined;
  const hypothesis = message?.kind === "proactive" && message?.source === "living_reaction" && metadata.epistemic_status === "hypothesis";
  const recorded = Boolean(selected);
  const closed = metadata.feedback_available === false || recorded;
  const controls = message?.kind === "proactive"
    && message?.source === "living_reaction"
    && metadata.feedback_available === true
    && typeof metadata.reaction_id === "string"
    && typeof metadata.situation_id === "string"
    && Number.isSafeInteger(metadata.situation_revision)
    && metadata.situation_revision >= 1
    && !closed;
  return { selected, hypothesis, closed, recorded, controls };
}

function updatedMessageFromFeedback(response, messageId) {
  const candidate = response?.updated?.message ?? response?.message;
  return candidate?.message_id === messageId ? candidate : undefined;
}

export function runProductProactiveContract() {
  if (JSON.stringify([...supportedLabels]) !== JSON.stringify(buttonLabels)) {
    throw new Error("proactive feedback button labels changed unexpectedly");
  }
  const fixture = {
    message_id: "proactive_fixture_1",
    kind: "proactive",
    source: "living_reaction",
    metadata: {
      reaction_id: "reaction_fixture_1",
      situation_id: "situation_fixture_1",
      situation_revision: 4,
      feedback_available: true,
      epistemic_status: "hypothesis",
    },
  };
  const initial = projectFeedbackState(fixture);
  if (!initial.hypothesis || !initial.controls || initial.selected !== undefined) {
    throw new Error("proactive fixture did not expose hypothesis and feedback controls");
  }

  const response = {
    status: "recorded",
    updated: {
      message: {
        ...fixture,
        metadata: { ...fixture.metadata, feedback_available: false, feedback_label: "useful", feedback_at: "2026-08-23T00:00:00Z" },
      },
    },
  };
  const updated = updatedMessageFromFeedback(response, fixture.message_id);
  const after = projectFeedbackState(updated);
  if (!updated || after.selected !== "useful" || !after.closed || after.controls) {
    throw new Error("feedback response did not close the proactive affordance");
  }

  const stale = projectFeedbackState({ ...fixture, metadata: { ...fixture.metadata, feedback_available: false, feedback_label: "too_early" } });
  if (stale.controls || stale.selected !== "too_early") {
    throw new Error("server-projected stale feedback remained clickable");
  }
  const unavailable = projectFeedbackState({ ...fixture, metadata: { ...fixture.metadata, feedback_available: false } });
  if (!unavailable.closed || unavailable.recorded || unavailable.selected !== undefined) {
    throw new Error("closed feedback without a label was treated as recorded");
  }
  const localWins = projectFeedbackState({ ...fixture, metadata: { ...fixture.metadata, feedback_label: "not_useful" } }, "resolved");
  if (localWins.selected !== "resolved") {
    throw new Error("local feedback did not take precedence over server metadata");
  }
  return true;
}
