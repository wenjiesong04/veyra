from __future__ import annotations

import json
from pathlib import Path


class SkillLoader:
    def __init__(self, root: str = "skills") -> None:
        self.root = Path(root)

    def load(self, name: str) -> dict:
        registry_path = self.root / "registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {"builtins": []}
        if name not in registry.get("builtins", []):
            return {"name": name, "status": "missing"}
        return {"name": name, "status": "available", "path": str(self.root / "builtins" / f"{name}.yaml")}
