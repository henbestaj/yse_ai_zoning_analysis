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

# Field kinds: for each, the units the model may report (as the document states them) and how each
# converts to a standard unit. Each standard unit becomes its own CSV column, so "height" yields
# separate _ft and _stories columns instead of mixing them. The model never converts units itself.
SQ_FT_PER_ACRE = 43_560
SQ_FT_PER_SQ_M = 10.7639
FT_PER_M = 3.28084
KINDS = {
    "text": {},
    "area": {"sq_ft": ("sq_ft", lambda v: v),
             "acres": ("sq_ft", lambda v: v * SQ_FT_PER_ACRE),
             "sq_m": ("sq_ft", lambda v: v * SQ_FT_PER_SQ_M)},
    "length": {"ft": ("ft", lambda v: v),
               "m": ("ft", lambda v: v * FT_PER_M)},
    "height": {"ft": ("ft", lambda v: v),
               "m": ("ft", lambda v: v * FT_PER_M),
               "stories": ("stories", lambda v: v)},
    "percent": {"percent": ("percent", lambda v: v)},
    "density": {"units_per_acre": ("units_per_acre", lambda v: v),
                "sq_ft_per_unit": ("units_per_acre", lambda v: SQ_FT_PER_ACRE / v),
                "acres_per_unit": ("units_per_acre", lambda v: 1 / v)},
    "count": {"count": ("count", lambda v: v)},
}
# not_applicable: the document says the requirement doesn't apply here (e.g. "NA", "none")
# not_found: the document doesn't say anything about it for this district
STATUSES = ["stated", "not_applicable", "not_found"]


def standard_units(kind):
    """The output units for a kind, in order, e.g. height -> ["ft", "stories"]."""
    return list(dict.fromkeys(target for target, _ in KINDS[kind].values()))


def load_fields(path):
    """Read the fields file: {name: {"kind": ..., "description": ...}}, checking every field."""
    fields = tomllib.loads(path.read_text(encoding="utf-8"))["fields"]
    for name, spec in fields.items():
        if not isinstance(spec, dict) or spec.get("kind") not in KINDS or not spec.get("description"):
            raise SystemExit(f"{path.name}: field {name!r} needs a kind (one of: {', '.join(KINDS)}) "
                             f"and a description")
    return fields


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
        instructions="Which column holds each property's zoning district code (e.g. RM, RL, BA-1), "
                     "as defined in the town's zoning ordinance?",
        options=options)
    file, name = keys[choice]
    print(f"Zone column: {name!r} in {file} ({decider_name}, confidence {confidence:.2f})")
    if confidence < MIN_CONFIDENCE:
        raise SystemExit(f"Not confident enough ({confidence:.2f} < {MIN_CONFIDENCE}); pass --zone-column NAME.")
    return file, name


def field_schema(spec):
    """JSON schema for one field: text or null, or a measurement with a status and allowed units."""
    if spec["kind"] == "text":
        return {"type": ["string", "null"], "description": spec["description"]}
    measurement = {"type": "object", "required": ["value", "unit"], "properties": {
        "value": {"type": "number"},
        "unit": {"type": "string", "enum": list(KINDS[spec["kind"]])},
    }}
    return {"type": "object", "description": spec["description"], "required": ["status", "measurements", "raw"],
            "properties": {
                "status": {"type": "string", "enum": STATUSES},
                "measurements": {"type": "array", "items": measurement},
                "raw": {"type": ["string", "null"], "description": "The document's exact wording"},
            }}


