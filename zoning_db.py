"""Zoning results database: SQLAlchemy Core, SQLite by default, written to also run on Postgres.

Every zoning run is appended and never changed after it finishes. The current results for a place are
its newest run that succeeded or partly succeeded; older runs stay available. The database is
zoning_output/zoning.db unless ZONING_DATABASE_URL is set (e.g. to a Postgres URL).

Tables (one row per ...):
    runs           attempt: inputs, hashes, where the zone list came from, start/finish time, status
                   (running/succeeded/partial/failed)
    zone_results   zone in a run: found_in_document, notes, and which run it was reused from (if any)
    field_values   field of a zone: its kind and status (stated / not_applicable / not_found)
    number_values  number the model reported for a field, in the unit the document used, with its
                   condition (null = applies generally) and quote
    text_values    text value for a field, with its condition and quote
    value_pages    page a value came from

Command line:
    python zoning_db.py runs (--town T | --name N)
    python zoning_db.py export (--run ID | --town T | --name N) [--format csv|json] [--values readable|json]
                               [--doc PATH] [--out FILE]
"""

import argparse
import hashlib
import json
import os
import shutil
import tomllib
from collections import defaultdict
from pathlib import Path

from sqlalchemy import (REAL, Column, ForeignKeyConstraint, Integer, MetaData, PrimaryKeyConstraint, Table, Text,
                        func, insert, literal, select, update)

from common import BASE_DIR, load_env, write_csv
# RESULT_STATUSES and database_label are used by zoning.py and app.py through this module
from db_common import (RESULT_STATUSES, RUN_STATUSES, STRICT, Timestamp, check_in, database_label,  # noqa: F401
                       make_engine, now_utc, safe_name)

DEFAULT_URL = f"sqlite:///{(BASE_DIR / 'zoning_output' / 'zoning.db').as_posix()}"

# ---------------------------------------------------------------- field kinds and units

# For each kind, the units the model may report (as the document states them) and how each converts
# to a standard unit. Conversions happen when exporting, so changing one here changes every export.
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
STATUSES = ("stated", "not_applicable", "not_found")
UNIT_LABELS = {"sq_ft": " sq ft", "ft": " ft", "stories": " stories", "percent": "%",
               "units_per_acre": " units per acre", "count": ""}  # for the readable format


def standard_units(kind):
    """The output units for a kind, in order, e.g. height -> ["ft", "stories"]."""
    return list(dict.fromkeys(target for target, _ in KINDS[kind].values()))


def parse_fields(text):
    """Parse a fields file's text into {name: {"kind": ..., "description": ...}}, checking every field.
    Raises ValueError with a readable message."""
    fields = tomllib.loads(text)["fields"]
    for name, spec in fields.items():
        if not isinstance(spec, dict) or spec.get("kind") not in KINDS or not spec.get("description"):
            raise ValueError(f"field {name!r} needs a kind (one of: {', '.join(KINDS)}) and a description")
    return fields


