class ConfirmationGate:
    def request(self, action: dict) -> dict:
        return {"status": "pending", "action": action}
