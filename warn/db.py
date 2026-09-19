"""SQL storage.

SQLite by default, Postgres by setting WARN_DB_URL -- the ORM layer means the
notification service and website in later phases can point at the same models.

The important behaviour here is `upsert_notices`: notices are keyed by a content
hash, so re-scraping a state is idempotent, and the first time we ever see a
hash we stamp `first_seen_at`. That timestamp is what a notification system
subscribes to.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    create_engine, func, select,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker,
)

from . import config
from .normalize import normalize_employer, record_hash
from .schema import WarnNotice

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Notice(Base):
    """One WARN filing, normalized."""

    __tablename__ = "notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hash: Mapped[str] = mapped_column(String(40), unique=True, index=True)

    state: Mapped[str] = mapped_column(String(2), index=True)
    state_name: Mapped[str] = mapped_column(String(40), default="")

    employer: Mapped[str] = mapped_column(String(300), index=True)
    employer_key: Mapped[str] = mapped_column(String(300), index=True, default="")

    city: Mapped[str | None] = mapped_column(String(160))
    county: Mapped[str | None] = mapped_column(String(160))
    address: Mapped[str | None] = mapped_column(String(300))
    zip_code: Mapped[str | None] = mapped_column(String(12))
    region: Mapped[str | None] = mapped_column(String(160))

    notice_date: Mapped[date | None] = mapped_column(Date, index=True)
    effective_date: Mapped[date | None] = mapped_column(Date, index=True)
    closing_date: Mapped[date | None] = mapped_column(Date)

    employees: Mapped[int | None] = mapped_column(Integer, index=True)
    notice_type: Mapped[str | None] = mapped_column(String(80))
    industry: Mapped[str | None] = mapped_column(String(200))
    union_name: Mapped[str | None] = mapped_column(String(200))
    contact: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    source_name: Mapped[str] = mapped_column(String(120), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[str | None] = mapped_column(Text)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    # Cleared when a notice disappears from its state's published list.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    __table_args__ = (
        Index("ix_notices_state_notice_date", "state", "notice_date"),
        Index("ix_notices_state_employer", "state", "employer_key"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<Notice {self.state} {self.employer!r} "
                f"{self.notice_date} n={self.employees}>")


class ScrapeRun(Base):
    """One invocation of the scraper, for observability."""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    states_attempted: Mapped[int] = mapped_column(Integer, default=0)
    states_ok: Mapped[int] = mapped_column(Integer, default=0)
    states_failed: Mapped[int] = mapped_column(Integer, default=0)
    rows_parsed: Mapped[int] = mapped_column(Integer, default=0)
    rows_new: Mapped[int] = mapped_column(Integer, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)

    results: Mapped[list["SourceResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class SourceResult(Base):
    """Per-state outcome within a run. This is the health dashboard."""

    __tablename__ = "source_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_runs.id"))
    state: Mapped[str] = mapped_column(String(2), index=True)
    source_name: Mapped[str] = mapped_column(String(120), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    rows_parsed: Mapped[int] = mapped_column(Integer, default=0)
    rows_new: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    run: Mapped[ScrapeRun | None] = relationship(back_populates="results")


class Subscription(Base):
    """A user's alert profile: "email me new notices matching these filters."

    Every filter is optional and they AND together. `states` and `keywords`
    are stored as comma-separated text rather than a child table -- the match
    query is simple substring/membership logic, not worth a join for what is,
    realistically, a handful of filters per subscriber.
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)

    # Comma-separated USPS codes, e.g. "MD,VA,DC". Empty/NULL = all states.
    states: Mapped[str | None] = mapped_column(String(200))
    # Comma-separated free-text terms matched against employer AND industry
    # (case-insensitive substring). Empty/NULL = any employer/industry.
    keywords: Mapped[str | None] = mapped_column(String(500))
    # Only alert on notices at least this large. NULL = any size.
    min_employees: Mapped[int | None] = mapped_column(Integer)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # One-time token mailed in every alert so a recipient can unsubscribe
    # without an account or a login.
    unsubscribe_token: Mapped[str] = mapped_column(String(40), unique=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    def state_list(self) -> list[str]:
        return [s.strip().upper() for s in (self.states or "").split(",") if s.strip()]

    def keyword_list(self) -> list[str]:
        return [k.strip().lower() for k in (self.keywords or "").split(",") if k.strip()]


class SentAlert(Base):
    """One (subscription, notice) pair we've already emailed.

    Prevents a subscriber from being re-notified about the same notice on
    every future `notify` run just because it's still the newest match.
    """

    __tablename__ = "sent_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), index=True
    )
    notice_id: Mapped[int] = mapped_column(ForeignKey("notices.id"), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_sent_alerts_sub_notice", "subscription_id", "notice_id", unique=True),
    )


