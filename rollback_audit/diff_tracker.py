import subprocess


class DiffTracker:
    def git_diff(self) -> dict:
        result = subprocess.run(["git", "diff", "--stat"], capture_output=True, text=True, check=False)
        return {"status": "ok" if result.returncode == 0 else "error", "diff_stat": result.stdout, "stderr": result.stderr}

    def git_diff_text(self) -> dict:
        result = subprocess.run(["git", "diff"], capture_output=True, text=True, check=False)
        return {"status": "ok" if result.returncode == 0 else "error", "diff": result.stdout, "stderr": result.stderr}
