"""Streamlit UI for the property scraper and the zoning table.

The app doesn't reimplement anything: it runs the existing command-line scripts (scraper.py,
zoning.py) as subprocesses, shows their progress, and offers the resulting CSVs for download.

Run from web_scraper/ with the project venv:
    streamlit run app.py
"""

import contextlib
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

from common import BASE_DIR, DEFAULT_REFRESH_DAYS, SCRAPER_OUTPUT_DIR, read_id_list
from models import DECIDERS, EXTRACTORS
from scraper import TOWNS
from zoning import DOCS_DIR, FIELDS_FILE, KINDS, ZONING_OUTPUT_DIR, default_model, safe_name, standard_units

PROGRESS_LINE = re.compile(r"^\[(\d+)/(\d+)\]")  # both scripts print "[3/47] ..." per item
FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def town_label(town):
    return town.replace("_", " ").title()


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
    last output if the script itself stopped."""
    summary = lines[-1] if lines else "Done"
    failures = [line.strip() for line in lines if "FAILED" in line]
    if code != 0:
        st.error("Stopped with an error:")
        st.code("\n".join(lines[-10:]))
    elif failures:
        st.warning(f"{summary}\n\nFailed items:")
        st.code("\n".join(failures))
    else:
        st.success(summary)


def read_csv(path):
    try:
        return pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    except pd.errors.EmptyDataError:  # e.g. errors.csv when nothing failed
        return pd.DataFrame()


def download_outputs(folder, label, only=None, key=""):
    """Preview and download buttons for the CSVs in `folder` (all of them, or just the names in `only`).
    `label` prefixes the downloaded file names."""
    files = sorted(p for p in folder.glob("*.csv") if only is None or p.name in only) if folder.is_dir() else []
    if not files:
        st.info("No output files yet.")
        return
    if len(files) > 1:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, path.name)
        st.download_button(f"Download all ({len(files)} files, .zip)", buffer.getvalue(),
                           file_name=f"{label}_outputs.zip", mime="application/zip", key=f"zip-{key}{label}")
    for path in files:
        table = read_csv(path)
        with st.expander(f"{path.name}  ·  {len(table)} rows"):
            st.download_button(f"Download {path.name}", path.read_bytes(), file_name=f"{label}_{path.name}",
                               mime="text/csv", key=f"dl-{key}{label}-{path.name}")
            st.dataframe(table, width="stretch", hide_index=True)


# ---------------------------------------------------------------- Scraping

def scraping_tab():
    town = st.selectbox("Town", list(TOWNS), format_func=town_label, key="scrape_town")
    default_ids = read_id_list(TOWNS[town].ID_LIST)

    source = st.radio("Property IDs", [f"Town's list ({len(default_ids)} IDs)", "Upload a .txt file", "Paste IDs"],
                      horizontal=True)
    if source.startswith("Upload"):
        uploaded = st.file_uploader("One ID per line", type=["txt", "csv"])
        ids = [line.strip() for line in uploaded.getvalue().decode("utf-8-sig").splitlines()
               if line.strip()] if uploaded else []
    elif source.startswith("Paste"):
        ids = [line.strip() for line in st.text_area("One ID per line").splitlines() if line.strip()]
    else:
        ids = default_ids

    with st.expander("Options"):
        refresh = st.checkbox("Re-download every page, ignoring the cache")
        refresh_days = st.number_input("Re-download cached pages older than (days)", min_value=0,
                                       value=DEFAULT_REFRESH_DAYS)

    if st.button(f"Scrape {len(ids)} parcels", type="primary", disabled=not ids):
        args = ["scraper.py", "--town", town, "--refresh_days", str(refresh_days)]
        if refresh:
            args.append("--refresh")
        if ids is not default_ids:
            args += ["--ids", *ids]
        show_result(*run_script(args))

    st.subheader(f"{town_label(town)} output files")
    download_outputs(SCRAPER_OUTPUT_DIR / town, town, key="scrape-")


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
    fd, path = tempfile.mkstemp(suffix=Path(document.name).suffix.lower())  # suffix tells the model the file type
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(document.getvalue())
        yield Path(path)
    finally:
        os.unlink(path)


def show_zoning_results(results_dir, label):
    """Let the user pick one run (provider.model.effort folder) and preview/download its zones.csv."""
    runs = sorted(p for p in results_dir.iterdir() if (p / "zones.csv").exists()) if results_dir.is_dir() else []
    if not runs:
        st.info("No zoning results yet.")
        return
    run = st.selectbox("Results from (provider.model.effort)", runs, format_func=lambda p: p.name,
                       key=f"run-{results_dir}")
    download_outputs(run, f"{label}_{run.name}", only={"zones.csv"}, key="zoning-")


def zoning_tab():
    mode = st.radio("Districts from", ["A scraped town", "A custom list"], horizontal=True)

    if mode == "A scraped town":
        towns = sorted(p.name for p in SCRAPER_OUTPUT_DIR.iterdir()
                       if (p / "parcels.csv").exists()) if SCRAPER_OUTPUT_DIR.is_dir() else []
        if not towns:
            st.info("No scraped towns yet. Scrape one first, or use a custom list.")
            return
        town = st.selectbox("Town", towns, format_func=town_label, key="zoning_town")
        document = choose_document(DOCS_DIR / f"{town}.pdf", key="town")
        parcel_columns = list(read_csv(SCRAPER_OUTPUT_DIR / town / "parcels.csv").columns)
        col1, col2 = st.columns(2)
        zone_column = col1.selectbox("Zone column", ["Detect automatically"] + parcel_columns)
        decider = col2.selectbox("Decider (used if the zone column is unclear)", list(DECIDERS))
        args = ["--town", town, "--decider", decider]
        if zone_column != "Detect automatically":
            args += ["--zone-column", f"parcels.csv:{zone_column}"]  # the dropdown lists parcels.csv's columns
        ready, results_dir, label = document is not None, ZONING_OUTPUT_DIR / "towns" / town, town
    else:
        name = st.text_input("Name (e.g. the town or county)").strip()
        document = choose_document(None, key="custom")
        codes = [c.strip() for c in st.text_area("Zone codes, one per line").splitlines() if c.strip()]
        args, ready = ["--name", name, "--zones", *codes], bool(name and document and codes)
        results_dir, label = ZONING_OUTPUT_DIR / "custom" / safe_name(name or "_"), safe_name(name or "custom")

    col1, col2, col3 = st.columns(3)
    provider = col1.selectbox("Model provider", list(EXTRACTORS))
    model_name = col2.text_input("Model name", value=default_model(EXTRACTORS[provider]), key=f"model-{provider}")
    effort = col3.selectbox("Model effort", ["high", "medium", "low"])
    refresh = st.checkbox("Re-query every zone, ignoring saved results")

    if st.button("Build zoning table", type="primary", disabled=not ready):
        with document_on_disk(document) as path:
            args = ["zoning.py", *args, "--doc", str(path), "--model-provider", provider,
                    "--model-name", model_name, "--model-effort", effort]
            if refresh:
                args.append("--refresh")
            show_result(*run_script(args))

    st.subheader("Zoning results")
    show_zoning_results(results_dir, label)


# ---------------------------------------------------------------- Fields

def fields_tab():
    st.write("What the model collects for each zone. Each name becomes a column in zones.csv (measurement "
             "kinds become one column per standard unit, plus _status and _raw; every field also gets _pages); "
             "the kind says what sort of value it is; the description tells the model what to look for. Saving "
             "changes means saved zone results are re-queried on the next run.")
    st.caption("Kinds and units: " + " · ".join(
        f"**{kind}** → {', '.join(standard_units(kind))}" for kind in KINDS if kind != "text"))
    fields = tomllib.loads(FIELDS_FILE.read_text(encoding="utf-8"))["fields"]
    table = pd.DataFrame({"name": list(fields),
                          "kind": [spec.get("kind", "text") for spec in fields.values()],
                          "description": [spec.get("description", "") for spec in fields.values()]})
    edited = st.data_editor(table, num_rows="dynamic", width="stretch", hide_index=True,
                            column_config={"name": st.column_config.TextColumn(width="medium"),
                                           "kind": st.column_config.SelectboxColumn(options=list(KINDS)),
                                           "description": st.column_config.TextColumn(width="large")})

    if st.button("Save fields", type="primary"):
        rows = [tuple(str(r[c] or "").strip() for c in ("name", "kind", "description")) for _, r in edited.iterrows()]
        rows = [r for r in rows if any(r)]  # ignore fully blank rows
        problems = [f"'{n}' isn't a valid name (letters, digits, underscores; can't start with a digit)"
                    for n, _, _ in rows if not FIELD_NAME.match(n)]
        problems += [f"'{n}' needs a kind" for n, k, _ in rows if k not in KINDS]
        problems += [f"'{n}' needs a description" for n, _, d in rows if not d]
        names = [n for n, _, _ in rows]
        problems += [f"'{n}' appears more than once" for n in sorted({n for n in names if names.count(n) > 1})]
        if problems:
            st.error("Not saved:\n\n" + "\n".join(f"- {p}" for p in problems))
        elif not rows:
            st.error("Not saved: at least one field is needed.")
        else:
            # Keep the explanatory comments at the top of the file (everything before the first line
            # starting with "[fields."); rewrite the field tables below them
            header = re.split(r"^\[fields\.", FIELDS_FILE.read_text(encoding="utf-8"), maxsplit=1, flags=re.M)[0]
            body = "\n".join(f"[fields.{n}]\nkind = {json.dumps(k)}\ndescription = {json.dumps(d, ensure_ascii=False)}\n"
                             for n, k, d in rows)
            FIELDS_FILE.write_text(header + body, encoding="utf-8")
            st.success(f"Saved {len(rows)} fields.")


st.set_page_config(page_title="CT Property & Zoning", layout="wide")
st.title("CT Property & Zoning")
scrape, zoning, fields_ = st.tabs(["Scraping", "Zoning table", "Zoning fields"])
with scrape:
    scraping_tab()
with zoning:
    zoning_tab()
with fields_:
    fields_tab()