def fields_sha256(fields):
    """Hash what the fields mean (names, kinds, descriptions, in order), so comment or spacing edits in
    the file don't count as a change."""
    canonical = json.dumps([[n, s["kind"], s["description"]] for n, s in fields.items()], ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path):
    """Hash a file's contents only (not its name or dates)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------- tables

metadata = MetaData()

runs = Table(
    "runs", metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("place_kind", Text, nullable=False),  # town (scraped data) or custom (a list of codes)
    Column("place", Text, nullable=False),       # town key like "new_haven", or the custom name as typed
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("effort", Text, nullable=False),
    Column("document_name", Text, nullable=False),
    Column("document_sha256", Text, nullable=False),
    Column("fields_toml", Text, nullable=False),  # the fields file as it was, so old runs export correctly
    Column("fields_sha256", Text, nullable=False),
    Column("started_at", Timestamp, nullable=False),
    Column("finished_at", Timestamp),             # null while running (or if the run was interrupted)
    Column("status", Text, nullable=False),
    Column("error", Text),
    Column("zones_requested", Integer, nullable=False),
    Column("zones_from", Text, nullable=False),   # scraped (town data), given (--zones) or model (found in the document)
    Column("zones_failed", Integer),
    check_in("place_kind", ("town", "custom")),
    check_in("zones_from", ("scraped", "given", "model")),
    check_in("effort", ("low", "medium", "high")),
    check_in("status", RUN_STATUSES),
    **STRICT,
)
zone_results = Table(
    "zone_results", metadata,
    Column("run_id", Integer, nullable=False),
    Column("zone", Text, nullable=False),
    Column("found_in_document", Integer, nullable=False),
    Column("notes", Text, nullable=False),
    Column("reused_from_run_id", Integer),  # the run whose model call produced this zone, if copied
    PrimaryKeyConstraint("run_id", "zone"),
    ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
    ForeignKeyConstraint(["reused_from_run_id"], ["runs.run_id"]),
    check_in("found_in_document", (0, 1)),
    **STRICT,
)
field_values = Table(
    "field_values", metadata,
    Column("run_id", Integer, nullable=False),
    Column("zone", Text, nullable=False),
    Column("field", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    PrimaryKeyConstraint("run_id", "zone", "field"),
    ForeignKeyConstraint(["run_id", "zone"], ["zone_results.run_id", "zone_results.zone"]),
    check_in("kind", tuple(KINDS)),
    check_in("status", STATUSES),
    **STRICT,
)
# Numbers and text values share one position sequence per field (the order the model gave them), so
# value_pages can point at either by (run_id, zone, field, position).
number_values = Table(
    "number_values", metadata,
    Column("run_id", Integer, nullable=False),
    Column("zone", Text, nullable=False),
    Column("field", Text, nullable=False),
    Column("position", Integer, nullable=False),
    Column("reported_value", REAL, nullable=False),
    Column("reported_unit", Text, nullable=False),
    Column("condition", Text),  # null: applies generally
    Column("quote", Text, nullable=False),
    PrimaryKeyConstraint("run_id", "zone", "field", "position"),
    ForeignKeyConstraint(["run_id", "zone", "field"],
                         ["field_values.run_id", "field_values.zone", "field_values.field"]),
    **STRICT,
)
text_values = Table(
    "text_values", metadata,
    Column("run_id", Integer, nullable=False),
    Column("zone", Text, nullable=False),
    Column("field", Text, nullable=False),
    Column("position", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("condition", Text),
    Column("quote", Text, nullable=False),
    PrimaryKeyConstraint("run_id", "zone", "field", "position"),
    ForeignKeyConstraint(["run_id", "zone", "field"],
                         ["field_values.run_id", "field_values.zone", "field_values.field"]),
    **STRICT,
)
value_pages = Table(
    "value_pages", metadata,
    Column("run_id", Integer, nullable=False),
    Column("zone", Text, nullable=False),
    Column("field", Text, nullable=False),
    Column("position", Integer, nullable=False),
    Column("page", Integer, nullable=False),
    PrimaryKeyConstraint("run_id", "zone", "field", "position", "page"),
    ForeignKeyConstraint(["run_id", "zone", "field"],
                         ["field_values.run_id", "field_values.zone", "field_values.field"]),
    **STRICT,
)
ZONE_TABLES = (zone_results, field_values, number_values, text_values, value_pages)  # parents first


def get_engine(url=None):
    """Connect (creating the database and tables if needed)."""
    return make_engine(url or os.environ.get("ZONING_DATABASE_URL") or DEFAULT_URL, metadata)


# ---------------------------------------------------------------- writing runs

REUSE_KEYS = ("place_kind", "place", "provider", "model", "effort", "document_sha256", "fields_sha256")


def start_run(engine, inputs, zones_requested, zones_from):
    """Record a new run as 'running' and return its id. `inputs` holds every runs column from
    place_kind through fields_sha256."""
    with engine.begin() as conn:
        return conn.execute(insert(runs).values(**inputs, started_at=now_utc(), status="running",
                                                zones_requested=zones_requested,
                                                zones_from=zones_from)).inserted_primary_key[0]


def finish_run(engine, run_id, status, zones_failed=0, error=None):
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.run_id == run_id)
                     .values(status=status, finished_at=now_utc(), zones_failed=zones_failed, error=error))


def find_reusable(engine, inputs):
    """For each zone code (casefolded), the newest result from a finished run with exactly these
    inputs: {code.casefold(): (source run_id, code as stored there)}."""
    query = (select(zone_results.c.run_id, zone_results.c.zone)
             .join(runs, runs.c.run_id == zone_results.c.run_id)
             .where(*(runs.c[k] == inputs[k] for k in REUSE_KEYS), runs.c.status.in_(RESULT_STATUSES))
             .order_by(zone_results.c.run_id.desc()))
    found = {}
    with engine.connect() as conn:
        for source_run, zone in conn.execute(query):
            found.setdefault(zone.casefold(), (source_run, zone))
    return found


def copy_zone(engine, run_id, source_run_id, old_zone, new_zone):
    """Copy a zone's results from an earlier run into this run (so every run is self-contained),
    noting which run's model call produced them."""
    with engine.begin() as conn:
        for table in ZONE_TABLES:
            columns = []
            for c in table.c:
                if c.name == "run_id":
                    columns.append(literal(run_id, Integer))
                elif c.name == "zone":
                    columns.append(literal(new_zone, Text))
                elif c.name == "reused_from_run_id":
                    columns.append(func.coalesce(c, literal(source_run_id, Integer)))  # keep the original producer
                else:
                    columns.append(c)
            source = select(*columns).where(table.c.run_id == source_run_id, table.c.zone == old_zone)
            conn.execute(insert(table).from_select([c.name for c in table.c], source))


