"""pan-eol: monitor Palo Alto Networks end-of-life pages and report changes.

Each run fetches the software and hardware EOL pages and parses every table
row into a record. It compares the records with the saved baseline
(state/latest.json), then writes a full snapshot and a change report as JSON,
CSV or both.

Usage:
    python3 pan-eol.py --format both
    python3 pan-eol.py --stdout --no-save-state      # dry run
    python3 pan-eol.py --help

Exit codes: 0 = no changes (or baseline created), 3 = changes detected,
1 = fetch/parse error (baseline untouched), 2 = invalid arguments.

Requires: requests, beautifulsoup4, lxml  (pip install -r requirements.txt)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__version__ = "0.1.0"

log = logging.getLogger("pan_eol")

# =============================================================================
# Configuration
# =============================================================================

BASE = "https://www.paloaltonetworks.com/services/support/end-of-life-announcements"

PAGES: dict[str, str] = {
    "software": f"{BASE}/end-of-life-summary",
    "hardware": f"{BASE}/hardware-end-of-life-dates",
}

USER_AGENT = f"pan-eol-monitor/{__version__} (+scheduled EOL change monitor; python-requests)"
DEFAULT_TIMEOUT = 30
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_STATE_DIR = "state"

# A page whose record count drops below this fraction of the baseline is treated
# as a parse failure (likely a site redesign), not as mass removal.
MIN_RECORD_RATIO = 0.5

STATE_SCHEMA_VERSION = 1
STATE_FILE = "latest.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHANGES = 3

# =============================================================================
# Models
# =============================================================================

ChangeType = Literal["added", "removed", "modified"]


@dataclass(frozen=True)
class EolRecord:
    """One row from an EOL table.

    ``version`` holds the row identifier from the first column. That is a
    version number for most software tables ("11.1"), or a product/model name
    for tables that list products. It is ``None`` for hardware rows, where the
    model name is the ``product``.
    """

    category: str
    product: str
    version: str | None
    dates: dict[str, str | None] = field(default_factory=dict)
    extra: dict[str, str] = field(default_factory=dict)
    source_url: str = ""

    @property
    def key(self) -> str:
        return f"{self.category}|{self.product}|{self.version or ''}"

    def fields(self) -> dict[str, str | None]:
        """All comparable values, flattened (date and extra names never collide)."""
        return {**self.dates, **self.extra}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EolRecord:
        return cls(
            category=d["category"],
            product=d["product"],
            version=d.get("version"),
            dates=dict(d.get("dates") or {}),
            extra=dict(d.get("extra") or {}),
            source_url=d.get("source_url", ""),
        )


@dataclass(frozen=True)
class Change:
    change_type: ChangeType
    key: str
    category: str
    product: str
    version: str | None
    field: str | None = None
    old_value: str | None = None
    new_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# =============================================================================
# Normalization: header names, cell text, dates
# =============================================================================

_MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}

_MDY = re.compile(r"([A-Za-z]+)\.? (\d{1,2}),? (\d{4})")
_DMY = re.compile(r"(\d{1,2}) ([A-Za-z]+)\.?,? (\d{4})")
_SLASH = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_MY = re.compile(r"([A-Za-z]+),? (\d{4})")


def clean_text(s: str) -> str:
    """Collapse whitespace (including non-breaking spaces)."""
    return " ".join(s.replace("\xa0", " ").split())


def cell_text(cell: Tag) -> str:
    return clean_text(cell.get_text(" ", strip=True))


def cell_lines(cell: Tag) -> list[str]:
    return [ln for ln in (clean_text(x) for x in cell.get_text("\n").split("\n")) if ln]


def normalize_header(s: str) -> str:
    """'End-of-Life Date' -> 'end_of_life_date'; 'Last Supported OS ^' -> 'last_supported_os'."""
    s = clean_text(s).lower()
    s = re.sub(r"[™®^*+†‡]", "", s)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _mk(y: str, m: int | None, d: str | None) -> str | None:
    if m is None:
        return None
    try:
        if d is None:
            return f"{int(y):04d}-{m:02d}"
        return date(int(y), m, int(d)).isoformat()
    except ValueError:
        return None


def parse_date(text: str | None) -> str | None:
    """Parse a single date in the formats seen on the EOL pages to ISO 8601.

    Returns ``YYYY-MM-DD`` (or ``YYYY-MM`` for "July, 2026"), or ``None`` if the
    text isn't exactly one recognizable date. Footnote markers (``*``, ``^``),
    ordinal suffixes ("30th") and parenthetical notes are ignored.
    """
    if not text:
        return None
    s = re.sub(r"\([^)]*\)", " ", text)
    s = re.sub(r"[*^†‡]+", " ", s)
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s, flags=re.I)
    s = clean_text(s).strip(" ,.;")
    if m := _MDY.fullmatch(s):
        return _mk(m[3], _MONTHS.get(m[1].lower()), m[2])
    if m := _DMY.fullmatch(s):
        return _mk(m[3], _MONTHS.get(m[2].lower()), m[1])
    if m := _SLASH.fullmatch(s):
        return _mk(m[3], int(m[1]), m[2]) if 1 <= int(m[1]) <= 12 else None
    if m := _ISO.fullmatch(s):
        return _mk(m[1], int(m[2]), m[3]) if 1 <= int(m[2]) <= 12 else None
    if m := _MY.fullmatch(s):
        return _mk(m[2], _MONTHS.get(m[1].lower()), None)
    return None


def normalize_date_value(text: str) -> tuple[str | None, str | None]:
    """Return ``(value, note)`` for a date cell.

    ``value`` is the ISO date when parseable, otherwise the raw text (e.g.
    "Latest", or multiple per-platform dates) so no information is lost.
    ``note`` holds the raw text when a parseable date carried a parenthetical
    note, e.g. "December 31, 2020 (EOL)".
    """
    raw = clean_text(text)
    if not raw:
        return None, None
    iso = parse_date(raw)
    if iso is None:
        return raw, None
    return iso, (raw if "(" in raw else None)


# =============================================================================
# HTML table helpers shared by the page parsers
# =============================================================================


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def row_cells(tr: Tag) -> list[Tag]:
    return tr.find_all(["td", "th"], recursive=False)


def _span(cell: Tag) -> int:
    try:
        return max(1, int(cell.get("colspan", 1)))
    except (TypeError, ValueError):
        return 1


def expand_texts(tr: Tag, repeat: bool) -> list[str]:
    """Row cell texts with colspans expanded (repeat the text, or pad with '')."""
    out: list[str] = []
    for c in row_cells(tr):
        t = cell_text(c)
        n = _span(c)
        out.extend([t] * n if repeat else [t] + [""] * (n - 1))
    return out


def expand_cells(tr: Tag) -> list[Tag | None]:
    out: list[Tag | None] = []
    for c in row_cells(tr):
        out.append(c)
        out.extend([None] * (_span(c) - 1))
    return out


def is_header_row(tr: Tag) -> bool:
    texts = [cell_text(c) for c in row_cells(tr)]
    if len(texts) < 2:
        return False
    has_date_label = any(re.search(r"\bdate\b", t, re.I) for t in texts)
    has_date_value = any(re.search(r"\d{4}", t) for t in texts)
    return has_date_label and not has_date_value


def is_subheader_row(texts: list[str]) -> bool:
    """A row like ['', '', 'Standard Support', '', 'Extended Support']."""
    return (
        bool(texts)
        and texts[0] == ""
        and any(texts)
        and not any(re.search(r"\d", t) for t in texts)
    )


def single_cell_title(tr: Tag) -> str | None:
    cells = row_cells(tr)
    if len(cells) == 1:
        lines = cell_lines(cells[0])
        if lines:
            return lines[0]
    return None


def clean_product_name(s: str) -> str:
    return clean_text(re.sub(r"[™®]", "", s))


def build_columns(header: list[str], sub: list[str] | None) -> list[str]:
    """Combine a (colspan-expanded) header row and optional sub-header into unique names."""
    names: list[str] = []
    seen: dict[str, int] = {}
    for i, h in enumerate(header):
        name = normalize_header(h)
        if sub and i < len(sub) and sub[i]:
            name = f"{name}_{normalize_header(sub[i])}".strip("_")
        if not name:
            name = f"col_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        names.append(name)
    return names


def is_date_column(name: str) -> bool:
    return "date" in name.split("_")


@dataclass
class ParsedRow:
    ident: str
    values: dict[str, str]  # column name -> raw text (identifier column excluded)
    ident_lines: list[str]


@dataclass
class ParsedTable:
    columns: list[str]
    header_texts: list[str]
    rows: list[ParsedRow]
    titles: list[str]


def parse_table_rows(table: Tag) -> ParsedTable:
    """Parse one EOL table into columns, data rows and pre-header title rows.

    ``columns`` is empty if no header row is found.
    """
    rows = table.find_all("tr")
    hdr_idx = next((i for i, r in enumerate(rows) if is_header_row(r)), None)
    if hdr_idx is None:
        return ParsedTable([], [], [], [])

    titles = [t for r in rows[:hdr_idx] if (t := single_cell_title(r))]
    header = expand_texts(rows[hdr_idx], repeat=True)
    start = hdr_idx + 1
    sub = None
    if start < len(rows):
        texts = expand_texts(rows[start], repeat=False)
        if is_subheader_row(texts):
            sub = texts
            start += 1
    columns = build_columns(header, sub)

    data: list[ParsedRow] = []
    for r in rows[start:]:
        cells = expand_cells(r)
        if len(row_cells(r)) < 2 or cells[0] is None:
            continue
        ident = cell_text(cells[0])
        if not ident or is_header_row(r):
            continue
        values: dict[str, str] = {}
        for name, c in zip(columns[1:], cells[1:], strict=False):
            values[name] = cell_text(c) if c is not None else ""
        data.append(ParsedRow(ident=ident, values=values, ident_lines=cell_lines(cells[0])))
    return ParsedTable(columns, header, data, titles)


def split_values(
    values: dict[str, str], drop: set[str]
) -> tuple[dict[str, str | None], dict[str, str]]:
    """Separate a row's columns into normalized dates and other text fields."""
    dates: dict[str, str | None] = {}
    extra: dict[str, str] = {}
    for name, raw in values.items():
        if name in drop:
            continue
        if is_date_column(name):
            val, note = normalize_date_value(raw)
            dates[name] = val
            if note:
                extra[f"{name}_note"] = note
        elif raw:
            extra[name] = raw
    return dates, extra


