class ExecutionController:
    def dispatch(self, route: str, payload: dict) -> dict:
        return {"route": route, "payload": payload, "status": "dispatch_not_configured"}
