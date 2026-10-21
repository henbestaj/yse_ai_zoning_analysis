"""Build a zones table: one row per zoning district, with the fields from zoning_fields.toml filled in
from a zoning document by a model.

Two ways to say which districts:
    python zoning.py --town new_haven [--doc PATH] [--zone-column NAME] [--decider clef]
        A town with scraped data in scraper_output/<town>/; the zone codes come from that data.
        The document defaults to zoning_docs/<town>.pdf.
    python zoning.py --name "Hartford" --doc PATH --zones RM RL CBD
        Anything else: give the document and the zone codes.
Both also take [--fields PATH] [--model-provider gemini|gcloud] [--model-name ID]
[--model-effort low|medium|high] [--refresh].

Results go to zoning_output/<towns|custom>/<town or name>/<provider>.<model>.<effort>/zones.csv,
next to copies of the document and fields file that produced them. A zone is reused, with no model
call, when it's already in that zones.csv and the document and fields file are byte-identical to
those copies. --refresh redoes every zone.
"""

import argparse
import csv
import filecmp
import inspect
import re
import shutil
import tomllib
from pathlib import Path

from common import BASE_DIR, SCRAPER_OUTPUT_DIR, load_env, write_csv
from models import DECIDERS, EXTRACTORS, ModelError

DOCS_DIR = BASE_DIR / "zoning_docs"
FIELDS_FILE = BASE_DIR / "zoning_fields.toml"
ZONING_OUTPUT_DIR = BASE_DIR / "zoning_output"
SAVED_DOC_STEM = "zoning_document"  # copies kept with each run's results
SAVED_FIELDS = "zoning_fields.toml"
SKIP_FILES = {"errors.csv"}  # scraper output that never holds zone codes
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


def safe_name(text):
    """Make text usable as a folder name: characters Windows forbids (< > : " / \\ | ? *) become '-',
    spaces become '_', and trailing dots are dropped (Windows silently strips them)."""
    return re.sub(r'[<>:"/\\|?*]', "-", text).replace(" ", "_").rstrip(".")


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
    columns = {}
    for path in sorted((SCRAPER_OUTPUT_DIR / town).glob("*.csv")):
        if path.name in SKIP_FILES:
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                for name, value in row.items():
                    columns.setdefault((path.name, name), []).append((value or "").strip())
    if not columns:
        raise SystemExit(f"No scraped data in scraper_output/{town}/; run scraper.py --town {town} first, "
                         f"or use --name with --doc and --zones.")
    return columns


def looks_like_zone_codes(values):
    present = [v for v in values if v]
    if not present or all(v.lower() in YES_NO for v in present):
        return False
    return sum(bool(CODE.match(v)) for v in present) >= 0.9 * len(present)


def find_zone_column(place, columns, decider_name):
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
        state=f"Property records for {place}.",
        instructions="Which column holds each property's zoning district code (e.g. RM, RL, BA-1), "
                     "as defined in the town's zoning ordinance?",
        options=options)
    file, name = keys[choice]
    print(f"Zone column: {name!r} in {file} ({decider_name}, confidence {confidence:.2f})")
    if confidence < MIN_CONFIDENCE:
        raise SystemExit(f"Not confident enough ({confidence:.2f} < {MIN_CONFIDENCE}); pass --zone-column NAME.")
    return file, name


def resolve_zone_column(spec, columns, town):
    """Turn --zone-column into a (csv name, column name) key. Accepts "parcels.csv:Zone" or just "Zone".

    File names can't contain ':', so the text before the first ':' is a file name only if it names one
    of the town's CSVs; otherwise the whole text is the column name (names like "Land: Zone" contain
    ':' themselves). A bare name found in several files stops with the choices instead of guessing.
    """
    file, sep, name = spec.partition(":")
    if sep and file in {f for f, _ in columns}:
        if (file, name) not in columns:
            raise SystemExit(f"No column named {name!r} in scraper_output/{town}/{file}")
        return file, name
    matches = [key for key in columns if key[1] == spec]
    if not matches:
        raise SystemExit(f"No column named {spec!r} in scraper_output/{town}/")
    if len(matches) > 1:
        choices = "\n".join(f'  --zone-column "{f}:{n}"' for f, n in matches)
        raise SystemExit(f"Column {spec!r} is in {len(matches)} files; say which one:\n{choices}")
    return matches[0]


