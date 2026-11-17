# Property & Zoning Toolkit

Tools for collecting Connecticut property and zoning data:

- **Property scraper**: downloads the public assessor record for each parcel in a list (currently Waterbury and New Haven) and saves it to a database.
- **Zoning tool**: sends a town's zoning ordinance (PDF) to a Gemini model and builds a table with one row per zoning district: minimum lot area, setbacks, height, density, permitted uses, and so on. Every value comes with the exact quote and page it was taken from.
- **Streamlit app**: does both of the above from a browser, with downloads of the results.

Every run is saved and never overwritten, so earlier results stay available and you can always see where a value came from.

## Setup

The project uses [uv](https://docs.astral.sh/uv/) with Python 3.13. From this folder:

```
uv sync
```

This creates `.venv` and installs the exact versions in `uv.lock`. Run everything with `uv run`, e.g. `uv run python scraper.py --town waterbury`, or activate `.venv` first and use plain `python`. To add a package, use `uv add <package>`, which updates `pyproject.toml` and `uv.lock` together.

Copy `.env.example` to `.env` and fill in the keys for the services you use:

| Variable | Needed for |
|---|---|
| `GEMINI_API_KEY` | zoning with `--model-provider gemini` (the default) |
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | zoning with `--model-provider gcloud` (Gemini on Vertex AI). Also run `gcloud auth application-default login` once. |
| `CLOUDFLARE_API_KEY`, `CLOUDFLARE_ACCOUNT_ID` | the decision model that picks a town's zone column when it's unclear |

Never commit `.env`; it's in `.gitignore`.

## The app

```
uv run streamlit run app.py
```

It has three tabs:

- **Scrape parcels** runs the scraper and shows and downloads a town's data, either the current data or a single run.
- **Build zoning table** runs the zoning tool for a scraped town or for any other document and downloads the results.
- **Zoning fields** shows and edits the fields the model looks for.

Use the terminal for long runs. The app stops a running job if the page reruns, for example when you start a second job.

## Scraping property records

```
uv run python scraper.py --town waterbury                 # every parcel in property_lists/waterbury.txt
uv run python scraper.py --town new_haven --limit 5       # only the first 5
uv run python scraper.py --town new_haven --ids 17518 15959
uv run python scraper.py --town waterbury --refresh       # re-download pages even if stored recently
```

- **Parcel lists** are in `property_lists/<town>.txt`, one ID per line. Waterbury uses the 12-digit unique ID; New Haven uses the site's PID.
- **Downloads are reused.** Pages downloaded within the last day (`--refresh_days`) are taken from the database instead of downloaded again, so rerunning is cheap. Pages saved by a parcel scrape that failed are never reused.
- **Failures** don't stop the run. A parcel that fails is recorded with its error and the run continues; the run is then marked `partial`. Ctrl+C stops the run and records it as stopped.

Get the data out as CSVs:

```
uv run python scraper_db.py runs --town new_haven              # list the runs
uv run python scraper_db.py export --town new_haven            # current data
uv run python scraper_db.py export --town new_haven --run 3    # exactly one run
```

An export is a folder of CSVs:

- `parcels.csv`: one row per parcel.
- One CSV per repeating table (`sales.csv`, `permits.csv`, `buildings.csv`, …): one row per sale, permit, etc., with a `parcel_id` column to join on.
- `errors.csv`: parcels that failed.

**Current data** means each parcel's latest successful scrape, which may come from different runs. The `run_id` and `scraped_at` columns say which. If a parcel's newest attempt failed, it keeps its older data and the `stale` column says so.

## Building a zoning table

There are three ways to say which districts to look up.

**A scraped town.** The zone codes come from the town's scraped parcels. The document defaults to `zoning_docs/<town>.pdf`.
```
uv run python zoning.py --town new_haven
```

**Any other place, with your own list of codes:**
```
uv run python zoning.py --name "Darien" --doc "path/to/Darien Zoning.pdf" --zones R-1 R-2 CBD
```

**Any other place, with the model listing the districts.** Leave out `--zones`, and the model first lists the document's districts (overlay districts are left out). Add `--list-zones` to only print that list, so you can check it before paying for a full run:
```
uv run python zoning.py --name "Darien" --doc "path/to/Darien Zoning.pdf" --list-zones
uv run python zoning.py --name "Darien" --doc "path/to/Darien Zoning.pdf"
```

### Model options

All three forms take these options:

- `--model-provider gemini|gcloud`
- `--model-name ID`
- `--model-effort low|medium|high`

### Reuse

A zone already looked up with the same place, model, effort, document and fields file is copied from the earlier run, with no model call. If a run fails partway, rerunning the same command only queries the zones that are missing. `--refresh` redoes every zone.

### Exporting

```
uv run python zoning_db.py runs --name "Darien"
uv run python zoning_db.py export --name "Darien"        # newest run with results, as a CSV
uv run python zoning_db.py export --run 5 --values json  # CSV with the _values cells as JSON
uv run python zoning_db.py export --run 5 --format json --doc "path/to/Darien Zoning.pdf"
```

The CSV has one row per district. Each field from `zoning_fields.toml` becomes these columns:

- **A number column in a standard unit**, e.g. `min_lot_area_sq_ft`. It's filled only when the document states exactly one value that applies generally.
- **`<field>_status`**: `stated`, `not_applicable` or `not_found`.
- **`<field>_values`**: every value the model found, with its condition, pages and quote. `--values readable` (the default) writes one line per value; `--values json` writes a JSON array.

A `flags` column explains blank or unusual values. Check `_values` before relying on a number.

`--format json` writes the whole run as one nested file instead, for code and the results viewer: the run's details, the fields, then each district's fields with their status, general value, flags and every value (standard and reported units, condition, quote, pages). The `export_json` docstring in `zoning_db.py` describes the layout. `--doc` copies the zoning document next to the JSON as `..._document.pdf`, so page numbers can link to it. It must be the exact file the run used (checked by its contents), and a scraped town's file in `zoning_docs/` is found without it. The app's **Download JSON** button gives the same file without the document copy.

### Choosing what to collect

`zoning_fields.toml` lists the fields: name, kind (`text`, `area`, `length`, `height`, `percent`, `density`, `count`) and the description the model is given. Add, remove or reword freely. The model reports values in the document's own units, and Python does the unit conversions.

## Where data is stored

| File | Contents |
|---|---|
| `scraper_output/scraper.db` | scrape runs, parcel values, and every downloaded page (compressed, each distinct page stored once) |
| `zoning_output/zoning.db` | zoning runs, the values the model found, quotes and pages |

Both are SQLite. Set `SCRAPER_DATABASE_URL` or `ZONING_DATABASE_URL` to use a different database (the code is written to also run on Postgres). The table layouts are described at the top of `scraper_db.py` and `zoning_db.py`, and in their `--help`.

## Code layout

| File | What it does |
|---|---|
| `scraper.py` | scraper command line: runs a town's list and saves each parcel |
| `towns/` | one module per town: how to download and parse its site |
| `scraper_db.py` | scraper database: tables, page cache, export |
| `zoning.py` | zoning command line: district list, model calls, validation |
| `zoning_db.py` | zoning database: tables, reuse, export |
| `zoning_fields.toml` | the fields the zoning model collects |
| `models.py` | model clients (Gemini, Gemini on Vertex, the Cloudflare decider) with retries |
| `db_common.py` | database code shared by both databases |
| `common.py` | HTTP fetching with retries, HTML table parsing, CSV writing |
| `app.py` | the Streamlit app |

### Adding a town

1. Write `towns/<town>.py` with `TOWN`, `ID_LIST` and a `scrape(session, raw_id, refresh, refresh_days)` function that returns `(parcel_id, fields, tables)`:
   - `fields` is a dict of the main record's values.
   - `tables` is a dict of `{table name: list of row dicts}`.

   Fetch pages with `common.cached_fetch` so they're stored and reused. Use `towns/waterbury.py` as an example.
2. Add the module to `TOWNS` in `scraper.py`.
3. Add its ID list to `property_lists/<town>.txt`.
