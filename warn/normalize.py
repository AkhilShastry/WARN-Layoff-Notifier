"""Turn messy state-published cell values into typed Python values.

State WARN tables contain things like "3/14/2024", "March 14, 2024", "45259"
(an Excel serial), "Various", "TBD", "approx. 150", "120-135", and "  " -- all
in the same column. Everything here is defensive: the goal is to extract a value
when one is really there and return None otherwise, never to raise.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from dateutil import parser as date_parser

from .config import MAX_YEAR_OFFSET, MIN_YEAR

# --------------------------------------------------------------------------
# State identity
# --------------------------------------------------------------------------

STATE_CODES: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "District of Columbia": "DC", "Puerto Rico": "PR",
}
STATE_NAMES: dict[str, str] = {v: k for k, v in STATE_CODES.items()}


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

_NULLISH = {
    "", "n/a", "na", "n.a.", "none", "null", "nan", "nat", "-", "--", "---",
    "tbd", "t.b.d.", "unknown", "unk", "not available", "not applicable",
    "pending", "n/a.", "<na>", "#n/a", "none listed", "not reported", "*",
    ".", "not specified", "to be determined", "no data",
}

_WS = re.compile(r"\s+")


def clean_text(value: Any, *, max_length: int = 500) -> str | None:
    """Normalize whitespace/unicode and map placeholder junk to None."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None

    text = str(value)
    # Normalize NBSP and friends, strip zero-width characters.
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("​", "").replace("﻿", "")
    text = _WS.sub(" ", text).strip()
    text = text.strip(" \t\r\n ")

    if text.lower() in _NULLISH:
        return None
    if not text:
        return None
    return text[:max_length]


_LEGAL_SUFFIX = re.compile(
    r"[,\s]+(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|corp|corp\.|corporation|co|co\.|"
    r"company|lp|l\.p\.|llp|plc|pllc|pc|p\.c\.|holdings|group)\.?$",
    re.IGNORECASE,
)


def normalize_employer(name: str | None) -> str:
    """A comparison key for employer names, used for dedupe and grouping.

    Strips punctuation, legal suffixes, and 'dba' tails so that
    "Acme Corp.", "ACME CORPORATION" and "Acme Corp dba Acme Foods" collapse.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name)).lower()
    text = re.sub(r"\bd/?b/?a\b.*$", " ", text)       # drop "dba ..." tail
    text = re.sub(r"\bf/?k/?a\b.*$", " ", text)       # drop "fka ..." tail
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = _WS.sub(" ", text).strip()
    # Legal suffixes can stack: "Acme Holdings Inc" -> "acme"
    for _ in range(3):
        stripped = _LEGAL_SUFFIX.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return text


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------

# No leading sign: headcounts are never negative, and allowing "-" would make
# the "60" in a range like "50-60" parse as -60 and get discarded.
_INT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_int(value: Any) -> int | None:
    """Pull an employee count out of a cell.

    Handles "1,234", "approx. 50", "50-60" (takes the larger), "45 employees",
    floats, and rejects anything implausible.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int,)):
        candidate = int(value)
        return candidate if 0 <= candidate <= 500_000 else None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        candidate = int(round(value))
        return candidate if 0 <= candidate <= 500_000 else None

    text = clean_text(value)
    if not text:
        return None

    matches = _INT_RE.findall(text.replace("–", "-").replace("—", "-"))
    if not matches:
        return None

    numbers: list[int] = []
    for m in matches:
        try:
            numbers.append(int(round(float(m.replace(",", "")))))
        except ValueError:
            continue
    numbers = [n for n in numbers if 0 <= n <= 500_000]
    if not numbers:
        return None
    # "50-60 employees" -> report the upper bound (worst case for workers).
    return max(numbers)


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

# Excel stores dates as days since 1899-12-30 (the leap-year bug offset).
_EXCEL_EPOCH = datetime(1899, 12, 30)
_EXCEL_MIN, _EXCEL_MAX = 32000, 60000  # ~1987 to ~2064

_DATE_RANGE_SPLIT = re.compile(r"\s*(?:-|–|—|to|thru|through|&|/{2,}|;)\s*", re.IGNORECASE)
_MONTH_WORD = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.IGNORECASE
)


def _plausible(d: date) -> bool:
    return MIN_YEAR <= d.year <= date.today().year + MAX_YEAR_OFFSET


_PACKED = re.compile(r"^(19|20)(\d{2})(\d{2})(\d{2})$")


