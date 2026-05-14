class CapabilityEvaluator:
    def needs_agent(self, task: dict) -> bool:
        return task.get("complexity") == "complex"