PAGES_SCHEMA = {"type": "array", "items": {"type": "integer"},
                "description": "PDF page numbers (1 = first page of the file) this field's value came from"}


def field_schema(spec):
    """JSON schema for one field: a text value, or a measurement with a status and allowed units;
    either way with the pages it came from."""
    if spec["kind"] == "text":
        return {"type": "object", "description": spec["description"], "required": ["value", "pages"],
                "properties": {"value": {"type": ["string", "null"]}, "pages": PAGES_SCHEMA}}
    measurement = {"type": "object", "required": ["value", "unit"], "properties": {
        "value": {"type": "number"},
        "unit": {"type": "string", "enum": list(KINDS[spec["kind"]])},
    }}
    return {"type": "object", "description": spec["description"],
            "required": ["status", "measurements", "raw", "pages"],
            "properties": {
                "status": {"type": "string", "enum": STATUSES},
                "measurements": {"type": "array", "items": measurement},
                "raw": {"type": ["string", "null"], "description": "The document's exact wording"},
                "pages": PAGES_SCHEMA,
            }}


def build_schema(fields):
    properties = {name: field_schema(spec) for name, spec in fields.items()}
    properties["found_in_document"] = {"type": "boolean",
                                       "description": "Whether the document defines this district at all"}
    properties["notes"] = {"type": "string",
                           "description": "Anything a reviewer should know: ambiguities, conditions, exceptions"}
    return {"type": "object", "properties": properties, "required": list(properties)}


def make_validator(fields):
    def validate(result):
        missing = [k for k in [*fields, "found_in_document", "notes"] if k not in result]
        if missing:
            raise ValueError(f"missing keys {missing}")
        for name, spec in fields.items():
            value = result[name]
            if not isinstance(value, dict):
                raise ValueError(f"{name}: not an object")
            pages = value.get("pages")
            if not isinstance(pages, list) or not all(isinstance(p, int) and not isinstance(p, bool) for p in pages):
                raise ValueError(f"{name}: pages must be a list of integers")
            if spec["kind"] == "text":
                if value.get("value") is not None and not isinstance(value["value"], str):
                    raise ValueError(f"{name}: not text")
                continue
            if value.get("status") not in STATUSES:
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
        if not isinstance(result["notes"], str):
            raise ValueError("notes is not text")
    return validate


def build_prompt(place, zone, fields):
    field_lines = "\n".join(
        f"- {name} (text): {spec['description']}" if spec["kind"] == "text" else
        f"- {name} ({spec['kind']}; units: {', '.join(KINDS[spec['kind']])}): {spec['description']}"
        for name, spec in fields.items())
    return f"""This document is the zoning regulations for {place}.
Find the requirements for the zoning district with code "{zone}". Look in dimensional/bulk tables
as well as the text.

Every field: give pages, the page numbers of the file (1 = first page, not printed page labels)
its value came from; empty if not found.

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

Also return found_in_document (false if the document doesn't define district "{zone}" at all)
and notes."""


def _number(x):
    """6000.0 -> "6000", 21780.004 -> "21780", 2.5 -> "2.5"."""
    x = round(x, 2)
    return str(int(x)) if x.is_integer() else f"{x:.2f}".rstrip("0").rstrip(".")


