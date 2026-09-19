# WARN Tracker

Watches every U.S. state for layoff announcements, collects them into one
database, and lets people search them or get emailed when a new one matches
what they care about.

## What is a WARN notice?

By federal law, a company planning a mass layoff or plant closing has to give
60 days' advance notice. That notice gets filed with the state. States are
supposed to publish these filings so workers, job seekers, and local
governments can see them coming but there's no single place to look. Each
of the 50 states posts them on its own website, in its own format.

## What this project does

1. **Scrapes** all 50 states' official labor department sites and pulls every
   WARN notice they publish company name, location, number of workers,
   dates, industry.
2. **Cleans and stores** it all in one database, in one consistent format,
   even though every state names its columns differently and uses different
   file types (web pages, Excel files, PDFs, etc.).
3. **A website** where anyone can search that database by state, company
   name, or industry, and see recent layoffs and running totals.
4. **Email alerts** set up a profile (e.g. "notify me about layoffs in
   Maryland over 100 people") and get emailed when a new matching notice
   shows up.

Right now it's collecting from **41 states**, with **~15,000 notices**
covering **~1.8 million workers**, back to 1998.

## Try it in three commands

```bash
pip install -r requirements.txt
python -m warn scrape          # collect the data (takes a few minutes)
python webapp/app.py           # start the website, then open http://localhost:5000
```

## Running it

### 1. Install

```bash
pip install -r requirements.txt
```

Requires Python 3.10+. `curl_cffi` and `pdfplumber` are optional but
recommended — several states sit behind bot filters or publish PDF-only.

### 2. Collect the data

```bash
python -m warn scrape              # every state, into data/warn.db
python -m warn scrape --states MD,VA,CA   # just a few, for a quick test
python -m warn stats               # see what's in the database now
```

### 3. Run the website

```bash
python webapp/app.py
```

Open **http://localhost:5000**. Search by company/industry keyword, state,
city, or minimum workers affected; browse a stats page; sign up for email
alerts.

### 4. Send alerts

```bash
python -m warn subscribe you@example.com --states MD,VA --keywords "retail"
python -m warn notify
```

Without email server settings configured, alerts are written to
`logs/alerts_sent.log` instead of actually emailed, so this is runnable and
testable out of the box. To send real email, set:

```bash
export WARN_SMTP_HOST=smtp.yourprovider.com
export WARN_SMTP_USER=you@example.com
export WARN_SMTP_PASS=your-app-password
```

In production you'd run `scrape` then `notify` on a schedule (cron, Task
Scheduler, GitHub Actions) every 15–60 minutes — subscribers hear about a new
layoff within one cycle of it appearing on the state's site.

## Current coverage

| Metric | Value |
|---|---|
| States with a working scraper | **41 of 48** that publish data |
| WARN notices in the database | **15,254** |
| Workers affected | **1,799,943** |
| Date range | July 1998 – present |

Run `python -m warn doctor` for live numbers; `exports/doctor.json` holds the
last machine-readable health report.

## Commands

```bash
python -m warn scrape --states MD,VA,CA    # scrape specific states
python -m warn doctor                      # health check across all sources
python -m warn inspect CO                  # debug one state's column mapping
python -m warn export --format csv --since 2024-01-01
python -m warn stats
python -m warn subscribe you@example.com --states MD,VA --keywords "retail,airlines" --min-employees 50
python -m warn notify --lookback-hours 72
```

## Testing

```bash
python -m pytest tests/ -q
```

## Project layout

```
warn/
├── registry.py     # all 50 state sources
├── schema.py       # canonical fields + header matching
├── normalize.py    # date/number/text parsing
├── source.py       # Source spec + generic strategies
├── http.py         # retries, caching, rate limiting, bot-filter fallback
├── db.py           # SQLAlchemy models + upsert + search + subscriptions
├── notify.py       # matches subscriptions against new notices, sends alerts
├── runner.py       # concurrent orchestration
├── cli.py          # command line interface
├── parsers/        # tables, files, links, records
└── states/         # custom handlers for awkward states

webapp/
├── app.py          # Flask routes: page views + the /api/search JSON endpoint
├── templates/      # Jinja2 pages (server-rendered, JS-off fallback)
└── static/
    ├── style.css   # all site styling, no external dependencies
    └── app.js      # search-as-you-type, AJAX pagination, no page reloads
```
