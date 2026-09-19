"""Nevada DETR -- an annual "WARN and Non-WARN Master" PDF.

The PDF has no ruled table, so pdfplumber's default extraction finds nothing.
Using its text-alignment strategy recovers clean columns at the edges (received
date, city, county, WARN/Non-WARN) but smears the middle ones together:

    ['1/22/2026', '3/15/2026La', 'yoff', '1Spirit Airli', 'ne', 's',
     'Las Vegas', 'Clark', 'WARN']

Rejoining the middle columns gives "3/15/2026Layoff 1Spirit Airlines", which
splits cleanly with one regex. Nevada also flags non-WARN filings in the same
report; those are kept and labelled rather than silently mixed in.
"""

from __future__ import annotations

import io
import logging
import re

from ..normalize import STATE_NAMES, classify_notice_type, clean_text, parse_date, parse_int
from ..parsers.links import find_data_links
from ..schema import WarnNotice
from ..source import Source, handler

log = logging.getLogger(__name__)

TEXT_SETTINGS = {"vertical_strategy": "text", "horizontal_strategy": "text"}

# "3/15/2026Layoff 1Spirit Airlines" -> date, type, count, employer
_MIDDLE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{4})\s*(Layoff|Closure|Closing)\s*"
    r"(\d+|unknown|NR|N/A)?\s*(.*)$",
    re.IGNORECASE,
)
_DATE_START = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


@handler("NV")
def scrape_nevada(source: Source) -> list[WarnNotice]:
    page = source.fetch()
    if not page.ok():
        raise RuntimeError(f"HTTP {page.status_code} for {source.url}")

    # Only the yearly "Master" reports are tables; the rest of the page links
    # to single-employer notice letters.
    links = [
        link for link in find_data_links(page.text, page.url or source.url)
        if "master" in link.url.lower() and link.extension == ".pdf"
    ]
    if not links:
        raise RuntimeError("no master WARN report linked on the DETR page")

    notices: list[WarnNotice] = []
    for link in links[: source.max_files]:
        response = source.fetch(link.url)
        if not response.ok() or not response.content.startswith(b"%PDF"):
            continue
        notices.extend(_parse_report(source, response.content, link.url))

    if not notices:
        raise RuntimeError(f"parsed no rows from {len(links)} master report(s)")
    return notices


def _parse_report(source: Source, content: bytes, url: str) -> list[WarnNotice]:
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover
        raise RuntimeError("pdfplumber is required for Nevada")

    notices: list[WarnNotice] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            try:
                table = page.extract_table(TEXT_SETTINGS)
            except Exception as exc:
                log.debug("NV: page extraction failed (%s)", exc)
                continue
            # Column extraction loses the space at each column boundary
            # ("Boulder City" + "Hospital" -> "Boulder CityHospital"), but the
            # plain text layer keeps it. Keep both and cross-reference.
            lines = [ln.strip() for ln in (page.extract_text() or "").splitlines()]
            for row in table or []:
                notice = _parse_row(source, row, url, lines)
                if notice:
                    notices.append(notice)
    return notices


def _employer_from_text(
    lines: list[str], received: str, city: str, county: str
) -> str | None:
    """Recover the employer with its spacing intact from the text layer.

    The line reads "1/27/2026 1/27/2026Layoff 35Ace Dragon Wok(...) Las Vegas
    Clark Non-WARN", so the employer is what sits between the affected count
    and the city.
    """
    for line in lines:
        if not line.startswith(received):
            continue
        match = _MIDDLE.match(line[len(received):].strip())
        if not match:
            continue
        remainder = match.group(4)
        # Trim the trailing city/county/notification columns.
        for trailer in (county, city):
            if trailer and trailer in remainder:
                remainder = remainder[: remainder.rindex(trailer)]
        return clean_text(remainder.rstrip(" -|"))
    return None


def _parse_row(
    source: Source, row: list[str | None], url: str, lines: list[str]
) -> WarnNotice | None:
    cells = [(c or "").strip() for c in row]
    if len(cells) < 6 or not _DATE_START.match(cells[0]):
        return None  # header, banner, or blank line

    # The last three columns stay aligned; everything between column 0 and
    # them is one smeared run of text.
    notification = cells[-1]
    county = cells[-2]
    city = cells[-3]
    middle = "".join(cells[1:-3])

    match = _MIDDLE.match(middle)
    if not match:
        return None

    effective_raw, action, count_raw, employer = match.groups()
    # Prefer the text-layer name, which keeps its internal spacing.
    employer = (
        _employer_from_text(lines, cells[0], city, county) or clean_text(employer)
    )
    if not employer:
        return None

    is_warn = "non" not in notification.lower()
    return WarnNotice(
        state="NV",
        state_name=STATE_NAMES["NV"],
        employer=employer,
        city=clean_text(city),
        county=clean_text(county),
        notice_date=parse_date(cells[0]),
        effective_date=parse_date(effective_raw),
        employees=parse_int(count_raw),
        notice_type=classify_notice_type(action),
        source_name=source.name,
        source_url=url,
        notes=None if is_warn else "Filed as a non-WARN notice (below WARN thresholds)",
        raw={"row": " | ".join(c for c in cells if c)},
    )