def to_columns(result, fields):
    """Turn the model's answer into CSV columns and a list of flags.

    Text fields pass through. Measurements are converted to standard units, one column per unit
    (<field>_<unit>), next to <field>_status and <field>_raw. A unit column holds a number only when
    the requirement reduces to exactly one value for it; anything else is left blank and flagged
    for a person to check. Every field also gets <field>_pages.
    """
    row, flags = {}, []
    for name, spec in fields.items():
        value = result[name]
        pages = "; ".join(map(str, value["pages"]))
        if spec["kind"] == "text":
            row[name] = value["value"] or ""
            row[f"{name}_pages"] = pages
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
        row[f"{name}_pages"] = pages
        if value["status"] == "stated" and not value["measurements"]:
            flags.append(f"{name}: stated, but not as a number")
    return row, flags


def saved_document(run_dir):
    """The copy of the zoning document kept with a run's results, if there is one."""
    return next(run_dir.glob(f"{SAVED_DOC_STEM}.*"), None)


def reusable_rows(run_dir, document, fields_file):
    """Rows from the run folder's zones.csv that are still valid: the document and fields file are
    byte-identical to the copies saved with those results. Keyed case-insensitively by zone."""
    zones_csv = run_dir / "zones.csv"
    if not zones_csv.exists():
        return {}
    saved_doc = saved_document(run_dir)
    if saved_doc is None or not filecmp.cmp(document, saved_doc, shallow=False):
        print("The zoning document differs from the one the saved results came from; re-querying all zones.")
        return {}
    saved_fields = run_dir / SAVED_FIELDS
    if not saved_fields.exists() or not filecmp.cmp(fields_file, saved_fields, shallow=False):
        print("The fields file differs from the one the saved results came from; re-querying all zones.")
        return {}
    with open(zones_csv, encoding="utf-8-sig", newline="") as f:
        return {row["zone"].casefold(): row for row in csv.DictReader(f)}


def save_inputs(run_dir, document, fields_file):
    """Keep copies of the document and fields file next to the results they produced."""
    target = run_dir / f"{SAVED_DOC_STEM}{document.suffix.lower()}"
    if not (target.exists() and target.samefile(document)):  # --doc may point at the saved copy itself
        for old in run_dir.glob(f"{SAVED_DOC_STEM}.*"):  # e.g. an earlier document of another file type
            old.unlink()
        shutil.copyfile(document, target)
    if not (run_dir / SAVED_FIELDS).exists() or not (run_dir / SAVED_FIELDS).samefile(fields_file):
        shutil.copyfile(fields_file, run_dir / SAVED_FIELDS)


