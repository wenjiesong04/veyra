import subprocess

from probes.schema import probe_payload


class ProcessProbe:
    def run(self, text: str = "") -> dict:
        result = subprocess.run(["ps", "-axo", "pid,comm"], capture_output=True, text=True, check=False)
        lines = result.stdout.splitlines()[:40]
        status = "ok" if result.returncode == 0 else "error"
        return probe_payload(
            probe="process_probe",
            target="local_processes",
            status=status,
            summary=f"Process probe returned {max(len(lines) - 1, 0)} visible rows.",
            confidence=0.85 if status == "ok" else 0.4,
            ttl_seconds=15,
            details={"processes": lines, "returncode": result.returncode, "stderr": result.stderr},
        )
