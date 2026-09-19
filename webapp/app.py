"""The public-facing website: search the WARN database, browse stats, and
subscribe to email alerts.

Run with:
    python webapp/app.py
and open http://localhost:5000

This is a normal Flask app -- it reads the same SQLite database the scraper
writes to (`data/warn.db`), so run `python -m warn scrape` at least once
before starting the site.
"""

from __future__ import annotations

import csv
import io
import os
import sys
from datetime import date
from pathlib import Path

# Let `python webapp/app.py` work without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, Response, abort, jsonify, render_template, request

from warn import db, notify
from warn.normalize import STATE_NAMES

app = Flask(__name__)
PER_PAGE = 25


# --------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------

@app.template_filter("commas")
def commas(value):
    """1234 -> "1,234"; None -> an em dash."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value


@app.context_processor
def inject_globals():
    return {"state_names": STATE_NAMES}


def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _read_filters() -> dict:
    """The search filters shared by the search page, the JSON API, CSV export,
    and pagination links -- one place so they can't drift apart."""
    return {
        "query": request.args.get("q", "").strip() or None,
        "state": request.args.get("state", "").strip() or None,
        "city": request.args.get("city", "").strip() or None,
        "min_employees": _parse_int(request.args.get("min_employees")),
        "since": _parse_date(request.args.get("since")),
        "active_only": request.args.get("active_only") == "1",
    }


def _notice_to_json(n: db.Notice) -> dict:
    """A notice as JSON for the search API -- dates as ISO strings, everything
    else as-is."""
    return {
        "employer": n.employer,
        "city": n.city,
        "county": n.county,
        "state": n.state,
        "notice_date": n.notice_date.isoformat() if n.notice_date else None,
        "effective_date": n.effective_date.isoformat() if n.effective_date else None,
        "employees": n.employees,
        "notice_type": n.notice_type,
        "industry": n.industry,
        "source_url": n.source_url,
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
#
# The search page is progressively enhanced: `index()` renders a normal HTML
# <form> that GETs this same route and works with JavaScript off. When JS is
# on, static/app.js intercepts that form and talks to `/api/search` instead,
# swapping the results table in place with no page reload. Both routes run
# the exact same `db.search_notices` query with the exact same query-string
# parameter names, so the two paths can't drift out of sync.

@app.route("/")
def index():
    db.init_db()
    filters = _read_filters()
    order = request.args.get("order", "notice_date_desc")
    page = max(1, _parse_int(request.args.get("page")) or 1)
    has_filters = any(v not in (None, False) for v in filters.values())

    with db.session_scope() as session:
        rows, total = db.search_notices(
            session, **filters, page=page, per_page=PER_PAGE, order=order
        )
        totals = db.summary(session)
        states = db.distinct_states(session)

    total_pages = max(1, -(-total // PER_PAGE))

    return render_template(
        "index.html",
        rows=rows, total=total, totals=totals, states=states,
        q=filters["query"] or "", state=filters["state"] or "",
        city=filters["city"] or "",
        min_employees=filters["min_employees"] or "",
        since=request.args.get("since", ""), active_only=filters["active_only"],
        order=order, page=page, total_pages=total_pages, has_filters=has_filters,
        per_page=PER_PAGE,
    )


@app.route("/api/search")
def api_search():
    """JSON search endpoint consumed by static/app.js.

    Same filters, same pagination, same ordering as `index()` -- this is
    what lets the JS front end and the no-JS fallback show identical results.
    """
    db.init_db()
    filters = _read_filters()
    order = request.args.get("order", "notice_date_desc")
    page = max(1, _parse_int(request.args.get("page")) or 1)

    with db.session_scope() as session:
        rows, total = db.search_notices(
            session, **filters, page=page, per_page=PER_PAGE, order=order
        )

    return jsonify({
        "results": [_notice_to_json(n) for n in rows],
        "total": total,
        "page": page,
        "per_page": PER_PAGE,
        "total_pages": max(1, -(-total // PER_PAGE)),
    })


@app.route("/export.csv")
def export_csv():
    db.init_db()
    filters = _read_filters()

    fieldnames = ["state", "employer", "city", "county", "notice_date",
                  "effective_date", "employees", "notice_type", "industry",
                  "source_url"]
    with db.session_scope() as session:
        rows, _ = db.search_notices(
            session, **filters, page=1, per_page=5000, order="notice_date_desc"
        )
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({name: getattr(r, name) for name in fieldnames})

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=warn_search_results.csv"},
    )


@app.route("/stats")
def stats():
    db.init_db()
    with db.session_scope() as session:
        totals = db.summary(session)
        by_state = db.per_state_counts(session)
    max_count = max((count for _, count, _ in by_state), default=1)
    return render_template(
        "stats.html", totals=totals, by_state=by_state, max_count=max_count
    )


@app.route("/subscribe", methods=["GET", "POST"])
def subscribe():
    db.init_db()
    if request.method == "GET":
        with db.session_scope() as session:
            states = db.distinct_states(session)
        return render_template("subscribe.html", states=states)

    email = request.form.get("email", "").strip()
    if not email or "@" not in email:
        with db.session_scope() as session:
            states = db.distinct_states(session)
        return render_template(
            "subscribe.html", states=states,
            error="Enter a valid email address.",
        ), 400

    selected_states = request.form.getlist("states")
    keywords = [k.strip() for k in request.form.get("keywords", "").split(",") if k.strip()]
    min_employees = _parse_int(request.form.get("min_employees"))

    with db.session_scope() as session:
        sub = db.create_subscription(
            session, email=email, states=selected_states,
            keywords=keywords, min_employees=min_employees,
        )
        session.flush()
        token = sub.unsubscribe_token

    # Sent after the subscription is committed (session_scope's session is
    # expire_on_commit=False, so `sub`'s already-loaded columns stay usable
    # here) so a slow or failing mail server never blocks signup itself --
    # the subscription exists either way; email is best-effort.
    try:
        delivered = notify.send_welcome_email(sub)
    except Exception:
        app.logger.exception("failed to send welcome email to %s", email)
        delivered = False

    return render_template(
        "subscribed.html", email=email, token=token, delivered=delivered
    )


@app.route("/unsubscribe/<token>")
def unsubscribe(token: str):
    db.init_db()
    with db.session_scope() as session:
        sub = db.find_subscription_by_token(session, token)
        if sub is None:
            abort(404)
        sub.is_active = False
        email = sub.email
    return render_template("unsubscribed.html", email=email)


@app.route("/about")
def about():
    db.init_db()
    with db.session_scope() as session:
        totals = db.summary(session)
    return render_template("about.html", totals=totals)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
