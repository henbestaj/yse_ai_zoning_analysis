"""Scraper database: SQLAlchemy Core, SQLite by default, written to also run on Postgres.

Every scrape run is appended and never changed after it finishes. A town's current data is, for each
parcel, its latest successful scrape; a parcel whose newest attempt failed keeps its older data but is
flagged as stale, never shown as if it were fresh. Downloaded pages live here too (every version, each
distinct page stored once), replacing the old cache folder. The database is scraper_output/scraper.db
unless SCRAPER_DATABASE_URL is set (e.g. to a Postgres URL).

Tables (one row per ...):
    runs            scrape attempt: town, start/finish time, status (running/succeeded/partial/failed)
    parcel_scrapes  parcel in a run: input ID, parcel ID, scraped_at, ok/failed and the error
    parcel_values   field of a parcel's main record: the text as scraped, plus a parsed number or date
    table_values    cell of one of a parcel's repeating tables (sales, permits, ...), by table, row and field
    pages           distinct downloaded page: its HTML (compressed), keyed by the HTML's sha256
    fetches         download of a page: town, parcel, page name, URL, when, and which content it got

A parcel scrape stamps the pages it downloads with its own scraped_at, so the pages it used are, for
each page name, the parcel's newest fetch at or before its scraped_at (downloaded then, or reused from
an earlier download), leaving out fetches made by parcel scrapes that failed: those are never reused.

Command line:
    python scraper_db.py runs --town T
    python scraper_db.py export --town T [--run ID] [--out FOLDER]
"""

import argparse
import hashlib
import os
import re
import zlib
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import (REAL, Column, ForeignKeyConstraint, Index, Integer, LargeBinary, MetaData,
                        PrimaryKeyConstraint, Table, Text, UniqueConstraint, insert, select, update)

from common import SCRAPER_OUTPUT_DIR, load_env, write_csv
from db_common import (RESULT_STATUSES, RUN_STATUSES, STRICT, Day, Timestamp, check_in, make_engine, now_utc,
                       safe_name)

DEFAULT_URL = f"sqlite:///{(SCRAPER_OUTPUT_DIR / 'scraper.db').as_posix()}"
PARCEL_STATUSES = ("ok", "failed")
EXPORT_COLUMNS = ("run_id", "scraped_at", "stale")  # added to parcels on export; not scraped from the site

# ---------------------------------------------------------------- parsing values

# A whole value that is a number: optional $, thousands commas, decimals, then optionally % or an area
# unit, e.g. "$376,909", "0.22", "65", "84sq.ft", "420.00 S.F.". IDs with leading zeros (0392...) and
# anything with other characters ("0392-0304-0132", "10946/154") stay text only.
NUMBER = re.compile(r"\$?\s*(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|-?\.\d+)\s*(?:%|sq\.?\s?ft\.?|s\.f\.|sf|acres?)?",
                    re.I)
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


def parse_value(text):
    """(number, date) parsed from a scraped value; either or both None. The text is always kept too."""
    text = text.strip()
    match = NUMBER.fullmatch(text)
    number = None
    if match:
        digits = match.group(1).lstrip("-").replace(",", "")
        if not (len(digits.split(".")[0]) > 1 and digits.startswith("0")):  # leading zero: an ID, not a number
            number = float(digits) * (-1 if match.group(1).startswith("-") else 1)
    for fmt in DATE_FORMATS:
        try:
            return number, datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return number, None


def _value_row(value):
    text = "" if value is None else str(value)  # a few values are numbers already (New Haven's Building)
    number, day = parse_value(text)
    return {"text": text, "number": number, "date_value": day}


# ---------------------------------------------------------------- tables

metadata = MetaData()

