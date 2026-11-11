"""Streamlit UI for the property scraper and the zoning table.

The app doesn't reimplement anything: it runs the existing command-line scripts (scraper.py,
zoning.py) as subprocesses, shows their progress, and shows their results from the scraper and zoning
databases, with the same CSVs the export commands write available for download.
Colors and fonts come from .streamlit/config.toml (Yale web palette, light and dark mode); the CSS
below only adds layout touches that work in both modes.

Run from web_scraper/ with the project venv:
    streamlit run app.py
"""

import contextlib
import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

import scraper_db
from common import BASE_DIR, DEFAULT_REFRESH_DAYS, csv_bytes, load_env, read_id_list
from models import DECIDERS, EXTRACTORS
from scraper import TOWNS
from zoning import DOCS_DIR, FIELDS_FILE, default_model
from zoning_db import (KINDS, RESULT_STATUSES, count_runs_with_results, default_filename, export_rows, get_engine,
                       list_runs, run_label, standard_units)

PROGRESS_LINE = re.compile(r"^\[(\d+)/(\d+)\]")  # both scripts print "[3/47] ..." per item
FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROVIDER_LABELS = {"gemini": "Gemini API", "gcloud": "Google Cloud (Vertex AI)"}

# Mode-independent styling only: Yale Blue bands with white text, #286dc0 accents (passes contrast on
# both the white and the navy background), and text that inherits the theme's color.
CSS = """
<style>
.block-container { max-width: 1160px; padding-top: 4.25rem; padding-bottom: 3rem; }  /* clears Streamlit's top bar */

.yale-hero {
  position: relative; overflow: hidden; border-radius: 16px; margin-bottom: 0.75rem;
  padding: 2.4rem 2.6rem 2.1rem; color: #ffffff;
  background: linear-gradient(118deg, #00356b 0%, #00356b 55%, #286dc0 100%);
  border: 1px solid rgba(99, 170, 255, 0.28); box-shadow: 0 14px 34px rgba(0, 53, 107, 0.22);
}
.yale-hero::before {  /* a soft light-blue glow in the corner */
  content: ""; position: absolute; right: -90px; top: -140px; width: 380px; height: 380px; border-radius: 50%;
  background: radial-gradient(circle, rgba(99, 170, 255, 0.42), rgba(99, 170, 255, 0) 70%);
}
.yale-hero::after {  /* faint vertical rules, like a ledger or a lot map */
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background-image: repeating-linear-gradient(90deg, rgba(255, 255, 255, 0.045) 0 1px, transparent 1px 72px);
}
.yale-hero > * { position: relative; z-index: 1; }
.yale-hero .eyebrow { font-size: 0.78rem; letter-spacing: 0.16em; text-transform: uppercase; font-weight: 600;
  color: #63aaff; }
.yale-hero h1 { font-family: "Crimson Pro", serif; font-weight: 600; font-size: 2.9rem; line-height: 1.1;
  color: #ffffff !important; margin: 0.35rem 0 0.6rem; padding: 0; }
.yale-hero p { font-size: 1.06rem; line-height: 1.55; max-width: 46rem; margin: 0; color: rgba(255, 255, 255, 0.9); }
.yale-hero .chips { margin-top: 1.2rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
.yale-hero .chip { font-size: 0.82rem; padding: 0.28rem 0.8rem; border-radius: 999px; color: #ffffff;
  border: 1px solid rgba(255, 255, 255, 0.32); background: rgba(255, 255, 255, 0.08); }

.stTabs [role="tablist"] { gap: 0.4rem; }
.stTabs [data-testid="stTab"] { padding: 0.75rem 1rem; }
.stTabs [data-testid="stTab"] p { font-size: 1.03rem; font-weight: 600; }

/* Selected tab / segment: no single blue is readable as text on the navy background AND as a button
   behind white text, so selected items keep the theme's text color (bold) and show their state through
   the primary-colored underline, border and tint instead. */
.stTabs [data-testid="stTab"][aria-selected="true"],
.stTabs [data-testid="stTab"][aria-selected="true"] * { color: inherit !important; }
button[data-variant="segmented_control"][aria-checked="true"],
button[data-variant="segmented_control"][aria-checked="true"] * { color: inherit !important; font-weight: 600; }

div[class*="st-key-card-"] { border-left: 4px solid #286dc0 !important; padding: 1.2rem 1.4rem 1.3rem !important; }
.step { display: flex; gap: 0.85rem; align-items: flex-start; }
.step .num { flex: 0 0 auto; width: 1.9rem; height: 1.9rem; border-radius: 50%; background: #286dc0; color: #ffffff;
  font-weight: 700; font-size: 0.95rem; display: flex; align-items: center; justify-content: center; margin-top: 0.1rem; }
.step .num .icon { font-family: "Material Symbols Rounded"; font-weight: 400; font-size: 1.15rem; }
.step .title { font-family: "Crimson Pro", serif; font-weight: 600; font-size: 1.45rem; line-height: 1.25; }
.step .sub { font-size: 0.93rem; opacity: 0.72; margin-top: 0.15rem; }

div[data-testid="stMetric"] { border-top: 3px solid #286dc0 !important; }

.yale-footer { margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid rgba(151, 141, 133, 0.35);
  font-size: 0.85rem; opacity: 0.75; display: flex; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
</style>
"""


