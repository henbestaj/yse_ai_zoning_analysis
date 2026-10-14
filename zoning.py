"""Build output/<town>/zones.csv: one row per zoning district used by the town's parcels, with the
fields from zoning_fields.toml filled in from the town's zoning document by a model.

Usage:
    python zoning.py --town new_haven [--doc PATH] [--zone-column NAME] [--fields PATH] [--refresh]

Run scraper.py for the town first. The document defaults to zoning_docs/<town>.pdf; --doc takes any
file. Results are reused, with no model calls, for zones already in zones.csv when that file is newer
than both the document and the fields file and came from the same document. --refresh redoes all.
"""

import argparse
import csv
import inspect
import re
import tomllib
from pathlib import Path

from common import BASE_DIR, OUTPUT_DIR, load_env, write_csv
from models import DECIDERS, EXTRACTORS, ModelError
from scraper import TOWNS

DOCS_DIR = BASE_DIR / "zoning_docs"
FIELDS_FILE = BASE_DIR / "zoning_fields.toml"
SKIP_FILES = {"zones.csv", "errors.csv"}
YES_NO = {"yes", "no", "y", "n", "true", "false", "0", "1"}
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .\-/]{0,11}$")  # e.g. RM, RM2, BD-1, CBD, I-L
MIN_CONFIDENCE = 0.6


def load_town_columns(town):
    """Return {(csv name, column name): [values]} for every CSV the scraper wrote for the town."""
    town_dir = OUTPUT_DIR / town
    if not town_dir.is_dir():
        raise SystemExit(f"No scraped data in {town_dir}; run scraper.py --town {town} first.")
    columns = {}
    for path in sorted(town_dir.glob("*.csv")):
        if path.name in SKIP_FILES:
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                for name, value in row.items():
                    columns.setdefault((path.name, name), []).append((value or "").strip())
    return columns


def looks_like_zone_codes(values):
    present = [v for v in values if v]
    if not present or all(v.lower() in YES_NO for v in present):
        return False
    return sum(bool(CODE.match(v)) for v in present) >= 0.9 * len(present)


def find_zone_column(town, columns, decider_name):
    """Rules first: a column named like 'zone' whose values look like codes. If that doesn't give
    exactly one answer, ask the decision model, and refuse to guess if it isn't confident."""
    candidates = [key for key, values in columns.items()
                  if re.search(r"zon", key[1], re.I) and looks_like_zone_codes(values)]
    if len(candidates) == 1:
        print(f"Zone column: {candidates[0][1]!r} in {candidates[0][0]} (by rules)")
        return candidates[0]

    pool = candidates or [key for key, values in columns.items() if looks_like_zone_codes(values)]
    if not pool:
        raise SystemExit("No column looks like zoning codes; pass --zone-column NAME.")
    print(f"Rules found {len(candidates)} candidate zone columns; asking {decider_name}...")
    keys = {f"c{i}": key for i, key in enumerate(pool, 1)}  # neutral option ids; column names may have odd characters
    options = {}
    for option, (file, name) in keys.items():
        samples = list(dict.fromkeys(v for v in columns[(file, name)] if v))[:8]
        options[option] = f"Column '{name}' in {file}. Sample values: {', '.join(samples)}"
    choice, confidence = DECIDERS[decider_name]().choose(
        state=f"Property records scraped from the {town.replace('_', ' ').title()} assessor website.",
        instructions="Which column holds each parcel's zoning district code (e.g. RM, RL, BA-1), "
                     "as defined in the town's zoning ordinance?",
        options=options)
    file, name = keys[choice]
    print(f"Zone column: {name!r} in {file} ({decider_name}, confidence {confidence:.2f})")
    if confidence < MIN_CONFIDENCE:
        raise SystemExit(f"Not confident enough ({confidence:.2f} < {MIN_CONFIDENCE}); pass --zone-column NAME.")
    return file, name


def build_schema(fields):
    properties = {name: {"type": ["string", "null"], "description": description}
                  for name, description in fields.items()}
    properties["found_in_document"] = {"type": "boolean",
                                       "description": "Whether the document defines this district at all"}
    properties["source_pages"] = {"type": "array", "items": {"type": "integer"},
                                  "description": "PDF page numbers (1 = first page of the file) the values came from"}
    properties["notes"] = {"type": "string",
                           "description": "Anything a reviewer should know: ambiguities, conditions, exceptions"}
    return {"type": "object", "properties": properties, "required": list(properties)}


def make_validator(fields):
    def validate(result):
        missing = [k for k in [*fields, "found_in_document", "source_pages", "notes"] if k not in result]
        if missing:
            raise ValueError(f"missing keys {missing}")
        bad = [k for k in fields if result[k] is not None and not isinstance(result[k], str)]
        if bad:
            raise ValueError(f"non-text values for {bad}")
        if not isinstance(result["found_in_document"], bool):
            raise ValueError("found_in_document is not true/false")
        if not all(isinstance(p, int) for p in result["source_pages"]):
            raise ValueError("source_pages must be integers")
    return validate


