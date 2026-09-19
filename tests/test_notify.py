"""Tests for the alert/notification system: matching logic and email content.

No real network calls here -- SMTP isn't configured in tests, so `_send_email`
always takes the local-log fallback path, which is exactly what we want to
assert against without needing a mail server.
"""

from datetime import date

import pytest

from warn import config
from warn.db import Notice, Subscription
from warn.notify import _format_welcome_email, _send_email, matches, send_welcome_email


def make_sub(states="MD,VA", keywords="retail", min_employees=50, **kwargs):
    defaults = dict(
        id=1, email="test@example.com", states=states, keywords=keywords,
        min_employees=min_employees, unsubscribe_token="tok-abc123",
    )
    defaults.update(kwargs)
    return Subscription(**defaults)


def make_notice(**kwargs):
    defaults = dict(state="MD", employer="Acme Corp")
    defaults.update(kwargs)
    return Notice(**defaults)


class TestMatches:
    def test_state_filter(self):
        sub = make_sub(states="MD", keywords=None, min_employees=None)
        assert matches(make_notice(state="MD"), sub)
        assert not matches(make_notice(state="VA"), sub)

    def test_no_states_matches_any_state(self):
        sub = make_sub(states=None, keywords=None, min_employees=None)
        assert matches(make_notice(state="ZZ"), sub)

    def test_min_employees_filter(self):
        sub = make_sub(states=None, keywords=None, min_employees=100)
        assert not matches(make_notice(employees=50), sub)
        assert matches(make_notice(employees=150), sub)

    def test_min_employees_excludes_unknown_headcount(self):
        """A notice with no headcount can't be known to clear the bar, so it
        shouldn't match a minimum-size filter -- silently matching would be
        worse than a subscriber missing a real match."""
        sub = make_sub(states=None, keywords=None, min_employees=1)
        assert not matches(make_notice(employees=None), sub)

    def test_keyword_matches_employer_or_industry(self):
        sub = make_sub(states=None, keywords="retail", min_employees=None)
        assert matches(make_notice(employer="Acme Retail Co"), sub)
        assert matches(make_notice(employer="Acme", industry="Retail Trade"), sub)
        assert not matches(make_notice(employer="Acme Manufacturing"), sub)

    def test_all_filters_must_pass(self):
        sub = make_sub(states="MD", keywords="retail", min_employees=100)
        assert matches(
            make_notice(state="MD", employer="Retail Co", employees=150), sub
        )
        assert not matches(  # wrong state
            make_notice(state="VA", employer="Retail Co", employees=150), sub
        )
        assert not matches(  # too small
            make_notice(state="MD", employer="Retail Co", employees=10), sub
        )


class TestWelcomeEmail:
    def test_mentions_every_filter_signed_up_for(self):
        sub = make_sub(states="MD,VA", keywords="retail,airlines", min_employees=50)
        subject, body = _format_welcome_email(sub)
        assert "MD" in body and "VA" in body
        assert "retail" in body and "airlines" in body
        assert "50" in body
        assert "welcome" in subject.lower()

    def test_unset_filters_read_as_any_not_blank(self):
        sub = make_sub(states=None, keywords=None, min_employees=None)
        _, body = _format_welcome_email(sub)
        assert "all states" in body.lower()
        assert "any" in body.lower()

    def test_includes_a_working_unsubscribe_link(self):
        sub = make_sub(unsubscribe_token="xyz789")
        _, body = _format_welcome_email(sub)
        assert "xyz789" in body

    def test_includes_the_site_link(self):
        sub = make_sub()
        _, body = _format_welcome_email(sub)
        assert "http" in body  # the browse/search link


class TestSendEmailFallback:
    """Without WARN_SMTP_HOST set, every send is written to a local log file
    instead -- this is what makes the whole alert pipeline runnable and
    testable with zero mail-server setup."""

    def test_writes_to_log_and_reports_not_delivered(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WARN_SMTP_HOST", raising=False)
        monkeypatch.setattr(config, "LOG_DIR", tmp_path)

        delivered = _send_email("test@example.com", "A subject", "A body line")

        assert delivered is False
        log_text = (tmp_path / "alerts_sent.log").read_text(encoding="utf-8")
        assert "A subject" in log_text
        assert "A body line" in log_text
        assert "test@example.com" in log_text

    def test_welcome_email_uses_the_same_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WARN_SMTP_HOST", raising=False)
        monkeypatch.setattr(config, "LOG_DIR", tmp_path)

        delivered = send_welcome_email(make_sub())

        assert delivered is False
        assert (tmp_path / "alerts_sent.log").exists()

    def test_a_real_smtp_failure_still_logs_a_copy_before_raising(
        self, tmp_path, monkeypatch
    ):
        """SMTP *is* configured here, but the connection itself fails. That
        must not silently lose the message -- it should be written to the
        log the same as the not-configured case, and still raise so
        run_notify's retry-on-failure logic keeps working."""
        import smtplib

        monkeypatch.setenv("WARN_SMTP_HOST", "smtp.invalid.example")
        monkeypatch.setattr(config, "LOG_DIR", tmp_path)

        def _boom(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(smtplib, "SMTP", _boom)

        with pytest.raises(OSError):
            _send_email("test@example.com", "Configured but broken", "body text")

        log_text = (tmp_path / "alerts_sent.log").read_text(encoding="utf-8")
        assert "Configured but broken" in log_text
