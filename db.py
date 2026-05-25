"""SQLite database for dedup, local staging, and structure monitoring."""
import sqlite3
import hashlib
import re
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "gov_cache.db"


def _normalize_title(title: str) -> str:
    """Normalize Chinese titles for dedup comparison."""
    if not title:
        return ""
    t = title.strip()
    # Full-width to half-width spaces
    t = t.replace("　", " ")
    # Normalize Chinese punctuation to English
    t = t.replace("，", ",").replace("、", ",")
    t = t.replace("（", "(").replace("）", ")")
    t = t.replace("【", "[").replace("】", "]")
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t)
    return t


def _normalize_url(url: str) -> str:
    """Canonicalize URL for dedup: force https, strip tracking params, strip trailing slash."""
    if not url:
        return ""
    u = url.strip()
    u = re.sub(r"^http://", "https://", u)
    u = re.sub(r"[?&](from|ref|utm_\w+)=[^&]*", "", u)
    u = re.sub(r"[?&]$", "", u)
    u = u.rstrip("/")
    return u


def title_hash(title: str) -> str:
    return hashlib.md5(_normalize_title(title).encode("utf-8")).hexdigest()


def url_hash(url: str) -> str:
    return hashlib.md5(_normalize_url(url).encode("utf-8")).hexdigest()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        -- Track every URL we've seen (primary dedup)
        CREATE TABLE IF NOT EXISTS seen_urls (
            url_hash TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title_hash TEXT,
            title TEXT,
            first_seen TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            site TEXT NOT NULL,
            section TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_seen_urls_site ON seen_urls(site, section);
        CREATE INDEX IF NOT EXISTS idx_seen_urls_first_seen ON seen_urls(first_seen);

        -- Local staging: records waiting to be pushed to Feishu
        CREATE TABLE IF NOT EXISTS staging (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            date_published TEXT,
            site TEXT NOT NULL,
            section TEXT NOT NULL,
            tags TEXT,
            body_text TEXT,
            screenshot_full_path TEXT,
            screenshot_body_path TEXT,
            screenshot_full_url TEXT,
            screenshot_body_url TEXT,
            is_pdf INTEGER NOT NULL DEFAULT 0,
            pdf_path TEXT,
            ai_fallback INTEGER NOT NULL DEFAULT 0,
            sync_status TEXT NOT NULL DEFAULT 'pending',
            -- pending | synced | failed
            sync_attempts INTEGER NOT NULL DEFAULT 0,
            last_sync_error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            synced_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_staging_status ON staging(sync_status);
        CREATE INDEX IF NOT EXISTS idx_staging_site ON staging(site, section);

        -- Structure change detection baseline (30-day rolling)
        CREATE TABLE IF NOT EXISTS structure_baseline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site TEXT NOT NULL,
            section TEXT NOT NULL,
            run_date TEXT NOT NULL DEFAULT (date('now','localtime')),
            item_count INTEGER NOT NULL,
            page_text_hash TEXT,
            dom_element_count INTEGER,
            UNIQUE(site, section, run_date)
        );
        CREATE INDEX IF NOT EXISTS idx_baseline_site ON structure_baseline(site, section, run_date);

        -- Crawl run log
        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            site TEXT NOT NULL,
            section TEXT NOT NULL,
            status TEXT NOT NULL,
            items_found INTEGER DEFAULT 0,
            items_new INTEGER DEFAULT 0,
            error TEXT,
            duration_ms INTEGER
        );

        -- Heartbeat tracking
        CREATE TABLE IF NOT EXISTS heartbeat (
            run_date TEXT PRIMARY KEY,
            run_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            total_items_found INTEGER DEFAULT 0,
            total_items_new INTEGER DEFAULT 0,
            total_errors INTEGER DEFAULT 0,
            sites_checked TEXT
        );
    """)
    conn.commit()
    conn.close()


def is_url_seen(conn: sqlite3.Connection, url: str) -> bool:
    uh = url_hash(url)
    row = conn.execute("SELECT 1 FROM seen_urls WHERE url_hash=?", (uh,)).fetchone()
    return row is not None


def mark_url_seen(conn: sqlite3.Connection, url: str, title: str, site: str, section: str):
    conn.execute(
        "INSERT OR IGNORE INTO seen_urls(url_hash, url, title_hash, title, site, section) VALUES(?,?,?,?,?,?)",
        (url_hash(url), url, title_hash(title), title, site, section),
    )


def stage_record(conn: sqlite3.Connection, **kwargs) -> bool:
    """Insert a record into staging. Returns True if new, False if dupe."""
    try:
        conn.execute(
            """INSERT INTO staging(url_hash, url, title, date_published, site, section, tags,
               body_text, screenshot_full_path, screenshot_body_path,
               screenshot_full_url, screenshot_body_url,
               is_pdf, pdf_path, ai_fallback)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                url_hash(kwargs["url"]),
                kwargs["url"],
                kwargs["title"],
                kwargs.get("date_published"),
                kwargs["site"],
                kwargs["section"],
                kwargs.get("tags"),
                kwargs.get("body_text"),
                kwargs.get("screenshot_full_path"),
                kwargs.get("screenshot_body_path"),
                kwargs.get("screenshot_full_url"),
                kwargs.get("screenshot_body_url"),
                kwargs.get("is_pdf", 0),
                kwargs.get("pdf_path"),
                kwargs.get("ai_fallback", 0),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def get_pending_records(conn: sqlite3.Connection, limit: int = 50):
    return conn.execute(
        "SELECT * FROM staging WHERE sync_status='pending' ORDER BY created_at LIMIT ?",
        (limit,),
    ).fetchall()


