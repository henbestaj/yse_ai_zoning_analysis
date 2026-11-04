"""Build a zones table: one row per zoning district, with the fields from zoning_fields.toml filled in
from a zoning document by a model.

Two ways to say which districts:
    python zoning.py --town new_haven [--doc PATH] [--zone-column NAME] [--decider clef]
        A town with scraped data in scraper_output/<town>/; the zone codes come from that data.
        The document defaults to zoning_docs/<town>.pdf.
    python zoning.py --name "Hartford" --doc PATH [--zones RM RL CBD]
        Anything else: give the document and the zone codes. Without --zones, the model lists the
        document's districts first (overlay districts are left out).
    python zoning.py --name "Hartford" --doc PATH --list-zones
        Only print the districts the model finds, to check them before a run; nothing is saved.
All of these also take [--model-provider gemini|gcloud] [--model-name ID] [--model-effort low|medium|high];
the two that build a table also take [--fields PATH] [--refresh].

Each run is added to the zoning database (see zoning_db.py) and never overwritten. A zone is reused,
with no model call, from an earlier run with the same place, model, effort, document contents and
fields. --refresh redoes every zone. Get results out with: python zoning_db.py export ...
"""

import argparse
import csv
import inspect
import re
from pathlib import Path

from common import BASE_DIR, SCRAPER_OUTPUT_DIR, load_env
from models import DECIDERS, EXTRACTORS, ModelError
from zoning_db import (KINDS, STATUSES, copy_zone, database_label, fields_sha256, file_sha256, find_reusable,
                       finish_run, get_engine, parse_fields, save_zone, start_run)

DOCS_DIR = BASE_DIR / "zoning_docs"
FIELDS_FILE = BASE_DIR / "zoning_fields.toml"
SKIP_FILES = {"errors.csv"}  # scraper output that never holds zone codes
YES_NO = {"yes", "no", "y", "n", "true", "false", "0", "1"}
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .\-/]{0,11}$")  # e.g. RM, RM2, BD-1, CBD, I-L
MIN_CONFIDENCE = 0.6


def load_fields(path):
    """Read and check the fields file. Returns (fields, the file's text)."""
    text = path.read_text(encoding="utf-8")
    try:
        return parse_fields(text), text
    except ValueError as e:
        raise SystemExit(f"{path.name}: {e}")


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


DISTRICTS_SCHEMA = {
    "type": "object", "required": ["districts"],
    "properties": {"districts": {"type": "array", "items": {
        "type": "object", "required": ["code", "name", "overlay"],
        "properties": {
            "code": {"type": "string", "description": "The district's short designation as the document writes it"},
            "name": {"type": "string", "description": "The district's full name"},
            "overlay": {"type": "boolean",
                        "description": "True for an overlay or floating district applied on top of a base district"},
        }}}},
}


def validate_districts(result):
    districts = result.get("districts")
    if not isinstance(districts, list):
        raise ValueError("districts must be a list")
    for d in districts:
        if not isinstance(d, dict) or not isinstance(d.get("code"), str) or not d["code"].strip():
            raise ValueError("every district needs a code")
        if not isinstance(d.get("name"), str) or not isinstance(d.get("overlay"), bool):
            raise ValueError(f"{d['code']}: needs a name and overlay true/false")
    if not any(not d["overlay"] for d in districts):
        raise ValueError("no base (non-overlay) districts")


def districts_prompt(place):
    return f"""This document is the zoning regulations for {place}.
List every zoning district it establishes. For each one give:
- code: the district's short designation exactly as the document writes it (e.g. "RM-1", "CBD"); if
  it has none, its name.
- name: its full name (e.g. "Residence Multi-Family District").
- overlay: true for an overlay or floating district, whose rules apply on top of an underlying base
  district (e.g. historic, flood plain or design overlays); false for base districts.
List each district once. Leave out districts the document only mentions without establishing them
(e.g. districts of neighboring towns, or ones it says were repealed)."""


