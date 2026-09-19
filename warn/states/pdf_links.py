"""States that publish no list at all -- only a page of links to notice PDFs.

West Virginia and Hawaii both do this. The link text carries the employer and
usually a date ("ECM Energy Services WARN 8-19-26", "Foodland Super Market,
Ltd. - 3/14/25"), and the upload path carries a year/month fallback. That is
less than a table would give us -- no headcount, rarely a city -- but it is
real sourced data, and every record keeps a link to the filing itself.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import urljoin

from ..normalize import STATE_NAMES, clean_text, parse_date
from ..parsers.tables import make_soup
from ..schema import WarnNotice
from ..source import Source, handler

log = logging.getLogger(__name__)

# A trailing date in the link text, with or without a "WARN"/"Notice" prefix.
_DATE_TAIL = re.compile(
    r"[\s\-–—_,]*(?:warn|notice|state|updated?|revised)?[\s\-–—_,]*"
    r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}[-/.]\d{4}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})"
    r"\s*$",
    re.IGNORECASE,
)
_NOISE = re.compile(
    r"\b(warn|notice[s]?|state|revised|signed|supplemental|updated?|copy|final|"
    r"letter|amended|rev|r\d|\d{4})\b",
    re.IGNORECASE,
)
# Links that are policy documents, navigation, or law references -- not notices.
_SKIP = re.compile(
    r"regulation|uscode|ecfr|guide|requirement|rapid response|dislocated|"
    r"aversion|compensation|trade adjustment|fact sheet|instructions|form|"
    r"^warn listing$|^layoffs|^archive|poster|espanol|spanish",
    re.IGNORECASE,
)

CONFIG = {
    "WV": {"host": "https://workforcewv.org"},
    "HI": {
        "host": "https://labor.hawaii.gov",
        # Hawaii splits its notices across one sub-page per year.
        "year_template": "https://labor.hawaii.gov/wdc/{year}-warn-notices/",
    },
    # Minnesota's link text is just the employer -- the date sits in the list
    # item next to the link ("Nilfisk Inc. - 12/10/25 Revised").
    "MN": {"host": "https://mn.gov"},
}


def _clean_employer(text: str) -> str:
    text = _DATE_TAIL.sub("", text)
    text = _NOISE.sub(" ", text)
    text = re.sub(r"[_]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -–—_.,·|")


_ANY_DATE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")


def _date_beside(anchor) -> date | None:
    """Look for a date in the text immediately following the link.

    Minnesota renders "<a>Nilfisk Inc.</a> - 12/10/25 Revised", so the date is
    a sibling text node rather than part of the link.
    """
    fragments: list[str] = []
    for sibling in anchor.next_siblings:
        text = sibling if isinstance(sibling, str) else getattr(sibling, "get_text", lambda **_: "")(strip=True)
        if text:
            fragments.append(str(text))
        if sum(len(f) for f in fragments) > 60:
            break
    trailing = " ".join(fragments)[:80]
    match = _ANY_DATE.search(trailing)
    if match:
        return parse_date(match.group(1))

    parent = anchor.parent
    if parent is not None:
        match = _ANY_DATE.search(parent.get_text(" ", strip=True)[:160])
        if match:
            return parse_date(match.group(1))
    return None


def _date_from_path(href: str) -> date | None:
    """Fall back to the CMS upload path, e.g. /wp-content/uploads/2026/08/."""
    match = re.search(r"/(20\d{2})/(\d{1,2})/", href)
    if match:
        month = int(match.group(2))
        if 1 <= month <= 12:
            try:
                return date(int(match.group(1)), month, 1)
            except ValueError:
                return None
    bare_year = re.search(r"/(20\d{2})/", href)
    if bare_year:
        try:
            return date(int(bare_year.group(1)), 1, 1)
        except ValueError:
            return None
    return None


def scrape_pdf_links(source: Source) -> list[WarnNotice]:
    state = source.state.upper()
    config = CONFIG.get(state, {})
    host = config.get("host", "")

    pages: list[str] = [source.url]
    year_template = config.get("year_template")
    if year_template:
        pages = [year_template.format(year=y)
                 for y in range(date.today().year, source.start_year - 1, -1)]

    notices: list[WarnNotice] = []
    seen: set[tuple[str, object]] = set()
    errors: list[str] = []

    for page_url in pages:
        response = source.fetch(page_url)
        if not response.ok():
            errors.append(f"HTTP {response.status_code} for {page_url}")
            continue
        _harvest(source, response.text, host, state, notices, seen)

    if not notices:
        raise RuntimeError(errors[0] if errors else "no notice PDF links found")
    return notices


def _harvest(
    source: Source,
    markup: str,
    host: str,
    state: str,
    notices: list[WarnNotice],
    seen: set[tuple[str, object]],
) -> None:
    soup = make_soup(markup)

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if ".pdf" not in href.lower():
            continue
        text = clean_text(anchor.get_text(" ", strip=True)) or ""
        if not text or _SKIP.search(text):
            continue
        # Require some WARN signal in either the text or the path.
        if not re.search(r"warn|layoff|closure", f"{text} {href}", re.IGNORECASE):
            continue

        notice_date = None
        tail = _DATE_TAIL.search(text)
        if tail:
            notice_date = parse_date(tail.group(1))
        if notice_date is None:
            # Several states put the date beside the link rather than in it.
            notice_date = _date_beside(anchor)
        if notice_date is None:
            notice_date = _date_from_path(href)

        employer = _clean_employer(text)
        if len(employer) < 3 or employer.isdigit():
            continue

        key = (employer.lower(), notice_date)
        if key in seen:
            continue
        seen.add(key)

        notices.append(
            WarnNotice(
                state=state,
                state_name=STATE_NAMES.get(state, ""),
                employer=employer,
                notice_date=notice_date,
                source_name=source.name,
                source_url=href if href.startswith("http") else urljoin(host, href),
                notes="Parsed from the notice PDF link; this state publishes "
                      "no summary table.",
                raw={"link_text": text, "href": href},
            )
        )


for _code in CONFIG:
    handler(_code)(scrape_pdf_links)
