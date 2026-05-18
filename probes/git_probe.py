import subprocess

from probes.schema import probe_payload


class GitProbe:
    def run(self, text: str = "") -> dict:
        result = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, check=False)
        dirty = bool(result.stdout.strip())
        status = "ok" if result.returncode == 0 else "error"
        return probe_payload(
            probe="git_probe",
            target="workspace",
            status=status,
            summary="Git workspace has changes." if dirty else "Git workspace is clean.",
            confidence=0.95 if status == "ok" else 0.5,
            ttl_seconds=20,
            details={
                "dirty": dirty,
                "short_status": result.stdout.splitlines(),
                "stderr": result.stderr,
                "returncode": result.returncode,
            },
        )