def make_extractor(provider, model, effort):
    """Create the model client. A missing setting or package raises ModelError."""
    try:
        return EXTRACTORS[provider](model=model, effort=effort)
    except ImportError as e:
        raise ModelError(f"{e.name} isn't installed; run: uv pip install {e.name}") from e


def find_districts(extractor, document, place):
    """Ask the model for the document's districts. Returns (base districts, overlay districts), each a
    list of {code, name, overlay}. Stops the program if the model can't answer."""
    print(f"Asking {extractor.model} for the districts in {document.name}...")
    try:
        result = extractor.extract(document, districts_prompt(place), DISTRICTS_SCHEMA, validate_districts)
    except ModelError as e:
        raise SystemExit(f"Couldn't list the districts: {e}")
    first = {}  # one district per code regardless of capitalization, as for --zones
    for d in result["districts"]:
        first.setdefault(d["code"].strip().casefold(), {**d, "code": d["code"].strip()})
    districts = list(first.values())
    overlays = [d for d in districts if d["overlay"]]
    if overlays:
        print(f"Leaving out {len(overlays)} overlay district(s): "
              + ", ".join(f"{d['code']} ({d['name']})" for d in overlays))
    return [d for d in districts if not d["overlay"]], overlays


def value_schema(kind):
    """JSON schema for one value of a field: text, or (for measurement kinds) a number with a unit from
    the kind's list, or text when the requirement isn't a number; each with condition, quote and pages."""
    shared = {
        "condition": {"type": ["string", "null"],
                      "description": "Null if the value applies generally; otherwise the situation the document "
                                     "limits it to, e.g. 'from any residential district'"},
        "quote": {"type": "string", "description": "The document's exact wording for this value"},
        "pages": {"type": "array", "items": {"type": "integer"},
                  "description": "PDF page numbers (1 = first page of the file) this value came from"},
    }
    if kind == "text":
        properties = {"text": {"type": "string"}, **shared}
    else:
        properties = {
            "number": {"type": ["number", "null"]},
            "unit": {"type": ["string", "null"], "enum": [*KINDS[kind], None]},
            "text": {"type": ["string", "null"],
                     "description": "Only for a requirement that isn't a number; then number and unit are null"},
            **shared,
        }
    return {"type": "object", "properties": properties, "required": list(properties)}


def build_schema(fields):
    properties = {name: {"type": "object", "description": spec["description"], "required": ["status", "values"],
                         "properties": {"status": {"type": "string", "enum": list(STATUSES)},
                                        "values": {"type": "array", "items": value_schema(spec["kind"])}}}
                  for name, spec in fields.items()}
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
            answer = result[name]
            if not isinstance(answer, dict) or answer.get("status") not in STATUSES:
                raise ValueError(f"{name}: missing or unknown status")
            values = answer.get("values")
            if not isinstance(values, list):
                raise ValueError(f"{name}: values must be a list")
            if answer["status"] != "stated" and values:
                raise ValueError(f"{name}: has values but status is {answer['status']}")
            for v in values:
                if not isinstance(v, dict):
                    raise ValueError(f"{name}: a value is not an object")
                pages = v.get("pages")
                if not isinstance(pages, list) or not all(isinstance(p, int) and not isinstance(p, bool) for p in pages):
                    raise ValueError(f"{name}: pages must be a list of integers")
                if not isinstance(v.get("quote"), str) or not v["quote"].strip():
                    raise ValueError(f"{name}: every value needs a quote")
                if v.get("condition") is not None and not isinstance(v["condition"], str):
                    raise ValueError(f"{name}: condition must be text or null")
                is_text = isinstance(v.get("text"), str) and bool(v["text"].strip())
                number = v.get("number")
                is_number = isinstance(number, (int, float)) and not isinstance(number, bool)
                if spec["kind"] == "text":
                    if not is_text:
                        raise ValueError(f"{name}: every value needs text")
                elif is_number == is_text:
                    raise ValueError(f"{name}: each value needs either a number with a unit, or text")
                elif is_number and v.get("unit") not in KINDS[spec["kind"]]:
                    raise ValueError(f"{name}: unit must be one of {list(KINDS[spec['kind']])}")
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

