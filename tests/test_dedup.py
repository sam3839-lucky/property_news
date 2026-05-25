"""Test dedup logic — idempotency and cross-column dedup."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import db


def test_dedup_idempotency():
    """Same URL twice should be seen only once."""
    conn = db.get_db()
    db.init_db()

    url = "https://zjj.sz.gov.cn/xxgk/tzgg/content/post_test123.html"
    title = "测试通知公告标题"

    # First insert
    assert not db.is_url_seen(conn, url), "URL should not be seen yet"
    db.mark_url_seen(conn, url, title, "zjj", "通知公告")
    assert db.is_url_seen(conn, url), "URL should be seen now"

    # Second insert (should be no-op)
    db.mark_url_seen(conn, url, title, "zjj", "通知公告")
    assert db.is_url_seen(conn, url), "URL should still be seen"

    # Stage idempotency
    ok1 = db.stage_record(conn, url=url, title=title, site="zjj", section="通知公告")
    ok2 = db.stage_record(conn, url=url, title=title, site="zjj", section="通知公告")
    assert ok1, "First stage should succeed"
    assert not ok2, "Second stage should be dupe"

    conn.close()


def test_url_normalization():
    """URL normalization strips tracking params and normalizes scheme."""
    url = "http://zjj.sz.gov.cn/xxgk/tzgg/content/post_123.html?from=groupmessage"
    expected = "https://zjj.sz.gov.cn/xxgk/tzgg/content/post_123.html"
    assert db._normalize_url(url) == expected


def test_title_normalization():
    """Title normalization handles full-width/half-width spaces and punctuation."""
    original = "深圳市住房和建设局　关于《2026年度行政复议、诉讼服务项目》项目采购的公告"
    normalized = db._normalize_title(original)
    # Full-width space should be normalized
    assert "　" not in normalized
    # 《》(书名号) are NOT normalized — they are semantically significant in Chinese
    # Only (), (), ，，。 are normalized
    assert "　" not in normalized  # full-width space normalized
    assert "（" not in normalized  # full-width paren normalized


def test_stage_and_retrieve():
    """Staged records can be retrieved as pending."""
    conn = db.get_db()
    db.init_db()

    url = "https://zjj.sz.gov.cn/xxgk/tzgg/content/post_stage_test.html"
    db.stage_record(
        conn,
        url=url,
        title="测试暂存记录",
        date_published="2026-05-25",
        site="zjj",
        section="通知公告",
        tags="通知公告",
        body_text="这是测试正文内容。",
    )

    pending = db.get_pending_records(conn)
    found = [r for r in pending if r["url"] == url]
    assert len(found) == 1, f"Expected 1 pending record, got {len(found)}"
    assert found[0]["sync_status"] == "pending"

    conn.close()


def test_heartbeat():
    """Heartbeat write and read."""
    conn = db.get_db()
    db.init_db()

    db.write_heartbeat(conn, found=10, new=5, errors=0, sites="zjj,pnr")
    assert db.has_heartbeat_today(conn), "Heartbeat should exist today"

    conn.close()
