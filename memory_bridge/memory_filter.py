from memory_bridge.memory_policy import MemoryPolicy


class MemoryFilter:
    def filter(self, patch: dict) -> dict | None:
        if not MemoryPolicy().allow_write(patch):
            return None
        filtered = dict(patch)
        filtered.setdefault("freshness", "fresh")
        filtered.setdefault("trust", "observed")
        return filtered
