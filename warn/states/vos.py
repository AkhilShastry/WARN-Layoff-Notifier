"""States running Geographic Solutions' Virtual OneStop job-bank software.

Six states publish WARN through the same Rails application, reachable at
`/search/warn_lookups` with Ransack-style query parameters. One handler covers
all of them; only the hostname differs.

The list view returns 25 rows per page and paginates with `page=N`. It does not
include an employee count -- that lives on each notice's detail page -- so
`fetch_details` trades a lot of requests for headcounts and is off by default.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode, urlparse

from ..normalize import clean_text, parse_int
from ..parsers.tables import make_soup
from ..schema import WarnNotice
from ..source import Source, handler
from ._helpers import scrape_paged

log = logging.getLogger(__name__)

# Hosts confirmed to serve the VOS WARN lookup.
VOS_HOSTS = {
    "AZ": "https://www.azjobconnection.gov",
    "DE": "https://joblink.delaware.gov",
    "KS": "https://www.kansasworks.com",
    "ME": "https://joblink.maine.gov",
    "OK": "https://okjobmatch.com",
    "VT": "https://www.vermontjoblink.com",
}

# Hosts with certificate chains that Python rejects but browsers accept.
_SKIP_VERIFY = {"OK"}

_SEARCH_PATH = "/search/warn_lookups"
_QUERY = {"q[notice_eq]": "true", "commit": "Search"}


def _base(source: Source) -> str:
    host = VOS_HOSTS.get(source.state.upper())
    if host:
        return host
    parsed = urlparse(source.url)
    return f"{parsed.scheme}://{parsed.netloc}"


def scrape_vos(source: Source, *, fetch_details: bool | None = None) -> list[WarnNotice]:
    base = _base(source)
    verify = source.state.upper() not in _SKIP_VERIFY

    def url_for_page(page: int) -> str:
        query = dict(_QUERY)
        if page > 1:
            query["page"] = str(page)
        return f"{base}{_SEARCH_PATH}?{urlencode(query)}"

    rows = scrape_paged(
        source, url_for_page, max_pages=120,
        fetch_kwargs={"verify": verify},
    )
    if not rows:
        return []

    notices = source.to_notices(rows, url_for_page(1))

    # The list view links each row to its detail page; rewrite the per-row
    # source URL so a reader can reach the actual filing.
    for notice, row in zip(notices, rows):
        link = row.get("_link")
        if isinstance(link, str) and link.startswith("/"):
            notice.source_url = base + link

    if source.fetch_details if fetch_details is None else fetch_details:
        _enrich_with_details(source, notices, base, verify)

    return notices


_COUNT_LABEL = re.compile(
    r"(employees?\s+affected|number\s+of\s+employees|affected\s+employees|"
    r"planned\s+number)", re.IGNORECASE
)


def _enrich_with_details(
    source: Source, notices: list[WarnNotice], base: str, verify: bool
) -> None:
    """Fetch each notice's detail page for the employee count.

    Expensive: one request per notice. Only worth it for small states.
    """
    for notice in notices:
        if notice.employees is not None or not notice.source_url.startswith(base):
            continue
        try:
            response = source.fetch(notice.source_url, verify=verify)
        except Exception:
            continue
        if not response.ok():
            continue
        soup = make_soup(response.text)
        # Detail pages render a definition list, but as headings and
        # paragraphs rather than <dt>/<dd>:
        #   <h3 class="definition-list__title">Number of Employees Affected</h3>
        #   <p class="definition-list__definition">39</p>
        for term in soup.find_all(["dt", "th", "strong", "label", "h3", "h4", "h5"]):
            label = term.get_text(" ", strip=True)
            if not _COUNT_LABEL.search(label):
                continue
            value = term.find_next(["dd", "td", "span", "p"])
            if value is None:
                continue
            count = parse_int(clean_text(value.get_text(" ", strip=True)))
            if count is not None:
                notice.employees = count
                break


for _code in VOS_HOSTS:
    handler(_code)(scrape_vos)
