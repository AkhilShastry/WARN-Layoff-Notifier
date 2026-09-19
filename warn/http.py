"""HTTP fetching with retries, polite rate limiting, and an on-disk raw cache.

Three things make state WARN sites annoying:
  1. Several run expired or misconfigured TLS certificates.
  2. Several sit behind bot filters that reject the stock `requests` TLS
     fingerprint but happily serve a real browser.
  3. They are small servers that should not be hammered.

`fetch` handles all three, and caches every raw response to disk so that
re-parsing (the part we iterate on) never re-hits a government server.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

log = logging.getLogger(__name__)

# Suppress the noise from sources we intentionally fetch with verify=False.
try:  # pragma: no cover - depends on urllib3 version
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:  # pragma: no cover
    pass


@dataclass
class Response:
    """A fetched resource plus where it came from."""

    url: str
    status_code: int
    content: bytes
    encoding: str | None = None
    from_cache: bool = False
    cache_path: Path | None = None
    content_type: str = ""

    @property
    def text(self) -> str:
        enc = self.encoding or "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except (LookupError, TypeError):
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.text)

    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class RateLimiter:
    """One minimum-interval gate per hostname."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        with self._lock:
            last = self._last.get(host, 0.0)
            gap = time.monotonic() - last
            sleep_for = self.delay - gap
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last[host] = time.monotonic()


_limiter = RateLimiter(config.REQUEST_DELAY)
_session_lock = threading.Lock()
_session: requests.Session | None = None


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=config.MAX_RETRIES,
        connect=config.MAX_RETRIES,
        read=config.MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    return session