def build_schema(fields):
    properties = {name: field_schema(spec) for name, spec in fields.items()}
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
        for name, spec in fields.items():
            value = result[name]
            if spec["kind"] == "text":
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{name}: not text")
                continue
            if not isinstance(value, dict) or value.get("status") not in STATUSES:
                raise ValueError(f"{name}: missing or unknown status")
            measurements = value.get("measurements")
            units = KINDS[spec["kind"]]
            if not isinstance(measurements, list) or not all(
                    isinstance(m, dict) and m.get("unit") in units
                    and isinstance(m.get("value"), (int, float)) and not isinstance(m.get("value"), bool)
                    for m in measurements):
                raise ValueError(f"{name}: measurements must be numbers with a unit from {list(units)}")
            if value["status"] != "stated" and measurements:
                raise ValueError(f"{name}: has measurements but status is {value['status']}")
            if value.get("raw") is not None and not isinstance(value["raw"], str):
                raise ValueError(f"{name}: raw is not text")
        if not isinstance(result["found_in_document"], bool):
            raise ValueError("found_in_document is not true/false")
        if not isinstance(result["source_pages"], list) or not all(isinstance(p, int) for p in result["source_pages"]):
            raise ValueError("source_pages must be a list of integers")
        if not isinstance(result["notes"], str):
            raise ValueError("notes is not text")
    return validate


def build_prompt(town, zone, fields):
    field_lines = "\n".join(
        f"- {name} (text): {spec['description']}" if spec["kind"] == "text" else
        f"- {name} ({spec['kind']}; units: {', '.join(KINDS[spec['kind']])}): {spec['description']}"
        for name, spec in fields.items())
    return f"""This document is the zoning regulations for {town.replace('_', ' ').title()}, Connecticut.
Find the requirements for the zoning district with code "{zone}". Look in dimensional/bulk tables
as well as the text.

Text fields: give the value as the document states it, or null if it doesn't.

Measurement fields (every field that lists units): give
- status: "stated" if the document gives the requirement for this district; "not_applicable" if the
  document says it doesn't apply to this district (e.g. "NA", "none", "not required"); "not_found" if
  the document says nothing about it for this district.
- measurements: each number in the requirement, with its unit exactly as the document uses it, chosen
  from the field's unit list. Do not convert units. If the requirement names several numbers (e.g.
  "the lesser of 35 feet or 2.5 stories"), list each one. Empty unless status is "stated".
- raw: the document's exact wording for the requirement, or null if not found.

Do not guess and do not borrow values from other districts, unless the document says this district
follows another one's rules (then say so in notes).

Fields:
{field_lines}

Also return found_in_document (false if the document doesn't define district "{zone}" at all),
source_pages (page numbers of the file, 1 = first page, not printed page labels), and notes."""


def _number(x):
    """6000.0 -> "6000", 21780.004 -> "21780", 2.5 -> "2.5"."""
    x = round(x, 2)
    return str(int(x)) if x.is_integer() else f"{x:.2f}".rstrip("0").rstrip(".")


def to_columns(result, fields):
    """Turn the model's answer into CSV columns and a list of flags.

    Text fields pass through. Measurements are converted to standard units, one column per unit
    (<field>_<unit>), next to <field>_status and <field>_raw. A unit column holds a number only when
    the requirement reduces to exactly one value for it; anything else is left blank and flagged
    for a person to check.
    """
    row, flags = {}, []
    for name, spec in fields.items():
        value = result[name]
        if spec["kind"] == "text":
            row[name] = value or ""
            continue
        converted = {unit: [] for unit in standard_units(spec["kind"])}
        for m in value["measurements"]:
            target, convert = KINDS[spec["kind"]][m["unit"]]
            try:
                converted[target].append(convert(float(m["value"])))
            except ZeroDivisionError:
                flags.append(f"{name}: can't convert 0 {m['unit']}")
        for unit, values in converted.items():
            distinct = sorted({round(v, 2) for v in values})
            column = name if spec["kind"] == "count" else f"{name}_{unit}"
            row[column] = _number(distinct[0]) if len(distinct) == 1 else ""
            if len(distinct) > 1:
                flags.append(f"{name}: several {unit} values ({', '.join(map(_number, distinct))})")
        row[f"{name}_status"] = value["status"]
        row[f"{name}_raw"] = value["raw"] or ""
        if value["status"] == "stated" and not value["measurements"]:
            flags.append(f"{name}: stated, but not as a number")
    return row, flags


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
        rows = [row for row in csv.DictReader(f) if row["source_document"] == source]
    matching = {row["zone"].casefold(): row for row in rows  # keyed case-insensitively, like the zones
                if row["model"] == model and row["effort"] == effort}
    if len(matching) < len(rows):
        print(f"{len(rows) - len(matching)} saved zone(s) came from a different model or effort; re-querying them.")
    return matching


