from shutil import copy2


class Restore:
    def restore(self, snapshot: dict) -> dict:
        if snapshot.get("status") != "created":
            return {"status": "not_restorable", "snapshot": snapshot}
        copy2(snapshot["snapshot"], snapshot["source"])
        return {"status": "restored", "source": snapshot["source"]}