def default_model(extractor_class):
    """The provider's default model id, read from the class without creating a client (so no API key
    is needed when everything can be reused)."""
    return inspect.signature(extractor_class.__init__).parameters["model"].default


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--town", help="a town with scraped data in scraper_output/<town>/")
    which.add_argument("--name", help="any name, for districts not taken from scraped data "
                                      "(needs --doc and --zones)")
    parser.add_argument("--doc", type=Path, help="zoning document (with --town, defaults to zoning_docs/<town>.pdf)")
    parser.add_argument("--zones", nargs="+", help="the zone codes to look up, e.g. --zones RM RL CBD (--name only)")
    parser.add_argument("--zone-column", help='column holding zone codes, e.g. "Zone" or "parcels.csv:Zone" '
                                              "(--town only; skips detection; name the file if the column is in several)")
    parser.add_argument("--decider", choices=DECIDERS,
                        help="decision model used if the zone column is unclear (--town only; default: clef)")
    parser.add_argument("--fields", type=Path, default=FIELDS_FILE, help="fields file (default: zoning_fields.toml)")
    parser.add_argument("--model-provider", default="gemini", choices=EXTRACTORS,
                        help="gemini (API key) or gcloud (Vertex AI) (default: gemini)")
    parser.add_argument("--model-name", help="model id (default: the provider's default)")
    parser.add_argument("--model-effort", default="high", choices=["low", "medium", "high"],
                        help="how much the model reasons before answering (default: high)")
    parser.add_argument("--refresh", action="store_true", help="re-query every zone, ignoring saved results")
    args = parser.parse_args()

    if args.town:
        if args.zones:
            parser.error("--zones is only for --name; with --town the zones come from the scraped data")
        place, document = args.town.replace("_", " ").title(), args.doc or DOCS_DIR / f"{args.town}.pdf"
    else:
        for option, value in (("--zone-column", args.zone_column), ("--decider", args.decider)):
            if value:
                parser.error(f"{option} is only for --town")
        if not args.doc or not args.zones:
            parser.error("--name needs both --doc and --zones")
        place, document = args.name, args.doc
    load_env()

    columns = load_town_columns(args.town) if args.town else None  # first: a town needs scraped data
    document, fields_file = document.resolve(), args.fields.resolve()
    if not document.exists():
        raise SystemExit(f"Zoning document not found: {document}")
    fields = load_fields(fields_file)

    if args.town:
        if args.zone_column:
            zone_column = resolve_zone_column(args.zone_column, columns, args.town)
        else:
            zone_column = find_zone_column(place, columns, args.decider or "clef")
        codes = columns[zone_column]
    else:
        # argparse already requires at least one value; this also catches blank ones like --zones ""
        codes = [code.strip() for code in args.zones if code.strip()]
        if not codes:
            raise SystemExit("No zone codes given; pass them after --zones")
    # One zone per code regardless of capitalization ("RM" and "rm"); keep the first spelling seen
    first_spelling = {}
    for value in codes:
        if value:
            first_spelling.setdefault(value.casefold(), value)
    zones = sorted(first_spelling.values(), key=str.casefold)
    print(f"{len(zones)} zones: {', '.join(zones)}")

    model_name = args.model_name or default_model(EXTRACTORS[args.model_provider])
    run_dir = (ZONING_OUTPUT_DIR / ("towns" if args.town else "custom") / safe_name(args.town or args.name)
               / safe_name(f"{args.model_provider}.{model_name}.{args.model_effort}"))
    zones_csv = run_dir / "zones.csv"
    saved = {} if args.refresh else reusable_rows(run_dir, document, fields_file)
    todo = [z for z in zones if z.casefold() not in saved]
    print(f"Reusing {len(zones) - len(todo)} saved zone(s); querying {len(todo)}.")

    # Saved rows take this run's spelling of the code, in case it changed (e.g. "Rm" -> "RM")
    rows = {z: {**saved[z.casefold()], "zone": z} for z in zones if z.casefold() in saved}
    failed = []
    if todo:
        try:
            extractor = EXTRACTORS[args.model_provider](model=model_name, effort=args.model_effort)
        except ModelError as e:
            raise SystemExit(str(e))
        except ImportError as e:
            raise SystemExit(f"{e.name} isn't installed; run: uv pip install {e.name}")
        print(f"Using {args.model_provider} ({extractor.model}, effort {extractor.effort})")
        schema, validate = build_schema(fields), make_validator(fields)
        for i, zone in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {zone}")
            try:
                result = extractor.extract(document, build_prompt(place, zone, fields), schema, validate)
            except ModelError as e:
                print(f"  FAILED - {e}")
                failed.append(zone)
                continue
            field_columns, flags = to_columns(result, fields)
            rows[zone] = {"zone": zone, **field_columns, "flags": "; ".join(flags),
                          "found_in_document": result["found_in_document"], "notes": result["notes"]}

    # Only rewrite zones.csv when this run got something new back: an all-reused or all-failed run
    # leaves the existing results alone. The inputs are saved with the results they produced.
    if len(todo) > len(failed):
        write_csv(zones_csv, [rows[z] for z in zones if z in rows])
        save_inputs(run_dir, document, fields_file)
    print(f"\n{len(rows)} zones in {zones_csv.relative_to(BASE_DIR)}"
          + (f"; failed (rerun to retry): {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
