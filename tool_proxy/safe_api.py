class SafeAPI:
    def request(self, payload: dict) -> dict:
        return {"status": "not_configured", "payload": payload}
