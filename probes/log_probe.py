from pathlib import Path


class LogProbe:
    def run(self, path: str) -> dict:
        target = Path(path)
        if not target.exists():
            return {"probe": "log_probe", "status": "missing", "path": str(target)}
        return {"probe": "log_probe", "status": "ok", "path": str(target), "tail": target.read_text(encoding="utf-8", errors="ignore")[-4000:]}
