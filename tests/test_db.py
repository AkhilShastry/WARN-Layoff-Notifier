"""Tests for storage and change detection.

The `first_seen_at` behaviour is what a notification system depends on, so it
gets the most attention here.
"""

from datetime import date

import pytest

from warn import db
from warn.schema import WarnNotice


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """A throwaway SQLite database per test."""
    monkeypatch.setattr(db, "_engine", None, raising=False)
    monkeypatch.setattr(db, "_SessionFactory", None, raising=False)
    db.init_db(f"sqlite:///{tmp_path / 'test.db'}")
    with db.session_scope() as s:
        yield s


def notice(employer="Acme Corp", employees=100, city="Baltimore", **kwargs):
    return WarnNotice(
        state="MD", state_name="Maryland", employer=employer, city=city,
        notice_date=kwargs.pop("notice_date", date(2024, 1, 15)),
        employees=employees, source_name="test", source_url="http://example.gov",
        **kwargs,
    )


class TestUpsert:
    def test_inserts_new_notices(self, session):
        new, updated, rows = db.upsert_notices(session, [notice()])
        assert (new, updated) == (1, 0)
        assert rows[0].employer == "Acme Corp"
        assert rows[0].employer_key == "acme"

    def test_is_idempotent(self, session):
        db.upsert_notices(session, [notice()])
        new, _, _ = db.upsert_notices(session, [notice()])
        assert new == 0, "re-scraping a state must not duplicate notices"

    def test_deduplicates_within_a_batch(self, session):
        new, _, _ = db.upsert_notices(session, [notice(), notice()])
        assert new == 1

    def test_distinct_notices_both_land(self, session):
        new, _, _ = db.upsert_notices(
            session, [notice(employer="Acme Corp"), notice(employer="Beta LLC")]
        )
        assert new == 2

    def test_backfills_missing_fields(self, session):
        db.upsert_notices(session, [notice(county=None)])
        _, updated, _ = db.upsert_notices(session, [notice(county="Baltimore County")])
        assert updated == 1
        row = session.query(db.Notice).one()
        assert row.county == "Baltimore County"

    def test_first_seen_survives_rescrape(self, session):
        _, _, rows = db.upsert_notices(session, [notice()])
        original = rows[0].first_seen_at
        db.upsert_notices(session, [notice()])
        row = session.query(db.Notice).one()
        assert row.first_seen_at == original, (
            "first_seen_at must be stable -- alerting keys off it"
        )

    def test_headcount_change_is_a_new_record(self, session):
        db.upsert_notices(session, [notice(employees=100)])
        new, _, _ = db.upsert_notices(session, [notice(employees=250)])
        assert new == 1


class TestDeactivate:
    def test_marks_vanished_notices_inactive(self, session):
        db.upsert_notices(session, [notice(employer="Acme"), notice(employer="Beta")])
        keep = db.notice_hash(notice(employer="Acme"))
        count = db.deactivate_missing(session, "MD", [keep])
        assert count == 1
        active = session.query(db.Notice).filter_by(is_active=True).all()
        assert [row.employer for row in active] == ["Acme"]


class TestSummary:
    def test_totals(self, session):
        db.upsert_notices(
            session, [notice(employer="Acme", employees=100),
                      notice(employer="Beta", employees=50)]
        )
        session.flush()
        totals = db.summary(session)
        assert totals["notices"] == 2
        assert totals["workers_affected"] == 150
        assert totals["states"] == 1
