"""List the zoning runs in a folder for the results viewer, as <folder>/index.json.

The viewer can't list a folder on a static site, so it reads this index instead. Every JSON file
written by `zoning_db.py export --format json` is included; other files are skipped with a note. A run
found in several files (e.g. attached to two releases) is listed once. Standard library only, so the
publishing workflow can run it without installing anything.

    python site/build_index.py site/data
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
    runs = {}
    for path in sorted(folder.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"Skipping {path.name}: not readable JSON ({e})")
            continue
        if not isinstance(data, dict) or data.get("format") != "zoning-run":
            print(f"Skipping {path.name}: not a zoning results export")
            continue
        if data.get("version") != 1:
            print(f"Skipping {path.name}: format version {data.get('version')}, which the viewer doesn't know")
            continue
        run, document = data["run"], data["document"]
        has_document = bool(document.get("file")) and (folder / document["file"]).is_file()
        if document.get("file") and not has_document:
            print(f"Note: {path.name} goes with {document['file']}, which wasn't published; its pages won't be links")
        key = (run["place_kind"], run["place"], run["id"], run["started_at"])
        runs[key] = {
            "file": path.name, "place": run["place"], "place_kind": run["place_kind"], "run_id": run["id"],
            "started_at": run["started_at"], "status": run["status"], "provider": run["provider"],
            "model": run["model"], "effort": run["effort"], "zones": len(data["zones"]),
            "zones_requested": run["zones_requested"], "zones_from": run["zones_from"], "document": has_document,
        }
    entries = sorted(runs.values(), key=lambda r: r["started_at"], reverse=True)
    index = {"generated_at": datetime.now(timezone.utc).isoformat(), "runs": entries}
    (folder / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Listed {len(entries)} run(s) in {folder / 'index.json'}")


if __name__ == "__main__":
    main()
