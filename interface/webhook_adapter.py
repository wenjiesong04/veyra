from interface.intake_gateway import IntakeGateway


class WebhookAdapter:
    def __init__(self, gateway: IntakeGateway) -> None:
        self.gateway = gateway
