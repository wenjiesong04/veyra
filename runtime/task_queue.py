from collections import deque


class TaskQueue:
    def __init__(self) -> None:
        self._queue = deque()

    def push(self, task: dict) -> None:
        self._queue.append(task)

    def pop(self) -> dict | None:
        return self._queue.popleft() if self._queue else None
