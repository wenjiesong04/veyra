from pathlib import Path
from shutil import copy2


class Snapshot:
    def create_file_snapshot(self, path: str, snapshot_dir: str = "state/snapshots") -> dict:
        source = Path(path)
        target_dir = Path(snapshot_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            return {"status": "missing", "path": str(source)}
        target = target_dir / source.name
        copy2(source, target)
        return {"status": "created", "source": str(source), "snapshot": str(target)}
