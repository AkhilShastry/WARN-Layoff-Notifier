"""Raw rows -> canonical `WarnNotice` objects.

This is the single funnel every state passes through, regardless of whether the
rows came from HTML, Excel, CSV, JSON or PDF. Doing the mapping in one place is
what keeps per-state code down to a URL and the occasional override.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Sequence

from ..normalize import (
    STATE_CODES, STATE_NAMES, classify_notice_type, clean_text, parse_date,
    parse_int,
)
from ..schema import WarnNotice, map_headers, normalize_header

log = logging.getLogger(__name__)

# Values that show up in an employer cell when the row is not a real notice.
_JUNK_EMPLOYER = re.compile(
    r"^(total|totals|grand total|company|company name|employer|employer name|"
    r"business name|n/?a|none|tbd|subtotal|sum|count|no notices?|"
    r"no warn notices?( (were )?(filed|received|reported))?|"
    r"there (are|were) no .*|blank|\d+|page \d+.*|continued.*)$",
    re.IGNORECASE,
)

_CITY_STATE = re.compile(r"^(.*?)[,\s]+([A-Z]{2})$")

# A handful of states (Maryland among them) publish one "Location" column that
# is really "<street address> <city>, <ST> <zip>" flattened into one string
# with no delimiter between the street and the city. There's no reliable way
# to split "2611 Pepsi Place Hyattsville" into street vs. city without a
# gazetteer, so rather than guess -- and mislabel a street address as a city --
# the whole thing goes to `address` and `city` is left honestly blank.
_FULL_ADDRESS = re.compile(
    r"^\d+\s+\S.*,\s*[A-Z]{2}\s+\d{5}(-\d{4})?$"
)
# A bare classification code (NAICS etc.) with no description is not
# something a reader would recognize as "the industry" -- better to omit it
# than display "621498" as if it were meaningful.
_BARE_CODE = re.compile(r"^\d{2,6}$")


def _is_junk_row(row: dict[str, Any], employer: str | None) -> bool:
    if not employer:
        return True
    if _JUNK_EMPLOYER.match(employer.strip()):
        return True
    if len(employer.strip()) < 2:
        return True
    # A repeated header row inside the body: most cells map to field names.
    values = [clean_text(v) for v in row.values()]
    values = [v for v in values if v]
    if len(values) >= 3:
        mapped = map_headers(values)
        if len(set(mapped.values())) >= max(3, len(values) - 1):
            return True
    return False


def _split_city_state(value: str | None) -> tuple[str | None, str | None]:
    """"Baltimore, MD" -> ("Baltimore", "MD")."""
    text = clean_text(value)
    if not text:
        return None, None
    match = _CITY_STATE.match(text)
    if match and match.group(2) in STATE_NAMES:
        return clean_text(match.group(1)), match.group(2)
    return text, None


def build_notice(
    row: dict[str, Any],
    mapping: dict[int, str],
    keys: Sequence[str],
    *,
    state: str,
    source_name: str,
    source_url: str,
    dayfirst: bool = False,
    defaults: dict[str, Any] | None = None,
) -> WarnNotice | None:
    """Assemble one notice from one raw row. Returns None for junk rows."""
    values: dict[str, Any] = {}
    for index, field_name in mapping.items():
        if index >= len(keys):
            continue
        raw_value = row.get(keys[index])
        # Keep the first non-empty value when two columns map to one field.
        if values.get(field_name) in (None, "") and raw_value not in (None, ""):
            values[field_name] = raw_value

    employer = clean_text(values.get("employer"), max_length=300)
    if _is_junk_row(row, employer):
        return None

    city, embedded_state = _split_city_state(values.get("city"))
    address = clean_text(values.get("address"), max_length=300)
    zip_code = _clean_zip(values.get("zip_code"))

    raw_city = clean_text(values.get("city"), max_length=300)
    if raw_city and _FULL_ADDRESS.match(raw_city):
        # This "city" is really an unparsed "<street> <city>, ST zip" string.
        # Keep it as an address rather than mislabeling it as a city.
        address = address or raw_city
        if not zip_code:
            zip_match = re.search(r"(\d{5})(?:-\d{4})?$", raw_city)
            zip_code = zip_match.group(1) if zip_match else None
        city = None

    industry = clean_text(values.get("industry"), max_length=200)
    if industry and _BARE_CODE.match(industry):
        industry = None  # a classification code with no description isn't useful

    notice = WarnNotice(
        state=state,
        state_name=STATE_NAMES.get(state, ""),
        employer=employer or "",
        city=city,
        county=clean_text(values.get("county"), max_length=160),
        address=address,
        zip_code=zip_code,
        region=clean_text(values.get("region"), max_length=160),
        notice_date=parse_date(values.get("notice_date"), dayfirst=dayfirst),
        effective_date=parse_date(values.get("effective_date"), dayfirst=dayfirst),
        closing_date=parse_date(values.get("closing_date"), dayfirst=dayfirst),
        employees=parse_int(values.get("employees")),
        notice_type=classify_notice_type(values.get("notice_type")),
        industry=industry,
        union_name=clean_text(values.get("union_name"), max_length=200),
        contact=clean_text(values.get("contact"), max_length=200),
        notes=clean_text(values.get("notes"), max_length=1000),
        source_name=source_name,
        source_url=source_url,
        raw={str(k): (str(v) if v is not None else None) for k, v in row.items()},
    )

    for key, value in (defaults or {}).items():
        if getattr(notice, key, None) in (None, "") and value not in (None, ""):
            setattr(notice, key, value)

    # A state column inside the data beats our assumption only if the source is
    # explicitly multi-state (rare); otherwise it is a worksite address artifact.
    if embedded_state and not notice.state:
        notice.state = embedded_state

    # A per-row link (PDF of the filing) is more precise provenance than the
    # list page URL.
    link = row.get("_link")
    if isinstance(link, str) and link.startswith("http"):
        notice.source_url = link

    # Some states put the closure/layoff wording in a notes or type-less column.
    if not notice.notice_type:
        notice.notice_type = classify_notice_type(notice.notes)

    return notice if notice.is_usable() else None


def _clean_zip(value: Any) -> str | None:
    text = clean_text(value, max_length=20)
    if not text:
        return None
    match = re.search(r"\b(\d{5})(?:-\d{4})?\b", text)
    return match.group(1) if match else None


def rows_to_notices(
    rows: Iterable[dict[str, Any]],
    *,
    state: str,
    source_name: str,
    source_url: str,
    overrides: dict[str, str] | None = None,
    dayfirst: bool = False,
    defaults: dict[str, Any] | None = None,
) -> list[WarnNotice]:
    """Map a batch of raw rows onto the canonical schema."""
    rows = list(rows)
    if not rows:
        return []

    # Union of keys preserves column order across ragged rows.
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen and key != "_link":
                seen.add(key)
                keys.append(key)

    mapping = map_headers(keys, overrides=overrides)
    if "employer" not in mapping.values():
        log.debug("%s: no employer column found in %s", state, keys[:12])
        return []

    notices: list[WarnNotice] = []
    for row in rows:
        try:
            notice = build_notice(
                row, mapping, keys, state=state, source_name=source_name,
                source_url=source_url, dayfirst=dayfirst, defaults=defaults,
            )
        except Exception as exc:  # one bad row must not kill a state
            log.debug("%s: row failed (%s): %r", state, exc, row)
            continue
        if notice:
            notices.append(notice)

    return notices


def describe_mapping(keys: Sequence[str]) -> dict[str, str]:
    """Human-readable header -> field mapping, for the `inspect` command."""
    mapping = map_headers(keys)
    return {str(keys[i]): field_name for i, field_name in sorted(mapping.items())}