def _parse_packed_date(text: str) -> date | None:
    """Parse an unseparated YYYYMMDD value (Wisconsin publishes these)."""
    match = _PACKED.match(text.strip())
    if not match:
        return None
    try:
        parsed = date(
            int(match.group(1) + match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
        )
    except ValueError:
        return None
    return parsed if _plausible(parsed) else None


def parse_date(value: Any, *, dayfirst: bool = False) -> date | None:
    """Parse almost anything into a date, or return None.

    Ranges like "3/1/2024 - 3/15/2024" resolve to the first date.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        d = value.date()
        return d if _plausible(d) else None
    if isinstance(value, date):
        return value if _plausible(value) else None

    # Excel serial numbers arrive as ints/floats when openpyxl loses formatting.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        packed = _parse_packed_date(str(int(value)))
        if packed:
            return packed
        if _EXCEL_MIN <= float(value) <= _EXCEL_MAX:
            d = (_EXCEL_EPOCH + timedelta(days=float(value))).date()
            return d if _plausible(d) else None
        return None

    text = clean_text(value, max_length=120)
    if not text:
        return None

    # Strip weekday prefixes and trailing times/timezones.
    text = re.sub(r"^(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*[,\s]+",
                  "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?\b.*$", "", text,
                  flags=re.IGNORECASE).strip()
    text = text.strip(" ,;")
    if not text or text.lower() in _NULLISH:
        return None

    # Packed YYYYMMDD, e.g. Wisconsin's "20200102".
    packed = _parse_packed_date(text)
    if packed:
        return packed

    # ISO-ish fast path (common in JSON/Socrata payloads).
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        try:
            d = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
            return d if _plausible(d) else None
        except ValueError:
            pass

    candidates = [text]
    # A range: take the first half, but only when the split looks like dates.
    parts = [p for p in _DATE_RANGE_SPLIT.split(text) if p.strip()]
    if len(parts) > 1 and not re.match(r"^\d{1,2}-\d{1,2}-\d{2,4}$", text):
        candidates.insert(0, parts[0])

    for candidate in candidates:
        candidate = candidate.strip(" ,;.")
        if not candidate or len(candidate) < 4:
            continue
        # A bare 4-digit year is not a date we want.
        if re.fullmatch(r"\d{4}", candidate):
            continue
        try:
            parsed = date_parser.parse(
                candidate, dayfirst=dayfirst, fuzzy=True,
                default=datetime(2000, 1, 1),
            )
        except (ValueError, OverflowError, TypeError):
            continue
        d = parsed.date()
        # fuzzy=True can invent a date from a stray number; require that the
        # source actually looked date-ish.
        if not (re.search(r"\d{1,4}[/\-.]\d{1,2}", candidate)
                or _MONTH_WORD.search(candidate)):
            continue
        if _plausible(d):
            return d
    return None


# --------------------------------------------------------------------------
# Categorical
# --------------------------------------------------------------------------

def classify_notice_type(*values: Any) -> str | None:
    """Collapse free-text action descriptions into Closure / Layoff / Both."""
    blob = " ".join(str(v).lower() for v in values if v is not None)
    if not blob.strip():
        return None

    # Some states publish two-letter codes rather than words.
    codes = {"cl": "Closure", "lo": "Layoff", "cl/lo": "Closure and Layoff",
             "c": "Closure", "l": "Layoff"}
    if blob.strip() in codes:
        return codes[blob.strip()]

    closure = bool(re.search(r"clos|shut|cessation|terminat\w*\s+of\s+oper|dissolv", blob))
    layoff = bool(re.search(r"lay\s*-?\s*off|layoff|reduction|rif|downsiz|furlough|separation", blob))

    if closure and layoff:
        return "Closure and Layoff"
    if closure:
        return "Closure"
    if layoff:
        return "Layoff"
    cleaned = clean_text(blob.title(), max_length=80)
    return cleaned


# --------------------------------------------------------------------------
# Identity / dedupe
# --------------------------------------------------------------------------

def record_hash(
    state: str,
    employer: str | None,
    city: str | None,
    notice_date: date | None,
    effective_date: date | None,
    employees: int | None,
) -> str:
    """Stable identity for a notice.

    Deliberately excludes free-text fields that states edit in place (notes,
    contact, address) so that a cosmetic edit upstream does not masquerade as a
    brand-new layoff notice in the alerting pipeline.
    """
    parts = [
        (state or "").upper(),
        normalize_employer(employer),
        re.sub(r"[^a-z0-9]", "", (city or "").lower()),
        notice_date.isoformat() if notice_date else "",
        effective_date.isoformat() if effective_date else "",
        str(employees) if employees is not None else "",
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