def save_zone(engine, run_id, zone, result, fields):
    """Store one zone's validated model answer, all in one transaction."""
    keys = {"run_id": run_id, "zone": zone}
    rows = {table: [] for table in ZONE_TABLES[1:]}
    for name, spec in fields.items():
        answer = result[name]
        rows[field_values].append({**keys, "field": name, "kind": spec["kind"], "status": answer["status"]})
        for position, value in enumerate(answer["values"]):
            common = {**keys, "field": name, "position": position,
                      "condition": value.get("condition") or None,  # "" means no condition too
                      "quote": value["quote"]}
            if value.get("text") is not None:
                rows[text_values].append({**common, "text": value["text"]})
            else:
                rows[number_values].append({**common, "reported_value": float(value["number"]),
                                            "reported_unit": value["unit"]})
            rows[value_pages] += [{**keys, "field": name, "position": position, "page": page}
                                  for page in dict.fromkeys(value["pages"])]
    with engine.begin() as conn:
        conn.execute(insert(zone_results).values(**keys, found_in_document=int(result["found_in_document"]),
                                                 notes=result["notes"]))
        for table, table_rows in rows.items():
            if table_rows:
                conn.execute(insert(table), table_rows)


# ---------------------------------------------------------------- reading and exporting

def list_runs(engine, place_kind, place):
    """All runs for a place, newest first, as dicts."""
    query = (select(runs).where(runs.c.place_kind == place_kind, runs.c.place == place)
             .order_by(runs.c.run_id.desc()))
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(query).mappings()]


def count_runs_with_results(engine):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(runs).where(runs.c.status.in_(RESULT_STATUSES))).scalar()


def get_run(engine, run_id):
    with engine.connect() as conn:
        row = conn.execute(select(runs).where(runs.c.run_id == run_id)).mappings().first()
    return dict(row) if row else None


def run_label(run):
    """e.g. 'Oct 28 2026, 14:02 · gcloud / gemini-4-argon / high · partial (5 of 6 zones)', plus
    ' · zones found by the model' when the model chose the zone list."""
    when = run["started_at"].astimezone().strftime("%b %d %Y, %H:%M")
    status = run["status"] if run["finished_at"] or run["status"] != "running" else "running or interrupted"
    done = run["zones_requested"] - (run["zones_failed"] or 0)
    detail = f" ({done} of {run['zones_requested']} zones)" if run["status"] == "partial" else ""
    source = " · zones found by the model" if run["zones_from"] == "model" else ""
    return f"{when} · {run['provider']} / {run['model']} / {run['effort']} · {status}{detail}{source}"


def default_filename(run, extension="csv"):
    when = run["started_at"].astimezone().strftime("%Y-%m-%d_%H%M")
    prefix = "custom_" if run["place_kind"] == "custom" else ""  # so a custom name can't pass for a town
    return f"{prefix}{safe_name(run['place'])}_{safe_name(run['provider'] + '.' + run['model'] + '.' + run['effort'])}_{when}_zones.{extension}"


def _plain(x):
    """A number for display or JSON: 6000.0 -> 6000, 21780.004 -> 21780, 2.5 -> 2.5."""
    x = round(x, 2)
    return int(x) if x.is_integer() else x