def empty_columns(rows: list[ParsedRow]) -> set[str]:
    """Columns that are blank in every row (spacer columns)."""
    if not rows:
        return set()
    names = set().union(*(r.values.keys() for r in rows))
    return {n for n in names if not any(r.values.get(n) for r in rows)}


def dedupe_keys(records: list[EolRecord]) -> list[EolRecord]:
    """Make record keys unique by suffixing repeated versions with ' #n'."""
    seen: dict[str, int] = {}
    out: list[EolRecord] = []
    for rec in records:
        n = seen.get(rec.key, 0) + 1
        seen[rec.key] = n
        if n > 1:
            log.warning("duplicate key %s; disambiguating as #%d", rec.key, n)
            rec = EolRecord(
                category=rec.category,
                product=rec.product,
                version=f"{rec.version or ''} #{n}".strip(),
                dates=rec.dates,
                extra=rec.extra,
                source_url=rec.source_url,
            )
        out.append(rec)
    return out


def tables_fingerprint(html: str) -> str:
    """SHA-256 of the normalized text of all tables (ignores scripts, nav, etc.)."""
    soup = soup_of(html)
    h = hashlib.sha256()
    for t in soup.find_all("table"):
        for r in t.find_all("tr"):
            h.update("\t".join(cell_text(c) for c in row_cells(r)).encode())
            h.update(b"\n")
        h.update(b"\f")
    return h.hexdigest()