runs = Table(
    "runs", metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True),
    Column("town", Text, nullable=False),
    Column("started_at", Timestamp, nullable=False),
    Column("finished_at", Timestamp),  # null while running (or if the run was interrupted)
    Column("status", Text, nullable=False),
    Column("error", Text),
    Column("parcels_requested", Integer, nullable=False),
    Column("parcels_failed", Integer),
    check_in("status", RUN_STATUSES),
    **STRICT,
)
parcel_scrapes = Table(
    "parcel_scrapes", metadata,
    Column("run_id", Integer, nullable=False),
    Column("position", Integer, nullable=False),  # place in the run's ID list, from 1
    Column("input_id", Text, nullable=False),
    Column("parcel_id", Text),                    # null if it failed before the ID was known
    Column("scraped_at", Timestamp, nullable=False),
    Column("status", Text, nullable=False),
    Column("error", Text),
    PrimaryKeyConstraint("run_id", "input_id"),
    UniqueConstraint("run_id", "parcel_id"),
    ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
    check_in("status", PARCEL_STATUSES),
    **STRICT,
)
# column_order keeps each field's place on the page, so exports rebuild the columns in the site's order
parcel_values = Table(
    "parcel_values", metadata,
    Column("run_id", Integer, nullable=False),
    Column("parcel_id", Text, nullable=False),
    Column("field", Text, nullable=False),
    Column("column_order", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("number", REAL),
    Column("date_value", Day),
    PrimaryKeyConstraint("run_id", "parcel_id", "field"),
    ForeignKeyConstraint(["run_id", "parcel_id"], ["parcel_scrapes.run_id", "parcel_scrapes.parcel_id"]),
    **STRICT,
)
table_values = Table(
    "table_values", metadata,
    Column("run_id", Integer, nullable=False),
    Column("parcel_id", Text, nullable=False),
    Column("table_name", Text, nullable=False),
    Column("row", Integer, nullable=False),  # the row's place in the parcel's table, from 0
    Column("field", Text, nullable=False),
    Column("column_order", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("number", REAL),
    Column("date_value", Day),
    PrimaryKeyConstraint("run_id", "parcel_id", "table_name", "row", "field"),
    ForeignKeyConstraint(["run_id", "parcel_id"], ["parcel_scrapes.run_id", "parcel_scrapes.parcel_id"]),
    **STRICT,
)
pages = Table(
    "pages", metadata,
    Column("sha256", Text, primary_key=True),  # of the HTML as UTF-8, before compressing
    Column("html", LargeBinary, nullable=False),  # zlib-compressed UTF-8
    **STRICT,
)
fetches = Table(
    "fetches", metadata,
    Column("fetch_id", Integer, primary_key=True, autoincrement=True),
    Column("town", Text, nullable=False),
    Column("parcel_id", Text, nullable=False),  # as the town's scraper names it when fetching
    Column("page", Text, nullable=False),       # e.g. "property", "sales"
    Column("url", Text, nullable=False),
    Column("fetched_at", Timestamp, nullable=False),  # the parcel scrape's stamp (its scraped_at)
    Column("sha256", Text, nullable=False),
    ForeignKeyConstraint(["sha256"], ["pages.sha256"]),
    Index("fetches_lookup", "town", "parcel_id", "page", "fetched_at"),
    **STRICT,
)


def get_engine(url=None):
    """Connect (creating the database and tables if needed)."""
    return make_engine(url or os.environ.get("SCRAPER_DATABASE_URL") or DEFAULT_URL, metadata)


# ---------------------------------------------------------------- the page cache

class PageCache:
    """Stored pages for one parcel scrape: reads the newest stored copy of a page, and saves downloads
    stamped with `stamp` (the parcel scrape's scraped_at). common.cached_fetch uses it via the Session."""

    def __init__(self, engine, stamp):
        self.engine, self.stamp = engine, stamp

    def latest(self, town, parcel_id, page):
        """(fetched_at, html) of the newest stored copy, or None. Copies downloaded by a parcel scrape
        that failed are skipped (they may be an error or maintenance page), so the next attempt
        downloads the page again instead of reusing a bad copy."""
        from_failed_scrape = (select(parcel_scrapes.c.scraped_at)
                              .join(runs, runs.c.run_id == parcel_scrapes.c.run_id)
                              .where(runs.c.town == fetches.c.town, parcel_scrapes.c.status == "failed",
                                     parcel_scrapes.c.scraped_at == fetches.c.fetched_at)  # its stamp
                              .exists())
        query = (select(fetches.c.fetched_at, pages.c.html).join(pages, pages.c.sha256 == fetches.c.sha256)
                 .where(fetches.c.town == town, fetches.c.parcel_id == parcel_id, fetches.c.page == page,
                        ~from_failed_scrape)
                 .order_by(fetches.c.fetched_at.desc(), fetches.c.fetch_id.desc()).limit(1))
        with self.engine.connect() as conn:
            row = conn.execute(query).first()
        return (row.fetched_at, zlib.decompress(row.html).decode("utf-8")) if row else None

    def save(self, town, parcel_id, page, url, html):
        data = html.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        with self.engine.begin() as conn:
            if conn.execute(select(pages.c.sha256).where(pages.c.sha256 == digest)).first() is None:
                conn.execute(insert(pages).values(sha256=digest, html=zlib.compress(data, 9)))
            conn.execute(insert(fetches).values(town=town, parcel_id=parcel_id, page=page, url=url,
                                                fetched_at=self.stamp, sha256=digest))


# ---------------------------------------------------------------- writing runs

def start_run(engine, town, parcels_requested):
    """Record a new run as 'running' and return its id."""
    with engine.begin() as conn:
        return conn.execute(insert(runs).values(town=town, started_at=now_utc(), status="running",
                                                parcels_requested=parcels_requested)).inserted_primary_key[0]


def finish_run(engine, run_id, status, parcels_failed=0, error=None):
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.run_id == run_id)
                     .values(status=status, finished_at=now_utc(), parcels_failed=parcels_failed, error=error))


