"""Georgia TCSG -- a GravityView DataTables grid backed by WordPress AJAX.

The served HTML contains only the table's header row; the entries come from
`admin-ajax.php?action=gv_datatables_data`. The request needs a view id and a
rotating nonce, both of which are published in a JSON config block on the page,
so we read them from the page rather than hard-coding them.
"""

from __future__ import annotations

import json
import logging
import re

from ..parsers.tables import make_soup
from ..schema import WarnNotice
from ..source import Source, handler

log = logging.getLogger(__name__)

AJAX_URL = "https://www.tcsg.edu/wp-admin/admin-ajax.php"
PAGE_SIZE = 200
_CONFIG = re.compile(r"const config = (\{.*?\});", re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


def _ajax_params(markup: str) -> dict:
    """Pull the AJAX payload (view id, post id, nonce) out of the page."""
    match = _CONFIG.search(markup)
    if not match:
        raise RuntimeError("GravityView config block not found on the page")
    try:
        config = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse GravityView config: {exc}") from exc

    data = (config.get("ajax") or {}).get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GravityView config has no ajax.data block")
    return data


def _column_names(markup: str) -> list[str]:
    soup = make_soup(markup)
    table = soup.find("table", class_=re.compile("gv-datatables"))
    if table is None:
        return []
    header = table.find("thead") or table
    row = header.find("tr")
    if row is None:
        return []
    return [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]


def _clean_cell(value: object) -> str:
    text = _TAGS.sub(" ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


@handler("GA")
def scrape_georgia(source: Source) -> list[WarnNotice]:
    page = source.fetch()
    if not page.ok():
        raise RuntimeError(f"HTTP {page.status_code} for {source.url}")

    base_params = _ajax_params(page.text)
    columns = _column_names(page.text)
    if not columns:
        raise RuntimeError("could not read the table header")

    rows: list[dict[str, str]] = []
    start = 0

    for draw in range(1, 60):
        payload = {f"{k}": (json.dumps(v) if isinstance(v, (dict, list)) else v)
                   for k, v in base_params.items()}
        payload.update({"draw": draw, "start": start, "length": PAGE_SIZE})

        response = source.fetch(
            AJAX_URL, method="POST", data=payload, use_cache=False,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": source.url},
        )
        if not response.ok():
            break
        try:
            payload_json = response.json()
        except Exception as exc:
            log.debug("GA: non-JSON AJAX response (%s)", exc)
            break

        batch = payload_json.get("data") or []
        if not batch:
            break

        for entry in batch:
            values = entry if isinstance(entry, list) else list(entry.values())
            record = {
                columns[i] if i < len(columns) else f"column_{i}": _clean_cell(v)
                for i, v in enumerate(values)
            }
            rows.append(record)

        start += len(batch)
        total = payload_json.get("recordsTotal")
        if isinstance(total, int) and start >= total:
            break
        if len(batch) < PAGE_SIZE:
            break

    if not rows:
        raise RuntimeError("AJAX returned no entries")
    return source.to_notices(rows, source.url)
