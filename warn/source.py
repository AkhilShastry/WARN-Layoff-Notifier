"""The `Source` spec and the generic strategies that execute it.

A state is described declaratively -- a URL, a strategy, and occasionally a
column override -- and one of the strategies below does the work. Only states
whose site genuinely needs bespoke logic (paginated search apps, PDF-per-notice
listings, JSON APIs with odd envelopes) get a custom handler, registered from
`warn.states`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from . import http
from .normalize import STATE_NAMES, clean_text
from .parsers.files import read_tabular
from .parsers.links import find_data_links
from .parsers.records import rows_to_notices
from .parsers.tables import extract_tables, merge_tables
from .schema import WarnNotice

log = logging.getLogger(__name__)

# state code -> handler function
HANDLERS: dict[str, Callable[["Source"], list[WarnNotice]]] = {}


def handler(state: str) -> Callable:
    """Register a custom scraper for a state."""

    def decorator(func: Callable[["Source"], list[WarnNotice]]):
        HANDLERS[state.upper()] = func
        return func

    return decorator


@dataclass
class Source:
    """How to obtain one state's WARN list."""

    state: str
    name: str
    url: str
    strategy: str = "auto"
    # auto    - sniff the payload and pick html/file
    # html    - scrape the best table on the page
    # file    - download and parse a CSV/XLSX/XLS/PDF directly
    # landing - find data-file links on a landing page, then parse them
    # json    - parse a JSON API response
    # custom  - dispatch to a registered handler

    # Request shaping
    method: str = "GET"
    params: dict[str, Any] | None = None
    data: Any = None
    json_body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    verify: bool = True
    impersonate: bool = False

    # Multi-year sources: url.format(year=YYYY) across `years`.
    year_template: str | None = None
    years: list[int] | None = None
    start_year: int = 2021

    # Parsing shaping
    overrides: dict[str, str] = field(default_factory=dict)
    dayfirst: bool = False
    defaults: dict[str, Any] = field(default_factory=dict)
    json_path: tuple[str, ...] = ()
    merge_all_tables: bool = False
    max_files: int = 4
    link_filter: str | None = None
    # Some sources only publish headcounts on a per-notice detail page. Set by
    # `scrape --details`, since it costs one request per notice.
    fetch_details: bool = False

    # Bookkeeping
    official: bool = True
    notes: str = ""
    # Set when a state genuinely does not publish WARN notices online. Such a
    # source is reported as unavailable rather than counted as a broken
    # scraper, so the health check stays meaningful.
    unavailable: str = ""

    @property
    def state_name(self) -> str:
        return STATE_NAMES.get(self.state.upper(), self.state)

    def target_years(self) -> list[int]:
        if self.years:
            return sorted(self.years, reverse=True)
        current = date.today().year
        return list(range(current, self.start_year - 1, -1))

    def urls(self) -> list[str]:
        if self.year_template:
            return [self.year_template.format(year=y) for y in self.target_years()]
        return [self.url]

    # ------------------------------------------------------------------
    def fetch(self, url: str | None = None, **kwargs: Any) -> http.Response:
        target = url or self.url
        options: dict[str, Any] = {
            "key": self.state.upper(),
            "method": self.method,
            "params": self.params,
            "data": self.data,
            "json_body": self.json_body,
            "headers": self.headers,
            "verify": self.verify,
            "impersonate": self.impersonate,
        }
        options.update(kwargs)
        return http.fetch(target, **options)

    def to_notices(self, rows: list[dict[str, Any]], url: str) -> list[WarnNotice]:
        return rows_to_notices(
            rows,
            state=self.state.upper(),
            source_name=self.name,
            source_url=url,
            overrides=self.overrides,
            dayfirst=self.dayfirst,
            defaults=self.defaults,
        )

    def scrape(self) -> list[WarnNotice]:
        strategy = self.strategy.lower()
        if strategy == "custom":
            func = HANDLERS.get(self.state.upper())
            if func is None:
                raise RuntimeError(f"no custom handler registered for {self.state}")
            return func(self)
        runner = _STRATEGIES.get(strategy)
        if runner is None:
            raise RuntimeError(f"unknown strategy {self.strategy!r} for {self.state}")
        return runner(self)


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

def parse_html_payload(source: Source, markup: str, url: str) -> list[WarnNotice]:
    """Scrape whichever table on the page looks like the WARN list."""
    tables = extract_tables(markup)
    if not tables:
        return []

    if source.merge_all_tables:
        merged = merge_tables(tables)
        candidates = [merged] if merged else []
    else:
        candidates = sorted(tables, key=lambda t: t.score, reverse=True)[:3]

    best: list[WarnNotice] = []
    for table in candidates:
        if table is None or table.score <= 0:
            continue
        notices = source.to_notices(table.rows, url)
        if len(notices) > len(best):
            best = notices
    return best


def scrape_html(source: Source) -> list[WarnNotice]:
    collected: list[WarnNotice] = []
    errors: list[str] = []
    empty_streak = 0

    for url in source.urls():
        response = source.fetch(url)
        if not response.ok():
            errors.append(f"HTTP {response.status_code} for {url}")
            # A year page that does not exist yet is normal, not a failure.
            empty_streak += 1
            if source.year_template and empty_streak >= 2:
                break
            continue
        notices = parse_html_payload(source, response.text, url)
        collected.extend(notices)
        # Two barren years in a row means we have run off the end of the
        # archive; stop rather than walking back to 2021 for nothing.
        empty_streak = 0 if notices else empty_streak + 1
        if source.year_template and empty_streak >= 2:
            break

    if not collected and errors:
        raise RuntimeError(errors[0])
    return collected


