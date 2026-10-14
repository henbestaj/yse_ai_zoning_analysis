"""Waterbury, CT: gis.waterburyct.org property pages, one page per tab."""

import html as htmllib
import re

from common import BASE_DIR, FetchError, cached_fetch, parse_rows

TOWN = "waterbury"
ID_LIST = BASE_DIR / "property_lists" / "waterbury.txt"
BASE_URL = "https://gis.waterburyct.org/GIS/AGOL/"

# tab name -> URL template. {mbl} is the dashed 4-4-4 form; {unique_id} is the undashed 12 digits.
PAGES = {
    "property": "propertyresults.asp?MBL={mbl}",
    "building": "Buildingresults.asp?MBL={mbl}",
    "value": "Valueresults.asp?MBL={mbl}",
    "sales": "Salesresults.asp?MBL={mbl}",
    "permit": "permitresults.asp?MBL={mbl}&UNIQUE_ID={unique_id}",
    "gismap": "GISMapresults.asp?MBL={mbl}",
}


def fetch_pages(session, unique_id, mbl, refresh=False, refresh_days=1):
    """Return {tab name: html} for one parcel, using the cache where possible."""
    return {
        page: cached_fetch(session, TOWN, unique_id, page,
                           BASE_URL + template.format(mbl=mbl, unique_id=unique_id),
                           refresh=refresh, refresh_days=refresh_days)
        for page, template in PAGES.items()
    }


def parse_page(html):
    """Classify each row and return (fields, records). Relies on Waterbury's convention that
    labels end in ':' and values don't.

    - one cell            -> section title (applies to the records that follow)
    - 'Label:' value ...  -> fields[label] = value
    - all <th>            -> column headers for the records that follow
    - matches the headers -> a record dict, with its section under '_section'
    A label seen twice (e.g. a second building) gets a ' (2)' suffix instead of overwriting.
    """
    fields, records = {}, []
    section, headers = "", None
    for row in parse_rows(html):
        texts = [c.text for c in row]
        if len(row) == 1:
            section, headers = texts[0].rstrip(":").strip(), None
        elif len(row) % 2 == 0 and all(t.endswith(":") for t in texts[::2]):
            for label, cell in zip(texts[::2], row[1::2]):
                key, n = label.rstrip(":").strip(), 2
                while key in fields:
                    key, n = f"{label.rstrip(':').strip()} ({n})", n + 1
                fields[key] = cell.value
        elif all(c.is_header for c in row):
            headers = [t.rstrip(":").strip() for t in texts]
        elif headers and len(row) == len(headers):
            records.append({"_section": section, **dict(zip(headers, (c.value for c in row)))})
    return fields, records


def _content(html):
    """Drop the site banner and tab menu; parcel data starts at the Location line on every tab."""
    start = html.find("<b>Location:")
    return html[start:] if start >= 0 else ""


def _header_field(html, label):
    match = re.search(rf"<b>{label}:</b>.*?<font[^>]*>(.*?)</font>", html, re.S | re.I)
    return " ".join(htmllib.unescape(match.group(1)).split()) if match else ""


def parse_property(html):
    fields, _ = parse_page(_content(html))
    # "Come Home to Downtown: No\nNew Market Tax Credit: Yes\n..." -> one column per program
    for line in fields.pop("Eligible Programs", "").split("\n"):
        name, _, answer = line.partition(":")
        if answer:
            fields[name.strip()] = answer.strip()
    return fields


def parse_building(html):
    fields, records = parse_page(_content(html))
    outbuildings = [{k: v for k, v in r.items() if k != "_section"} for r in records]
    return fields, outbuildings


def parse_value(html):
    # Rows are Building/Land/OutBuilding/Total; columns are Appraised Value/Assessed Value (70%)
    _, records = parse_page(_content(html))
    fields = {}
    for record in records:
        row_name = record.pop("")
        record.pop("_section")
        for column, value in record.items():
            fields[f"{row_name} {column}"] = value
    return fields


def parse_sales(html):
    _, records = parse_page(_content(html))
    return [{k: v for k, v in r.items() if k != "_section"} for r in records]


def parse_permits(html):
    # Three sub-tables (Building Permits, Planning Applications, Code Enforcements) with parallel
    # columns, e.g. Permit Date / Application Date / Case Date. Normalize so they share one CSV.
    _, records = parse_page(_content(html))
    rows = []
    for record in records:
        section = record.pop("_section")
        date, number, kind, details = record.values()
        rows.append({"Category": section, "Date": date, "Number": number, "Type": kind, "Details": details})
    return rows


def parse_maps(html):
    """Each map is a table with an underlined title and a 'View Full Size Map' PDF link."""
    maps = {}
    for title, url in re.findall(r"<u>([^<]+)</u>.*?<a href=\"([^\"]+\.pdf)\"", _content(html), re.S | re.I):
        # "Full GIS Tax Map #392" -> "Full GIS Tax Map", so columns line up across parcels
        name = re.sub(r"\s*#\S+$", "", " ".join(title.split()))
        maps[name] = url
    return maps


def scrape(session, raw_id, refresh=False, refresh_days=1):
    """Return (parcel_id, fields, tables) for one parcel."""
    unique_id = raw_id.strip()
    if len(unique_id) != 12 or not unique_id.isdigit():
        raise ValueError(f"expected a 12-digit ID, got {len(unique_id)} characters")
    mbl = f"{unique_id[:4]}-{unique_id[4:8]}-{unique_id[8:]}"  # the site requires the dashed form
    pages = fetch_pages(session, unique_id, mbl, refresh, refresh_days)
    location = _header_field(pages["property"], "Location")
    if not location:
        raise FetchError(f"no property found for MBL {mbl}")

    building, outbuildings = parse_building(pages["building"])
    fields = {
        "MBL": mbl,
        "Location": location,
        "Owner": _header_field(pages["property"], "Owner"),
        **parse_property(pages["property"]),
        **building,
        **parse_value(pages["value"]),
        **parse_maps(pages["gismap"]),
    }
    tables = {
        "outbuildings": outbuildings,
        "sales": parse_sales(pages["sales"]),
        "permits": parse_permits(pages["permit"]),
    }
    return unique_id, fields, tables