# =============================================================================
# Page parsers
# =============================================================================

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "b", "strong"}
_GENERIC_IDENT = re.compile(r"^(versions?|products?|platform|end-of-sale product)$", re.I)


def _preceding_heading(table: Tag) -> str | None:
    """Nearest heading between this table and the previous table, if any."""
    for el in table.find_all_previous(True):
        if el.name in ("table", "td", "th", "tr"):
            return None
        if el.name in _HEADING_TAGS:
            text = el.get_text(" ", strip=True)
            if text:
                return text
    return None


def _qualifier(ident_header: str, group: str) -> str | None:
    """'AWS Version' -> 'AWS'; 'Nutanix' -> 'Nutanix'; 'Version' / 'Products' -> None."""
    h = ident_header.strip()
    if not h or _GENERIC_IDENT.match(h) or re.search(r"\bproducts?\b", h, re.I):
        return None
    q = re.sub(r"\s*\bversions?\b\s*$", "", h, flags=re.I).strip()
    if not q or q.lower() == group.lower():
        return None
    return q


def _product_name(group: str, qualifier: str | None) -> str:
    if not qualifier:
        return group
    if group.lower() in qualifier.lower():
        return qualifier  # "GlobalProtect App" under group "GlobalProtect"
    return f"{group} - {qualifier}"


