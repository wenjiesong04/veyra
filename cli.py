from core.awareness_loop import AwarenessLoop
from core.runtime_entity import RuntimeEntity
from core.world_state import WorldStateStore
from interface.intake_gateway import IntakeGateway


def main() -> None:
    state_store = WorldStateStore()
    runtime = RuntimeEntity(state_store)
    gateway = IntakeGateway(AwarenessLoop(state_store, runtime))
    text = input("Veyra> ").strip()
    if not text:
        return
    print(gateway.receive_text(text).to_dict())


if __name__ == "__main__":
    main()
