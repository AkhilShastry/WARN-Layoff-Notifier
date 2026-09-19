"""Command line interface.

    python -m warn scrape --states MD,VA,CA
    python -m warn doctor            # health check every state source
    python -m warn inspect MD        # show what a state's page parses into
    python -m warn export --format csv
    python -m warn stats
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date, datetime

from . import config, db
from .registry import SOURCES, get_sources
from .runner import dry_run, scrape_and_store
from .schema import CANONICAL_FIELDS
from .source import ScrapeResult

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _force_utf8_output() -> None:
    """Keep Windows consoles from dying on employer names.

    The default Windows code page is cp1252, so printing a company name with a
    curly apostrophe or an accent raises UnicodeEncodeError mid-scrape.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - redirected stream
                pass


def setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr)
    # These libraries are noisy at DEBUG and drown out our own output.
    for noisy in ("urllib3", "charset_normalizer", "pdfminer", "PIL"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def _state_list(value: str | None) -> list[str] | None:
    if not value or value.lower() == "all":
        return None
    return [part.strip().upper() for part in value.replace(" ", ",").split(",") if part.strip()]


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

_GREEN, _RED, _YELLOW, _DIM, _RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
)


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _paint(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}" if _supports_color() else text


def _print_result(result: ScrapeResult) -> None:
    if result.ok:
        mark = _paint("OK  ", _GREEN)
        detail = f"{result.count:>5} notices  {result.workers:>7,} workers"
    elif result.unavailable:
        mark = _paint("N/A ", _YELLOW)
        detail = result.unavailable[:90]
    else:
        mark = _paint("FAIL", _RED)
        detail = (result.error or "unknown error")[:90]
    print(f"  {mark} {result.state}  {detail}  {_paint(f'{result.duration_s}s', _DIM)}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_scrape(args: argparse.Namespace) -> int:
    sources = get_sources(_state_list(args.states))
    if getattr(args, "details", False):
        for source in sources:
            source.fetch_details = True
        print("Detail fetching enabled: one extra request per notice.")
    print(f"Scraping {len(sources)} state source(s)...\n")

    runner = dry_run if args.dry_run else scrape_and_store
    kwargs = {"workers": args.workers, "on_result": _print_result}
    if not args.dry_run:
        kwargs["deactivate"] = args.deactivate

    summary = runner(sources, **kwargs)

    scrapeable = len(summary.results) - len(summary.unavailable)
    print()
    print(f"  states ok      : {len(summary.ok)}/{scrapeable} scrapeable")
    if summary.unavailable:
        codes = " ".join(sorted(r.state for r in summary.unavailable))
        print(f"  no public data : {len(summary.unavailable)} ({codes})")
    print(f"  notices parsed : {summary.total_rows:,}")
    print(f"  workers        : {summary.total_workers:,}")
    if not args.dry_run:
        print(f"  new notices    : {summary.rows_new:,}")
        print(f"  enriched       : {summary.rows_updated:,}")
        print(f"  database       : {config.DB_URL}")

    if summary.failed:
        print(f"\n  {_paint('Failing sources:', _YELLOW)}")
        for result in sorted(summary.failed, key=lambda r: r.state):
            print(f"    {result.state}  {(result.error or '')[:100]}")
        print(f"\n  Re-check them with: python -m warn inspect <STATE>")

    return 0 if summary.ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """Verify every source still returns parseable data."""
    sources = get_sources(_state_list(args.states))
    print(f"Checking {len(sources)} source(s)...\n")
    summary = dry_run(sources, workers=args.workers, on_result=_print_result)

    healthy = sorted(r.state for r in summary.ok)
    broken = sorted(r.state for r in summary.failed)
    na = sorted(r.state for r in summary.unavailable)
    scrapeable = len(summary.results) - len(na)

    print(f"\n  healthy : {len(healthy)}/{scrapeable} scrapeable  {' '.join(healthy)}")
    if broken:
        print(f"  broken  : {len(broken)}  {_paint(' '.join(broken), _RED)}")
    if na:
        print(f"  no data : {len(na)}  {_paint(' '.join(na), _YELLOW)}"
              f"  (state does not publish WARN notices)")
    print(f"  notices : {summary.total_rows:,}")
    print(f"  workers : {summary.total_workers:,}")

    if args.json:
        payload = [
            {
                "state": r.state, "source": r.source_name, "url": r.url,
                "ok": r.ok, "rows": r.count, "workers": r.workers,
                "duration_s": r.duration_s, "error": r.error,
            }
            for r in sorted(summary.results, key=lambda r: r.state)
        ]
        config.ensure_dirs()
        path = config.EXPORT_DIR / "doctor.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\n  report written to {path}")

    return 0 if not broken else 1