For every field, give:
- status: "stated" if the document gives the requirement for this district; "not_applicable" if the
  document says it doesn't apply to this district (e.g. "NA", "none", "not required"); "not_found" if
  the document says nothing about it for this district.
- values: one entry per distinct requirement; empty unless status is "stated". Each entry has:
  - Measurement fields (those that list units): number and unit, with the unit exactly as the
    document uses it, chosen from the field's unit list. Do not convert units. A requirement that
    names several numbers (e.g. "the lesser of 35 feet or 2.5 stories") is one entry per number. If
    a requirement isn't a number (e.g. "average depth of buildings within 200 ft"), give it as text
    and leave number and unit null.
  - Text fields: text. A list, such as permitted uses, is one entry per item.
  - condition: null if the value applies generally; otherwise the situation the document limits it
    to (e.g. "from any residential district", "for corner lots"). Whenever the document gives a
    general rule, include it with condition null, alongside any conditional values.
  - quote: the document's exact wording for this value.
  - pages: the page numbers of the file (1 = first page, not printed page labels) it came from.

Do not guess and do not borrow values from other districts, unless the document says this district
follows another one's rules (then say so in notes).

Fields:
{field_lines}

Also return found_in_document (false if the document doesn't define district "{zone}" at all)
and notes."""


def default_model(extractor_class):
    """The provider's default model id, read from the class without creating a client (so no API key
    is needed when everything can be reused)."""
    return inspect.signature(extractor_class.__init__).parameters["model"].default


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--town", help="a town with scraped data in scraper_output/<town>/")
    which.add_argument("--name", help="any name, for districts not taken from scraped data (needs --doc)")
    parser.add_argument("--doc", type=Path, help="zoning document (with --town, defaults to zoning_docs/<town>.pdf)")
    parser.add_argument("--zones", nargs="+", help="the zone codes to look up, e.g. --zones RM RL CBD "
                                                   "(--name only; leave out to have the model list the districts)")
    parser.add_argument("--list-zones", action="store_true",
                        help="only print the districts the model finds in --doc, then stop (--name only)")
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
        for option, value in (("--zones", args.zones), ("--list-zones", args.list_zones)):
            if value:
                parser.error(f"{option} is only for --name; with --town the zones come from the scraped data")
        place, document = args.town.replace("_", " ").title(), args.doc or DOCS_DIR / f"{args.town}.pdf"
    else:
        for option, value in (("--zone-column", args.zone_column), ("--decider", args.decider)):
            if value:
                parser.error(f"{option} is only for --town")
        if not args.doc:
            parser.error("--name needs --doc")
        if args.list_zones and args.zones:
            parser.error("--list-zones finds the zones itself; leave out --zones")
        place, document = args.name, args.doc
    load_env()

    columns = load_town_columns(args.town) if args.town else None  # first: a town needs scraped data
    document = document.resolve()
    if not document.exists():
        raise SystemExit(f"Zoning document not found: {document}")
    if not args.list_zones:  # before any model call, so a bad fields file doesn't waste one
        fields, fields_text = load_fields(args.fields.resolve())
    model_name = args.model_name or default_model(EXTRACTORS[args.model_provider])

    extractor = None
    if not args.town and not args.zones:  # the model lists the districts
        try:
            extractor = make_extractor(args.model_provider, model_name, args.model_effort)
        except ModelError as e:
            raise SystemExit(f"Couldn't list the districts: {e}")
        districts, _ = find_districts(extractor, document, place)
        if args.list_zones:
            width = max(len(d["code"]) for d in districts)
            print(f"{len(districts)} districts:")
            for d in districts:
                print(f"  {d['code']:<{width}}  {d['name']}")
            pasteable = [c if re.fullmatch(r"[\w.\-/]+", c) else f'"{c}"' for c in (d["code"] for d in districts)]
            print(f"To look them up (edit the list if needed):  --zones {' '.join(pasteable)}")
            return

    if args.town:
        if args.zone_column:
            zone_column = resolve_zone_column(args.zone_column, columns, args.town)
        else:
            zone_column = find_zone_column(place, columns, args.decider or "clef")
        codes, zones_from = columns[zone_column], "scraped"
    elif args.zones:
        # argparse already requires at least one value; this also catches blank ones like --zones ""
        codes, zones_from = [code.strip() for code in args.zones if code.strip()], "given"
        if not codes:
            raise SystemExit("No zone codes given; pass them after --zones, or leave out --zones")
    else:
        codes, zones_from = [d["code"] for d in districts], "model"
    # One zone per code regardless of capitalization ("RM" and "rm"); keep the first spelling seen
    first_spelling = {}
    for value in codes:
        if value:
            first_spelling.setdefault(value.casefold(), value)
    zones = sorted(first_spelling.values(), key=str.casefold)
    print(f"{len(zones)} zones: {', '.join(zones)}")

    # From here on everything is recorded as a run in the database, including failures
    inputs = {"place_kind": "town" if args.town else "custom", "place": args.town or args.name,
              "provider": args.model_provider, "model": model_name, "effort": args.model_effort,
              "document_name": document.name, "document_sha256": file_sha256(document),
              "fields_toml": fields_text, "fields_sha256": fields_sha256(fields)}
    engine = get_engine()
    run_id = start_run(engine, inputs, len(zones), zones_from)
    place_option = f"--town {args.town}" if args.town else f'--name "{args.name}"'

    saved = {} if args.refresh else find_reusable(engine, inputs)
    reused = [z for z in zones if z.casefold() in saved]
    for zone in reused:
        copy_zone(engine, run_id, *saved[zone.casefold()], zone)
    todo = [z for z in zones if z.casefold() not in saved]
    print(f"Run {run_id}: reusing {len(reused)} saved zone(s); querying {len(todo)}.")

    failed, error, stored = [], None, list(reused)
    try:
        if todo:
            try:
                extractor = extractor or make_extractor(args.model_provider, model_name, args.model_effort)
            except ModelError as e:
                failed, error = list(todo), str(e)
            else:
                print(f"Using {args.model_provider} ({extractor.model}, effort {extractor.effort})")
                schema, validate = build_schema(fields), make_validator(fields)
                for i, zone in enumerate(todo, 1):
                    print(f"[{i}/{len(todo)}] {zone}")
                    try:
                        result = extractor.extract(document, build_prompt(place, zone, fields), schema, validate)
                    except ModelError as e:
                        print(f"  FAILED - {e}")
                        failed.append(zone)
                        error = f"{len(failed)} zone(s) failed; last error: {e}"
                        continue
                    save_zone(engine, run_id, zone, result, fields)
                    stored.append(zone)
    except BaseException as e:  # Ctrl+C or a crash: record the run as stopped rather than leave it "running"
        finish_run(engine, run_id, "partial" if stored else "failed", zones_failed=len(zones) - len(stored),
                   error=f"stopped: {type(e).__name__} {e}".strip())
        raise

    done = len(stored)
    status = "succeeded" if done == len(zones) else "partial" if done else "failed"
    finish_run(engine, run_id, status, zones_failed=len(failed), error=error)

    if status == "failed":
        print(f"To export the most recent run with results:  python zoning_db.py export {place_option}")
        raise SystemExit(f"Run {run_id} failed: {error}")
    print(f"To save it as a CSV:  python zoning_db.py export --run {run_id}")
    print(f"Run {run_id} {status}: {done} of {len(zones)} zones ({len(reused)} reused), saved to {database_label(engine)}"
          + (f"; failed (rerun to retry): {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
