"""Washington ESD -- an ASP.NET WebForms GridView.

The list is paginated with `__doPostBack('ucPSW$gvMain','Page$N')`, so each page
is a POST that echoes back the page's own `__VIEWSTATE`. We replay that
handshake: parse the hidden fields, post for page N+1, repeat.
"""

from __future__ import annotations

import logging

from ..parsers.tables import make_soup
from ..schema import WarnNotice
from ..source import Source, handler
from ._helpers import pick_table, row_signature

log = logging.getLogger(__name__)

SEARCH_URL = "https://fortress.wa.gov/esd/file/WARN/Public/SearchWARN.aspx"
GRID = "ucPSW$gvMain"
_HIDDEN = (
    "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED",
    "__EVENTVALIDATION", "__LASTFOCUS",
)


def _hidden_fields(markup: str) -> dict[str, str]:
    soup = make_soup(markup)
    fields: dict[str, str] = {}
    for name in _HIDDEN:
        tag = soup.find("input", {"name": name})
        if tag is not None:
            fields[name] = tag.get("value", "") or ""
    return fields


@handler("WA")
def scrape_washington(source: Source, max_pages: int = 80) -> list[WarnNotice]:
    response = source.fetch(SEARCH_URL, use_cache=False)
    if not response.ok():
        raise RuntimeError(f"HTTP {response.status_code} for {SEARCH_URL}")

    markup = response.text
    rows: list[dict] = []
    seen: set[tuple] = set()

    for page in range(1, max_pages + 1):
        table = pick_table(markup)
        if table is None or not table.rows:
            break

        fresh = 0
        for row in table.rows:
            signature = row_signature(row)
            if signature in seen:
                continue
            seen.add(signature)
            rows.append(row)
            fresh += 1
        if fresh == 0:
            break

        # Request the next page using this page's viewstate.
        payload = _hidden_fields(markup)
        payload["__EVENTTARGET"] = GRID
        payload["__EVENTARGUMENT"] = f"Page${page + 1}"
        next_response = source.fetch(
            SEARCH_URL, method="POST", data=payload, use_cache=False,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": SEARCH_URL},
        )
        if not next_response.ok():
            break
        markup = next_response.text

    notices = source.to_notices(rows, SEARCH_URL)
    for notice, row in zip(notices, rows):
        link = row.get("_link")
        if isinstance(link, str) and link.startswith("/"):
            notice.source_url = "https://fortress.wa.gov" + link
    return notices
