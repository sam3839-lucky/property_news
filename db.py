"""PostgreSQL database for dedup, staging, and structure monitoring."""
import os
import hashlib
import re
import time
import logging
from pathlib import Path

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# ── PG 连接配置 ──
DB_CONFIG = dict(
    dbname=os.environ.get('PG_DATABASE', 'property_clawer'),
    user=os.environ.get('PG_USER', 'property_clawer'),
    host=os.environ.get('PG_HOST', 'localhost'),
    port=os.environ.get('PG_PORT', '15432'),
    password=os.environ.get('PG_PASSWORD', ''),
    connect_timeout=5,
    keepalives=1,
    keepalives_idle=60,
    keepalives_interval=10,
    keepalives_count=3,
)

# ── 辅助函数 ──

def _normalize_title(title: str) -> str:
    """Normalize Chinese titles for dedup comparison."""
    if not title:
        return ""
    t = title.strip()
    t = t.replace("　", " ")
    t = t.replace("，", ",").replace("、", ",")
    t = t.replace("（", "(").replace("）", ")")
    t = t.replace("【", "[").replace("】", "]")
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


def url_hash(url: str) -> str:
    return hashlib.md5(_normalize_url(url).encode("utf-8")).hexdigest()


def _normalize_date(date_str: str):
    """各种日期格式 → YYYY-MM-DD 或 None。移植自 feishu_writer.py"""
    if not date_str:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _extract_filename(path):
    """从全路径提取文件名"""
    if not path:
        return None
    return Path(path).name


# ── PG 连接 ──

def get_db():
    """获取 PostgreSQL 连接（调用方负责关闭）。autocommit=True，每条语句自动提交。"""
    for attempt in (1, 2):
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError as e:
            if attempt == 1:
                logger.warning(f"PG 连接失败，5s 后重试: {e}")
                time.sleep(5)
            else:
                raise


def init_db():
    """建表（幂等）。"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS curation_materials (
                id              SERIAL PRIMARY KEY,
                url             TEXT NOT NULL,
                url_hash        TEXT NOT NULL UNIQUE,
                title           TEXT NOT NULL,
                source_type     TEXT NOT NULL DEFAULT 'gov_article',
                site            TEXT NOT NULL,
                site_name       TEXT,
                section         TEXT NOT NULL,
                tags            TEXT,
                date_published  DATE,
                body_text       TEXT,
                is_pdf          BOOLEAN DEFAULT FALSE,
                pdf_path        TEXT,
                screenshot_full TEXT,
                screenshot_body TEXT,
                curation_status TEXT NOT NULL DEFAULT '待定'
                                CHECK (curation_status IN ('待定', '入选', '放弃')),
                generated_at    TIMESTAMPTZ,
                creative_material TEXT,
                script          TEXT,
                ai_fallback     BOOLEAN DEFAULT FALSE,
                first_seen_at   TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS structure_baseline (
                id              SERIAL PRIMARY KEY,
                site            TEXT NOT NULL,
                section         TEXT NOT NULL,
                run_date        DATE NOT NULL DEFAULT CURRENT_DATE,
                item_count      INTEGER NOT NULL,
                page_text_hash  TEXT,
                dom_element_count INTEGER,
                UNIQUE(site, section, run_date)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crawl_run_log (
                id              SERIAL PRIMARY KEY,
                run_at          TIMESTAMPTZ DEFAULT NOW(),
                site            TEXT NOT NULL,
                section         TEXT NOT NULL,
                status          TEXT NOT NULL,
                items_found     INTEGER DEFAULT 0,
                items_new       INTEGER DEFAULT 0,
                error           TEXT,
                duration_ms     INTEGER
            )
        """)
    finally:
        conn.close()


# ── 去重 ──

def is_url_seen(conn, url: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM curation_materials WHERE url_hash=%s", (url_hash(url),))
    return cur.fetchone() is not None


def mark_url_seen(conn, url: str, title: str, site: str, section: str):
    """插入最小记录，标记 URL 已见。后续 stage_record 补充完整字段。"""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO curation_materials (url, url_hash, title, site, section, date_published, curation_status)"
        " VALUES (%s, %s, %s, %s, %s, NULL, '待定')"
        " ON CONFLICT (url_hash) DO NOTHING",
        (url, url_hash(url), title, site, section),
    )