def town_label(town):
    return town.replace("_", " ").title()


@contextlib.contextmanager
def card(key, step, title, subtitle):
    """A step: a bordered container (styled through its st-key-card-* class) with a heading. `step` is
    a number, or a Material icon name (e.g. "tune") for a card that isn't part of a sequence."""
    badge = step if isinstance(step, int) else f'<span class="icon">{html.escape(step)}</span>'
    with st.container(border=True, key=f"card-{key}"):
        st.html(f'<div class="step"><span class="num">{badge}</span><div>'
                f'<div class="title">{html.escape(title)}</div><div class="sub">{html.escape(subtitle)}</div>'
                f'</div></div>')
        yield


def run_script(args):
    """Run `python <args>` in web_scraper/, showing the current item above a progress bar.
    Returns (exit code, output lines)."""
    progress = st.progress(0.0, text="Starting...")
    lines = []
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # Windows pipes default to cp1252 otherwise
    with subprocess.Popen([sys.executable, "-u", *args], cwd=BASE_DIR, env=env, text=True,
                          encoding="utf-8", errors="replace",
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        try:
            for line in proc.stdout:
                lines.append(line.rstrip())
                match = PROGRESS_LINE.match(line)
                if match:
                    progress.progress(int(match[1]) / int(match[2]), text=line.strip())
            code = proc.wait()
        finally:
            if proc.poll() is None:  # the page was rerun or closed mid-run: don't leave the script running
                proc.kill()
    progress.progress(1.0, text="Finished" if code == 0 else "Stopped with an error")
    return code, lines


def show_result(code, lines):
    """Green if everything worked, yellow listing the failures if some items failed, red with the
    last output if the script itself stopped. Each state has its own icon, never color alone."""
    summary = lines[-1] if lines else "Done"
    failures = [line.strip() for line in lines if "FAILED" in line]
    if code != 0:
        st.error("Stopped with an error:", icon=":material/error:")
        st.code("\n".join(lines[-10:]))
    elif failures:
        st.warning(f"{summary}\n\nFailed items:", icon=":material/flag:")
        st.code("\n".join(failures))
    else:
        st.success(summary, icon=":material/check_circle:")


def read_csv(data):
    """CSV file contents (bytes) as a table of text, exactly as written."""
    try:
        return pd.read_csv(io.BytesIO(data), encoding="utf-8-sig", dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:  # e.g. errors.csv when nothing failed
        return pd.DataFrame()


def download_tables(tables, folder):
    """Preview and download buttons for export_tables() output: one CSV per table, the same files
    `scraper_db.py export` writes, plus a .zip of them all named after `folder`."""
    files = {f"{name}.csv": csv_bytes(rows) for name, rows in tables.items()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(f"{folder}/{name}", data)
    st.download_button(f"Download all ({len(files)} files, .zip)", buffer.getvalue(), icon=":material/folder_zip:",
                       file_name=f"{folder}.zip", mime="application/zip", key=f"zip-{folder}")
    for name, data in files.items():
        table = read_csv(data)
        with st.expander(f"{name}  ·  {len(table):,} rows", icon=":material/table:"):
            st.download_button(f"Download {name}", data, file_name=f"{folder}_{name}", mime="text/csv",
                               key=f"dl-{folder}-{name}", icon=":material/download:")
            st.dataframe(table, width="stretch", hide_index=True)


@st.cache_resource
def zoning_engine():
    """One database connection pool for the app (zoning_output/zoning.db unless ZONING_DATABASE_URL is set)."""
    load_env()
    return get_engine()


@st.cache_resource
def scraper_engine():
    """Same, for scraped data (scraper_output/scraper.db unless SCRAPER_DATABASE_URL is set)."""
    load_env()
    return scraper_db.get_engine()


def hero():
    scraped = scraper_db.towns_with_data(scraper_engine())
    runs = count_runs_with_results(zoning_engine())
    st.html(f"""
    <div class="yale-hero">
      <div class="eyebrow">Municipal research tools</div>
      <h1>Property &amp; Zoning Toolkit</h1>
      <p>Scrape parcel records from town assessor sites, then have Gemini read a zoning ordinance into a
         table you can download, with every value traced back to the page it came from.</p>
      <div class="chips">
        <span class="chip">{len(scraped)} town{'s' if len(scraped) != 1 else ''} scraped</span>
        <span class="chip">{runs} zoning table{'s' if runs != 1 else ''} built</span>
        <span class="chip">Gemini · Vertex AI</span>
      </div>
    </div>""")


# ---------------------------------------------------------------- Scraping

def scraping_tab():
    with card("scrape-town", 1, "Choose a town", "Each town has its own assessor website and default parcel list."):
        town = st.segmented_control("Town", list(TOWNS), default=next(iter(TOWNS)), required=True,
                                    format_func=town_label, key="scrape_town", label_visibility="collapsed")
        default_ids = read_id_list(TOWNS[town].ID_LIST)
        current = scraper_db.export_tables(scraper_engine(), town)
        scraped_before = bool(scraper_db.list_runs(scraper_engine(), town))
        col1, col2, col3 = st.columns(3)
        col1.metric("Parcels in the town list", f"{len(default_ids):,}", border=True)
        col2.metric("Scraped", f"{len(current['parcels']):,}" if scraped_before else "–", border=True,
                    help="Parcels with data: each one's latest successful scrape, from any run")
        col3.metric("Failed", f"{len(current['errors']):,}" if scraped_before else "–", border=True,
                    help="Parcels whose latest attempt failed (some may still have older data)")

    with card("scrape-ids", 2, "Pick the parcels", "Use the town's list, or scrape specific parcel IDs."):
        source = st.segmented_control("Property IDs", ["Town's list", "Upload a .txt file", "Paste IDs"],
                                      default="Town's list", required=True, label_visibility="collapsed")
        if source == "Upload a .txt file":
            uploaded = st.file_uploader("One ID per line", type=["txt", "csv"])
            ids = [line.strip() for line in uploaded.getvalue().decode("utf-8-sig").splitlines()
                   if line.strip()] if uploaded else []
        elif source == "Paste IDs":
            ids = [line.strip() for line in st.text_area("One ID per line").splitlines() if line.strip()]
        else:
            ids = default_ids
        with st.expander("Stored pages", icon=":material/history:"):
            st.caption("Every downloaded page is kept in the database; a recent copy is reused instead of "
                       "downloading the page again.")
            refresh = st.checkbox("Re-download every page, ignoring stored copies")
            refresh_days = st.number_input("Re-download pages whose stored copy is older than (days)", min_value=0,
                                           value=DEFAULT_REFRESH_DAYS)

    if st.button(f"Scrape {len(ids):,} parcels", type="primary", disabled=not ids, width="stretch",
                 icon=":material/travel_explore:"):
        args = ["scraper.py", "--town", town, "--refresh_days", str(refresh_days)]
        if refresh:
            args.append("--refresh")
        if ids is not default_ids:
            args += ["--ids", *ids]
        show_result(*run_script(args))

    with card("scrape-results", 3, "Results", "Each parcel's latest successful scrape, or any single run."):
        show_scrape_results(town)


def show_scrape_results(town):
    """A town's scraped data from the database. A failed newest run is always reported, never hidden
    behind the current data; stale parcels (newest attempt failed) are counted and flagged."""
    engine = scraper_engine()
    runs = scraper_db.list_runs(engine, town)
    if not runs:
        st.info("Nothing scraped yet.", icon=":material/info:")
        return
    newest = runs[0]
    with_results = [r for r in runs if r["status"] in RESULT_STATUSES]
    if newest["status"] not in RESULT_STATUSES:
        st.error(f"The latest run ({scraper_db.run_label(newest)}) has no results"
                 + (f": {newest['error']}" if newest["error"] else ".")
                 + ("\n\nThe current data comes from earlier runs." if with_results else ""), icon=":material/error:")
    if not with_results:
        return

    run = st.selectbox("Data from", [None, *with_results], key=f"scrape-run-{town}",
                       format_func=lambda r: "Current data: each parcel's latest successful scrape" if r is None
                       else f"Only run {r['run_id']}: {scraper_db.run_label(r)}")
    tables = scraper_db.export_tables(engine, town, run["run_id"] if run else None)
    parcels, errors = tables["parcels"], tables["errors"]
    col1, col2, col3 = st.columns(3)
    col1.metric("Parcels", f"{len(parcels):,}", border=True)
    if run is None:
        stale = sum(1 for p in parcels if p["stale"])
        col2.metric("Stale", f"{stale:,}", border=True,
                    help="Parcels showing older data because their newest attempt failed; see the stale column")
        col3.metric("Failed", f"{len(errors):,}", border=True, help="Parcels whose newest attempt failed")
        if stale:
            st.warning(f"{stale} parcel(s) show older data because their newest attempt failed; "
                       "see the stale column in parcels.csv.", icon=":material/flag:")
    else:
        col2.metric("Tables", f"{len(tables) - 2:,}", border=True, help="Repeating tables, e.g. sales")
        col3.metric("Failed in this run", f"{len(errors):,}", border=True)
    download_tables(tables, scraper_db.default_folder(town, run))


# ---------------------------------------------------------------- Zoning

def choose_document(default, key):
    """Pick the zoning document: the town's default (if any) or an upload. Returns a path, an
    uploaded file (kept in memory), or None until one is chosen."""
    options = ([f"Default: {default.name}"] if default and default.exists() else []) + ["Upload a document"]
    if st.selectbox("Zoning document", options, key=f"doc-{key}").startswith("Default"):
        return default
    return st.file_uploader("Zoning document (PDF works best)", key=f"upload-{key}")


@contextlib.contextmanager
def document_on_disk(document):
    """Yield a path zoning.py can read. An upload is written to a temporary file only for the run and
    deleted afterwards (even if the run is interrupted); zoning.py keeps its own copy with the results."""
    if isinstance(document, Path):
        yield document
        return
    # A temporary folder, so the file keeps its real name (recorded with the run, and its suffix tells the
    # model the file type); the folder and file are removed when the run ends
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / Path(document.name).name
        path.write_bytes(document.getvalue())
        yield path


def show_zoning_results(place_kind, place):
    """Results for a place from the database. The newest attempt is always reported first, so a failed
    or interrupted run is never hidden behind older results; then pick any run that has results."""
    engine = zoning_engine()
    runs = list_runs(engine, place_kind, place) if place else []
    if not runs:
        st.info("No zoning results yet.", icon=":material/info:")
        return
    with_results = [r for r in runs if r["status"] in RESULT_STATUSES]
    newest = runs[0]
    if newest["status"] not in RESULT_STATUSES:
        message = f"The latest run ({run_label(newest)}) has no results" + (f": {newest['error']}" if newest["error"] else ".")
        if with_results:
            message += f"\n\nShowing the most recent run with results instead ({run_label(with_results[0])})."
        st.error(message, icon=":material/error:")
    if not with_results:
        return

    run = st.selectbox("Results from", with_results, format_func=run_label, key=f"run-{place_kind}-{place}")
    if run["status"] == "partial":
        st.warning(f"This run is partial: {run['zones_failed']} of {run['zones_requested']} zones are missing"
                   + (f" ({run['error']})" if run["error"] else "")
                   + ". Pick an earlier run above if you need them.", icon=":material/flag:")
    value_format = st.segmented_control("Values format", ["readable", "json"], default="readable", required=True,
                                        format_func=lambda f: "Readable" if f == "readable" else "JSON",
                                        key=f"format-{place_kind}-{place}",
                                        help="How the <field>_values columns are written, on screen and in the download")
    table = pd.DataFrame(export_rows(engine, run["run_id"], value_format))
    found = int(table["found_in_document"].sum()) if len(table) else 0
    flagged = int((table["flags"] != "").sum()) if len(table) else 0
    col1, col2, col3 = st.columns(3)
    col1.metric("Zones", f"{len(table):,}", border=True)
    col2.metric("Found in the document", f"{found} of {len(table)}", border=True)
    col3.metric("Flagged for review", f"{flagged:,}", border=True,
                help="Rows with a value that needs a person's judgment; see the flags column")
    st.dataframe(table, width="stretch", hide_index=True)
    st.download_button("Download CSV", table.to_csv(index=False).encode("utf-8-sig"), icon=":material/download:",
                       file_name=default_filename(run), mime="text/csv",
                       key=f"dl-zones-{run['run_id']}-{value_format}")


def zoning_tab():
    with card("zone-districts", 1, "Choose the districts", "From a town you've scraped, or a custom list you type in or let the model find."):
        mode = st.segmented_control("Districts from", ["A scraped town", "A custom list"], default="A scraped town",
                                    required=True, key="zoning_mode", label_visibility="collapsed")
        if mode == "A scraped town":
            towns = scraper_db.towns_with_data(scraper_engine())
            if not towns:
                st.info("No scraped towns yet. Scrape one first, or use a custom list.", icon=":material/info:")
                return
            town = st.segmented_control("Town", towns, default=towns[0], required=True, format_func=town_label,
                                        key="zoning_town")
            parcels = scraper_db.export_tables(scraper_engine(), town)["parcels"]
            parcel_columns = [c for c in dict.fromkeys(k for row in parcels for k in row)
                              if c not in scraper_db.EXPORT_COLUMNS]
            col1, col2 = st.columns(2)
            zone_column = col1.selectbox("Zone column", ["Detect automatically"] + parcel_columns)
            decider = col2.selectbox("Decider (used if the zone column is unclear)", list(DECIDERS))
            args = ["--town", town, "--decider", decider]
            if zone_column != "Detect automatically":
                args += ["--zone-column", f"parcels:{zone_column}"]  # the dropdown lists the parcels table's columns
            default_doc, place_kind, place = DOCS_DIR / f"{town}.pdf", "town", town
            doc_hint = "The town's ordinance is used unless you upload another."
        else:
            name = st.text_input("Name (e.g. the town or county)").strip()
            codes = [c.strip() for c in st.text_area("Zone codes, one per line (optional)").splitlines() if c.strip()]
            if not codes:
                st.caption("No codes: the model lists the document's districts first, leaving out overlay districts.")
            args = ["--name", name] + (["--zones", *codes] if codes else [])
            default_doc = None
            place_kind, place = "custom", name
            doc_hint = "Upload the ordinance to read."

    with card("zone-document", 2, "Zoning document", doc_hint):
        document = choose_document(default_doc, key="town" if mode == "A scraped town" else "custom")
    ready = document is not None and (mode == "A scraped town" or bool(name))

    with card("zone-model", 3, "Model", "Which Gemini model reads the document, and how much it reasons first."):
        col1, col2 = st.columns([3, 2])
        provider = col1.segmented_control("Model provider", list(EXTRACTORS), default=next(iter(EXTRACTORS)),
                                          required=True, format_func=lambda p: PROVIDER_LABELS.get(p, p))
        effort = col2.segmented_control("Model effort", ["high", "medium", "low"], default="high", required=True,
                                        format_func=str.capitalize)
        model_name = st.text_input("Model name", value=default_model(EXTRACTORS[provider]), key=f"model-{provider}")
        refresh = st.checkbox("Re-query every zone, ignoring saved results")

    if st.button("Build zoning table", type="primary", disabled=not ready, width="stretch",
                 icon=":material/auto_awesome:"):
        with document_on_disk(document) as path:
            args = ["zoning.py", *args, "--doc", str(path), "--model-provider", provider,
                    "--model-name", model_name, "--model-effort", effort]
            if refresh:
                args.append("--refresh")
            show_result(*run_script(args))

    with card("zone-results", 4, "Results", "Every run is kept; pick an earlier one if you need it."):
        show_zoning_results(place_kind, place)


# ---------------------------------------------------------------- Fields

def fields_tab():
    with card("fields", "tune", "What the model collects",
              "Each row becomes columns in the zones table. Saving changes means saved zone results are "
              "re-queried on the next run."):
        with st.container(horizontal=True, gap="small"):
            st.badge("text → as written", color="gray")
            for kind in KINDS:
                if kind != "text":
                    st.badge(f"{kind} → {', '.join(standard_units(kind))}", color="blue")
        st.caption("Measurement kinds get one column per standard unit, filled when the document gives a single "
                   "general value. Every field also gets _status and _values (each value with its condition, pages "
                   "and quote); the flags column explains any blanks. The description tells the model what to "
                   "look for.")
        fields = tomllib.loads(FIELDS_FILE.read_text(encoding="utf-8"))["fields"]
        table = pd.DataFrame({"name": list(fields),
                              "kind": [spec.get("kind", "text") for spec in fields.values()],
                              "description": [spec.get("description", "") for spec in fields.values()]})
        edited = st.data_editor(table, num_rows="dynamic", width="stretch", hide_index=True,
                                height=(len(table) + 2) * 35 + 3,  # every field plus the "add row" line, no scrolling
                                column_config={"name": st.column_config.TextColumn(width="medium"),
                                               "kind": st.column_config.SelectboxColumn(options=list(KINDS)),
                                               "description": st.column_config.TextColumn(width="large")})

        if st.button("Save fields", type="primary", icon=":material/save:"):
            rows = [tuple(str(r[c] or "").strip() for c in ("name", "kind", "description")) for _, r in edited.iterrows()]
            rows = [r for r in rows if any(r)]  # ignore fully blank rows
            problems = [f"'{n}' isn't a valid name (letters, digits, underscores; can't start with a digit)"
                        for n, _, _ in rows if not FIELD_NAME.match(n)]
            problems += [f"'{n}' needs a kind" for n, k, _ in rows if k not in KINDS]
            problems += [f"'{n}' needs a description" for n, _, d in rows if not d]
            names = [n for n, _, _ in rows]
            problems += [f"'{n}' appears more than once" for n in sorted({n for n in names if names.count(n) > 1})]
            if problems:
                st.error("Not saved:\n\n" + "\n".join(f"- {p}" for p in problems), icon=":material/error:")
            elif not rows:
                st.error("Not saved: at least one field is needed.", icon=":material/error:")
            else:
                # Keep the explanatory comments at the top of the file (everything before the first line
                # starting with "[fields."); rewrite the field tables below them
                header = re.split(r"^\[fields\.", FIELDS_FILE.read_text(encoding="utf-8"), maxsplit=1, flags=re.M)[0]
                body = "\n".join(f"[fields.{n}]\nkind = {json.dumps(k)}\ndescription = {json.dumps(d, ensure_ascii=False)}\n"
                                 for n, k, d in rows)
                FIELDS_FILE.write_text(header + body, encoding="utf-8")
                st.success(f"Saved {len(rows)} fields.", icon=":material/check_circle:")


st.set_page_config(page_title="Property & Zoning Toolkit", page_icon=":material/location_city:", layout="wide")
st.html(CSS)
hero()
scrape, zoning, fields_ = st.tabs([":material/travel_explore: Scrape parcels", ":material/map: Build zoning table",
                                   ":material/tune: Zoning fields"])
with scrape:
    scraping_tab()
with zoning:
    zoning_tab()
with fields_:
    fields_tab()
st.html('<div class="yale-footer"><span>Model-extracted zoning values can be wrong: check them against the PDF '
        'pages and quotes in each _values column.</span><span>Yale web palette</span></div>')
