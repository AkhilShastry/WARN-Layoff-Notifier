"""Find the actual data file on a state's landing page.

Many states do not publish a table at all -- the WARN page is a wrapper around
"2026 WARN Notices (XLSX)" links that change URL every year. Hard-coding those
URLs guarantees breakage each January, so instead we crawl the landing page and
pick the newest matching file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin, urlparse

from .tables import make_soup

DATA_EXTENSIONS = (".xlsx", ".xls", ".csv", ".pdf", ".xlsm")

_YEAR_RE = re.compile(r"(20\d{2})")
_WARN_WORDS = re.compile(r"warn|layoff|dislocat|notice|closure", re.IGNORECASE)


def absolute(base: str, href: str) -> str:
    return urljoin(base, (href or "").strip())


def _detect_extension(
    url: str, text: str, extensions: tuple[str, ...]
) -> str | None:
    """Work out what kind of file a link points at.

    Three cases in the wild:
      1. The path ends in the extension (the easy case).
      2. The extension is in the query string -- SharePoint's
         `/_layouts/download.aspx?SourceUrl=.../WARN.xlsx` (Illinois).
      3. There is no extension anywhere and only the link text says so --
         "Open XLSX file, 21.09 KB, FY26 WARN Report" (Massachusetts).
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    for extension in extensions:
        if path.endswith(extension):
            return extension

    query = parsed.query.lower()
    for extension in extensions:
        if extension in query:
            return extension

    lowered = text.lower()
    for extension in extensions:
        bare = extension.lstrip(".")
        # Require the word to read as a file reference, not a passing mention.
        if re.search(rf"\b{bare}\b\s*(file|document|download|report)?", lowered) and (
            "file" in lowered or "download" in lowered or "open" in lowered
        ):
            return extension
    return None


@dataclass
class DataLink:
    url: str
    text: str
    extension: str
    year: int | None = None

    @property
    def sort_key(self) -> tuple[int, int]:
        # Newest year first; unknown years last.
        return (self.year or 0, 1 if self.extension in (".xlsx", ".csv") else 0)


def find_data_links(
    markup: str | bytes,
    base_url: str,
    *,
    extensions: tuple[str, ...] = DATA_EXTENSIONS,
    require_warn_words: bool = True,
) -> list[DataLink]:
    """All downloadable data files linked from a page, newest first."""
    soup = make_soup(markup)
    found: dict[str, DataLink] = {}

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = absolute(base_url, href)
        text = anchor.get_text(" ", strip=True)
        extension = _detect_extension(url, text, extensions)
        if not extension:
            continue

        haystack = f"{text} {url}"
        if require_warn_words and not _WARN_WORDS.search(haystack):
            continue

        years = [int(y) for y in _YEAR_RE.findall(haystack)]
        # Ignore implausible years scraped out of unrelated digits.
        years = [y for y in years if 2000 <= y <= date.today().year + 1]
        found.setdefault(
            url, DataLink(url=url, text=text, extension=extension,
                          year=max(years) if years else None)
        )

    return sorted(found.values(), key=lambda link: link.sort_key, reverse=True)


def find_year_links(
    markup: str | bytes,
    base_url: str,
    *,
    min_year: int = 2020,
) -> list[tuple[int, str]]:
    """Links to per-year WARN sub-pages, e.g. ".../warn-notices-2025".

    Returns (year, url) newest first.
    """
    soup = make_soup(markup)
    results: dict[str, int] = {}
    current_year = date.today().year

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        text = anchor.get_text(" ", strip=True)
        haystack = f"{text} {href}"
        if not _WARN_WORDS.search(haystack):
            continue
        years = [int(y) for y in _YEAR_RE.findall(haystack)]
        years = [y for y in years if min_year <= y <= current_year + 1]
        if not years:
            continue
        url = absolute(base_url, href)
        if urlparse(url).path.lower().endswith(DATA_EXTENSIONS):
            continue
        results[url] = max(max(years), results.get(url, 0))

    return sorted(((y, u) for u, y in results.items()), reverse=True)
