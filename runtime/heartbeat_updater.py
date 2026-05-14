from core.runtime_entity import RuntimeEntity


class HeartbeatUpdater:
    def __init__(self, runtime_entity: RuntimeEntity) -> None:
        self.runtime_entity = runtime_entity

    def tick(self) -> None:
        self.runtime_entity.set_status(self.runtime_entity.lifecycle.status)