def resolve_zone_column(spec, columns, town):
    """Turn --zone-column into a (csv name, column name) key. Accepts "parcels.csv:Zone" or just "Zone".

    File names can't contain ':', so the text before the first ':' is a file name only if it names one
    of the town's CSVs; otherwise the whole text is the column name (names like "Land: Zone" contain
    ':' themselves). A bare name found in several files stops with the choices instead of guessing.
    """
    file, sep, name = spec.partition(":")
    if sep and file in {f for f, _ in columns}:
        if (file, name) not in columns:
            raise SystemExit(f"No column named {name!r} in output/{town}/{file}")
        return file, name
    matches = [key for key in columns if key[1] == spec]
    if not matches:
        raise SystemExit(f"No column named {spec!r} in output/{town}/")
    if len(matches) > 1:
        choices = "\n".join(f'  --zone-column "{f}:{n}"' for f, n in matches)
        raise SystemExit(f"Column {spec!r} is in {len(matches)} files; say which one:\n{choices}")
    return matches[0]


def default_model(extractor_class):
    """The provider's default model id, read from the class without creating a client (so no API key
    is needed when everything can be reused)."""
    return inspect.signature(extractor_class.__init__).parameters["model"].default


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--town", required=True, choices=TOWNS)
    parser.add_argument("--doc", type=Path, help="zoning document (default: zoning_docs/<town>.pdf)")
    parser.add_argument("--zone-column", help='column holding zone codes, e.g. "Zone" or "parcels.csv:Zone" '
                                              "(skips detection; name the file if the column is in several)")
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
    fields = load_fields(args.fields)

    columns = load_town_columns(args.town)
    if args.zone_column:
        zone_column = resolve_zone_column(args.zone_column, columns, args.town)
    else:
        zone_column = find_zone_column(args.town, columns, args.decider)
    # One zone per code regardless of capitalization ("RM" and "rm"); keep the first spelling seen
    first_spelling = {}
    for value in columns[zone_column]:
        if value:
            first_spelling.setdefault(value.casefold(), value)
    zones = sorted(first_spelling.values(), key=str.casefold)
    print(f"{len(zones)} zones: {', '.join(zones)}")

    source = str(document.relative_to(BASE_DIR)) if document.is_relative_to(BASE_DIR) else str(document)
    zones_csv = OUTPUT_DIR / args.town / "zones.csv"
    model = f"{args.model}/{args.model_name or default_model(EXTRACTORS[args.model])}"  # e.g. gemini/gemini-3.5-flash-lite
    saved = {} if args.refresh else reusable_rows(zones_csv, document, args.fields.resolve(), source,
                                                  model, args.effort)
    todo = [z for z in zones if z.casefold() not in saved]
    print(f"Reusing {len(zones) - len(todo)} saved zone(s); querying {len(todo)}.")

    # Saved rows take this run's spelling of the code, in case it changed (e.g. "Rm" -> "RM")
    rows = {z: {**saved[z.casefold()], "zone": z} for z in zones if z.casefold() in saved}
    failed = []
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
            field_columns, flags = to_columns(result, fields)
            rows[zone] = {"zone": zone, **field_columns, "flags": "; ".join(flags),
                          "found_in_document": result["found_in_document"],
                          "source_pages": "; ".join(map(str, result["source_pages"])),
                          "notes": result["notes"], "source_document": source,
                          "model": model, "effort": args.effort}

    # Only rewrite zones.csv when this run got something new back: an all-reused or all-failed run
    # leaves the existing file (and its timestamp) alone
    if len(todo) > len(failed):
        write_csv(zones_csv, [rows[z] for z in zones if z in rows])
    print(f"\n{len(rows)} zones in {zones_csv}" + (f"; failed (rerun to retry): {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