# site_name 映射
_SITE_NAMES = {
    'zjj': '深圳住建局',
    'pnr': '深圳规自局',
}


# ── 完整记录写入 ──

def stage_record(conn, **kwargs) -> bool:
    """写入/更新完整记录。mark_url_seen 已插入最小行，这里 UPDATE 补充。"""
    cur = conn.cursor()
    uh = url_hash(kwargs["url"])
    date_val = _normalize_date(kwargs.get("date_published"))

    cur.execute(
        """UPDATE curation_materials SET
            body_text=%s, is_pdf=%s, pdf_path=%s,
            screenshot_full=%s, screenshot_body=%s,
            ai_fallback=%s, tags=%s, date_published=%s,
            source_type='gov_article', site_name=%s, updated_at=NOW()
           WHERE url_hash=%s""",
        (
            kwargs.get("body_text"),
            bool(kwargs.get("is_pdf", False)),
            kwargs.get("pdf_path"),
            _extract_filename(kwargs.get("screenshot_full_path")),
            _extract_filename(kwargs.get("screenshot_body_path")),
            bool(kwargs.get("ai_fallback", False)),
            kwargs.get("tags"),
            date_val,
            _SITE_NAMES.get(kwargs.get("site", ""), kwargs.get("site", "")),
            uh,
        ),
    )
    return cur.rowcount > 0


# ── 结构变化检测 ──

def get_baseline_stats(conn, site: str, section: str, days: int = 30):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT item_count, page_text_hash, dom_element_count
           FROM structure_baseline
           WHERE site=%s AND section=%s AND run_date >= CURRENT_DATE - %s
           ORDER BY run_date DESC""",
        (site, section, days),
    )
    return cur.fetchall()


def update_baseline(conn, site: str, section: str,
                    item_count: int, page_text_hash: str, dom_element_count: int):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO structure_baseline (site, section, run_date, item_count, page_text_hash, dom_element_count)
           VALUES (%s, %s, CURRENT_DATE, %s, %s, %s)
           ON CONFLICT (site, section, run_date) DO UPDATE SET
             item_count=EXCLUDED.item_count,
             page_text_hash=EXCLUDED.page_text_hash,
             dom_element_count=EXCLUDED.dom_element_count""",
        (site, section, item_count, page_text_hash, dom_element_count),
    )


# ── 爬虫运行日志 ──

def log_run(conn, site: str, section: str, status: str,
            items_found: int = 0, items_new: int = 0,
            error: str = None, duration_ms: int = None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO crawl_run_log (site, section, status, items_found, items_new, error, duration_ms)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (site, section, status, items_found, items_new, error, duration_ms),
    )


# ── 清理 ──

def cleanup_old(conn, days: int = 30):
    """Remove old baseline data and run logs."""
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM structure_baseline WHERE run_date < CURRENT_DATE - %s",
        (days,),
    )
    # INTERVAL 不接受 psycopg2 的 %s 占位符（会生成无效 SQL ''N' days'），
    # 这里用 f-string 但 days 参数已通过 int() 强制类型转换，不存在注入风险。
    cur.execute(
        f"DELETE FROM crawl_run_log WHERE run_at < NOW() - INTERVAL '{int(days)} days'"
    )


# ── 以下函数保留签名（兼容旧代码），但不再使用 ──

def write_heartbeat(conn, found: int, new: int, errors: int, sites: str):
    """心跳通知改为飞书 IM（见 feishu_writer.send_notification）。此处 no-op。"""
    pass


def has_heartbeat_today(conn) -> bool:
    """PG 模式下不跟踪心跳文件。"""
    return True


def get_pending_records(conn, limit: int = 50):
    """不再有 staging 暂存表。"""
    return []


def mark_synced(conn, url: str):
    """不再有 sync_status。"""
    pass


def mark_sync_failed(conn, url: str, error: str):
    """不再有 sync_status。"""
    pass
