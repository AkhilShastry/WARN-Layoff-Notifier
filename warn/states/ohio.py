"""Ohio JFS.

The WARN pages are a Next.js app, so there is no table in the served HTML --
but the page payload references the CSV the client fetches, on Ohio's asset
CDN. We pull the CSV URL out of the payload rather than hard-coding it, because
the URL carries a cache-busting version segment that changes on every update.

Ohio keeps one page (and one CSV) per year.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from ..http import sniff_kind
from ..parsers.files import read_tabular
from ..schema import WarnNotice
from ..source import Source, handler

log = logging.getLogger(__name__)

CURRENT = ("https://jfs.ohio.gov/job-workforce-services/job-programs-and-services/"
           "submit-a-warn-notice/current-public-notices-of-layoffs-and-closures")
ARCHIVE = ("https://jfs.ohio.gov/job-workforce-services/job-programs-and-services/"
           "submit-a-warn-notice/{year}-public-notices-of-layoffs-and-closures")

# e.g. https://dam.assets.ohio.gov/raw/upload/f_auto/q_auto/v1776198587/
#      jfs.ohio.gov/2026/2025_warn_notice.csv
_CSV_URL = re.compile(
    r"https://dam\.assets\.ohio\.gov/raw/upload/[^\"'\\\s]+?\.csv", re.IGNORECASE
)


@handler("OH")
def scrape_ohio(source: Source) -> list[WarnNotice]:
    pages = [CURRENT]
    pages += [ARCHIVE.format(year=y)
              for y in range(date.today().year, source.start_year - 1, -1)]

    csv_urls: list[str] = []
    errors: list[str] = []

    for page_url in pages:
        response = source.fetch(page_url)
        if not response.ok():
            errors.append(f"HTTP {response.status_code} for {page_url}")
            continue
        for match in _CSV_URL.finditer(response.text):
            url = match.group(0).rstrip("\\")
            if url not in csv_urls:
                csv_urls.append(url)

    if not csv_urls:
        raise RuntimeError(
            errors[0] if errors else "no WARN CSV link found in the JFS pages"
        )

    notices: list[WarnNotice] = []
    for url in csv_urls:
        file_response = source.fetch(url)
        if not file_response.ok():
            continue
        kind = sniff_kind(file_response, url)
        if kind == "html":
            kind = "csv"  # the CDN serves CSV as text/html
        rows = read_tabular(file_response.content, kind)
        notices.extend(source.to_notices(rows, url))

    if not notices:
        raise RuntimeError(f"found {len(csv_urls)} CSV link(s) but parsed no rows")
    return notices
