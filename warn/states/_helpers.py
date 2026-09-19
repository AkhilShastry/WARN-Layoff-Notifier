"""Shared building blocks for custom state handlers.

Leading underscore keeps this out of the handler auto-import in
`warn.states.__init__` -- it is a library, not a state.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from ..parsers.tables import TableData, extract_tables
from ..schema import WarnNotice
from ..source import Source

log = logging.getLogger(__name__)


def pick_table(markup: str, minimum_score: float = 1.0) -> TableData | None:
    tables = extract_tables(markup)
    if not tables:
        return None
    best = max(tables, key=lambda t: t.score)
    return best if best.score >= minimum_score else None


def row_signature(row: dict[str, Any]) -> tuple:
    """Identity for a raw row, used to detect when paging has looped."""
    return tuple(
        str(v) for k, v in sorted(row.items()) if k != "_link" and v is not None
    )


def scrape_paged(
    source: Source,
    url_for_page: Callable[[int], str],
    *,
    max_pages: int = 60,
    first_page: int = 1,
    fetch_kwargs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Walk a paginated HTML table until it stops yielding new rows.

    Guards against the common failure where a site silently returns page 1 for
    every out-of-range page number, which would otherwise loop forever.
    """
    collected: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    for offset in range(max_pages):
        page = first_page + offset
        url = url_for_page(page)
        try:
            response = source.fetch(url, **(fetch_kwargs or {}))
        except Exception as exc:
            log.debug("%s: page %s fetch failed (%s)", source.state, page, exc)
            break
        if not response.ok():
            break

        table = pick_table(response.text)
        if table is None or not table.rows:
            break

        fresh = 0
        for row in table.rows:
            signature = row_signature(row)
            if signature in seen:
                continue
            seen.add(signature)
            collected.append(row)
            fresh += 1

        if fresh == 0:
            break  # same page served again: we are past the end

    return collected


def notices_from_rows(
    source: Source, rows: Iterable[dict[str, Any]], url: str | None = None
) -> list[WarnNotice]:
    return source.to_notices(list(rows), url or source.url)
