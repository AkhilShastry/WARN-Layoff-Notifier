"""Oregon's Rapid Response Activity Tracking System.

A plain paginated table at `/Layoff/WARN?page=N`.
"""

from __future__ import annotations

from ..schema import WarnNotice
from ..source import Source, handler
from ._helpers import scrape_paged

BASE = "https://ccwd.hecc.oregon.gov/Layoff/WARN"


@handler("OR")
def scrape_oregon(source: Source) -> list[WarnNotice]:
    rows = scrape_paged(
        source,
        lambda page: BASE if page == 1 else f"{BASE}?page={page}",
        max_pages=120,
    )
    notices = source.to_notices(rows, BASE)
    for notice, row in zip(notices, rows):
        link = row.get("_link")
        if isinstance(link, str) and link.startswith("/"):
            notice.source_url = "https://ccwd.hecc.oregon.gov" + link
    return notices
