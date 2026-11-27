"""Merge a GitHub /traffic/clones response into the checked-out traffic-stats branch."""
import json
import sys
from pathlib import Path


def update(snapshot: Path, stats: Path) -> None:
    history_path = stats / "clones.json"
    # Require an existing ledger: a missing file must not silently reset the total.
    history = json.loads(history_path.read_text())
    for day in json.loads(snapshot.read_text())["clones"]:
        count, uniques = day["count"], day["uniques"]
        if type(count) is not int or type(uniques) is not int or not 0 <= uniques <= count:
            raise ValueError(f"Invalid daily traffic: {day!r}")
        history[day["timestamp"]] = {"count": count, "uniques": uniques}

    # Daily uniques overlap; only clone counts are additive across windows.
    total = sum(day["count"] for day in history.values())
    badge = {"schemaVersion": 1, "label": "Clones", "message": str(total), "color": "0ea5e9"}
    history_path.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n")
    (stats / "badge.json").write_text(json.dumps(badge) + "\n")


if __name__ == "__main__":
    update(Path(sys.argv[1]), Path(sys.argv[2]))
