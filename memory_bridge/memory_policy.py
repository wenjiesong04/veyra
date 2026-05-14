class MemoryPolicy:
    def allow_write(self, patch: dict) -> bool:
        text = str(patch)
        return ".env" not in text and "api_key" not in text.lower()