def build_prompt(town, zone, fields):
    field_lines = "\n".join(f"- {name}: {description}" for name, description in fields.items())
    return f"""This document is the zoning regulations for {town.replace('_', ' ').title()}, Connecticut.
Find the requirements for the zoning district with code "{zone}".

For each field below, give the value as the document states it for this district, keeping units and
conditions. Look in dimensional/bulk tables and use tables as well as the text. If the document does
not state a value for this district, return null. Do not guess and do not borrow values from other
districts, unless the document says this district follows another one's rules (then say so in notes).

Fields:
{field_lines}

Also return found_in_document (false if the document doesn't define district "{zone}" at all),
source_pages (page numbers of the file, 1 = first page, not printed page labels), and notes."""


def reusable_rows(zones_csv, document, fields_file, source, model, effort):
    """Rows from an existing zones.csv that are still valid: the file is newer than the document and
    the fields file, and the row came from the same document, model and effort as this run."""
    if not zones_csv.exists():
        return {}
    csv_time = zones_csv.stat().st_mtime
    if csv_time <= document.stat().st_mtime or csv_time <= fields_file.stat().st_mtime:
        print("zones.csv is older than the document or the fields file; re-querying all zones.")
        return {}
    with open(zones_csv, encoding="utf-8-sig", newline="") as f:
        rows = [row for row in csv.DictReader(f) if row.get("source_document") == source]
    # Rows saved before model/effort were recorded have neither, so they never match: redo them
    matching = {row["zone"]: row for row in rows if row.get("model") == model and row.get("effort") == effort}
    if len(matching) < len(rows):
        print(f"{len(rows) - len(matching)} saved zone(s) came from a different model or effort; re-querying them.")
    return matching


def default_model(extractor_class):
    """The provider's default model id, read from the class without creating a client (so no API key
    is needed when everything can be reused)."""
    return inspect.signature(extractor_class.__init__).parameters["model"].default


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--town", required=True, choices=TOWNS)
    parser.add_argument("--doc", type=Path, help="zoning document (default: zoning_docs/<town>.pdf)")
    parser.add_argument("--zone-column", help="column holding zone codes (skips detection)")
    parser.add_argument("--fields", type=Path, default=FIELDS_FILE, help="fields file (default: zoning_fields.toml)")
    parser.add_argument("--model", default="gemini", choices=EXTRACTORS)
    parser.add_argument("--model-name", help="override the provider's default model id")
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high"],
                        help="how much the model reasons before answering (default: high)")
    parser.add_argument("--decider", default="clef", choices=DECIDERS)
    parser.add_argument("--refresh", action="store_true", help="re-query every zone, ignoring saved results")
    args = parser.parse_args()
    load_env()

    document = (args.doc or DOCS_DIR / f"{args.town}.pdf").resolve()
    if not document.exists():
        raise SystemExit(f"Zoning document not found: {document}")
    fields = tomllib.loads(args.fields.read_text(encoding="utf-8"))["fields"]

    columns = load_town_columns(args.town)
    if args.zone_column:
        matches = [key for key in columns if key[1] == args.zone_column]
        if not matches:
            raise SystemExit(f"No column named {args.zone_column!r} in output/{args.town}/")
        zone_column = matches[0]
    else:
        zone_column = find_zone_column(args.town, columns, args.decider)
    zones = sorted({v.upper() for v in columns[zone_column] if v})
    print(f"{len(zones)} zones: {', '.join(zones)}")

    source = str(document.relative_to(BASE_DIR)) if document.is_relative_to(BASE_DIR) else str(document)
    zones_csv = OUTPUT_DIR / args.town / "zones.csv"
    model = f"{args.model}/{args.model_name or default_model(EXTRACTORS[args.model])}"  # e.g. gemini/gemini-3.5-flash-lite
    saved = {} if args.refresh else reusable_rows(zones_csv, document, args.fields.resolve(), source,
                                                  model, args.effort)
    todo = [z for z in zones if z not in saved]
    print(f"Reusing {len(zones) - len(todo)} saved zone(s); querying {len(todo)}.")

    rows, failed = {z: saved[z] for z in zones if z in saved}, []
    if todo:
        kwargs = {"effort": args.effort, **({"model": args.model_name} if args.model_name else {})}
        try:
            extractor = EXTRACTORS[args.model](**kwargs)
        except ModelError as e:
            raise SystemExit(str(e))
        except ImportError as e:
            raise SystemExit(f"{e.name} isn't installed; run: uv pip install {e.name}")
        print(f"Using {args.model} ({extractor.model}, effort {extractor.effort})")
        schema, validate = build_schema(fields), make_validator(fields)
        for i, zone in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {zone}")
            try:
                result = extractor.extract(document, build_prompt(args.town, zone, fields), schema, validate)
            except ModelError as e:
                print(f"  FAILED - {e}")
                failed.append(zone)
                continue
            rows[zone] = {"zone": zone, **{k: result[k] or "" for k in fields},
                          "found_in_document": result["found_in_document"],
                          "source_pages": "; ".join(map(str, result["source_pages"])),
                          "notes": result["notes"], "source_document": source,
                          "model": model, "effort": args.effort}

    if todo and len(failed) < len(todo) or not zones_csv.exists():
        write_csv(zones_csv, [rows[z] for z in zones if z in rows])
    print(f"\n{len(rows)} zones in {zones_csv}" + (f"; failed (rerun to retry): {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
