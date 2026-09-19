"""Pennsylvania L&I -- notices live in nested accordion panels, not a table.

The page nests year -> month -> employer. Each leaf panel is a labelled block:

    150 Cesanek Road, Northampton, PA 18067
    COUNTY: Northampton
    # AFFECTED: 52
    EFFECTIVE DATE: beginning 11/16/2026, ending 4/1/2027
    CLOSURE OR LAYOFF: Closure

so the employer comes from the accordion title, the year and month from the
ancestor accordion titles, and the rest from the labelled lines.
"""

from __future__ import annotations

import logging
import re

from ..normalize import (
    STATE_NAMES, classify_notice_type, clean_text, parse_date, parse_int,
)
from ..parsers.tables import make_soup
from ..schema import WarnNotice
from ..source import Source, handler

log = logging.getLogger(__name__)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# The plurals matter: the page uses both "EFFECTIVE DATE:" and "EFFECTIVE
# DATES:". Missing the plural made the "# AFFECTED" value run on into the date
# text, so a count of 129 parsed as 20223 out of "7/3/20223".
_LABEL = re.compile(
    r"(COUNT(?:Y|IES)|#\s*AFFECTED|AFFECTED|EFFECTIVE\s+DATES?|"
    r"CLOSURE\s+OR\s+LAYOFF|NOTICE\s+DATES?|UNION|INDUSTRY)\s*:",
    re.IGNORECASE,
)
_ADDRESS = re.compile(r",\s*PA\s+\d{5}")
_YEAR = re.compile(r"^(20\d{2})$")
# "beginning 11/16/2026, ending 4/1/2027" -> we want the beginning
_BEGINNING = re.compile(r"beginning\s+([^,;]+)", re.IGNORECASE)


def _first_int(value: str | None) -> int | None:
    """Take the first number in a labelled field.

    `parse_int` returns the largest number it finds, which is right for ranges
    ("50-60 workers") but wrong here: if a label ever fails to match, the next
    field's text runs on and a stray year would win.
    """
    if not value:
        return None
    match = re.search(r"\d[\d,]*", value)
    return parse_int(match.group(0)) if match else None


def _panel_fields(text: str) -> dict[str, str]:
    """Split a panel's text on its uppercase labels."""
    fields: dict[str, str] = {}
    matches = list(_LABEL.finditer(text))
    if matches:
        # Anything before the first label is the street address.
        lead = text[: matches[0].start()].strip(" |​ ")
        if lead:
            fields["_address"] = lead
    for i, match in enumerate(matches):
        key = re.sub(r"[^a-z]", "", match.group(1).lower())
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        fields[key] = text[match.end():end].strip(" |​ ")
    if not matches:
        fields["_address"] = text.strip(" |​ ")
    return fields


def _notice_context(item) -> tuple[int | None, int | None]:
    """Find the year and month a notice is filed under.

    The two are marked up differently, which is the trap here:

      * the **month** is an enclosing accordion item ("September"), so it is
        found by walking up; and
      * the **year** is a plain <h2> heading that *precedes* the year's
        accordion block rather than wrapping it, so it is found by walking
        backwards in document order.

    Looking for both as ancestors finds only the month, leaving every notice
    undated.
    """
    month = None
    for ancestor in item.find_parents(class_="cmp-accordion__item"):
        title_tag = ancestor.find(class_=re.compile("cmp-accordion__title"))
        if title_tag is None:
            continue
        title = (clean_text(title_tag.get_text(" ", strip=True)) or "").strip()
        if title.lower() in _MONTHS:
            month = _MONTHS[title.lower()]
            break

    year = None
    for heading in item.find_all_previous(["h2", "h3"]):
        text = (clean_text(heading.get_text(" ", strip=True)) or "").strip()
        if _YEAR.match(text):
            year = int(text)
            break

    return year, month


@handler("PA")
def scrape_pennsylvania(source: Source) -> list[WarnNotice]:
    response = source.fetch()
    if not response.ok():
        raise RuntimeError(f"HTTP {response.status_code} for {source.url}")

    soup = make_soup(response.text)
    notices: list[WarnNotice] = []

    for item in soup.find_all(class_="cmp-accordion__item"):
        title_tag = item.find(class_="cmp-accordion__title")
        panel = item.find(class_=re.compile("cmp-accordion__panel"))
        if title_tag is None or panel is None:
            continue

        # Container items (a year or a month) hold nested items; skip them.
        if panel.find(class_="cmp-accordion__item") is not None:
            continue

        employer = clean_text(title_tag.get_text(" ", strip=True))
        if not employer or _YEAR.match(employer) or employer.lower() in _MONTHS:
            continue

        text = panel.get_text(" ", strip=True).replace("​", "")
        fields = _panel_fields(text)

        address = fields.get("_address")
        city = zip_code = None
        if address:
            match = re.search(r",\s*([A-Za-z .'\-]+),\s*PA\s+(\d{5})", address)
            if match:
                city = clean_text(match.group(1))
                zip_code = match.group(2)

        effective_raw = fields.get("effectivedate", "")
        beginning = _BEGINNING.search(effective_raw)
        effective_date = parse_date(
            beginning.group(1) if beginning else effective_raw
        )

        notice_date = parse_date(fields.get("noticedate", ""))
        if notice_date is None:
            year, month = _notice_context(item)
            if year:
                from datetime import date as _date

                notice_date = _date(year, month or 1, 1)

        notices.append(
            WarnNotice(
                state="PA",
                state_name=STATE_NAMES["PA"],
                employer=employer,
                city=city,
                county=clean_text(fields.get("county")),
                address=clean_text(address) if address and _ADDRESS.search(address) else None,
                zip_code=zip_code,
                notice_date=notice_date,
                effective_date=effective_date,
                employees=_first_int(fields.get("affected")),
                notice_type=classify_notice_type(fields.get("closureorlayoff")),
                industry=clean_text(fields.get("industry")),
                union_name=clean_text(fields.get("union")),
                source_name=source.name,
                source_url=source.url,
                raw={"title": employer, "panel": text[:900]},
            )
        )

    return [n for n in notices if n.is_usable()]
