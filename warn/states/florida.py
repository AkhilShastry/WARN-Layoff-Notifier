"""Florida DEO's RE ACT system.

One list per program year at `/WarnList/Records?year=YYYY`, paginated with
`&page=N`. Walking back through the year parameter is how we get history.
"""

from __future__ import annotations

import logging
from datetime import date

from ..schema import WarnNotice
from ..source import Source, handler
from ._helpers import row_signature, scrape_paged

log = logging.getLogger(__name__)

BASE = "https://reactwarn.floridajobs.org/WarnList/Records"


@handler("FL")
def scrape_florida(source: Source) -> list[WarnNotice]:
    collected: list[dict] = []
    seen: set[tuple] = set()
    current_year = date.today().year

    for year in range(current_year, source.start_year - 1, -1):
        def url_for_page(page: int, _year: int = year) -> str:
            suffix = f"&page={page}" if page > 1 else ""
            return f"{BASE}?year={_year}{suffix}"

        rows = scrape_paged(source, url_for_page, max_pages=40)
        if not rows:
            # Years before the system's coverage return an empty list; one
            # empty year is not proof we are done, two in a row is.
            continue
        for row in rows:
            signature = row_signature(row)
            if signature not in seen:
                seen.add(signature)
                collected.append(row)

    return source.to_notices(collected, f"{BASE}?year={current_year}")
