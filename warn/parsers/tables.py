"""HTML table extraction.

Written by hand rather than leaning on `pandas.read_html` because state WARN
tables routinely use rowspan/colspan (one employer spanning several worksite
rows), put the header in the second row under a title row, and hide the PDF of
the actual notice behind a link in a cell. We want all of that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..normalize import clean_text
from ..schema import map_headers, normalize_header


def make_soup(markup: str | bytes) -> BeautifulSoup:
    """Parse HTML, preferring lxml and degrading gracefully."""
    for parser in ("lxml", "html5lib", "html.parser"):
        try:
            return BeautifulSoup(markup, parser)
        except Exception:
            continue
    return BeautifulSoup(markup, "html.parser")


@dataclass
class TableData:
    """A table lifted out of a page."""

    headers: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    caption: str = ""
    index: int = 0
    mapped_fields: dict[int, str] = field(default_factory=dict)

    @property
    def score(self) -> float:
        """How much this looks like a WARN notice table."""
        if not self.rows or not self.headers:
            return 0.0
        fields = set(self.mapped_fields.values())
        # Employer plus at least one date is the signature of a real WARN table.
        signal = 0.0
        if "employer" in fields:
            signal += 4
        for key in ("notice_date", "effective_date", "employees", "city", "county"):
            if key in fields:
                signal += 1.5
        if signal == 0:
            return 0.0
        # Prefer bigger tables, but only logarithmically -- a 5-row table with
        # perfect headers beats a 200-row navigation table.
        import math
        return signal * 10 + math.log10(len(self.rows) + 1) * 5


def _cell_text(cell: Tag) -> str:
    # <br> should become a space, not glue words together.
    for br in cell.find_all("br"):
        br.replace_with(" ")
    text = cell.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _grid_from_table(table: Tag) -> tuple[list[list[str]], list[str | None]]:
    """Flatten a <table> into a rectangular grid, expanding row/colspans.

    Also returns, per emitted grid row, the first hyperlink found in that row --
    usually the PDF of the notice itself. Links are collected alongside the grid
    (rather than by a second positional pass) because rowspan expansion and
    blank-row skipping make raw <tr> indices unreliable.
    """
    grid: list[list[str]] = []
    row_links: list[str | None] = []
    # Pending rowspans: (column index) -> [remaining rows, text]
    carry: dict[int, list[Any]] = {}

    rows = table.find_all("tr")
    for tr in rows:
        # Skip rows belonging to a nested table.
        if tr.find_parent("table") is not table:
            continue

        line: list[str] = []
        col = 0
        anchor = tr.find("a", href=True)
        link = anchor["href"] if anchor else None

        def place_carried() -> None:
            nonlocal col
            while col in carry:
                remaining, text = carry[col]
                line.append(text)
                if remaining <= 1:
                    del carry[col]
                else:
                    carry[col] = [remaining - 1, text]
                col += 1

        place_carried()

        for cell in tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"]):
            if cell.find_parent("table") is not table:
                continue
            text = _cell_text(cell)
            try:
                colspan = max(1, min(int(cell.get("colspan", 1) or 1), 30))
            except (TypeError, ValueError):
                colspan = 1
            try:
                rowspan = max(1, min(int(cell.get("rowspan", 1) or 1), 200))
            except (TypeError, ValueError):
                rowspan = 1

            for _ in range(colspan):
                line.append(text)
                if rowspan > 1:
                    carry[col] = [rowspan - 1, text]
                col += 1
                place_carried()

        place_carried()
        if any(c.strip() for c in line):
            grid.append(line)
            row_links.append(link)

    return grid, row_links


def _pick_header_row(grid: list[list[str]], max_scan: int = 8) -> int:
    """Find the row index that best matches known WARN column names.

    State spreadsheets and pages often open with a title row
    ("2025 WARN Notices") before the real header.
    """
    best_index, best_score = 0, -1.0
    for i, row in enumerate(grid[:max_scan]):
        non_empty = [c for c in row if c.strip()]
        if len(non_empty) < 2:
            continue
        mapped = map_headers(row)
        score = len(set(mapped.values())) * 2
        # Headers are short labels, not sentences of data.
        avg_len = sum(len(c) for c in non_empty) / len(non_empty)
        if avg_len > 60:
            score -= 3
        if len(set(normalize_header(c) for c in non_empty)) < len(non_empty) / 2:
            score -= 2  # mostly duplicate cells: probably a spanned title row
        if score > best_score:
            best_index, best_score = i, score
    return best_index if best_score > 0 else 0


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    output = []
    for i, h in enumerate(headers):
        name = h.strip() or f"column_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}__{seen[name]}"
        else:
            seen[name] = 0
        output.append(name)
    return output


def table_to_data(table: Tag, index: int = 0) -> TableData | None:
    grid, row_links = _grid_from_table(table)
    if len(grid) < 2:
        return None

    header_index = _pick_header_row(grid)
    header_row = grid[header_index]
    width = max(len(r) for r in grid)
    header_row = header_row + [""] * (width - len(header_row))
    headers = _dedupe_headers(header_row)

    records: list[dict[str, Any]] = []
    for r in range(header_index + 1, len(grid)):
        padded = grid[r] + [""] * (width - len(grid[r]))
        values = [clean_text(v) for v in padded]
        if not any(values):
            continue
        record: dict[str, Any] = dict(zip(headers, values))
        href = row_links[r]
        if href:
            record["_link"] = href
        records.append(record)

    if not records:
        return None

    data = TableData(
        headers=headers,
        rows=records,
        caption=_cell_text(table.caption) if table.caption else "",
        index=index,
    )
    data.mapped_fields = map_headers(headers)
    return data


def extract_tables(markup: str | bytes) -> list[TableData]:
    """Every table on the page that parsed into at least one data row."""
    soup = make_soup(markup)
    tables: list[TableData] = []
    for i, table in enumerate(soup.find_all("table")):
        try:
            data = table_to_data(table, i)
        except Exception:
            continue
        if data:
            tables.append(data)
    return tables


def best_table(markup: str | bytes, minimum_score: float = 1.0) -> TableData | None:
    """The table on the page most likely to be the WARN list."""
    tables = extract_tables(markup)
    if not tables:
        return None
    ranked = sorted(tables, key=lambda t: t.score, reverse=True)
    top = ranked[0]
    return top if top.score >= minimum_score else None


def merge_tables(tables: list[TableData]) -> TableData | None:
    """Combine tables that share a header signature.

    Some states publish one table per year on a single page.
    """
    if not tables:
        return None
    groups: dict[tuple[str, ...], list[TableData]] = {}
    for t in tables:
        key = tuple(sorted(set(t.mapped_fields.values())))
        groups.setdefault(key, []).append(t)

    best_group = max(
        groups.values(),
        key=lambda g: (sum(x.score for x in g), sum(len(x.rows) for x in g)),
    )
    if not best_group or best_group[0].score <= 0:
        return None

    merged = TableData(
        headers=best_group[0].headers,
        rows=[r for t in best_group for r in t.rows],
        caption=best_group[0].caption,
        mapped_fields=best_group[0].mapped_fields,
    )
    return merged
