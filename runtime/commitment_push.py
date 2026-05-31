from __future__ import annotations

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
        status = "success" if processed and all(item.get("status") == "sent" for item in processed) else ("idle" if not processed else "degraded")
        result = {"status": status, "reason": reason, "due_count": len(due), "processed_count": len(processed), "processed": processed}
        self.state_store.append_jsonl(
            "action_record.jsonl",
            {"route": "commitment_push_due", "status": status, "artifacts": {"reason": reason, "processed_count": len(processed)}},
        )
        return result

    def _push_one(self, commitment: dict[str, Any], *, reason: str) -> dict[str, Any]:
        commitment_id = str(commitment.get("commitment_id"))
        message = self._build_message(commitment)
        if not message:
            return {"commitment_id": commitment_id, "status": "skipped", "reason": "empty_message"}

        decision = Decision(
            route=Route.DIRECT_ANSWER,
            risk_level=RiskLevel.R0,
            reason="proactive commitment push",
            intent="information",
            complexity="simple",
            capability="native_answer",
            signals=["commitment:push", f"kind:{commitment.get('kind')}"],
            constraints=["read-only proactive notification", "no destructive side effects"],
        )
        foresight = self.foresight.predict_text_action(message, RiskLevel.R0, decision=decision.to_dict())
        guardian = self.guardian.review_text_action(text=message, decision=decision, foresight=foresight)
        if guardian.get("decision") not in {"allow", "allow_with_constraints"}:
            self.commitment_core.record_push(
                commitment_id,
                push_result={"status": "blocked", "guardian": guardian.get("decision")},
                message=message,
            )
            return {
                "commitment_id": commitment_id,
                "status": "blocked",
                "guardian": guardian,
                "reason": guardian.get("reason"),
            }

        channel = str(commitment.get("channel") or "api")
        session_id = str(commitment.get("session_id") or "local-session")
        delivery = ChannelAdapter(self.state_store, channel=channel).send(
            session_id,
            message,
            metadata={
                "route": "commitment_push",
                "commitment_id": commitment_id,
                "kind": commitment.get("kind"),
                "push_reason": reason,
                "risk_level": RiskLevel.R0.value,
                "guardian": guardian.get("decision"),
                "pushed_at": utc_now_iso(),
            },
        )
        self.commitment_core.record_push(commitment_id, push_result=delivery, message=message)
        return {
            "commitment_id": commitment_id,
            "status": delivery.get("status"),
            "delivery_status": delivery.get("delivery_status") or delivery.get("status"),
            "channel": channel,
            "session_id": session_id,
            "guardian": guardian.get("decision"),
            "message_preview": message[:240],
        }

    def _build_message(self, commitment: dict[str, Any]) -> str:
        kind = str(commitment.get("kind") or "")
        payload = commitment.get("payload") if isinstance(commitment.get("payload"), dict) else {}
        title = str(commitment.get("title") or "提醒")
        if kind == "weather_daily":
            location = str(payload.get("location") or "北京")
            raw = self.weather_probe.run(f"{location}天气")
            summary = str(raw.get("summary") or "暂时无法获取天气。")
            return f"【每日天气】{summary}"
        if kind == "learning_digest":
            topic = str(payload.get("topic") or "学习")
            phase = str(payload.get("phase") or "进行中")
            tips = [
                "回顾上一轮笔记并列出 3 个不懂的概念",
                "完成一小节练习并记录错题",
                "用你自己的话总结今天学到的要点",
            ]
            tip = tips[int(commitment.get("run_count") or 0) % len(tips)]
            return f"【学习辅导·{topic}】当前阶段：{phase}。今日建议：{tip}。如需调整计划或推送频率，直接告诉我即可。"
        note = str(payload.get("note") or title)
        return f"【定时提醒】{note}"
