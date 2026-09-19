"""The canonical WARN record, and the column-matching logic that gets us there.

Every state publishes the same handful of facts under different column names.
Alabama says "Company", New York says "Company Name", Texas says "JOB_SITE_NAME".
Rather than hand-map 50 x ~8 columns (and re-map them every time a state edits a
header), each canonical field owns a list of header synonyms plus a few token
rules, and `map_headers` scores every incoming header against them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Canonical field definitions
# --------------------------------------------------------------------------

# Exact (normalized) header synonyms. Normalization strips everything that is
# not a letter or digit, so "Notice Date" / "notice_date" / "NOTICE-DATE" all
# collapse to "noticedate".
SYNONYMS: dict[str, list[str]] = {
    "employer": [
        "company", "companyname", "employer", "employername", "businessname",
        "nameofcompany", "name", "business", "organization", "organizationname",
        "companyorganization", "employerbusinessname", "subjectcompany",
        "jobsitename", "worksite", "worksitename", "sitename", "facility",
        "facilityname", "affectedcompany", "companyemployer", "employercompany",
        "companyname1", "warnemployer", "entity", "establishment",
        # North Carolina labels the column "WARN Notice: WARN Notice Name".
        "warnnoticename", "noticename", "employerlegalname",
    ],
    "city": [
        "city", "cities", "citytown", "town", "citystate", "joblocation",
        "layofflocation", "location", "locationcity", "cityname", "sitecity",
        "worksitecity", "citywherelayoffwilloccur", "physicalcity",
        "citylocation", "affectedcity", "citycounty",
    ],
    "county": [
        "county", "countyname", "counties", "countyparish", "parish",
        "countyregion", "sitecounty", "worksitecounty", "countyarea",
        "countyorregion",
    ],
    "address": [
        "address", "streetaddress", "address1", "addressline1", "locationaddress",
        "siteaddress", "worksiteaddress", "physicaladdress", "companyaddress",
        "addressoflayoff", "streetaddressoflayoff",
    ],
    "zip_code": ["zip", "zipcode", "postalcode", "zip5"],
    "region": [
        "region", "regionname", "workforceregion", "localarea", "lwia", "lwib",
        "lwiaarea", "lwibarea", "wda", "wdaname", "servicedeliveryarea",
        "wib", "wdb", "workforcedevelopmentarea", "workforcearea", "area",
        "workforcedevelopmentboard", "rapidresponsearea", "district",
        "serviceareacode", "localworkforcearea",
    ],
    "notice_date": [
        "noticedate", "warndate", "datereceived", "receiveddate", "dateofnotice",
        "noticereceived", "datewarnreceived", "warnnoticedate", "initialreportdate",
        "datenoticereceived", "reportdate", "filingdate", "datefiled",
        "noticereceiveddate", "warnreceived", "dateofwarnnotice", "postingdate",
        "datewarnnoticereceived", "receivedon", "noticedatereceived",
        "noticercvd", "rcvd", "noticeon", "warnreceiveddate",
        "submitteddate", "datesubmitted", "submitted", "createddate",
        "statenotificationdate", "notificationdate", "warnnoticereceiveddate",
        "dateofreceipt", "issuedate", "dateposted", "warnfileddate",
    ],
    "effective_date": [
        "layoffdate", "effectivedate", "separationdate", "layoffbegindate",
        "dateoflayoff", "layoffstartdate", "plannedstartingdate", "startdate",
        "effectivelayoffdate", "anticipatedseparationdate", "firstseparationdate",
        "layoffclosuredate", "locldate", "lodate", "impactdate", "layoffeffectivedate",
        "beginningdate", "projectedlayoffdate", "dateoflayoffclosure",
        "layoffclosingdate", "separationstartdate", "anticipatedlayoffdate",
        "expectedlayoffdate", "layoffsbegin", "datelayoffsbegin", "closureorlayoffdate",
        "layoffdates", "scheduleddate", "plannedlayoffdate", "actionstartdate",
        "begindate", "beginningdates", "layoffbegins", "firstlayoffdate",
    ],
    "closing_date": [
        "closingdate", "closuredate", "layoffenddate", "plannedendingdate",
        "enddate", "lastdayofwork", "finalseparationdate", "layofffinaldate",
        "dateofclosure", "completiondate", "actionenddate",
    ],
    "employees": [
        "numberofemployees", "employeesaffected", "affectedemployees",
        "numberaffected", "totalemployees", "workers", "ofemployees",
        "impactedworkers", "numberofworkers", "numemployees", "affected",
        "numberofaffectedemployees", "employeecount", "totalaffected",
        "numberofemployeesaffected", "workersaffected", "affectedworkers",
        "numberofimpactedworkers", "employees", "headcount", "empcount",
        # Seen in the wild: Iowa "Emp #", Oregon "Count", Texas Socrata
        # "total_layoff_number", Florida "Employees Affected".
        "emp", "emps", "count", "totallayoffnumber", "layoffnumber",
        "totalnumberofemployees", "numberofworkersaffected", "totalnotified",
        "numberofpeopleaffected", "estimatedimpact", "plannedjoblosses",
        "numberoflayoffs", "totalworkersaffected", "employeeimpact", "jobslost",
        # NB: deliberately not a bare "layoffs" -- as a substring it captures
        # free-text columns like Colorado's "Reason for Layoffs".
    ],
    "notice_type": [
        "type", "noticetype", "closureorlayoff", "layoffclosure", "actiontype",
        "closurelayoff", "typeofaction", "warntype", "layofftype", "eventtype",
        "reason", "typeofnotice", "closingorlayoff", "permanentortemporary",
        "layoffpermanenttemporary", "actionclosurelayoff", "naturalofaction",
        "natureofaction", "closingtype",
    ],
    "industry": [
        "industry", "naics", "naicscode", "sector", "industrysector",
        "industrycode", "industrydescription", "businesstype", "naicsdescription",
    ],
    "union_name": ["union", "unionname", "unionaffiliation", "bargainingunit", "unions"],
    "notes": [
        "notes", "comments", "remarks", "additionalinformation", "description",
        "comment", "note", "details",
    ],
    "contact": ["contact", "contactname", "companycontact", "contactperson", "phone"],
}

# Token-level fallbacks. If no exact synonym matched, a header is scored by how
# many of these tokens it contains. Keyed by canonical field.
TOKEN_HINTS: dict[str, list[tuple[tuple[str, ...], int]]] = {
    "employer": [(("company",), 6), (("employer",), 6), (("business", "name"), 5),
                 (("site", "name"), 4), (("name",), 1)],
    "city": [(("city",), 6), (("town",), 5), (("location",), 2)],
    "county": [(("county",), 6), (("parish",), 5)],
    "address": [(("address",), 6), (("street",), 4)],
    "zip_code": [(("zip",), 6), (("postal",), 5)],
    "region": [(("region",), 5), (("lwia",), 6), (("workforce", "area"), 5), (("area",), 2)],
    "notice_date": [(("notice", "date"), 8), (("warn", "date"), 8),
                    (("received",), 5), (("filed",), 5), (("notification",), 4)],
    "effective_date": [(("layoff", "date"), 8), (("effective",), 6),
                       (("separation",), 5), (("begin",), 3), (("start",), 3)],
    "closing_date": [(("closing",), 6), (("closure", "date"), 6), (("end", "date"), 5)],
    "employees": [(("employees",), 6), (("workers",), 6), (("affected",), 5),
                  (("number",), 2), (("impact",), 3), (("count",), 3)],
    "notice_type": [(("type",), 5), (("closure",), 3), (("layoff",), 1)],
    "industry": [(("industry",), 6), (("naics",), 6), (("sector",), 5)],
    "union_name": [(("union",), 6)],
    "notes": [(("notes",), 5), (("comments",), 5), (("remarks",), 5)],
    "contact": [(("contact",), 5), (("phone",), 4)],
}

# Fields a row must have to be worth storing.
REQUIRED_FIELDS = ("employer",)

DATE_FIELDS = ("notice_date", "effective_date", "closing_date")

# Order matters for CSV export readability.
CANONICAL_FIELDS = (
    "state", "state_name", "employer", "city", "county", "address", "zip_code",
    "region", "notice_date", "effective_date", "closing_date", "employees",
    "notice_type", "industry", "union_name", "contact", "notes",
    "source_name", "source_url",
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def normalize_header(header: Any) -> str:
    """Collapse a raw column header to a comparable key."""
    if header is None:
        return ""
    text = str(header).strip().lower()
    # Pandas gives unnamed columns names like "Unnamed: 3".
    if text.startswith("unnamed:"):
        return ""
    text = text.replace("&", "and")
    # Drop parenthetical qualifiers: "Employees (est.)" -> "employees"
    text = re.sub(r"\([^)]*\)", " ", text)
    return _NON_ALNUM.sub("", text)


def header_tokens(header: Any) -> list[str]:
    if header is None:
        return []
    text = str(header).strip().lower().replace("&", "and")
    text = re.sub(r"\([^)]*\)", " ", text)
    return [t for t in _TOKEN_SPLIT.split(text) if t]


# Reverse index built once: normalized synonym -> canonical field.
_SYNONYM_INDEX: dict[str, str] = {}
for _canon, _variants in SYNONYMS.items():
    for _v in _variants:
        _SYNONYM_INDEX.setdefault(normalize_header(_v), _canon)


def score_header(header: Any) -> list[tuple[int, str]]:
    """Return (score, canonical_field) candidates for one raw header, best first."""
    norm = normalize_header(header)
    if not norm:
        return []

    scored: dict[str, int] = {}

    # 1. Exact synonym hit is near-unbeatable.
    exact = _SYNONYM_INDEX.get(norm)
    if exact:
        scored[exact] = 100

    # 2. A synonym contained inside a longer header, e.g.
    #    "numberofemployeesaffectedbythisnotice" -> employees.
    for syn, canon in _SYNONYM_INDEX.items():
        if len(syn) >= 6 and syn in norm:
            scored[canon] = max(scored.get(canon, 0), 40 + len(syn))

    # 3. Token hints.
    tokens = set(header_tokens(header))
    if tokens:
        for canon, rules in TOKEN_HINTS.items():
            total = sum(weight for needed, weight in rules if set(needed) <= tokens)
            if total:
                scored[canon] = max(scored.get(canon, 0), total)

    return sorted(((s, c) for c, s in scored.items()), reverse=True)


def map_headers(
    headers: Iterable[Any],
    overrides: dict[str, str] | None = None,
    minimum_score: int = 4,
) -> dict[int, str]:
    """Map column positions to canonical field names.

    Resolves conflicts globally: if two columns both look like `notice_date`,
    the higher-scoring one wins and the loser falls back to its next-best
    candidate. This is what keeps states with both "Notice Date" and "Layoff
    Date" from collapsing into one field.

    `overrides` maps a normalized header to a canonical field and always wins;
    it is the escape hatch for genuinely ambiguous state tables.
    """
    headers = list(headers)
    overrides = {normalize_header(k): v for k, v in (overrides or {}).items()}

    # candidates[i] = [(score, field), ...] best first
    candidates: dict[int, list[tuple[int, str]]] = {}
    for i, h in enumerate(headers):
        norm = normalize_header(h)
        if norm in overrides:
            candidates[i] = [(999, overrides[norm])]
        else:
            candidates[i] = [c for c in score_header(h) if c[0] >= minimum_score]

    assigned: dict[int, str] = {}
    taken: dict[str, tuple[int, int]] = {}  # field -> (score, column index)

    # Greedy by score, highest first, with displacement.
    pending = [(score, canon, idx)
               for idx, cands in candidates.items()
               for score, canon in cands]
    pending.sort(key=lambda t: (-t[0], t[2]))

    for score, canon, idx in pending:
        if idx in assigned:
            continue
        prev = taken.get(canon)
        if prev is None:
            assigned[idx] = canon
            taken[canon] = (score, idx)
        elif score > prev[0]:
            # This column is a better fit; evict the previous holder so it can
            # be reconsidered for its next-best field on a later pass.
            del assigned[prev[1]]
            assigned[idx] = canon
            taken[canon] = (score, idx)

    return assigned


# --------------------------------------------------------------------------
# The record itself
# --------------------------------------------------------------------------

@dataclass
class WarnNotice:
    """One normalized WARN filing."""

    state: str = ""            # USPS code, e.g. "MD"
    state_name: str = ""
    employer: str = ""
    city: str | None = None
    county: str | None = None
    address: str | None = None
    zip_code: str | None = None
    region: str | None = None
    notice_date: date | None = None
    effective_date: date | None = None
    closing_date: date | None = None
    employees: int | None = None
    notice_type: str | None = None
    industry: str | None = None
    union_name: str | None = None
    contact: str | None = None
    notes: str | None = None

    source_name: str = ""
    source_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in DATE_FIELDS:
            value = d.get(key)
            if isinstance(value, (date, datetime)):
                d[key] = value.isoformat()
        return d

    def is_usable(self) -> bool:
        """Reject rows that carry no employer or no date at all."""
        if not self.employer or len(self.employer) < 2:
            return False
        return any(getattr(self, f) for f in DATE_FIELDS) or self.employees is not None
