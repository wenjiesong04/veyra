class MemoryPatchBuilder:
    def build(self, task: dict, result: dict) -> dict:
        return {"task": task, "result_summary": result.get("status")}
