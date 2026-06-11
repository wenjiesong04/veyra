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
        lowered = tail.lower()
        error_count = sum(lowered.count(marker) for marker in ("error", "traceback", "exception", "critical", "fatal"))
        warning_count = sum(lowered.count(marker) for marker in ("warning", "warn"))
        anomaly = error_count >= 5
        return probe_payload(
            probe="log_probe",
            target=str(target),
            status="anomaly" if anomaly else "ok",
            summary=(
                f"Log file {target} shows {error_count} error markers." if anomaly else f"Log file {target} was read."
            ),
            confidence=0.9,
            ttl_seconds=30,
            details={
                "path": str(target),
                "exists": True,
                "error_count": error_count,
                "warning_count": warning_count,
                "anomaly": anomaly,
                "tail": tail,
            },
        )