def get_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = _build_session()
        return _session


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _cache_path(url: str, key: str, method: str, body: Any) -> Path:
    digest = hashlib.sha1(
        f"{method}|{url}|{body!r}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".html", ".htm", ".csv", ".xlsx", ".xls", ".json", ".pdf", ".xml"}:
        suffix = ".bin"
    folder = config.CACHE_DIR / "raw" / (key or "misc")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{digest}{suffix}"


def _cache_fresh(path: Path, ttl_hours: float) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    if ttl_hours <= 0:
        return False
    age_hours = (time.time() - path.stat().st_mtime) / 3600.0
    return age_hours < ttl_hours


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def fetch(
    url: str,
    *,
    key: str = "",
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: Any = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    verify: bool = True,
    use_cache: bool = True,
    ttl_hours: float | None = None,
    impersonate: bool = False,
    allow_status: tuple[int, ...] = (),
) -> Response:
    """Fetch a URL, going through the disk cache when possible.

    `impersonate=True` routes through curl_cffi with a real Chrome TLS
    fingerprint, which is what gets us past the bot filters on a handful of
    state sites. We fall back to it automatically on a 403.
    """
    ttl = config.CACHE_TTL_HOURS if ttl_hours is None else ttl_hours
    body_key = (params, data, json_body)
    path = _cache_path(url, key, method, body_key)

    if use_cache and _cache_fresh(path, ttl):
        content = path.read_bytes()
        log.debug("cache hit %s (%s)", url, path.name)
        return Response(
            url=url, status_code=200, content=content,
            from_cache=True, cache_path=path,
        )

    _limiter.wait(url)

    merged_headers = dict(headers or {})
    response = _do_request(
        url, method=method, params=params, data=data, json_body=json_body,
        headers=merged_headers, verify=verify, impersonate=impersonate,
    )

    # Bot filter? Retry once with a browser TLS fingerprint.
    if response.status_code in (403, 406, 429) and not impersonate:
        log.debug("status %s on %s, retrying with impersonation",
                  response.status_code, url)
        _limiter.wait(url)
        retry_response = _do_request(
            url, method=method, params=params, data=data, json_body=json_body,
            headers=merged_headers, verify=verify, impersonate=True,
        )
        if retry_response.ok():
            response = retry_response

    if (response.ok() or response.status_code in allow_status) and response.content:
        try:
            path.write_bytes(response.content)
            response.cache_path = path
        except OSError as exc:  # pragma: no cover - disk issues
            log.warning("could not cache %s: %s", url, exc)

    return response


def _do_request(
    url: str,
    *,
    method: str,
    params: dict[str, Any] | None,
    data: Any,
    json_body: Any,
    headers: dict[str, str],
    verify: bool,
    impersonate: bool,
) -> Response:
    if impersonate:
        try:
            return _curl_request(url, method, params, data, json_body, headers, verify)
        except Exception as exc:  # pragma: no cover - optional dependency
            log.debug("curl_cffi failed for %s (%s); falling back", url, exc)

    session = get_session()
    resp = session.request(
        method=method, url=url, params=params, data=data, json=json_body,
        headers=headers or None, timeout=config.REQUEST_TIMEOUT,
        verify=verify, allow_redirects=True,
    )
    return Response(
        url=str(resp.url),
        status_code=resp.status_code,
        content=resp.content,
        encoding=resp.encoding or resp.apparent_encoding,
        content_type=resp.headers.get("Content-Type", ""),
    )


def _curl_request(
    url: str,
    method: str,
    params: dict[str, Any] | None,
    data: Any,
    json_body: Any,
    headers: dict[str, str],
    verify: bool,
) -> Response:
    from curl_cffi import requests as curl_requests

    merged = {"User-Agent": config.USER_AGENT, **(headers or {})}
    resp = curl_requests.request(
        method, url, params=params, data=data, json=json_body,
        headers=merged, timeout=config.REQUEST_TIMEOUT, verify=verify,
        impersonate="chrome124", allow_redirects=True,
    )
    return Response(
        url=str(resp.url),
        status_code=resp.status_code,
        content=resp.content,
        encoding=getattr(resp, "encoding", None) or "utf-8",
        content_type=resp.headers.get("Content-Type", ""),
    )


def sniff_kind(response: Response, url: str = "") -> str:
    """Identify a payload as html / csv / xlsx / xls / json / pdf.

    Content-Type headers from state servers are frequently wrong (an .xlsx
    served as text/html is common), so magic bytes win over the header.
    """
    head = response.content[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # OLE2 compound document
        return "xls"

    ctype = (response.content_type or "").lower()
    full = (url or response.url).lower()
    target = full.split("?")[0]

    if target.endswith(".csv") or "text/csv" in ctype:
        return "csv"
    if target.endswith((".xlsx", ".xlsm")) or "spreadsheetml" in ctype:
        return "xlsx"
    if target.endswith(".xls") or "ms-excel" in ctype:
        return "xls"
    if target.endswith(".pdf") or "application/pdf" in ctype:
        return "pdf"
    if target.endswith(".json") or "application/json" in ctype:
        return "json"

    # Export endpoints put the format in the query string, e.g. Google Sheets'
    # "/export?format=csv" or "/gviz/tq?tqx=out:csv".
    for token, kind in (("format=csv", "csv"), ("out:csv", "csv"),
                        ("format=xlsx", "xlsx"), ("format=json", "json")):
        if token in full:
            return kind

    sample = response.content[:2048].lstrip()
    if sample[:1] in (b"{", b"["):
        return "json"
    lowered = sample[:1024].lower()
    if b"<html" in lowered or b"<!doctype" in lowered[:200] or b"<body" in lowered:
        return "html"

    # Last resort, and the one that matters when a cached response has no
    # Content-Type: does it actually look like delimited text?
    if _looks_delimited(sample):
        return "csv"
    return "html"


def _looks_delimited(sample: bytes) -> bool:
    """True when the payload reads as CSV/TSV rather than markup."""
    try:
        text = sample.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        return False
    if "<" in text[:200]:
        return False
    lines = [line for line in text.splitlines() if line.strip()][:5]
    if len(lines) < 2:
        return False
    for delimiter in (",", "\t", ";", "|"):
        counts = [line.count(delimiter) for line in lines]
        # A real table has the same number of delimiters on every line.
        if counts[0] >= 2 and len(set(counts)) == 1:
            return True
    return False