def cmd_inspect(args: argparse.Namespace) -> int:
    """Show exactly what one state's source yields -- the debugging workhorse."""
    from .parsers.records import describe_mapping
    from .registry import BY_STATE
    from .source import run_source

    code = args.state.upper()
    source = BY_STATE.get(code)
    if source is None:
        print(f"No source registered for {code}", file=sys.stderr)
        return 2

    print(f"State     : {code} ({source.state_name})")
    print(f"Source    : {source.name}")
    print(f"URL       : {source.url}")
    print(f"Strategy  : {source.strategy}")
    if source.notes:
        print(f"Notes     : {source.notes}")
    print()

    result = run_source(source)
    if not result.notices:
        print(_paint(f"FAILED: {result.error}", _RED))
        return 1

    print(f"Parsed {result.count} notices ({result.workers:,} workers) "
          f"in {result.duration_s}s\n")

    first = result.notices[0]
    if first.raw:
        print("Column mapping:")
        mapping = describe_mapping(list(first.raw))
        for raw_key in first.raw:
            target = mapping.get(raw_key)
            arrow = f"-> {target}" if target else _paint("-> (unused)", _DIM)
            print(f"  {raw_key[:44]:<46} {arrow}")
        print()

    print(f"First {min(args.limit, result.count)} notices:")
    for notice in result.notices[: args.limit]:
        print(f"  {notice.employer[:44]:<46} "
              f"{str(notice.city or '')[:18]:<20} "
              f"{str(notice.notice_date or ''):<12} "
              f"{str(notice.effective_date or ''):<12} "
              f"{notice.employees if notice.employees is not None else '-':>6}")

    # Coverage tells us whether the mapping is actually working.
    print("\nField coverage:")
    for field_name in ("city", "county", "notice_date", "effective_date",
                       "employees", "notice_type"):
        filled = sum(1 for n in result.notices if getattr(n, field_name) is not None)
        pct = 100 * filled / result.count
        flag = "" if pct > 50 else _paint("  <-- low", _YELLOW)
        print(f"  {field_name:<16} {filled:>5}/{result.count} ({pct:5.1f}%){flag}")

    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    db.init_db()
    config.ensure_dirs()

    with db.session_scope() as session:
        query = select(db.Notice).order_by(
            db.Notice.notice_date.desc().nullslast(), db.Notice.state
        )
        if args.states:
            codes = _state_list(args.states)
            if codes:
                query = query.where(db.Notice.state.in_(codes))
        if args.since:
            query = query.where(db.Notice.notice_date >= args.since)
        if args.active_only:
            query = query.where(db.Notice.is_active.is_(True))
        rows = session.execute(query).scalars().all()

        records = []
        for row in rows:
            record = {f: getattr(row, f, None) for f in CANONICAL_FIELDS}
            record["first_seen_at"] = row.first_seen_at
            records.append(record)

    if not records:
        print("No notices to export. Run `python -m warn scrape` first.")
        return 1

    stamp = datetime.now().strftime("%Y%m%d")
    if args.format == "csv":
        path = config.EXPORT_DIR / f"warn_notices_{stamp}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0]))
            writer.writeheader()
            for record in records:
                writer.writerow({
                    k: (v.isoformat() if isinstance(v, (date, datetime)) else v)
                    for k, v in record.items()
                })
    else:
        path = config.EXPORT_DIR / f"warn_notices_{stamp}.json"
        path.write_text(
            json.dumps(records, indent=2, default=str), encoding="utf-8"
        )

    print(f"Exported {len(records):,} notices to {path}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    db.init_db()
    with db.session_scope() as session:
        totals = db.summary(session)
        by_state = db.per_state_counts(session)

    if not totals["notices"]:
        print("Database is empty. Run `python -m warn scrape` first.")
        return 1

    print("WARN database")
    print(f"  notices          : {totals['notices']:,}")
    print(f"  workers affected : {totals['workers_affected']:,}")
    print(f"  states covered   : {totals['states']}")
    print(f"  notice dates     : {totals['earliest_notice']} to {totals['latest_notice']}")
    print("\n  state   notices    workers")
    for code, count, workers in by_state:
        print(f"    {code:<4} {count:>8,} {workers:>10,}")
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    from .notify import run_notify, _smtp_configured

    if not _smtp_configured():
        print(_paint(
            "WARN_SMTP_HOST is not set -- alerts will be written to "
            "logs/alerts_sent.log instead of emailed.\n", _YELLOW
        ))

    result = run_notify(lookback_hours=args.lookback_hours)
    print(f"Subscriptions checked : {result.subscriptions_checked}")
    print(f"Alerts sent           : {result.emails_sent}")
    print(f"Notices recorded      : {result.alerts_recorded}")
    if result.errors:
        print(_paint(f"Errors ({len(result.errors)}):", _RED))
        for err in result.errors:
            print(f"  {err}")
    return 0 if not result.errors else 1


def cmd_subscribe(args: argparse.Namespace) -> int:
    db.init_db()
    with db.session_scope() as session:
        sub = db.create_subscription(
            session,
            email=args.email,
            states=_state_list(args.states) or [],
            keywords=[k.strip() for k in (args.keywords or "").split(",") if k.strip()],
            min_employees=args.min_employees,
        )
        session.flush()
        print(f"Subscribed {sub.email}")
        print(f"  states        : {sub.states or 'all'}")
        print(f"  keywords      : {sub.keywords or 'any'}")
        print(f"  min employees : {sub.min_employees or 'any'}")
        print(f"  unsubscribe   : {sub.unsubscribe_token}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    print(f"{len(SOURCES)} registered sources\n")
    print(f"  {'ST':<4} {'STRATEGY':<9} {'SOURCE':<42} URL")
    for source in sorted(SOURCES, key=lambda s: s.state):
        print(f"  {source.state:<4} {source.strategy:<9} "
              f"{source.name[:40]:<42} {source.url[:70]}")
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warn",
        description="Aggregate WARN layoff notices from all 50 U.S. states.",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v for info, -vv for debug")
    sub = parser.add_subparsers(dest="command", required=True)

    scrape = sub.add_parser("scrape", help="scrape states and store to SQL")
    scrape.add_argument("--states", "-s", default=None,
                        help="comma-separated USPS codes, or 'all' (default)")
    scrape.add_argument("--workers", "-w", type=int, default=None)
    scrape.add_argument("--dry-run", action="store_true",
                        help="scrape without writing to the database")
    scrape.add_argument("--deactivate", action="store_true",
                        help="mark notices missing from a state's list inactive")
    scrape.add_argument("--details", action="store_true",
                        help="follow each notice's detail page for fields the "
                             "list view omits (headcounts on AZ/DE/KS/ME/VT); "
                             "slow -- one request per notice. Pair with "
                             "--deactivate: adding a headcount changes a "
                             "notice's hash, so the un-enriched row must be "
                             "retired or the state's count doubles")
    scrape.set_defaults(func=cmd_scrape)

    doctor = sub.add_parser("doctor", help="verify every source still parses")
    doctor.add_argument("--states", "-s", default=None)
    doctor.add_argument("--workers", "-w", type=int, default=None)
    doctor.add_argument("--json", action="store_true", help="write exports/doctor.json")
    doctor.set_defaults(func=cmd_doctor)

    inspect = sub.add_parser("inspect", help="debug one state's parsing")
    inspect.add_argument("state")
    inspect.add_argument("--limit", "-n", type=int, default=10)
    inspect.set_defaults(func=cmd_inspect)

    export = sub.add_parser("export", help="dump the database to CSV or JSON")
    export.add_argument("--format", "-f", choices=("csv", "json"), default="csv")
    export.add_argument("--states", "-s", default=None)
    export.add_argument("--since", type=date.fromisoformat, default=None,
                        help="only notices on or after this ISO date")
    export.add_argument("--active-only", action="store_true")
    export.set_defaults(func=cmd_export)

    stats = sub.add_parser("stats", help="summarize what is in the database")
    stats.set_defaults(func=cmd_stats)

    listing = sub.add_parser("list", help="show the source registry")
    listing.set_defaults(func=cmd_list)

    notify = sub.add_parser("notify", help="email subscribers about new notices")
    notify.add_argument("--lookback-hours", type=float, default=72.0,
                        help="only consider notices first seen within this "
                             "window (default 72h)")
    notify.set_defaults(func=cmd_notify)

    subscribe = sub.add_parser("subscribe", help="create an alert profile from the CLI")
    subscribe.add_argument("email")
    subscribe.add_argument("--states", "-s", default=None,
                           help="comma-separated USPS codes; omit for all states")
    subscribe.add_argument("--keywords", "-k", default=None,
                           help="comma-separated terms matched against "
                                "employer/industry")
    subscribe.add_argument("--min-employees", type=int, default=None)
    subscribe.set_defaults(func=cmd_subscribe)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    config.ensure_dirs()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except KeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
