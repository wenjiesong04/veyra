from interface.intake_gateway import IntakeGateway


class CliAdapter:
    def __init__(self, gateway: IntakeGateway) -> None:
        self.gateway = gateway

    def handle(self, text: str):
        return self.gateway.receive_text(text=text, channel="cli")
