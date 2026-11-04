"""Shared helpers: polite fetching with retries, an on-disk HTML cache, and CSV output."""

import csv
import http.cookiejar
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from datetime import datetime, timedelta, timezone

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
SCRAPER_OUTPUT_DIR = BASE_DIR / "scraper_output"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
MAX_ATTEMPTS = 4
DELAY_RANGE = (1.0, 2.0)  # seconds to wait before every network request
DEFAULT_REFRESH_DAYS = 1  # cached pages older than this are re-downloaded; the one place to change it


class FetchError(Exception):
    pass


def load_env(path=BASE_DIR / ".env"):
    """Load KEY=VALUE lines from .env into os.environ. Variables already set in the environment win.
    Values are never printed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class Session:
    """A cookie-keeping HTTP client. Hartford needs one per parcel so the search result stays in the session."""

    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def fetch(self, url, data=None):
        """GET `url`, or POST it if `data` (a dict) is given. Retries 5xx errors and timeouts with backoff."""
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            time.sleep(random.uniform(*DELAY_RANGE))
            try:
                request = urllib.request.Request(url, data=body, headers=headers)
                with self.opener.open(request, timeout=30) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read().decode(charset, "replace")
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    raise FetchError(f"HTTP {e.code} for {url}") from e
                last_error = f"HTTP {e.code}"
            except (urllib.error.URLError, TimeoutError) as e:
                last_error = str(getattr(e, "reason", e))
            if attempt < MAX_ATTEMPTS - 1:  # back off before the next try, not after the last one
                time.sleep(2 ** attempt)  # 1, 2, 4 s
        raise FetchError(f"{last_error} after {MAX_ATTEMPTS} attempts for {url}")


def cached_fetch(session, town, parcel_id, page, url, refresh, refresh_days, data=None):
    """Return the page's HTML, cached at cache/<town>/<parcel_id>_<page>.html.

    The cached copy is used if it's less than `refresh_days` old (and `refresh` is off); otherwise the
    page is re-downloaded. If that download fails but an older copy exists, the older copy is used
    with a warning, so a flaky server doesn't cost us a parcel we already have.
    """
    path = CACHE_DIR / town / f"{parcel_id}_{page}.html"
    if path.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if not refresh and age < timedelta(days=refresh_days):
            return path.read_text(encoding="utf-8")
    try:
        html = session.fetch(url, data)
    except FetchError as e:
        if not path.exists():
            raise  # nothing cached to fall back on
        print(f"  warning: {e}; using cached copy from {age.days} day(s) ago")
        return path.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return html


class Cell:
    """One <td>/<th>. Link text is kept apart from plain text so e.g. a 'Details' link becomes its URL."""

    def __init__(self, is_header):
        self.is_header = is_header
        self.parts = []
        self.links = []

    @property
    def text(self):
        """Plain text outside links; <br> line breaks kept as '\\n'."""
        lines = (" ".join(line.split()) for line in "".join(self.parts).replace("\xa0", " ").split("\n"))
        return "\n".join(line for line in lines if line)

    @property
    def value(self):
        return self.text or (self.links[0] if self.links else "")


class _RowParser(HTMLParser):
    """Flattens HTML into rows of cells. Row-by-row scanning copes with the unclosed/misnested
    tags on these sites, where building a proper tree would nest rows inside each other."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.in_link = self.in_skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.in_skip = True
        elif tag == "tr":
            self.row, self.cell = [], None
            self.rows.append(self.row)
        elif tag in ("td", "th"):
            if self.row is None:
                self.row = []
                self.rows.append(self.row)
            self.cell = Cell(tag == "th")
            self.row.append(self.cell)
        elif tag == "br" and self.cell:
            self.cell.parts.append("\n")
        elif tag == "a" and self.cell:
            href = dict(attrs).get("href")
            if href and not href.startswith("javascript"):
                self.cell.links.append(href)
            self.in_link = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.in_skip = False
        elif tag in ("td", "th"):
            self.cell = None
        elif tag == "a":
            self.in_link = False

    def handle_data(self, data):
        if self.cell and not self.in_link and not self.in_skip:
            self.cell.parts.append(data)


def parse_rows(html):
    """Return a list of rows, each a list of Cells; empty rows dropped."""
    parser = _RowParser()
    parser.feed(html)
    return [row for row in parser.rows if any(c.value for c in row)]


def read_id_list(path):
    """Read one parcel ID per line, skipping blanks."""
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def write_csv(path, rows, join_lines=True):
    """Write a list of dicts to CSV. Columns are the union of all keys, ordered by the most complete
    row first so that sections missing from some parcels (e.g. buildings on empty lots) stay in page order.
    With join_lines, multi-line values (e.g. mailing addresses) are joined onto one line with ", ";
    without it, line breaks are kept inside the (quoted) cell."""
    columns = []
    for row in sorted(rows, key=len, reverse=True):
        columns.extend(k for k in row if k not in columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel opens it with the right encoding
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        if join_lines:
            rows = ({k: v.replace("\n", ", ") if isinstance(v, str) else v for k, v in row.items()} for row in rows)
        writer.writerows(rows)