def _load_values(conn, run_id):
    """{(zone, field): [value dicts in the model's order]}, each with its pages, and numbers converted
    to the standard unit of the field's kind ('standard_value'/'standard_unit' None if impossible)."""
    kinds = {(r.zone, r.field): r.kind for r in conn.execute(
        select(field_values.c.zone, field_values.c.field, field_values.c.kind).where(field_values.c.run_id == run_id))}
    pages = defaultdict(list)
    for r in conn.execute(select(value_pages).where(value_pages.c.run_id == run_id).order_by(value_pages.c.page)):
        pages[(r.zone, r.field, r.position)].append(r.page)
    values = defaultdict(list)
    for r in conn.execute(select(number_values).where(number_values.c.run_id == run_id)):
        target, convert = KINDS[kinds[(r.zone, r.field)]].get(r.reported_unit, (None, None))
        try:
            standard = convert(r.reported_value) if convert else None
        except ZeroDivisionError:
            standard = None
        values[(r.zone, r.field)].append({
            "position": r.position, "number": r.reported_value, "unit": r.reported_unit,
            "standard_value": standard, "standard_unit": target if standard is not None else None,
            "condition": r.condition, "quote": r.quote, "pages": pages[(r.zone, r.field, r.position)]})
    for r in conn.execute(select(text_values).where(text_values.c.run_id == run_id)):
        values[(r.zone, r.field)].append({"position": r.position, "text": r.text, "condition": r.condition,
                                          "quote": r.quote, "pages": pages[(r.zone, r.field, r.position)]})
    for field_list in values.values():
        field_list.sort(key=lambda v: v["position"])
    return values


def format_values(values, value_format):
    """The <field>_values cell: one readable line per value, or a pretty-printed JSON array. Empty if no values."""
    if not values:
        return ""
    if value_format == "json":
        items = []
        for v in values:
            if "text" in v:
                item = {"text": v["text"]}
            elif v["standard_value"] is not None:
                item = {"value": _plain(v["standard_value"]), "unit": v["standard_unit"]}
            else:  # couldn't convert (e.g. 0 sq ft per unit): give it as reported
                item = {"value": _plain(v["number"]), "unit": v["unit"]}
            items.append({**item, "condition": v["condition"], "pages": v["pages"], "quote": v["quote"]})
        return json.dumps(items, ensure_ascii=False, indent=2)
    lines = []
    for v in values:
        if "text" in v:
            line = v["text"]
        elif v["standard_value"] is not None:
            line = f"{_plain(v['standard_value'])}{UNIT_LABELS.get(v['standard_unit'], ' ' + v['standard_unit'])}"
        else:
            line = f"{_plain(v['number'])} {v['unit']}"
        if v["condition"]:
            line += f" — IF {v['condition']}"
        quote = (v["quote"] or "").replace('"', "''")  # keep the outer quote marks unambiguous
        source = ", ".join(map(str, v["pages"]))
        source = (f'p. {source}: "{quote}"' if source and quote else f"p. {source}" if source
                  else f'"{quote}"' if quote else "")
        lines.append(f"{line} ({source})" if source else line)
    return "\n".join(lines)


def summarize_field(kind, field_vals, status):
    """A field's general (unconditional) value and the flags a person should check, as (general, flags).
    general: for text fields, the general texts joined with "; "; otherwise {standard unit: a number when
    exactly one general value has that unit, else None (the reason is in flags)}."""
    if kind == "text":
        return "; ".join(v["text"] for v in field_vals if "text" in v and not v["condition"]), []
    general, flags = {}, []
    numbers = [v for v in field_vals if "number" in v]
    if any(v["standard_value"] is None for v in numbers):
        flags.append("a value couldn't be converted to a standard unit")
    for unit in standard_units(kind):
        found = sorted({round(v["standard_value"], 2) for v in numbers
                        if v["standard_unit"] == unit and not v["condition"]})
        general[unit] = _plain(found[0]) if len(found) == 1 else None
        if len(found) > 1:
            flags.append(f"several general {unit} values ({', '.join(str(_plain(g)) for g in found)})")
    if status == "stated" and not numbers:
        flags.append("stated, but not as a number")
    elif numbers and all(v["condition"] for v in numbers):
        flags.append("only conditional values")
    return general, flags


def _load_run(engine, run_id):
    """(run, fields, zones sorted by code, {(zone, field): status}, {(zone, field): values})."""
    run = get_run(engine, run_id)
    with engine.connect() as conn:
        zones = conn.execute(select(zone_results).where(zone_results.c.run_id == run_id)).mappings().all()
        statuses = {(r.zone, r.field): r.status for r in conn.execute(
            select(field_values).where(field_values.c.run_id == run_id))}
        values = _load_values(conn, run_id)
    return run, parse_fields(run["fields_toml"]), sorted(zones, key=lambda z: z["zone"].casefold()), statuses, values