def parse_software(html: str, source_url: str) -> list[EolRecord]:
    """Parse the software End-of-Life Summary page (many product tables)."""
    soup = soup_of(html)
    records: list[EolRecord] = []
    group = "Unknown"
    for table in soup.find_all("table"):
        pt = parse_table_rows(table)
        if not pt.columns or not pt.rows:
            continue
        heading = pt.titles[0] if pt.titles else _preceding_heading(table)
        if heading:
            group = clean_product_name(heading)
        product = _product_name(group, _qualifier(pt.header_texts[0], group))
        drop = empty_columns(pt.rows)
        for row in pt.rows:
            dates, extra = split_values(row.values, drop)
            records.append(
                EolRecord(
                    category="software",
                    product=product,
                    version=row.ident,
                    dates=dates,
                    extra=extra,
                    source_url=source_url,
                )
            )
    return dedupe_keys(records)


def parse_hardware(html: str, source_url: str) -> list[EolRecord]:
    """Parse the Hardware End-of-Life Dates page."""
    soup = soup_of(html)
    records: list[EolRecord] = []
    for table in soup.find_all("table"):
        pt = parse_table_rows(table)
        if not pt.columns or not pt.rows:
            continue
        drop = empty_columns(pt.rows)
        for row in pt.rows:
            lines = row.ident_lines or [row.ident]
            # The first line (e.g. "PA-5450 Series") identifies the row; the full list of
            # covered SKUs goes in extra["models"].
            product = clean_product_name(lines[0])
            dates, extra = split_values(row.values, drop)
            if len(lines) > 1:
                extra["models"] = "; ".join(clean_product_name(x) for x in lines)
            records.append(
                EolRecord(
                    category="hardware",
                    product=product,
                    version=None,
                    dates=dates,
                    extra=extra,
                    source_url=source_url,
                )
            )
    return dedupe_keys(records)


Parser = Callable[[str, str], list[EolRecord]]

PARSERS: dict[str, Parser] = {
    "software": parse_software,
    "hardware": parse_hardware,
}

# =============================================================================
# Fetching
# =============================================================================


class FetchError(RuntimeError):
    pass


@dataclass
class FetchResult:
    url: str
    html: str | None  # None when the server answered 304 Not Modified
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.html is None


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    return s


