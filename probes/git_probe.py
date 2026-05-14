import subprocess

from interface.event_schema import utc_now_iso


class GitProbe:
    def run(self, text: str = "") -> dict:
        result = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, check=False)
        dirty = bool(result.stdout.strip())
        return {
            "probe": "git_probe",
            "status": "ok" if result.returncode == 0 else "error",
            "dirty": dirty,
            "short_status": result.stdout.splitlines(),
            "stderr": result.stderr,
            "timestamp": utc_now_iso(),
            "summary": "Git workspace has changes." if dirty else "Git workspace is clean.",
        }
