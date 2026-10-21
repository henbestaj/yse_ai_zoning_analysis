"""New Haven, CT: Vision Government Solutions (gis.vgsi.com) parcel pages; everything is on one page.

Unlike Waterbury, the HTML is well-formed and every section has a stable element ID, so sections
are located by ID instead of guessed from layout. Field shapes on the page:
- summary block: <dt>Label</dt><dd>value</dd>
- detail rows:   <td class="plabel">Label</td><td class="data">value</td>
- grids:         <table id="MainContent_grdX"> with a header row, e.g. sales or valuation history
"""

import html as htmllib
import re
from urllib.parse import urljoin

from common import BASE_DIR, FetchError, cached_fetch, parse_rows

TOWN = "new_haven"
ID_LIST = BASE_DIR / "property_lists" / "new_haven.txt"
PARCEL_URL = "https://gis.vgsi.com/newhavenct/Parcel.aspx?Pid={pid}"

# Top-level panels, in page order. Each panel runs until the next one starts. Only some are read by
# name (for their label/value pairs); the rest are listed so they mark where their neighbors end.
PANELS = ["panParcel", "panCurOwn", "panSlh", "panBldg", "panXF", "panLand", "panOB", "panAshHist"]

# grid id -> output table name
GRID_TABLES = {"grdSales": "ownership_history", "grdXf": "extra_features", "grdOb": "outbuildings"}


def fetch_page(session, pid, refresh, refresh_days):
    return cached_fetch(session, TOWN, pid, "parcel", PARCEL_URL.format(pid=pid),
                        refresh=refresh, refresh_days=refresh_days)


def _clean(fragment):
    """HTML fragment -> text, keeping <br> line breaks as '\\n'."""
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ")
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _label(fragment):
    # "Replacement Cost<br />Less Depreciation:" -> "Replacement Cost Less Depreciation"
    return " ".join(_clean(fragment).split()).rstrip(":").strip()


def _pairs(html):
    """All label/value pairs in `html`, from both <dt>/<dd> and plabel/data layouts."""
    pattern = (r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>"
               r"|<td class=\"plabel\"[^>]*>(.*?)</td>\s*<td class=\"data\"[^>]*>(.*?)</td>")
    pairs = {}
    for match in re.finditer(pattern, html, re.S):
        dt, dd, td_label, td_value = match.groups()
        label = _label(dt if dt is not None else td_label)
        if label:  # some rows are unused placeholders with no label
            pairs[label] = _clean(dd if dd is not None else td_value)
    return pairs


def _grid(html, grid_id):
    """Return (caption, list of row dicts) for a GridView table, or ('', []) if absent."""
    match = re.search(rf"<table[^>]*id=\"{grid_id}\"[^>]*>(.*?)</table>", html, re.S)
    if not match:
        return "", []
    inner = match.group(1)
    caption = re.search(r"<caption>(.*?)</caption>", inner, re.S)
    inner = re.sub(r"<tr class=\"FooterStyle\">.*?</tr>", "", inner, flags=re.S)  # totals row
    rows = parse_rows(inner)
    if not rows or not all(c.is_header for c in rows[0]):
        return "", []  # only a "No Data for ..." row
    headers = [" ".join(c.text.split()) for c in rows[0]]  # "Gross\nArea" -> "Gross Area"
    records = [dict(zip(headers, (c.value for c in row))) for row in rows[1:] if len(row) == len(headers)]
    return (_clean(caption.group(1)) if caption else ""), records


def _panels(html):
    """Split the page into {panel id: html} using the top-level panel IDs."""
    starts = sorted((html.find(f'id="MainContent_{p}"'), p) for p in PANELS if f'id="MainContent_{p}"' in html)
    return {p: html[s:e] for (s, p), (e, _) in zip(starts, starts[1:] + [(len(html), None)])}


def _add(fields, new, prefix=""):
    """Merge `new` into `fields`; identical repeats (e.g. Owner in two places) are skipped."""
    for key, value in new.items():
        key = prefix + key
        if fields.get(key) == value:
            continue
        name, n = key, 2
        while name in fields:
            name, n = f"{key} ({n})", n + 1
        fields[name] = value


def parse_buildings(html, page_url):
    """Return (buildings, sub_areas), one building row per section.

    The page has one block per building section, headed "Building N : Section M", with element IDs
    sharing a prefix like MainContent_ctl02_. A parcel can have several buildings (165 Elm St has 3),
    and a building can have several sections, e.g. an addition built later (1839 Chapel St).
    """
    buildings, sub_areas = [], []
    for n, prefix in enumerate(re.findall(r"MainContent_(ctl\d+)_tblBldg", html), 1):
        first, last = html.find(f"MainContent_{prefix}_"), html.rfind(f"MainContent_{prefix}_")
        block = html[html.rfind("<", 0, first): html.find("</table>", last) + len("</table>")]
        heading = re.search(r"Building\s+(\d+)\s*:\s*Section\s+(\d+)", block)
        ids = {"Building": int(heading.group(1)), "Section": int(heading.group(2))} if heading else \
            {"Building": n, "Section": 1}
        building = _pairs(block)
        # Building Attributes grid is Field/Description rows -> one column per attribute
        _, attributes = _grid(block, f"MainContent_{prefix}_grdCns")
        for row in attributes:
            building[row["Field"].rstrip(":").strip()] = row["Description"]
        for kind in ("Photo", "Sketch"):
            link = re.search(rf"id=\"MainContent_{prefix}_img{kind}(?:Url|Link)\"[^>]*href=\"([^\"]+)\"", block)
            if link:
                # hrefs are HTML-escaped (&amp;) and photo paths use Windows-style backslashes
                href = htmllib.unescape(link.group(1)).replace("\\", "/")
                building[f"Building {kind} URL"] = urljoin(page_url, href)
        buildings.append({**ids, **building})
        _, areas = _grid(block, f"MainContent_{prefix}_grdSub")
        sub_areas.extend({**ids, **row} for row in areas)
    return buildings, sub_areas


def scrape(session, raw_id, refresh, refresh_days):
    """Return (parcel_id, fields, tables) for one parcel."""
    pid = raw_id.strip()
    if not pid.isdigit():
        raise ValueError(f"expected a numeric PID, got {raw_id!r}")
    html = fetch_page(session, pid, refresh, refresh_days)
    panels = _panels(html)
    if "panParcel" not in panels:
        raise FetchError(f"no parcel found for PID {pid}")

    fields = {}
    _add(fields, _pairs(panels["panParcel"]))
    _add(fields, _pairs(panels.get("panCurOwn", "")))
    _add(fields, _pairs(panels.get("panLand", "")), "Land: ")

    buildings, sub_areas = parse_buildings(panels.get("panBldg", ""), PARCEL_URL.format(pid=pid))
    tables = {"buildings": buildings, "building_sub_areas": sub_areas}
    for grid_id, name in GRID_TABLES.items():
        tables[name] = _grid(html, f"MainContent_{grid_id}")[1]  # grid IDs are unique on the page
    # Current-year values (top of page) and past years (bottom) form one history, newest first per type
    history = []
    for kind in ("Appr", "Asmt"):
        for grid_id in (f"MainContent_grdCurrentValue{kind}", f"MainContent_grdHistoryValues{kind}"):
            caption, rows = _grid(html, grid_id)
            history.extend({"Type": caption, **row} for row in rows)
    tables["valuation_history"] = history
    return pid, fields, tables