def save_parcel(engine, run_id, position, input_id, parcel_id, scraped_at, fields, tables):
    """Store one parcel's scraped fields and tables, all in one transaction."""
    keys = {"run_id": run_id, "parcel_id": parcel_id}
    field_rows = [{**keys, "field": field, "column_order": i, **_value_row(value)}
                  for i, (field, value) in enumerate(fields.items())]
    cell_rows = [{**keys, "table_name": name, "row": r, "field": field, "column_order": i, **_value_row(value)}
                 for name, rows in tables.items() for r, row in enumerate(rows)
                 for i, (field, value) in enumerate(row.items())]
    with engine.begin() as conn:
        conn.execute(insert(parcel_scrapes).values(**keys, position=position, input_id=input_id,
                                                   scraped_at=scraped_at, status="ok"))
        for table, rows in ((parcel_values, field_rows), (table_values, cell_rows)):
            if rows:
                conn.execute(insert(table), rows)


def save_failure(engine, run_id, position, input_id, scraped_at, error):
    with engine.begin() as conn:
        conn.execute(insert(parcel_scrapes).values(run_id=run_id, position=position, input_id=input_id,
                                                   scraped_at=scraped_at, status="failed", error=error))


# ---------------------------------------------------------------- reading and exporting

def list_runs(engine, town):
    """All runs for a town, newest first, as dicts."""
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(runs).where(runs.c.town == town)
                                              .order_by(runs.c.run_id.desc())).mappings()]


def get_run(engine, run_id):
    with engine.connect() as conn:
        row = conn.execute(select(runs).where(runs.c.run_id == run_id)).mappings().first()
    return dict(row) if row else None


def towns_with_data(engine):
    """Towns that have at least one successfully scraped parcel, sorted."""
    query = (select(runs.c.town).join(parcel_scrapes, parcel_scrapes.c.run_id == runs.c.run_id)
             .where(parcel_scrapes.c.status == "ok").distinct())
    with engine.connect() as conn:
        return sorted(conn.execute(query).scalars())


def run_label(run):
    """e.g. 'Nov 03 2026, 14:02 · partial (45 of 47 parcels)'."""
    when = run["started_at"].astimezone().strftime("%b %d %Y, %H:%M")
    status = run["status"] if run["finished_at"] or run["status"] != "running" else "running or interrupted"
    done = run["parcels_requested"] - (run["parcels_failed"] or 0)
    detail = f" ({done} of {run['parcels_requested']} parcels)" if run["status"] == "partial" else \
        f" ({run['parcels_requested']} parcels)" if run["status"] == "succeeded" else ""
    return f"{when} · {status}{detail}"


def default_folder(town, run=None):
    """<town>_run<N>_<date> for one run, or <town>_current_<now> for the current data."""
    if run is None:
        return f"{safe_name(town)}_current_{now_utc().astimezone().strftime('%Y-%m-%d_%H%M')}"
    return f"{safe_name(town)}_run{run['run_id']}_{run['started_at'].astimezone().strftime('%Y-%m-%d_%H%M')}"


def _attempts(conn, town, run_id):
    """Parcel scrapes to read: one run's, or every run's for the town, oldest first."""
    query = (select(parcel_scrapes, runs.c.town).join(runs, runs.c.run_id == parcel_scrapes.c.run_id)
             .where(runs.c.town == town).order_by(parcel_scrapes.c.scraped_at, parcel_scrapes.c.run_id))
    if run_id is not None:
        query = query.where(parcel_scrapes.c.run_id == run_id)
    return [dict(r) for r in conn.execute(query).mappings()]