def fetch_page(
    session: requests.Session,
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> FetchResult:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        resp = session.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise FetchError(f"{url}: {exc}") from exc
    if resp.status_code == 304:
        log.info("%s: 304 Not Modified", url)
        return FetchResult(url, None, etag, last_modified)
    if resp.status_code != 200:
        raise FetchError(f"{url}: HTTP {resp.status_code}")
    resp.encoding = resp.encoding or "utf-8"
    log.info("%s: HTTP 200, %d bytes", url, len(resp.content))
    return FetchResult(
        url,
        resp.text,
        resp.headers.get("ETag"),
        resp.headers.get("Last-Modified"),
    )


def read_local(path: str | Path, url: str) -> FetchResult:
    try:
        html = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise FetchError(f"{path}: {exc}") from exc
    return FetchResult(url, html)


# =============================================================================
# Persisted baseline state (state/latest.json), written atomically
# =============================================================================


@dataclass
class PageState:
    url: str
    sha256: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    record_count: int = 0
    fetched_at: str | None = None


@dataclass
class State:
    generated_at: str | None = None
    pages: dict[str, PageState] = field(default_factory=dict)
    records: list[EolRecord] = field(default_factory=list)

    def records_for(self, category: str) -> list[EolRecord]:
        return [r for r in self.records if r.category == category]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "pages": {k: vars(v) for k, v in self.pages.items()},
            "records": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> State:
        return cls(
            generated_at=d.get("generated_at"),
            pages={k: PageState(**v) for k, v in (d.get("pages") or {}).items()},
            records=[EolRecord.from_dict(r) for r in d.get("records") or []],
        )


def load_state(state_dir: str | Path) -> State | None:
    path = Path(state_dir) / STATE_FILE
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return State.from_dict(json.load(f))


def atomic_write_text(path: str | Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_state(state_dir: str | Path, state: State) -> Path:
    path = Path(state_dir) / STATE_FILE
    atomic_write_text(path, json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return path


# =============================================================================
# Diffing
# =============================================================================


class SuspiciousParseError(RuntimeError):
    """Raised when a parse result looks like a broken page rather than real data."""


def diff_records(old: list[EolRecord], new: list[EolRecord]) -> list[Change]:
    old_by = {r.key: r for r in old}
    new_by = {r.key: r for r in new}
    changes: list[Change] = []

    for key, rec in new_by.items():
        if key not in old_by:
            changes.append(Change("added", key, rec.category, rec.product, rec.version))
    for key, rec in old_by.items():
        if key not in new_by:
            changes.append(Change("removed", key, rec.category, rec.product, rec.version))
    for key in new_by.keys() & old_by.keys():
        o, n = old_by[key].fields(), new_by[key].fields()
        for f in sorted(o.keys() | n.keys()):
            ov, nv = o.get(f) or None, n.get(f) or None
            if ov != nv:
                rec = new_by[key]
                changes.append(
                    Change("modified", key, rec.category, rec.product, rec.version, f, ov, nv)
                )

    order = {"added": 0, "removed": 1, "modified": 2}
    changes.sort(
        key=lambda c: (order[c.change_type], c.category, c.product, c.version or "", c.field or "")
    )
    return changes


def summarize(changes: list[Change]) -> dict[str, int]:
    counts = Counter(c.change_type for c in changes)
    return {
        "added": counts["added"],
        "removed": counts["removed"],
        "modified": counts["modified"],
        "records_modified": len({c.key for c in changes if c.change_type == "modified"}),
    }


def check_plausible(page: str, new_count: int, baseline_count: int | None) -> None:
    """Reject empty or sharply shrunken parse results. The site probably changed layout."""
    if new_count == 0:
        raise SuspiciousParseError(f"{page}: parsed 0 records; page layout may have changed")
    if baseline_count and new_count < baseline_count * MIN_RECORD_RATIO:
        raise SuspiciousParseError(
            f"{page}: parsed {new_count} records vs {baseline_count} in baseline "
            f"(< {MIN_RECORD_RATIO:.0%}); refusing to treat as mass removal"
        )


# =============================================================================
# Writers: snapshots and change reports as JSON / CSV
# =============================================================================

SNAPSHOT_BASE_COLUMNS = ["category", "product", "version"]
CHANGE_COLUMNS = [
    "detected_at",
    "change_type",
    "category",
    "product",
    "version",
    "field",
    "old_value",
    "new_value",
]


def _sorted(records: list[EolRecord]) -> list[EolRecord]:
    return sorted(records, key=lambda r: (r.category, r.product, r.version or ""))


def snapshot_dict(records: list[EolRecord], meta: dict[str, Any]) -> dict[str, Any]:
    return {**meta, "records": [r.to_dict() for r in _sorted(records)]}


def snapshot_json(records: list[EolRecord], meta: dict[str, Any]) -> str:
    return json.dumps(snapshot_dict(records, meta), indent=2, ensure_ascii=False) + "\n"


def snapshot_csv(records: list[EolRecord]) -> str:
    date_cols = sorted({k for r in records for k in r.dates})
    cols = SNAPSHOT_BASE_COLUMNS + date_cols + ["extra_json", "source_url"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for r in _sorted(records):
        row: dict[str, Any] = {
            "category": r.category,
            "product": r.product,
            "version": r.version or "",
            "extra_json": json.dumps(r.extra, ensure_ascii=False, sort_keys=True)
            if r.extra
            else "",
            "source_url": r.source_url,
        }
        for c in date_cols:
            row[c] = r.dates.get(c) or ""
        w.writerow(row)
    return buf.getvalue()


def changes_json(changes: list[Change], meta: dict[str, Any]) -> str:
    doc = {**meta, "changes": [c.to_dict() for c in changes]}
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def changes_csv(changes: list[Change], detected_at: str) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CHANGE_COLUMNS, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for c in changes:
        row = {k: ("" if v is None else v) for k, v in c.to_dict().items()}
        row["detected_at"] = detected_at
        w.writerow(row)
    return buf.getvalue()


def formats_for(fmt: str) -> list[str]:
    return ["json", "csv"] if fmt == "both" else [fmt]


STAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"
_DATED_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{6}Z)_(snapshot|changes)\.(json|csv)$")


def prune_outputs(output_dir: str | Path, retain_days: int, now: datetime) -> list[Path]:
    """Delete timestamped snapshot/change files older than ``retain_days``.

    Age comes from the timestamp in the file name, not the file's mtime, so
    copying or touching files doesn't affect it. Only files this tool writes
    (``<stamp>_snapshot.*`` / ``<stamp>_changes.*`` under ``snapshots/`` and
    ``changes/``) are considered. ``latest_snapshot.*`` and the baseline are never
    touched.
    """
    cutoff = now - timedelta(days=retain_days)
    removed: list[Path] = []
    for sub in ("snapshots", "changes"):
        d = Path(output_dir) / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            m = _DATED_FILE.match(p.name)
            if not m or not p.is_file():
                continue
            stamp = datetime.strptime(m[1], STAMP_FORMAT).replace(tzinfo=timezone.utc)
            if stamp < cutoff:
                p.unlink()
                removed.append(p)
    return removed


def write_outputs(
    output_dir: str | Path,
    stamp: str,
    fmt: str,
    records: list[EolRecord],
    snapshot_meta: dict[str, Any],
    changes: list[Change] | None,
    changes_meta: dict[str, Any],
    detected_at: str,
) -> list[Path]:
    """Write timestamped snapshot + change files and refresh latest_snapshot.*.

    ``changes`` of ``None`` means no baseline existed (first run), so no change
    report is written.
    """
    out = Path(output_dir)
    written: list[Path] = []
    for ext in formats_for(fmt):
        snap = snapshot_json(records, snapshot_meta) if ext == "json" else snapshot_csv(records)
        for p in (out / "snapshots" / f"{stamp}_snapshot.{ext}", out / f"latest_snapshot.{ext}"):
            atomic_write_text(p, snap)
            written.append(p)
        if changes is not None:
            body = (
                changes_json(changes, changes_meta)
                if ext == "json"
                else changes_csv(changes, detected_at)
            )
            p = out / "changes" / f"{stamp}_changes.{ext}"
            atomic_write_text(p, body)
            written.append(p)
    return written


# =============================================================================
# Command line: fetch -> parse -> diff -> write -> save baseline
# =============================================================================


@dataclass
class PageResult:
    name: str
    state: PageState
    records: list[EolRecord]
    reused: bool  # True when the baseline was reused (304 or identical table content)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_from_file(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values:
        name, sep, path = v.partition("=")
        if not sep or name not in PAGES or not path:
            raise argparse.ArgumentTypeError(
                f"--from-file expects NAME=PATH with NAME in {sorted(PAGES)}, got {v!r}"
            )
        out[name] = path
    return out


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of days >= 1, got {value!r}")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pan-eol.py",
        description=(
            "Fetch the Palo Alto Networks end-of-life pages, compare them with the "
            "saved baseline, and write a snapshot and a change report."
        ),
        epilog="Exit codes: 0 = no changes, 3 = changes detected, 1 = error, 2 = bad arguments.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--format", choices=("json", "csv", "both"), default="json")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    p.add_argument("--pages", choices=(*PAGES, "all"), default="all")
    p.add_argument(
        "--stdout", action="store_true", help="print change report instead of writing files"
    )
    p.add_argument("--no-save-state", action="store_true", help="do not update the baseline")
    p.add_argument(
        "--only-on-change", action="store_true", help="skip writing files if nothing changed"
    )
    p.add_argument(
        "--from-file",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="parse a local HTML file instead of fetching (repeatable)",
    )
    p.add_argument(
        "--retain-days",
        type=_positive_int,
        default=None,
        metavar="N",
        help="delete timestamped snapshots/change reports older than N days "
        "(default: keep everything)",
    )
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    v = p.add_mutually_exclusive_group()
    v.add_argument("-v", "--verbose", action="store_true")
    v.add_argument("-q", "--quiet", action="store_true")
    return p


def _process_page(
    name: str,
    baseline: State | None,
    local_path: str | None,
    session: Any,
    timeout: float,
    now_iso: str,
) -> PageResult:
    url = PAGES[name]
    prev = baseline.pages.get(name) if baseline else None
    prev_records = baseline.records_for(name) if baseline else []

    if local_path:
        res: FetchResult = read_local(local_path, url)
    else:
        res = fetch_page(
            session,
            url,
            etag=prev.etag if prev and prev_records else None,
            last_modified=prev.last_modified if prev and prev_records else None,
            timeout=timeout,
        )

    if res.not_modified and prev:
        prev.fetched_at = now_iso
        return PageResult(name, prev, prev_records, reused=True)

    assert res.html is not None
    sha = tables_fingerprint(res.html)
    page_state = PageState(
        url=url,
        sha256=sha,
        etag=res.etag,
        last_modified=res.last_modified,
        fetched_at=now_iso,
    )
    if prev and prev.sha256 == sha and prev_records:
        log.info("%s: table content unchanged (sha256 %s…)", name, sha[:12])
        page_state.record_count = len(prev_records)
        return PageResult(name, page_state, prev_records, reused=True)

    records = PARSERS[name](res.html, url)
    check_plausible(name, len(records), len(prev_records) or None)
    page_state.record_count = len(records)
    log.info("%s: parsed %d records", name, len(records))
    return PageResult(name, page_state, records, reused=False)


def run(args: argparse.Namespace) -> int:
    now = _utcnow()
    now_iso = now.isoformat().replace("+00:00", "Z")
    stamp = now.strftime(STAMP_FORMAT)
    selected = list(PAGES) if args.pages == "all" else [args.pages]

    try:
        local = _parse_from_file(args.from_file)
    except argparse.ArgumentTypeError as exc:
        log.error("%s", exc)
        return EXIT_ERROR

    try:
        baseline = load_state(args.state_dir)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.error("could not read baseline in %s: %s", args.state_dir, exc)
        return EXIT_ERROR

    session = None if all(n in local for n in selected) else make_session()
    results: dict[str, PageResult] = {}
    try:
        for name in selected:
            results[name] = _process_page(
                name, baseline, local.get(name), session, args.timeout, now_iso
            )
    except (FetchError, SuspiciousParseError) as exc:
        log.error("%s", exc)
        log.error("aborting; baseline left unchanged")
        return EXIT_ERROR

    # Pages that weren't selected this run keep their baseline records and state.
    new_records: list[EolRecord] = []
    new_pages: dict[str, PageState] = {}
    for name in PAGES:
        if name in results:
            new_records += results[name].records
            new_pages[name] = results[name].state
        elif baseline and name in baseline.pages:
            new_records += baseline.records_for(name)
            new_pages[name] = baseline.pages[name]

    changes: list[Change] | None
    if baseline is None:
        changes = None
        log.info("no baseline found; creating one")
    else:
        old = [r for r in baseline.records if r.category in selected]
        new = [r for r in new_records if r.category in selected]
        changes = diff_records(old, new)

    summary = summarize(changes or [])
    sources = {
        n: {"url": s.url, "sha256": s.sha256, "record_count": s.record_count}
        for n, s in new_pages.items()
    }
    snapshot_meta = {"generated_at": now_iso, "tool_version": __version__, "sources": sources}
    changes_meta = {
        "generated_at": now_iso,
        "previous_run": baseline.generated_at if baseline else None,
        "baseline_created": baseline is None,
        "pages_checked": selected,
        "summary": summary,
    }

    if args.stdout:
        if args.format == "csv":
            sys.stdout.write(changes_csv(changes or [], now_iso))
        elif changes is None:
            sys.stdout.write(snapshot_json(new_records, {**snapshot_meta, **changes_meta}))
        else:
            sys.stdout.write(changes_json(changes, changes_meta))
    elif args.only_on_change and changes == []:
        log.info("no changes; skipping output files (--only-on-change)")
    else:
        for p in write_outputs(
            args.output_dir,
            stamp,
            args.format,
            new_records,
            snapshot_meta,
            changes,
            changes_meta,
            now_iso,
        ):
            log.info("wrote %s", p)

    if args.retain_days is not None:
        pruned = prune_outputs(args.output_dir, args.retain_days, now)
        log.info(
            "pruned %d file(s) older than %d day(s) from %s",
            len(pruned),
            args.retain_days,
            args.output_dir,
        )

    if not args.no_save_state:
        path = save_state(args.state_dir, State(now_iso, new_pages, new_records))
        log.info("baseline saved to %s", path)

    if changes:
        log.warning(
            "changes detected: %(added)d added, %(removed)d removed, "
            "%(modified)d field(s) modified",
            summary,
        )
        return EXIT_CHANGES
    log.info("no changes detected" if changes is not None else "baseline created")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
