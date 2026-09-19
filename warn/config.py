"""Project-wide paths and tunables.

Everything here can be overridden with an environment variable so the same code
runs locally against SQLite and in production against Postgres.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load a local `.env` file (SMTP credentials, etc.) if one exists, before
# reading any of the settings below -- this is what lets `python
# webapp/app.py` or `python -m warn notify` pick up secrets automatically
# instead of needing them re-typed into the shell every session. `.env` is
# gitignored; see `.env.example` for the expected keys. Real environment
# variables set in the shell still win if both are present (`override=False`
# is the default), and this is a no-op with nothing printed if the file
# doesn't exist, so it's harmless for anyone who never creates one.
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # pragma: no cover - optional dependency
    pass

DATA_DIR = Path(os.environ.get("WARN_DATA_DIR", PROJECT_ROOT / "data"))
CACHE_DIR = Path(os.environ.get("WARN_CACHE_DIR", PROJECT_ROOT / "cache"))
EXPORT_DIR = Path(os.environ.get("WARN_EXPORT_DIR", PROJECT_ROOT / "exports"))
LOG_DIR = Path(os.environ.get("WARN_LOG_DIR", PROJECT_ROOT / "logs"))

# SQLite by default; set WARN_DB_URL=postgresql+psycopg://... to swap.
DB_URL = os.environ.get("WARN_DB_URL", f"sqlite:///{DATA_DIR / 'warn.db'}")

# --- HTTP behaviour -------------------------------------------------------
# State sites are small government servers. Be polite or they will block us.
REQUEST_TIMEOUT = float(os.environ.get("WARN_TIMEOUT", "45"))
REQUEST_DELAY = float(os.environ.get("WARN_DELAY", "1.0"))  # seconds between hits to one host
MAX_RETRIES = int(os.environ.get("WARN_RETRIES", "3"))
CACHE_TTL_HOURS = float(os.environ.get("WARN_CACHE_TTL", "12"))

USER_AGENT = os.environ.get(
    "WARN_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)

# How many state scrapers run concurrently. Each state is a different host, so
# parallelism here does not hammer any single server.
MAX_WORKERS = int(os.environ.get("WARN_WORKERS", "8"))

# Rows whose parsed year falls outside this window are treated as junk (usually
# a mis-parsed header or a footer row that leaked into the table).
MIN_YEAR = 1988  # the WARN Act itself
MAX_YEAR_OFFSET = 3  # allow notices scheduled a few years out


def ensure_dirs() -> None:
    """Create the runtime directories. Safe to call repeatedly."""
    for d in (DATA_DIR, CACHE_DIR, EXPORT_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