def export_rows(engine, run_id, value_format="readable"):
    """A run as CSV rows (one per zone), using the fields file the run itself used:
        <field>                  text fields: the general (unconditional) values, joined with "; "
        <field>_<standard unit>  measurement fields: a number only when exactly one general value has
                                 that unit; otherwise blank, with the reason in flags
        <field>_status           stated / not_applicable / not_found
        <field>_values           every value with its condition, pages and quote (readable or JSON)
    then flags, found_in_document and notes."""
    _, fields, zones, statuses, values = _load_run(engine, run_id)
    rows = []
    for z in zones:
        row, flags = {"zone": z["zone"]}, []
        for name, spec in fields.items():
            field_vals = values[(z["zone"], name)]
            status = statuses.get((z["zone"], name), "")
            general, field_flags = summarize_field(spec["kind"], field_vals, status)
            if spec["kind"] == "text":
                row[name] = general
            else:
                for unit, number in general.items():  # None, not "": keeps the column numeric
                    row[name if spec["kind"] == "count" else f"{name}_{unit}"] = number
            flags += [f"{name}: {flag}" for flag in field_flags]
            row[f"{name}_status"] = status
            row[f"{name}_values"] = format_values(field_vals, value_format)
        row.update(flags="; ".join(flags), found_in_document=bool(z["found_in_document"]), notes=z["notes"])
        rows.append(row)
    return rows


def _number(x):
    """A stored number for JSON, unrounded: 6000.0 -> 6000, 0.115 -> 0.115."""
    return int(x) if x.is_integer() else x


def _when(timestamp):
    return timestamp.isoformat() if timestamp else None


def export_json(engine, run_id, document_file=None):
    """A run as one nested JSON-ready dict, for code and the results viewer. `document_file` is the
    name of the document copy saved next to the JSON (pages can then link to it), or None.

    {"format": "zoning-run", "version": 1,
     "run": {id, place, place_kind, provider, model, effort, status, error, started_at, finished_at
             (ISO 8601, UTC), zones_requested, zones_failed, zones_from, label},
     "document": {name, sha256, file},
     "fields": [{name, kind, description, units}]    in the fields file's order; units = standard units
     "zones": [{zone, found_in_document, notes, reused_from_run,
                fields: {<field>: {status, general, flags, values}}}]}

    general and flags are as in summarize_field. Each value is {"text"} or {"value", "unit"} in the
    standard unit (both None if it couldn't be converted) plus {"reported_value", "reported_unit"} as the
    document gave it; then condition (None = applies generally), quote and pages (1 = first page of the file)."""
    run, fields, zones, statuses, values = _load_run(engine, run_id)
    out_zones = []
    for z in zones:
        out_fields = {}
        for name, spec in fields.items():
            field_vals = values[(z["zone"], name)]
            status = statuses.get((z["zone"], name))
            general, flags = summarize_field(spec["kind"], field_vals, status)
            items = []
            for v in field_vals:
                if "text" in v:
                    item = {"text": v["text"]}
                else:
                    standard = v["standard_value"]
                    item = {"value": _plain(standard) if standard is not None else None, "unit": v["standard_unit"],
                            "reported_value": _number(v["number"]), "reported_unit": v["unit"]}
                items.append({**item, "condition": v["condition"], "quote": v["quote"], "pages": v["pages"]})
            out_fields[name] = {"status": status, "general": general, "flags": flags, "values": items}
        out_zones.append({"zone": z["zone"], "found_in_document": bool(z["found_in_document"]), "notes": z["notes"],
                          "reused_from_run": z["reused_from_run_id"], "fields": out_fields})
    return {
        "format": "zoning-run", "version": 1,
        "run": {"id": run["run_id"], "place": run["place"], "place_kind": run["place_kind"],
                "provider": run["provider"], "model": run["model"], "effort": run["effort"],
                "status": run["status"], "error": run["error"],
                "started_at": _when(run["started_at"]), "finished_at": _when(run["finished_at"]),
                "zones_requested": run["zones_requested"], "zones_failed": run["zones_failed"],
                "zones_from": run["zones_from"], "label": run_label(run)},
        "document": {"name": run["document_name"], "sha256": run["document_sha256"], "file": document_file},
        "fields": [{"name": name, "kind": spec["kind"], "description": spec["description"],
                    "units": standard_units(spec["kind"])} for name, spec in fields.items()],
        "zones": out_zones,
    }


