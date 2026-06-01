from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.commitment_core import CommitmentCore
from core.definitions import RiskLevel
from core.foresight_engine import ForesightEngine
from core.guardian_controller import GuardianController
from core.world_state import WorldStateStore
from interface.channel_adapter import ChannelAdapter
from interface.event_schema import Decision, Route, utc_now_iso
from probes.weather_probe import WeatherProbe


class CommitmentPushRuntime:
    """Execute due user commitments with Guardian-gated outbound delivery."""

    DELIVERY_ADVANCE_STATUSES = {"sent", "delivered", "provider_sent"}
    DELIVERY_QUEUE_STATUSES = {"queued", "local_queued"}
    DELIVERY_ATTENTION_STATUSES = {"not_configured"}

    def __init__(
        self,
        *,
        state_store: WorldStateStore,
        commitment_core: CommitmentCore,
        guardian: GuardianController | None = None,
        foresight: ForesightEngine | None = None,
    ) -> None:
        self.state_store = state_store
        self.commitment_core = commitment_core
        self.guardian = guardian or GuardianController()
        self.foresight = foresight or ForesightEngine()
        self.weather_probe = WeatherProbe()

    def run_due(self, *, limit: int = 10, reason: str = "scheduled") -> dict[str, Any]:
        due = self.commitment_core.due_commitments(limit=limit)
        processed: list[dict[str, Any]] = []
        for commitment in due:
            processed.append(self._push_one(commitment, reason=reason))
        delivered = [item for item in processed if item.get("status") == "delivered"]
        queued = [item for item in processed if item.get("status") == "queued"]
        if not processed:
            status = "idle"
        elif len(delivered) == len(processed):
            status = "success"
        elif delivered or queued:
            status = "degraded"
        else:
            status = "degraded"
        result = {
            "status": status,
            "reason": reason,
            "due_count": len(due),
            "processed_count": len(processed),
            "delivered_count": len(delivered),
            "queued_count": len(queued),
            "processed": processed,
        }
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {"route": "commitment_push_due", "status": status, "artifacts": {"reason": reason, "processed_count": len(processed), "due_count": len(due)}},
        )
        return result

    def _push_one(self, commitment: dict[str, Any], *, reason: str) -> dict[str, Any]:
        commitment_id = str(commitment.get("commitment_id"))
        fresh = self.commitment_core.get_commitment(commitment_id) or commitment
        pushable, skip_reason = self.commitment_core.pushable_reason(fresh)
        if not pushable:
            result = {"commitment_id": commitment_id, "status": "skipped", "reason": skip_reason}
            self.commitment_core.touch_attempt(commitment_id)
            self._audit_push_attempt(commitment_id, result, reason=reason)
            return result

        message, message_context = self._build_message(fresh)
        if not message:
            result = {"commitment_id": commitment_id, "status": "skipped", "reason": "empty_message"}
            self._audit_push_attempt(commitment_id, result, reason=reason)
            return result

        decision = Decision(
            route=Route.DIRECT_ANSWER,
            risk_level=RiskLevel.R0,
            reason="proactive commitment push",
            intent="information",
            complexity="simple",
            capability="native_answer",
            signals=["commitment:push", f"kind:{fresh.get('kind')}"],
            constraints=["read-only proactive notification", "no destructive side effects"],
        )
        foresight = self.foresight.predict_text_action(message, RiskLevel.R0, decision=decision.to_dict())
        guardian = self.guardian.review_text_action(text=message, decision=decision, foresight=foresight)
        if guardian.get("decision") not in {"allow", "allow_with_constraints"}:
            self.commitment_core.touch_attempt(commitment_id)
            self.commitment_core.record_push(
                commitment_id,
                push_result={"status": "blocked", "guardian": guardian.get("decision")},
                message=message,
                advance_schedule=False,
                count_run=False,
            )
            result = {
                "commitment_id": commitment_id,
                "status": "blocked",
                "guardian": guardian.get("decision"),
                "reason": guardian.get("reason"),
                "weather_probe_status": self._weather_probe_status(message_context),
                "candidate_id": self._candidate_id(message_context),
            }
            self._mark_push_candidate(message_context, "blocked")
            self._audit_push_attempt(commitment_id, result, reason=reason)
            return result

        channel = str(fresh.get("channel") or "api")
        session_id = str(fresh.get("session_id") or "local-session")
        delivery = ChannelAdapter(self.state_store, channel=channel).send(
            session_id,
            message,
            metadata={
                "route": "commitment_push",
                "commitment_id": commitment_id,
                "kind": fresh.get("kind"),
                "push_reason": reason,
                "risk_level": RiskLevel.R0.value,
                "guardian": guardian.get("decision"),
                "pushed_at": utc_now_iso(),
            },
        )
        delivery_status = str(delivery.get("delivery_status") or delivery.get("status") or "")
        normalized_status = self._delivery_status(delivery)
        if normalized_status != "delivered":
            self.commitment_core.touch_attempt(commitment_id)
            self.commitment_core.record_push(
                commitment_id,
                push_result={"status": normalized_status, "delivery_status": delivery_status, "delivery": delivery},
                message=message,
                advance_schedule=False,
                count_run=False,
            )
            self._mark_push_candidate(message_context, normalized_status)
            result = {
                "commitment_id": commitment_id,
                "status": normalized_status,
                "delivery_status": delivery_status or delivery.get("status"),
                "delivery": delivery,
                "guardian": guardian.get("decision"),
                "message_preview": message[:240],
                "weather_probe_status": self._weather_probe_status(message_context),
                "candidate_id": self._candidate_id(message_context),
            }
            self._audit_push_attempt(commitment_id, result, reason=reason)
            return result

        updated = self.commitment_core.record_push(commitment_id, push_result={**delivery, "status": "delivered"}, message=message)
        self._mark_push_candidate(message_context, "delivered")
        result = {
            "commitment_id": commitment_id,
            "status": "delivered",
            "delivery_status": delivery_status or delivery.get("status"),
            "channel": channel,
            "session_id": session_id,
            "guardian": guardian.get("decision"),
            "message_preview": message[:240],
            "weather_probe_status": self._weather_probe_status(message_context),
            "candidate_id": self._candidate_id(message_context),
            "next_run_at": (updated or {}).get("next_run_at"),
            "push_history_count": len((updated or {}).get("push_history") or []),
        }
        self._audit_push_attempt(commitment_id, result, reason=reason)
        return result

    def _build_message(self, commitment: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        kind = str(commitment.get("kind") or "")
        payload = commitment.get("payload") if isinstance(commitment.get("payload"), dict) else {}
        title = str(commitment.get("title") or "提醒")
        if kind == "weather_daily":
            location = str(payload.get("location") or "北京")
            raw = self.weather_probe.run(f"{location}天气")
            summary = str(raw.get("summary") or "暂时无法获取天气。")
            if str(raw.get("status") or "") in {"missing_target", "unavailable", "error", "http_error"}:
                summary = f"暂时无法获取{location}天气（{raw.get('status')}）"
            return f"【每日天气】{summary}", raw
        if kind == "learning_digest":
            topic = str(payload.get("topic") or "学习")
            phase = str(payload.get("phase") or "进行中")
            candidate = self._next_push_candidate(str(commitment.get("commitment_id") or ""))
            if candidate:
                title_text = str(candidate.get("title") or topic)
                url = str(candidate.get("url") or "")
                snippet = str(candidate.get("snippet") or "")
                message = f"【学习资料·{topic}】{title_text}\n{snippet[:220]}\n链接：{url}\n如需调整计划或推送频率，直接告诉我即可。"
                return message, {"kind": "external_candidate", "candidate_id": candidate.get("candidate_id")}
            tips = [
                "回顾上一轮笔记并列出 3 个不懂的概念",
                "完成一小节练习并记录错题",
                "用你自己的话总结今天学到的要点",
            ]
            tip = tips[int(commitment.get("run_count") or 0) % len(tips)]
            return f"【学习辅导·{topic}】当前阶段：{phase}。今日建议：{tip}。如需调整计划或推送频率，直接告诉我即可。", None
        note = str(payload.get("note") or title)
        return f"【定时提醒】{note}", None

    def _audit_push_attempt(self, commitment_id: str, result: dict[str, Any], *, reason: str) -> None:
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {
                "route": "commitment_push_attempt",
                "status": result.get("status"),
                "artifacts": {"commitment_id": commitment_id, "reason": reason, "result": result},
            },
        )

    def _delivery_status(self, delivery: dict[str, Any]) -> str:
        status_values = {str(delivery.get("status") or ""), str(delivery.get("delivery_status") or "")}
        if status_values & self.DELIVERY_ADVANCE_STATUSES:
            return "delivered"
        if status_values & self.DELIVERY_QUEUE_STATUSES:
            return "queued"
        if status_values & self.DELIVERY_ATTENTION_STATUSES:
            return "requires_user_attention"
        return "failed"

    def _weather_probe_status(self, message_context: dict[str, Any] | None) -> str | None:
        if isinstance(message_context, dict) and message_context.get("probe") == "weather_probe":
            return str(message_context.get("status") or "")
        return None

    def _candidate_id(self, message_context: dict[str, Any] | None) -> str | None:
        if isinstance(message_context, dict) and message_context.get("kind") == "external_candidate":
            return str(message_context.get("candidate_id") or "")
        return None

    def _next_push_candidate(self, commitment_id: str) -> dict[str, Any] | None:
        external = self.state_store.read_json("external_world.json")
        candidates = external.get("push_candidates") if isinstance(external.get("push_candidates"), list) else []
        eligible = [
            item
            for item in candidates
            if isinstance(item, dict) and item.get("commitment_id") == commitment_id and item.get("status") in {"new", "queued", "failed"}
        ]
        if not eligible:
            return None
        return sorted(eligible, key=lambda item: float(item.get("score") or 0), reverse=True)[0]

    def _mark_push_candidate(self, message_context: dict[str, Any] | None, status: str) -> None:
        candidate_id = self._candidate_id(message_context)
        if not candidate_id:
            return
        external = self.state_store.read_json("external_world.json")
        candidates = external.get("push_candidates") if isinstance(external.get("push_candidates"), list) else []
        changed = False
        for item in candidates:
            if not isinstance(item, dict) or item.get("candidate_id") != candidate_id:
                continue
            item["status"] = status
            item["updated_at"] = utc_now_iso()
            if status == "delivered":
                item["delivered_at"] = utc_now_iso()
            else:
                item["last_attempt_at"] = utc_now_iso()
            changed = True
            break
        if changed:
            external["push_candidates"] = candidates[-100:]
            self.state_store.write_json("external_world.json", external)