def export_tables(engine, town, run_id=None):
    """The town's data as {table name: list of row dicts}, ready for CSV: "parcels", one entry per
    repeating table (e.g. "sales"), and "errors".

    Without run_id, the current data: each parcel's latest successful scrape, with parcels.csv saying
    when and in which run it was scraped, and "stale" naming a newer attempt that failed; errors lists
    the parcels whose newest attempt failed. With run_id, exactly that run: its parcels and its failures.
    Values are the text as scraped; columns follow the site's order."""
    with engine.connect() as conn:
        attempts = _attempts(conn, town, run_id)
        newest = {}  # input ID -> its newest attempt; parcels are matched across runs by input ID
        chosen = {}  # input ID -> its newest successful attempt
        for a in attempts:
            newest[a["input_id"]] = a
            if a["status"] == "ok":
                chosen[a["input_id"]] = a
        ids_by_run = defaultdict(list)
        for a in chosen.values():
            ids_by_run[a["run_id"]].append(a["parcel_id"])
        # (run, parcel) -> its field values; (run, parcel) -> {table name: {row: its cells}}
        fields = defaultdict(list)
        cells = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        for run, ids in ids_by_run.items():
            for r in conn.execute(select(parcel_values).where(parcel_values.c.run_id == run,
                                                              parcel_values.c.parcel_id.in_(ids))):
                fields[(r.run_id, r.parcel_id)].append(r)
            for r in conn.execute(select(table_values).where(table_values.c.run_id == run,
                                                             table_values.c.parcel_id.in_(ids))):
                cells[(r.run_id, r.parcel_id)][r.table_name][r.row].append(r)

    # One run: its list order. Current data: by parcel ID (it mixes runs, so no single list order applies).
    order = sorted(chosen.values(), key=(lambda a: a["position"]) if run_id is not None else (lambda a: a["parcel_id"]))
    parcels_rows, table_rows = [], defaultdict(list)
    for a in order:
        key = (a["run_id"], a["parcel_id"])
        row = {"parcel_id": a["parcel_id"], "input_id": a["input_id"]}
        row.update((v.field, v.text) for v in sorted(fields[key], key=lambda v: v.column_order))
        row.update(run_id=a["run_id"], scraped_at=a["scraped_at"].astimezone().strftime("%Y-%m-%d %H:%M"))
        if run_id is None:
            latest = newest[a["input_id"]]
            row["stale"] = ("" if latest is a else f"newest attempt failed (run {latest['run_id']}, "
                            f"{latest['scraped_at'].astimezone().strftime('%Y-%m-%d %H:%M')}): {latest['error']}")
        parcels_rows.append(row)
        for name, rows in cells[key].items():
            for r in sorted(rows):
                table_rows[name].append({"parcel_id": a["parcel_id"],
                                         **{v.field: v.text for v in sorted(rows[r], key=lambda v: v.column_order)}})
    errors = [{"input_id": a["input_id"], "run_id": a["run_id"],
               "attempted_at": a["scraped_at"].astimezone().strftime("%Y-%m-%d %H:%M"), "error": a["error"],
               **({"older_data": "yes" if a["input_id"] in chosen else "no"} if run_id is None else {})}
              for a in newest.values() if a["status"] == "failed"]
    return {"parcels": parcels_rows, **dict(sorted(table_rows.items())), "errors": errors}


def write_export(tables, folder):
    """Write export_tables() output as <table>.csv files in `folder` (multi-line values joined with ", ")."""
    for name, rows in tables.items():
        write_csv(Path(folder) / f"{name}.csv", rows)


# ---------------------------------------------------------------- command line

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    runs_command = commands.add_parser("runs", help="list a town's runs, newest first")
    export_command = commands.add_parser("export", help="write a town's data as CSVs (default: the current data)")
    for command in (runs_command, export_command):
        command.add_argument("--town", required=True, help="a town key, e.g. new_haven")
    export_command.add_argument("--run", type=int, help="export only this run (see the runs command)")
    export_command.add_argument("--out", type=Path,
                                help="folder for the CSVs (default: <town>_current_<date>, or <town>_run<N>_<date>)")
    args = parser.parse_args()
    load_env()
    engine = get_engine()

    all_runs = list_runs(engine, args.town)
    if args.command == "runs":
        if not all_runs:
            raise SystemExit(f"No scrape runs for {args.town!r}.")
        for run in all_runs:
            print(f"run {run['run_id']:>4}  {run_label(run)}" + (f"  ({run['error']})" if run["error"] else ""))
        return

    run = None
    if args.run:
        run = get_run(engine, args.run)
        if run is None or run["town"] != args.town:
            raise SystemExit(f"No run {args.run} for {args.town!r}.")
    elif not all_runs:
        raise SystemExit(f"No scrape runs for {args.town!r}.")
    elif all_runs[0]["status"] not in RESULT_STATUSES:  # never let a failed newest run go unmentioned
        latest = all_runs[0]
        print(f"Note: the newest run ({latest['run_id']}, {run_label(latest)}) has no results"
              + (f": {latest['error']}" if latest["error"] else "") + ". Exporting each parcel's latest success.")
    tables = export_tables(engine, args.town, run["run_id"] if run else None)
    folder = args.out or Path(default_folder(args.town, run))
    write_export(tables, folder)
    stale = sum(1 for row in tables["parcels"] if row.get("stale"))
    print(f"Wrote {len(tables['parcels'])} parcels" + (f" ({stale} stale)" if stale else "")
          + f", {len(tables) - 2} tables and {len(tables['errors'])} errors to {folder}")


if __name__ == "__main__":
    main()
