from pathlib import Path

from probes.schema import probe_payload


class FileProbe:
    def run(self, path: str) -> dict:
        target = Path(path)
        exists = target.exists()
        return probe_payload(
            probe="file_probe",
            target=str(target),
            status="ok",
            summary=f"File {target} exists={exists}.",
            confidence=0.95,
            ttl_seconds=30,
            details={"path": str(target), "exists": exists, "is_file": target.is_file() if exists else False},
        )
