class ToolPolicy:
    FORBIDDEN = ["rm -rf", "curl | bash", "drop database", "git push --force"]
    REQUIRES_REVIEW = ["sudo", "chmod -R", "chown -R", "restart", "重启"]

    def review_command(self, command: list[str] | str) -> dict:
        text = " ".join(command) if isinstance(command, list) else command
        if any(token in text for token in self.FORBIDDEN):
            return {"decision": "block", "reason": "forbidden command pattern"}
        if any(token in text for token in self.REQUIRES_REVIEW):
            return {"decision": "ask_user", "reason": "command requires human confirmation"}
        return {"decision": "allow"}