_engine = None
_SessionFactory = None


def get_engine(url: str | None = None, echo: bool = False):
    global _engine, _SessionFactory
    if _engine is None or url is not None:
        config.ensure_dirs()
        target = url or config.DB_URL
        kwargs: dict[str, Any] = {"echo": echo, "future": True}
        if target.startswith("sqlite"):
            # Let concurrent writers queue instead of erroring immediately.
            kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}
        _engine = create_engine(target, **kwargs)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db(url: str | None = None, echo: bool = False) -> None:
    engine = get_engine(url, echo)
    Base.metadata.create_all(engine)
    if engine.url.get_backend_name() == "sqlite":
        with engine.begin() as conn:
            from sqlalchemy import text as sql_text

            conn.execute(sql_text("PRAGMA journal_mode=WAL"))
            conn.execute(sql_text("PRAGMA synchronous=NORMAL"))


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionFactory is None:
        init_db()
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

# Fields we will backfill onto an existing row if the state later publishes
# a value we did not previously have.
_ENRICHABLE = (
    "city", "county", "address", "zip_code", "region", "closing_date",
    "notice_type", "industry", "union_name", "contact", "notes",
)


def notice_hash(notice: WarnNotice) -> str:
    return record_hash(
        notice.state, notice.employer, notice.city,
        notice.notice_date, notice.effective_date, notice.employees,
    )


def upsert_notices(
    session: Session, notices: Sequence[WarnNotice]
) -> tuple[int, int, list[Notice]]:
    """Insert new notices, refresh ones we have already seen.

    Returns (new_count, updated_count, new_rows).
    """
    if not notices:
        return 0, 0, []

    # Deduplicate within the incoming batch first: states often list the same
    # notice twice (e.g. one row per worksite in a multi-site filing).
    staged: dict[str, WarnNotice] = {}
    for notice in notices:
        staged.setdefault(notice_hash(notice), notice)

    hashes = list(staged)
    existing: dict[str, Notice] = {}
    # SQLite caps host parameters, so chunk the IN clause.
    for i in range(0, len(hashes), 400):
        chunk = hashes[i:i + 400]
        rows = session.execute(
            select(Notice).where(Notice.hash.in_(chunk))
        ).scalars().all()
        existing.update({row.hash: row for row in rows})

    now = utcnow()
    new_rows: list[Notice] = []
    updated = 0

    for digest, notice in staged.items():
        row = existing.get(digest)
        if row is None:
            row = Notice(
                hash=digest,
                state=notice.state,
                state_name=notice.state_name,
                employer=notice.employer,
                employer_key=normalize_employer(notice.employer),
                city=notice.city,
                county=notice.county,
                address=notice.address,
                zip_code=notice.zip_code,
                region=notice.region,
                notice_date=notice.notice_date,
                effective_date=notice.effective_date,
                closing_date=notice.closing_date,
                employees=notice.employees,
                notice_type=notice.notice_type,
                industry=notice.industry,
                union_name=notice.union_name,
                contact=notice.contact,
                notes=notice.notes,
                source_name=notice.source_name,
                source_url=notice.source_url,
                raw=json.dumps(notice.raw, default=str)[:20000] if notice.raw else None,
                first_seen_at=now,
                last_seen_at=now,
                is_active=True,
            )
            session.add(row)
            new_rows.append(row)
        else:
            row.last_seen_at = now
            row.is_active = True
            changed = False
            for field_name in _ENRICHABLE:
                incoming = getattr(notice, field_name)
                if incoming and not getattr(row, field_name):
                    setattr(row, field_name, incoming)
                    changed = True
            if changed:
                updated += 1

    session.flush()
    return len(new_rows), updated, new_rows


