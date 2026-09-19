"""Orchestration: run sources concurrently, persist results, report health."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Sequence

from . import config, db
from .db import Notice, ScrapeRun, SourceResult, session_scope, upsert_notices
from .schema import WarnNotice
from .source import ScrapeResult, Source, run_source

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    results: list[ScrapeResult] = field(default_factory=list)
    rows_new: int = 0
    rows_updated: int = 0
    started_at: datetime = field(default_factory=db.utcnow)
    finished_at: datetime | None = None

    @property
    def ok(self) -> list[ScrapeResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[ScrapeResult]:
        """Sources that should work but did not. Excludes states that simply
        do not publish WARN data, which are not a scraper problem."""
        return [r for r in self.results if not r.ok and not r.unavailable]

    @property
    def unavailable(self) -> list[ScrapeResult]:
        return [r for r in self.results if r.unavailable]

    @property
    def total_rows(self) -> int:
        return sum(r.count for r in self.results)

    @property
    def total_workers(self) -> int:
        return sum(r.workers for r in self.results)


def scrape_sources(
    sources: Sequence[Source],
    *,
    workers: int | None = None,
    on_result: Callable[[ScrapeResult], None] | None = None,
) -> list[ScrapeResult]:
    """Run sources in parallel. Each state is a different host, so this is safe."""
    worker_count = max(1, min(workers or config.MAX_WORKERS, len(sources) or 1))
    results: list[ScrapeResult] = []

    if worker_count == 1:
        for source in sources:
            result = run_source(source)
            results.append(result)
            if on_result:
                on_result(result)
        return results

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(run_source, s): s for s in sources}
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - run_source catches
                result = ScrapeResult(
                    state=source.state.upper(), source_name=source.name,
                    url=source.url, error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            if on_result:
                on_result(result)

    results.sort(key=lambda r: r.state)
    return results


def persist(
    results: Sequence[ScrapeResult],
    *,
    deactivate: bool = False,
    record_run: bool = True,
) -> RunSummary:
    """Write scraped notices to the database and record run health."""
    db.init_db()
    summary = RunSummary(results=list(results))

    with session_scope() as session:
        run = None
        if record_run:
            run = ScrapeRun(started_at=summary.started_at)
            session.add(run)
            session.flush()

        for result in results:
            new_count = updated = 0
            if result.notices:
                new_count, updated, _ = upsert_notices(session, result.notices)
                result.rows_new = new_count
                summary.rows_new += new_count
                summary.rows_updated += updated

                if deactivate and result.ok:
                    hashes = [db.notice_hash(n) for n in result.notices]
                    db.deactivate_missing(session, result.state, hashes)

            session.add(
                SourceResult(
                    run_id=run.id if run else None,
                    state=result.state,
                    source_name=result.source_name,
                    url=result.url,
                    ok=result.ok,
                    http_status=result.http_status,
                    rows_parsed=result.count,
                    rows_new=new_count,
                    duration_s=result.duration_s,
                    error=result.error,
                )
            )

        summary.finished_at = db.utcnow()
        if run:
            run.finished_at = summary.finished_at
            run.states_attempted = len(results)
            run.states_ok = len(summary.ok)
            run.states_failed = len(summary.failed)
            run.rows_parsed = summary.total_rows
            run.rows_new = summary.rows_new
            run.rows_updated = summary.rows_updated

    return summary


def scrape_and_store(
    sources: Sequence[Source],
    *,
    workers: int | None = None,
    deactivate: bool = False,
    on_result: Callable[[ScrapeResult], None] | None = None,
) -> RunSummary:
    results = scrape_sources(sources, workers=workers, on_result=on_result)
    return persist(results, deactivate=deactivate)


def dry_run(
    sources: Sequence[Source],
    *,
    workers: int | None = None,
    on_result: Callable[[ScrapeResult], None] | None = None,
) -> RunSummary:
    """Scrape without touching the database."""
    results = scrape_sources(sources, workers=workers, on_result=on_result)
    summary = RunSummary(results=list(results))
    summary.finished_at = db.utcnow()
    return summary