def find_document(run, given=None):
    """The run's document on disk, checked against the hash stored with the run, or None. `given` is a
    path from the user: a mismatch is an error. Without one, a town run's file in zoning_docs/ is used if
    it still matches."""
    if given is not None:
        if not given.is_file():
            raise ValueError(f"No file at {given}.")
        if file_sha256(given) != run["document_sha256"]:
            raise ValueError(f"{given.name} isn't the document run {run['run_id']} used ({run['document_name']}): "
                             "its contents are different, so page numbers wouldn't match.")
        return given
    if run["place_kind"] == "town":
        default = BASE_DIR / "zoning_docs" / run["document_name"]
        if default.is_file() and file_sha256(default) == run["document_sha256"]:
            return default
    return None


# ---------------------------------------------------------------- command line

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    runs_command = commands.add_parser("runs", help="list a place's runs, newest first")
    export_command = commands.add_parser("export", help="write a run as a CSV or JSON (default: the newest run with results)")
    for command in (runs_command, export_command):
        which = command.add_mutually_exclusive_group(required=True)
        which.add_argument("--town", help="a town key, e.g. new_haven")
        which.add_argument("--name", help="a custom name, as given to zoning.py --name")
        if command is export_command:
            which.add_argument("--run", type=int, help="a specific run id (see the runs command)")
    export_command.add_argument("--format", choices=["csv", "json"], default="csv",
                                help="csv: one row per zone (default). json: one nested file with every value, "
                                     "for code and the results viewer")
    export_command.add_argument("--values", choices=["readable", "json"],
                                help="csv only: how the <field>_values columns are written (default: readable)")
    export_command.add_argument("--doc", type=Path,
                                help="json only: the run's zoning document, copied next to the JSON so page numbers "
                                     "can link to it. It must be the exact file the run used. A town's file in "
                                     "zoning_docs/ is found without this.")
    export_command.add_argument("--out", type=Path,
                                help="output path (default: [custom_]<place>_<model>_<date>_zones.csv or .json)")
    args = parser.parse_args()
    if args.command == "export" and args.format == "json" and args.values:
        parser.error("--values only applies to --format csv")
    if args.command == "export" and args.format == "csv" and args.doc:
        parser.error("--doc only applies to --format json")
    load_env()
    engine = get_engine()
    place_kind, place = ("town", args.town) if args.town else ("custom", args.name)

    if args.command == "runs":
        found = list_runs(engine, place_kind, place) if place else []
        if not found:
            raise SystemExit(f"No zoning runs for {place!r}.")
        for run in found:
            print(f"run {run['run_id']:>4}  {run_label(run)}" + (f"  ({run['error']})" if run["error"] else ""))
        return

    if getattr(args, "run", None):
        run = get_run(engine, args.run)
        if run is None:
            raise SystemExit(f"No run {args.run}.")
    else:
        all_runs = list_runs(engine, place_kind, place)
        run = next((r for r in all_runs if r["status"] in RESULT_STATUSES), None)
        if run is None:
            raise SystemExit(f"No zoning results for {place!r}.")
        if all_runs[0]["run_id"] != run["run_id"]:  # never fall back silently
            latest = all_runs[0]
            print(f"Note: the newest run ({latest['run_id']}, {run_label(latest)}) has no results"
                  + (f": {latest['error']}" if latest["error"] else "") + f". Exporting run {run['run_id']} instead.")
    out = args.out or Path(default_filename(run, args.format))
    if args.format == "csv":
        rows = export_rows(engine, run["run_id"], args.values or "readable")
        write_csv(out, rows, join_lines=False)
        print(f"Wrote {len(rows)} zones from run {run['run_id']} ({run_label(run)}) to {out}")
        return

    try:
        document = find_document(run, args.doc)
    except ValueError as e:
        raise SystemExit(str(e))
    document_copy = None
    if document:  # named after the JSON, so the pair stays together (e.g. as release files)
        document_copy = out.with_name(out.stem.removesuffix("_zones") + "_document" + document.suffix)
        shutil.copyfile(document, document_copy)
    data = export_json(engine, run["run_id"], document_copy.name if document_copy else None)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(data['zones'])} zones from run {run['run_id']} ({run_label(run)}) to {out}")
    if document_copy:
        print(f"Copied the zoning document to {document_copy}")
    else:
        print(f"No document copy, so page numbers won't link to it. To add one: --doc <path to {run['document_name']}>")


if __name__ == "__main__":
    main()
