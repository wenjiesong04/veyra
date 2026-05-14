import subprocess

from interface.event_schema import utc_now_iso


class ProcessProbe:
    def run(self, text: str = "") -> dict:
        result = subprocess.run(["ps", "-axo", "pid,comm"], capture_output=True, text=True, check=False)
        lines = result.stdout.splitlines()[:40]
        return {
            "probe": "process_probe",
            "status": "ok" if result.returncode == 0 else "error",
            "processes": lines,
            "timestamp": utc_now_iso(),
            "summary": f"Process probe returned {max(len(lines) - 1, 0)} visible rows.",
        }