def mark_synced(conn: sqlite3.Connection, url: str):
    conn.execute(
        "UPDATE staging SET sync_status='synced', synced_at=datetime('now','localtime') WHERE url_hash=?",
        (url_hash(url),),
    )


def mark_sync_failed(conn: sqlite3.Connection, url: str, error: str):
    conn.execute(
        """UPDATE staging SET sync_status='failed', sync_attempts=sync_attempts+1,
           last_sync_error=? WHERE url_hash=?""",
        (error[:500], url_hash(url)),
    )


def get_baseline_stats(conn: sqlite3.Connection, site: str, section: str, days: int = 30):
    return conn.execute(
        """SELECT item_count, page_text_hash, dom_element_count
           FROM structure_baseline
           WHERE site=? AND section=? AND run_date >= date('now','localtime',?)
           ORDER BY run_date DESC""",
        (site, section, f"-{days} days"),
    ).fetchall()


def update_baseline(conn: sqlite3.Connection, site: str, section: str,
                    item_count: int, page_text_hash: str, dom_element_count: int):
    conn.execute(
        """INSERT OR REPLACE INTO structure_baseline(site, section, run_date, item_count, page_text_hash, dom_element_count)
           VALUES(?,?,date('now','localtime'),?,?,?)""",
        (site, section, item_count, page_text_hash, dom_element_count),
    )


def log_run(conn: sqlite3.Connection, site: str, section: str, status: str,
            items_found: int = 0, items_new: int = 0, error: str = None, duration_ms: int = None):
    conn.execute(
        "INSERT INTO run_log(site, section, status, items_found, items_new, error, duration_ms) VALUES(?,?,?,?,?,?,?)",
        (site, section, status, items_found, items_new, error, duration_ms),
    )


def write_heartbeat(conn: sqlite3.Connection, found: int, new: int, errors: int, sites: str):
    conn.execute(
        "INSERT OR REPLACE INTO heartbeat(run_date, run_time, total_items_found, total_items_new, total_errors, sites_checked) VALUES(date('now','localtime'),datetime('now','localtime'),?,?,?,?)",
        (found, new, errors, sites),
    )


def has_heartbeat_today(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM heartbeat WHERE run_date=date('now','localtime')"
    ).fetchone()
    return row is not None


def cleanup_old(conn: sqlite3.Connection, days: int = 30):
    """Remove baseline data and run logs older than N days."""
    conn.execute(
        "DELETE FROM structure_baseline WHERE run_date < date('now','localtime',?)",
        (f"-{days} days",),
    )
    conn.execute(
        "DELETE FROM run_log WHERE run_time < datetime('now','localtime',?)",
        (f"-{days} days",),
    )
    conn.commit()
