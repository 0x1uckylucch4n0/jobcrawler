"""
SQLite store to track seen jobs — prevents duplicate alerts.
"""
import sqlite3
import hashlib
import os

DB_FILE = "jobs.db"


def _conn():
    return sqlite3.connect(DB_FILE)


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS seen_jobs (
                job_hash TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                location TEXT,
                url TEXT,
                description TEXT,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate existing DB if description column is missing
        cols = [r[1] for r in c.execute("PRAGMA table_info(seen_jobs)").fetchall()]
        if "description" not in cols:
            c.execute("ALTER TABLE seen_jobs ADD COLUMN description TEXT")


def job_hash(title: str, company: str) -> str:
    key = f"{title.lower().strip()}|{company.lower().strip()}"
    return hashlib.md5(key.encode()).hexdigest()


def purge_old_entries(days: int = 7):
    """Remove jobs older than N days so they can resurface as new postings."""
    with _conn() as c:
        c.execute("DELETE FROM seen_jobs WHERE seen_at < datetime('now', ?)", (f'-{days} days',))


def is_new(title: str, company: str) -> bool:
    h = job_hash(title, company)
    with _conn() as c:
        row = c.execute("SELECT 1 FROM seen_jobs WHERE job_hash=?", (h,)).fetchone()
    return row is None


def mark_seen(title: str, company: str, location: str, url: str, description: str = ""):
    h = job_hash(title, company)
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO seen_jobs (job_hash, title, company, location, url, description) VALUES (?,?,?,?,?,?)",
            (h, title, company, location, url, description)
        )