def deactivate_missing(session: Session, state: str, seen_hashes: Iterable[str]) -> int:
    """Mark notices that vanished from a state's list as inactive.

    Only called when a state's scrape succeeded, so a transient failure never
    wipes out history.
    """
    seen = set(seen_hashes)
    rows = session.execute(
        select(Notice).where(Notice.state == state, Notice.is_active.is_(True))
    ).scalars().all()
    count = 0
    for row in rows:
        if row.hash not in seen:
            row.is_active = False
            count += 1
    return count


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def summary(session: Session) -> dict[str, Any]:
    total = session.execute(select(func.count(Notice.id))).scalar_one()
    workers = session.execute(select(func.sum(Notice.employees))).scalar() or 0
    states = session.execute(
        select(func.count(func.distinct(Notice.state)))
    ).scalar_one()
    newest = session.execute(select(func.max(Notice.notice_date))).scalar()
    oldest = session.execute(select(func.min(Notice.notice_date))).scalar()
    return {
        "notices": total,
        "workers_affected": int(workers),
        "states": states,
        "earliest_notice": oldest,
        "latest_notice": newest,
    }


def per_state_counts(session: Session) -> list[tuple[str, int, int]]:
    rows = session.execute(
        select(Notice.state, func.count(Notice.id), func.sum(Notice.employees))
        .group_by(Notice.state)
        .order_by(func.count(Notice.id).desc())
    ).all()
    return [(r[0], int(r[1] or 0), int(r[2] or 0)) for r in rows]


def recent_notices(session: Session, limit: int = 12) -> list[Notice]:
    """Newest notices by when we first saw them -- the website's "Latest" feed."""
    return list(session.execute(
        select(Notice).order_by(Notice.first_seen_at.desc()).limit(limit)
    ).scalars())


def distinct_states(session: Session) -> list[str]:
    rows = session.execute(
        select(Notice.state).distinct().order_by(Notice.state)
    ).scalars()
    return list(rows)


def search_notices(
    session: Session,
    *,
    query: str | None = None,
    state: str | None = None,
    city: str | None = None,
    min_employees: int | None = None,
    since: date | None = None,
    active_only: bool = False,
    page: int = 1,
    per_page: int = 25,
    order: str = "notice_date_desc",
) -> tuple[list[Notice], int]:
    """The search the website's search page runs.

    `query` is a single free-text box matched against employer, industry and
    notes -- covers "search by company" and a loose "search by job/role" at
    once, since job/industry text tends to live in the `industry` field.
    Returns (page of rows, total matching count) for pagination.
    """
    from sqlalchemy import or_

    stmt = select(Notice)
    conditions = []

    if query:
        like = f"%{query.strip()}%"
        conditions.append(or_(
            Notice.employer.ilike(like),
            Notice.industry.ilike(like),
            Notice.notes.ilike(like),
        ))
    if state:
        conditions.append(Notice.state == state.upper())
    if city:
        conditions.append(Notice.city.ilike(f"%{city.strip()}%"))
    if min_employees is not None:
        conditions.append(Notice.employees >= min_employees)
    if since is not None:
        conditions.append(Notice.notice_date >= since)
    if active_only:
        conditions.append(Notice.is_active.is_(True))

    for cond in conditions:
        stmt = stmt.where(cond)

    total = session.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    order_map = {
        "notice_date_desc": Notice.notice_date.desc().nullslast(),
        "notice_date_asc": Notice.notice_date.asc().nullsfirst(),
        "employees_desc": Notice.employees.desc().nullslast(),
        "employer_asc": Notice.employer.asc(),
    }
    stmt = stmt.order_by(order_map.get(order, order_map["notice_date_desc"]))

    page = max(1, page)
    stmt = stmt.limit(per_page).offset((page - 1) * per_page)
    rows = list(session.execute(stmt).scalars())
    return rows, int(total)


# --------------------------------------------------------------------------
# Subscriptions (notification profiles)
# --------------------------------------------------------------------------

def create_subscription(
    session: Session,
    *,
    email: str,
    states: list[str] | None = None,
    keywords: list[str] | None = None,
    min_employees: int | None = None,
) -> Subscription:
    import secrets

    sub = Subscription(
        email=email.strip().lower(),
        states=",".join(s.strip().upper() for s in (states or []) if s.strip()) or None,
        keywords=",".join(k.strip() for k in (keywords or []) if k.strip()) or None,
        min_employees=min_employees,
        unsubscribe_token=secrets.token_urlsafe(24),
        is_active=True,
    )
    session.add(sub)
    session.flush()
    return sub


def find_subscription_by_token(session: Session, token: str) -> Subscription | None:
    return session.execute(
        select(Subscription).where(Subscription.unsubscribe_token == token)
    ).scalar_one_or_none()


def active_subscriptions(session: Session) -> list[Subscription]:
    return list(session.execute(
        select(Subscription).where(Subscription.is_active.is_(True))
    ).scalars())