def scrape_file(source: Source) -> list[WarnNotice]:
    collected: list[WarnNotice] = []
    errors: list[str] = []
    for url in source.urls():
        response = source.fetch(url)
        if not response.ok():
            errors.append(f"HTTP {response.status_code} for {url}")
            continue
        kind = http.sniff_kind(response, url)
        if kind == "html":
            # The link pointed at a wrapper page rather than the file.
            collected.extend(parse_html_payload(source, response.text, url))
            continue
        rows = read_tabular(response.content, kind)
        collected.extend(source.to_notices(rows, url))

    if not collected and errors:
        raise RuntimeError(errors[0])
    return collected


def scrape_landing(source: Source) -> list[WarnNotice]:
    """Follow a landing page to the data files it links to."""
    response = source.fetch()
    if not response.ok():
        raise RuntimeError(f"HTTP {response.status_code} for {source.url}")

    kind = http.sniff_kind(response, source.url)
    if kind != "html":
        rows = read_tabular(response.content, kind)
        return source.to_notices(rows, source.url)

    collected: list[WarnNotice] = []

    # A landing page sometimes carries the current year inline and links only
    # to archives; take the inline table too.
    collected.extend(parse_html_payload(source, response.text, source.url))

    links = find_data_links(response.text, response.url or source.url)
    if source.link_filter:
        needle = source.link_filter.lower()
        links = [l for l in links if needle in l.url.lower() or needle in l.text.lower()]

    seen_urls: set[str] = set()
    for link in links[: source.max_files]:
        if link.url in seen_urls:
            continue
        seen_urls.add(link.url)
        try:
            file_response = source.fetch(link.url)
        except Exception as exc:
            log.debug("%s: could not fetch %s (%s)", source.state, link.url, exc)
            continue
        if not file_response.ok():
            continue
        file_kind = http.sniff_kind(file_response, link.url)
        if file_kind == "html":
            continue
        try:
            rows = read_tabular(file_response.content, file_kind)
        except Exception as exc:
            log.debug("%s: could not parse %s (%s)", source.state, link.url, exc)
            continue
        collected.extend(source.to_notices(rows, link.url))

    return collected


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if isinstance(payload, dict):
            payload = payload.get(key)
        elif isinstance(payload, list) and key.isdigit():
            index = int(key)
            payload = payload[index] if index < len(payload) else None
        else:
            return None
    return payload


def scrape_json(source: Source) -> list[WarnNotice]:
    collected: list[WarnNotice] = []
    for url in source.urls():
        response = source.fetch(url)
        if not response.ok():
            if url == source.urls()[0]:
                raise RuntimeError(f"HTTP {response.status_code} for {url}")
            continue
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"invalid JSON from {url}: {exc}") from exc

        rows = _dig(payload, source.json_path) if source.json_path else payload
        if isinstance(rows, dict):
            # Find the first list of dicts inside the envelope.
            rows = next(
                (v for v in rows.values()
                 if isinstance(v, list) and v and isinstance(v[0], dict)),
                [],
            )
        if not isinstance(rows, list):
            continue
        flat = [_flatten(r) for r in rows if isinstance(r, dict)]
        collected.extend(source.to_notices(flat, url))
    return collected


def _flatten(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{name}_"))
        elif isinstance(value, list):
            flat[name] = ", ".join(clean_text(v) or "" for v in value) or None
        else:
            flat[name] = value
    return flat


def scrape_auto(source: Source) -> list[WarnNotice]:
    """Sniff the payload and dispatch."""
    response = source.fetch()
    if not response.ok():
        raise RuntimeError(f"HTTP {response.status_code} for {source.url}")

    kind = http.sniff_kind(response, source.url)
    if kind in ("csv", "xlsx", "xls", "pdf"):
        rows = read_tabular(response.content, kind)
        return source.to_notices(rows, source.url)
    if kind == "json":
        return scrape_json(source)

    notices = parse_html_payload(source, response.text, source.url)
    if notices:
        return notices
    # No usable table inline -- fall back to hunting for linked data files.
    return scrape_landing(source)


_STRATEGIES: dict[str, Callable[[Source], list[WarnNotice]]] = {
    "auto": scrape_auto,
    "html": scrape_html,
    "file": scrape_file,
    "landing": scrape_landing,
    "json": scrape_json,
}


# --------------------------------------------------------------------------
# Result envelope
# --------------------------------------------------------------------------

@dataclass
class ScrapeResult:
    state: str
    source_name: str
    url: str
    ok: bool = False
    notices: list[WarnNotice] = field(default_factory=list)
    error: str | None = None
    http_status: int | None = None
    duration_s: float = 0.0
    rows_new: int = 0
    unavailable: str = ""

    @property
    def count(self) -> int:
        return len(self.notices)

    @property
    def workers(self) -> int:
        return sum(n.employees or 0 for n in self.notices)


def run_source(source: Source) -> ScrapeResult:
    """Execute one source, converting any failure into a result object."""
    started = time.monotonic()
    result = ScrapeResult(
        state=source.state.upper(), source_name=source.name, url=source.url
    )
    if source.unavailable:
        result.unavailable = source.unavailable
        result.error = source.unavailable
        return result

    try:
        notices = source.scrape()
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        log.warning("%s failed: %s", source.state, result.error)
    else:
        # Guard against a state silently returning a parsed-but-empty page.
        result.notices = [n for n in notices if n.is_usable()]
        result.ok = bool(result.notices)
        if not result.ok:
            result.error = "parsed 0 usable rows"
    result.duration_s = round(time.monotonic() - started, 2)
    return result
