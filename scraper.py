"""Scrape property records for a town's parcel list into the scraper database (see scraper_db.py).

Usage:
    python scraper.py --town waterbury [--limit N] [--ids ID ...] [--refresh]

Each run is added to the database and never overwritten; downloaded pages are stored there too and
reused for --refresh_days. Get CSVs out with: python scraper_db.py export --town T
"""

import argparse

from common import DEFAULT_REFRESH_DAYS, FetchError, Session, load_env, read_id_list
from db_common import database_label, now_utc
from scraper_db import PageCache, finish_run, get_engine, save_failure, save_parcel, start_run
from towns import waterbury, new_haven

TOWNS = {"waterbury": waterbury,
         "new_haven": new_haven}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--town", required=True, choices=TOWNS)
    parser.add_argument("--limit", type=int, help="only scrape the first N parcels in the list")
    parser.add_argument("--ids", nargs="+", help="scrape these parcel IDs instead of the town's list")
    parser.add_argument("--refresh", action="store_true", help="re-download pages even if stored recently")
    parser.add_argument("--refresh_days", type=int, default=DEFAULT_REFRESH_DAYS,
                        help=f"re-download stored pages if they are this old (default: {DEFAULT_REFRESH_DAYS})")
    args = parser.parse_args()
    load_env()

    town = TOWNS[args.town]
    ids = {}  # a dict keeps list order and finds repeats quickly
    for raw_id in (i.strip() for i in (args.ids or read_id_list(town.ID_LIST))):
        if raw_id in ids:
            print(f"Skipping {raw_id}: it's in the list more than once")
        elif raw_id:
            ids[raw_id] = None
    ids = list(ids)
    if args.limit:
        ids = ids[: args.limit]

    engine = get_engine()
    run_id = start_run(engine, args.town, len(ids))
    ok, failed = 0, []
    try:
        for i, raw_id in enumerate(ids, 1):
            stamp = now_utc()  # this parcel's scraped_at; pages it downloads get the same time
            try:
                parcel_id, fields, tables = town.scrape(Session(PageCache(engine, stamp)), raw_id,
                                                        args.refresh, args.refresh_days)
            except (FetchError, ValueError) as e:
                print(f"[{i}/{len(ids)}] {raw_id}: FAILED - {e}")
                save_failure(engine, run_id, i, raw_id, stamp, str(e))
                failed.append(raw_id)
                continue
            save_parcel(engine, run_id, i, raw_id, parcel_id, stamp, fields, tables)
            ok += 1
            print(f"[{i}/{len(ids)}] {raw_id}: {fields.get('Location', '')}")
    except BaseException as e:  # Ctrl+C or a crash: record the run as stopped rather than leave it "running"
        finish_run(engine, run_id, "partial" if ok else "failed", parcels_failed=len(ids) - ok,
                   error=f"stopped: {type(e).__name__} {e}".strip())
        raise

    status = "succeeded" if ok == len(ids) else "partial" if ok else "failed"
    error = f"{len(failed)} parcel(s) failed" if failed else None
    finish_run(engine, run_id, status, parcels_failed=len(failed), error=error)
    if status == "failed":
        raise SystemExit(f"Run {run_id} failed: no parcel could be scraped.")
    print(f"To save the town's data as CSVs:  python scraper_db.py export --town {args.town}")
    print(f"\n{ok} ok, {len(failed)} failed. Run {run_id} {status}, saved to {database_label(engine)}")


if __name__ == "__main__":
    main()
