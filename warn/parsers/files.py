"""Tabular file parsing: CSV, XLSX, XLS, and PDF tables.

The recurring problem with state spreadsheets is that the real header is not row
zero -- there is a title, a logo row, a "revised 4/2/2025" note, and then the
columns. So everything is read header-less and the header row is detected by
scoring against the canonical field names.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any

import pandas as pd

from ..normalize import clean_text
from ..schema import map_headers, normalize_header

log = logging.getLogger(__name__)


def _score_header_row(values: list[Any]) -> float:
    non_empty = [clean_text(v) for v in values]
    non_empty = [v for v in non_empty if v]
    if len(non_empty) < 2:
        return -10.0
    mapped = map_headers(values)
    score = len(set(mapped.values())) * 2.0
    avg_len = sum(len(v) for v in non_empty) / len(non_empty)
    if avg_len > 60:
        score -= 3
    # A header row is mostly text, not numbers or dates.
    numeric = sum(1 for v in non_empty if re.fullmatch(r"[\d,.$%/\- ]+", v))
    if numeric > len(non_empty) / 2:
        score -= 4
    return score


def _promote_header(df: pd.DataFrame, max_scan: int = 12) -> pd.DataFrame:
    """Find the header row inside a header-less DataFrame and apply it."""
    if df.empty:
        return df

    best_index, best_score = None, 0.5
    for i in range(min(max_scan, len(df))):
        score = _score_header_row(list(df.iloc[i]))
        if score > best_score:
            best_index, best_score = i, score

    if best_index is None:
        return df

    header = [clean_text(v) or f"column_{j}" for j, v in enumerate(df.iloc[best_index])]
    # Disambiguate duplicates so pandas does not silently merge columns.
    seen: dict[str, int] = {}
    unique_header = []
    for name in header:
        if name in seen:
            seen[name] += 1
            name = f"{name}__{seen[name]}"
        else:
            seen[name] = 0
        unique_header.append(name)

    out = df.iloc[best_index + 1:].copy()
    out.columns = unique_header
    return out.reset_index(drop=True)


def rows_from_dataframe(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame -> list of plain dicts, dropping empty rows and columns."""
    if df is None or df.empty:
        return []
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
    records: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        record = {}
        for key, value in row.items():
            if isinstance(value, float) and pd.isna(value):
                value = None
            elif value is pd.NaT:
                value = None
            record[str(key)] = value
        if any(clean_text(v) for v in record.values()):
            records.append(record)
    return records


def _read_csv(content: bytes) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        for sep in (",", ";", "\t", "|"):
            try:
                df = pd.read_csv(
                    io.BytesIO(content), header=None, dtype=object,
                    encoding=encoding, sep=sep, engine="python",
                    on_bad_lines="skip", skip_blank_lines=True,
                )
            except Exception:
                continue
            if df.shape[1] >= 2 and len(df) >= 1:
                frames.append(df)
                return frames
    return frames


def _read_excel(content: bytes, kind: str) -> list[pd.DataFrame]:
    engine = "openpyxl" if kind == "xlsx" else "xlrd"
    frames: list[pd.DataFrame] = []
    try:
        book = pd.read_excel(
            io.BytesIO(content), sheet_name=None, header=None,
            dtype=object, engine=engine,
        )
    except Exception as exc:
        log.debug("excel read failed with %s: %s", engine, exc)
        # Some states mislabel the format; try the other engine.
        other = "xlrd" if engine == "openpyxl" else "openpyxl"
        try:
            book = pd.read_excel(
                io.BytesIO(content), sheet_name=None, header=None,
                dtype=object, engine=other,
            )
        except Exception:
            return frames
    for name, sheet in (book or {}).items():
        if sheet is not None and not sheet.empty:
            frames.append(sheet)
    return frames


def _read_pdf(content: bytes, max_pages: int = 60) -> list[pd.DataFrame]:
    """Pull tables out of a PDF with pdfplumber."""
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover
        log.warning("pdfplumber not installed; cannot parse PDF sources")
        return []

    frames: list[pd.DataFrame] = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            collected: list[list[list[str | None]]] = []
            for page in pdf.pages[:max_pages]:
                try:
                    tables = page.extract_tables()
                except Exception:
                    continue
                for table in tables or []:
                    if table and len(table) >= 2:
                        collected.append(table)

            # Tables that continue across pages share a column count; stitch
            # them so a multi-page WARN report parses as one list.
            by_width: dict[int, list[list[str | None]]] = {}
            for table in collected:
                width = max(len(r) for r in table)
                by_width.setdefault(width, []).extend(table)
            for width, rows in by_width.items():
                padded = [list(r) + [None] * (width - len(r)) for r in rows]
                frames.append(pd.DataFrame(padded, dtype=object))
    except Exception as exc:
        log.debug("pdf parse failed: %s", exc)
    return frames


def read_tabular(content: bytes, kind: str) -> list[dict[str, Any]]:
    """Parse a tabular payload into raw row dicts.

    When a workbook has several sheets (or a PDF several tables), the one that
    best matches the WARN schema wins, and sheets with an identical column
    signature are concatenated -- that is how the multi-year workbooks and
    multi-page PDF reports come through complete.
    """
    if kind == "csv":
        frames = _read_csv(content)
    elif kind in ("xlsx", "xls"):
        frames = _read_excel(content, kind)
    elif kind == "pdf":
        frames = _read_pdf(content)
    else:
        return []

    candidates: list[tuple[float, list[dict[str, Any]], tuple[str, ...]]] = []
    for frame in frames:
        promoted = _promote_header(frame)
        if promoted.empty:
            continue
        mapped = set(map_headers(promoted.columns).values())
        if "employer" not in mapped:
            continue
        rows = rows_from_dataframe(promoted)
        if not rows:
            continue
        signature = tuple(sorted(normalize_header(c) for c in promoted.columns))
        score = len(mapped) * 100 + len(rows)
        candidates.append((score, rows, signature))

    if not candidates:
        return []

    candidates.sort(key=lambda t: t[0], reverse=True)
    best_signature = candidates[0][2]
    merged: list[dict[str, Any]] = []
    for _, rows, signature in candidates:
        if signature == best_signature:
            merged.extend(rows)
    return merged
