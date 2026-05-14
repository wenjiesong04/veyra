from pathlib import Path


class FileProbe:
    def run(self, path: str) -> dict:
        target = Path(path)
        return {"probe": "file_probe", "path": str(target), "exists": target.exists(), "status": "ok"}
