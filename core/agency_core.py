class AgencyCore:
    def detect_state_gap(self, goals: dict, world_state: dict) -> list[dict]:
        gaps = []
        if goals.get("selected_agent_must_be_available") and world_state.get("executor_state", {}).get("status") != "available":
            gaps.append({"goal": "selected_agent_available", "next_action": "probe_executor_status"})
        return gaps
