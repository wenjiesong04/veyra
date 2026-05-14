from memory_bridge.memory_policy import MemoryPolicy


class MemoryFilter:
    def filter(self, patch: dict) -> dict | None:
        return patch if MemoryPolicy().allow_write(patch) else None
