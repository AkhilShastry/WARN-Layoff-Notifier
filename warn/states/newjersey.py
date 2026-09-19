"""New Jersey DOL -- one PDF per year, with no usable header row.

Page 1's header is merged into a single cell ("Company City Month P...") and
later pages have no header at all, so the generic header detection picks a data
row. The column order is stable, so we assign it explicitly.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date

from ..normalize import STATE_NAMES, clean_text, parse_date, parse_int
from ..schema import WarnNotice
from ..source import Source, handler

log = logging.getLogger(__name__)

URL_TEMPLATE = "https://www.nj.gov/labor/assets/PDFs/WARN/{year}_WARN_Notice_Archive.pdf"

COLUMNS = ("employer", "city", "month_posted", "effective_date", "employees")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_HEADER_ROW = re.compile(r"company\s+city|workforce\s+affected", re.IGNORECASE)


@handler("NJ")
def scrape_new_jersey(source: Source) -> list[WarnNotice]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pdfplumber is required for New Jersey") from exc

    notices: list[WarnNotice] = []
    misses = 0

    for year in range(date.today().year, source.start_year - 1, -1):
        url = URL_TEMPLATE.format(year=year)
        response = source.fetch(url)
        if not response.ok() or not response.content.startswith(b"%PDF"):
            misses += 1
            if misses >= 2:
                break
            continue
        misses = 0

        try:
            with pdfplumber.open(io.BytesIO(response.content)) as pdf:
                for page in pdf.pages:
                    for table in page.extract_tables() or []:
                        notices.extend(_rows_to_notices(table, year, source, url))
        except Exception as exc:
            log.debug("NJ %s: pdf parse failed (%s)", year, exc)

    return [n for n in notices if n.is_usable()]


def _rows_to_notices(
    table: list[list[str | None]], year: int, source: Source, url: str
) -> list[WarnNotice]:
    notices: list[WarnNotice] = []
    for row in table:
        values = [clean_text((cell or "").replace("\n", " ")) for cell in row]
        if not any(values):
            continue
        record = dict(zip(COLUMNS, values + [None] * len(COLUMNS)))

        employer = record.get("employer")
        if not employer or _HEADER_ROW.search(employer):
            continue

        # "Month Posted" is the only notice-date signal NJ gives us.
        notice_date = None
        month_name = (record.get("month_posted") or "").strip().lower()
        month = _MONTHS.get(month_name.split()[0]) if month_name else None
        if month:
            notice_date = date(year, month, 1)

        # Effective dates are often ranges: "4/10/26 - 11/26/26".
        effective_raw = record.get("effective_date") or ""
        effective_date = parse_date(effective_raw.split("-")[0].strip())

        city, _, tail = (record.get("city") or "").partition(",")
        notices.append(
            WarnNotice(
                state="NJ",
                state_name=STATE_NAMES["NJ"],
                employer=employer,
                city=clean_text(city) or None,
                notice_date=notice_date,
                effective_date=effective_date,
                employees=parse_int(record.get("employees")),
                source_name=source.name,
                source_url=url,
                notes=clean_text(tail) or None,
                raw={k: v for k, v in record.items() if v},
            )
        )
    return notices
