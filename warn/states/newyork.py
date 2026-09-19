"""New York DOL.

The public WARN dashboard is a Tableau embed with no data endpoint we can use,
but the same notices are published as plain HTML: a current list at
`/warn-notices` and one archive page per past year.
"""

from __future__ import annotations

import logging
from datetime import date

from ..schema import WarnNotice
from ..source import Source, handler, parse_html_payload
from ._helpers import row_signature

log = logging.getLogger(__name__)

CURRENT = "https://dol.ny.gov/warn-notices"
ARCHIVE = "https://dol.ny.gov/{year}-warn-notices"


@handler("NY")
def scrape_new_york(source: Source) -> list[WarnNotice]:
    notices: list[WarnNotice] = []
    seen: set[tuple] = set()

    urls = [CURRENT]
    urls += [ARCHIVE.format(year=y)
             for y in range(date.today().year, source.start_year - 1, -1)]

    misses = 0
    for url in urls:
        response = source.fetch(url)
        if not response.ok():
            misses += 1
            # The current year's archive page appears partway through the year.
            if misses >= 3:
                break
            continue

        page_notices = parse_html_payload(source, response.text, url)
        if not page_notices:
            misses += 1
            continue
        misses = 0

        for notice in page_notices:
            signature = row_signature(notice.raw)
            if signature in seen:
                continue
            seen.add(signature)
            notices.append(notice)

    if not notices:
        raise RuntimeError("no WARN rows found on dol.ny.gov")
    return notices
