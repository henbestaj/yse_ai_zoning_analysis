"""Scrape property records for a town's parcel list into CSVs under output/.

Usage:
    python scraper.py --town waterbury [--limit N] [--ids ID ...] [--refresh]
"""

import argparse
from collections import defaultdict

from common import OUTPUT_DIR, FetchError, Session, read_id_list, write_csv
from towns import waterbury, new_haven

TOWNS = {"waterbury": waterbury,
         "new_haven": new_haven}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--town", required=True, choices=TOWNS)
    parser.add_argument("--limit", type=int, help="only scrape the first N parcels in the list")
    parser.add_argument("--ids", nargs="+", help="scrape these parcel IDs instead of the town's list")
    parser.add_argument("--refresh", action="store_true", help="re-download pages even if cached")
    parser.add_argument("--refresh_days", type=int, default=1, help="re-download cached pages if they are this old")
    args = parser.parse_args()

    town = TOWNS[args.town]
    ids = args.ids or read_id_list(town.ID_LIST)
    if args.limit:
        ids = ids[: args.limit]

    parcels, tables, errors = [], defaultdict(list), []
    for i, raw_id in enumerate(ids, 1):
        try:
            parcel_id, fields, parcel_tables = town.scrape(Session(), raw_id, args.refresh, args.refresh_days)
        except (FetchError, ValueError) as e:
            print(f"[{i}/{len(ids)}] {raw_id}: FAILED - {e}")
            errors.append({"input_id": raw_id, "error": str(e)})
            continue
        print(f"[{i}/{len(ids)}] {raw_id}: {fields.get('Location', '')}")
        parcels.append({"parcel_id": parcel_id, "input_id": raw_id, **fields})
        for name, rows in parcel_tables.items():
            tables[name].extend({"parcel_id": parcel_id, **row} for row in rows)

    write_csv(OUTPUT_DIR / args.town / "parcels.csv", parcels)
    for name, rows in tables.items():
        write_csv(OUTPUT_DIR / args.town / f"{name}.csv", rows)
    write_csv(OUTPUT_DIR / args.town / "errors.csv", errors)
    print(f"\n{len(parcels)} ok, {len(errors)} failed. CSVs written to {OUTPUT_DIR}\\{args.town}")


if __name__ == "__main__":
    main()
