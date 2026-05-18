from pathlib import Path

from probes.schema import probe_payload


class LogProbe:
    def run(self, path: str) -> dict:
        target = Path(path)
        if not target.exists():
            return probe_payload(
                probe="log_probe",
                target=str(target),
                status="missing",
                summary=f"Log file {target} is missing.",
                confidence=0.9,
                ttl_seconds=30,
                details={"path": str(target), "exists": False},
            )
        tail = target.read_text(encoding="utf-8", errors="ignore")[-4000:]
        return probe_payload(
            probe="log_probe",
            target=str(target),
            status="ok",
            summary=f"Log file {target} was read.",
            confidence=0.9,
            ttl_seconds=30,
            details={"path": str(target), "exists": True, "tail": tail},
        )
