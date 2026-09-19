"""Real-time-ish alerts: email subscribers when a new notice matches their profile.

"Real time" for a scraper means "as fresh as the last scrape" -- there is no
live feed to subscribe to, the states themselves only update daily at best.
So the actual mechanism is:

    scrape (writes first_seen_at on brand-new rows)
        -> notify (finds rows no subscription has been alerted about yet,
                    matches them against each subscriber's filters, emails)

Run both back to back on a schedule (cron / Task Scheduler / GitHub Actions,
every 15-60 minutes) and subscribers hear about a new layoff within one cycle
of it appearing on the state's site -- which is as "real time" as the source
data gets.

Email delivery is via SMTP, configured through environment variables so no
credentials live in code:

    WARN_SMTP_HOST, WARN_SMTP_PORT, WARN_SMTP_USER, WARN_SMTP_PASS,
    WARN_ALERT_FROM, WARN_SITE_URL (used to build unsubscribe links)

Without WARN_SMTP_HOST set, alerts are written to `logs/alerts_sent.log`
instead of emailed -- so `notify` is runnable and testable with zero setup,
and switching to real email later is a config change, not a code change.
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Sequence

from sqlalchemy import select

from . import config, db
from .db import Notice, SentAlert, Subscription, session_scope

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def matches(notice: Notice, sub: Subscription) -> bool:
    """Does this notice satisfy every filter on this subscription?"""
    states = sub.state_list()
    if states and notice.state not in states:
        return False

    if sub.min_employees is not None:
        if notice.employees is None or notice.employees < sub.min_employees:
            return False

    keywords = sub.keyword_list()
    if keywords:
        haystack = " ".join(
            filter(None, [notice.employer, notice.industry, notice.notice_type])
        ).lower()
        if not any(kw in haystack for kw in keywords):
            return False

    return True


@dataclass
class NotifyResult:
    subscriptions_checked: int = 0
    emails_sent: int = 0
    alerts_recorded: int = 0
    errors: list[str] = field(default_factory=list)


def find_new_matches(
    session, *, lookback_hours: float = 72.0
) -> dict[int, list[Notice]]:
    """For every active subscription, the recently-seen notices it hasn't
    been alerted about yet.

    `lookback_hours` bounds the query to recent arrivals -- a subscriber who
    signs up today shouldn't be mailed 15,000 historical notices, only ones
    that showed up going forward. It's generous (3 days) so a missed run
    doesn't drop anything, since `SentAlert` is what actually prevents repeats.
    """
    from datetime import timedelta

    cutoff = db.utcnow() - timedelta(hours=lookback_hours)
    subs = db.active_subscriptions(session)
    if not subs:
        return {}

    recent = list(session.execute(
        select(Notice).where(Notice.first_seen_at >= cutoff)
        .order_by(Notice.first_seen_at.asc())
    ).scalars())
    if not recent:
        return {}

    already_sent = {
        (row.subscription_id, row.notice_id)
        for row in session.execute(select(SentAlert)).scalars()
    }

    matched: dict[int, list[Notice]] = {}
    for sub in subs:
        hits = [
            n for n in recent
            if (sub.id, n.id) not in already_sent and matches(n, sub)
        ]
        if hits:
            matched[sub.id] = hits
    return matched


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------

def _smtp_configured() -> bool:
    return bool(os.environ.get("WARN_SMTP_HOST"))


def _site_url() -> str:
    return os.environ.get("WARN_SITE_URL", "http://localhost:5000").rstrip("/")


def _format_email(sub: Subscription, notices: Sequence[Notice]) -> tuple[str, str]:
    """Returns (subject, plain-text body)."""
    count = len(notices)
    subject = f"WARN Alert: {count} new layoff notice{'s' if count != 1 else ''}"

    lines = [
        f"{count} new WARN notice{'s' if count != 1 else ''} matched your alert:",
        "",
    ]
    for n in notices[:25]:
        pieces = [n.employer]
        if n.city:
            pieces.append(n.city)
        pieces.append(n.state)
        if n.employees:
            pieces.append(f"{n.employees:,} workers")
        if n.notice_date:
            pieces.append(str(n.notice_date))
        lines.append("  - " + " | ".join(pieces))
        if n.source_url:
            lines.append(f"    {n.source_url}")
    if count > 25:
        lines.append(f"  ... and {count - 25} more.")

    unsub = f"{_site_url()}/unsubscribe/{sub.unsubscribe_token}"
    lines += ["", "-" * 40, f"Manage or cancel this alert: {unsub}"]
    return subject, "\n".join(lines)


def _write_local_log(to_addr: str, subject: str, body: str) -> None:
    """The durable fallback record for a message that wasn't (or couldn't be)
    emailed for real."""
    config.ensure_dirs()
    log_path = config.LOG_DIR / "alerts_sent.log"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{'=' * 60}\nTo: {to_addr}\nSubject: {subject}\n\n{body}\n")
    log.info("email for %s written to %s", to_addr, log_path)


def _send_email(to_addr: str, subject: str, body: str) -> bool:
    """Send an email over SMTP, or fall back to the local log.

    Returns True only when it actually went out over SMTP. False covers two
    different situations -- no mail server configured at all (expected in
    local/dev use), or one configured but unreachable or erroring (a real
    failure) -- and *both* write a copy to `logs/alerts_sent.log` so nothing
    is silently lost either way. On a real SMTP failure the exception is
    still re-raised after logging, so callers doing retry logic (see
    `run_notify`) still see the failure and don't mark the message as sent.
    """
    if not _smtp_configured():
        _write_local_log(to_addr, subject, body)
        return False

    host = os.environ["WARN_SMTP_HOST"]
    port = int(os.environ.get("WARN_SMTP_PORT", "587"))
    user = os.environ.get("WARN_SMTP_USER")
    password = os.environ.get("WARN_SMTP_PASS")
    sender = os.environ.get("WARN_ALERT_FROM", user or "alerts@warn-tracker.local")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_addr
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(message)
    except Exception:
        _write_local_log(to_addr, subject, body)
        raise
    return True


def _format_welcome_email(sub: Subscription) -> tuple[str, str]:
    """Returns (subject, plain-text body) for the signup confirmation.

    Sent once, immediately at signup -- separate from `_format_email`, which
    is the recurring "here are new matches" alert. This one exists so a
    subscriber knows right away that signup worked, exactly what they're
    watching for, and roughly when to expect to hear from us again.
    """
    states = sub.state_list()
    keywords = sub.keyword_list()

    scope = [
        "States: " + (", ".join(states) if states else "all states"),
        "Keywords: " + (", ".join(keywords) if keywords else "any company or industry"),
        "Minimum workers affected: " + (
            f"{sub.min_employees:,}" if sub.min_employees is not None else "any size"
        ),
    ]

    unsub = f"{_site_url()}/unsubscribe/{sub.unsubscribe_token}"

    lines = [
        f"Thanks for signing up, {sub.email}.",
        "",
        "You're now watching for new WARN layoff notices matching:",
        "",
        *("  - " + line for line in scope),
        "",
        "When to expect alerts:",
        "  We check official state sources for new filings regularly and email",
        "  you as soon as one matches your filters above -- typically the same",
        "  day it's published. Quiet days mean no matches, not a broken alert;",
        "  you won't get an email unless something matches.",
        "",
        f"Browse or search notices anytime: {_site_url()}/",
        f"Manage or cancel this alert:      {unsub}",
    ]
    return "Welcome to WARN Tracker alerts", "\n".join(lines)


def send_welcome_email(sub: Subscription) -> bool:
    """Send the signup confirmation. Returns True if it went out via SMTP,
    False if it fell back to the local log (see `_send_email`)."""
    subject, body = _format_welcome_email(sub)
    return _send_email(sub.email, subject, body)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run_notify(lookback_hours: float = 72.0) -> NotifyResult:
    """Match subscriptions against recent notices and send/record alerts."""
    db.init_db()
    result = NotifyResult()

    with session_scope() as session:
        matched = find_new_matches(session, lookback_hours=lookback_hours)
        result.subscriptions_checked = len(db.active_subscriptions(session))

        for sub_id, notices in matched.items():
            sub = session.get(Subscription, sub_id)
            if sub is None:
                continue
            subject, body = _format_email(sub, notices)
            try:
                _send_email(sub.email, subject, body)
                result.emails_sent += 1
            except Exception as exc:
                result.errors.append(f"{sub.email}: {exc}")
                log.warning("failed to alert %s: %s", sub.email, exc)
                continue

            sub.last_notified_at = db.utcnow()
            for notice in notices:
                session.add(SentAlert(subscription_id=sub.id, notice_id=notice.id))
                result.alerts_recorded += 1

    return result
