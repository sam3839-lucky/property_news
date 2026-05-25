"""End-to-end test: crawl → stage → build record → notification."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
from feishu_writer import build_record, _normalize_date, _map_section_tag, _validate_field_value
from gov_crawler import _extract_articles_from_list, _extract_article_body, _detect_pdf_links

FIXTURES = Path(__file__).parent / "fixtures"
ZJJ_HOME = FIXTURES / "zjj_homepage.html"


def test_full_extract_flow():
    """Full extraction flow from fixture HTML to staged records."""
    conn = db.get_db()
    db.init_db()

    html = ZJJ_HOME.read_text()
    section = {
        "name": "通知公告",
        "article_pattern": "/xxgk/tzgg/content/post_{id}.html",
        "tags": ["通知公告"],
    }

    articles = _extract_articles_from_list(html, "https://zjj.sz.gov.cn", section)
    assert len(articles) >= 5

    staged = 0
    for art in articles[:3]:  # test first 3
        if db.is_url_seen(conn, art["url"]):
            continue

        db.mark_url_seen(conn, art["url"], art["title"], "zjj", "通知公告")
        ok = db.stage_record(
            conn,
            url=art["url"],
            title=art["title"],
            date_published=art.get("date_published"),
            site="zjj",
            section="通知公告",
            tags="通知公告",
            body_text="测试正文。",
            screenshot_full_path=None,
            screenshot_body_path=None,
            is_pdf=0,
            pdf_path=None,
            ai_fallback=0,
        )
        if ok:
            staged += 1

    assert staged > 0, f"Should have staged at least 1 record, got {staged}"

    # Verify pending records
    pending = db.get_pending_records(conn)
    assert len(pending) >= staged

    conn.close()


def test_build_record():
    """build_record maps staging columns to Base field names."""
    row = {
        "url": "https://zjj.sz.gov.cn/xxgk/tzgg/content/post_123.html",
        "title": "测试公告标题",
        "body_text": "这是公告正文内容，用于验证摘要截取。",
        "section": "通知公告",
        "tags": "通知公告",
        "date_published": "2026-05-20",
        "screenshot_full_url": "https://feishu.cn/file/abc123",
        "screenshot_body_url": "https://feishu.cn/file/def456",
        "ai_fallback": 0,
        "is_pdf": 0,
        "pdf_path": None,
    }

    record = build_record(row)

    assert record["标题"] == "测试公告标题"
    assert record["原文链接"] == row["url"]
    assert "公告正文内容" in record["正文全文"]
    assert len(record["正文摘要"]) <= 200
    assert record["来源栏目"] == "通知公告"
    assert record["选题决策"] == "待定"
    assert record["文案状态"] == "待生成"
    assert record["AI提取标记"] == "正常"
    assert record["PDF标记"] == "否"
    assert record["全页截图"] == row["screenshot_full_url"]


def test_date_normalization():
    assert _normalize_date("2026-05-20") == "2026-05-20"
    assert _normalize_date("2026年5月20日") == "2026-05-20"
    assert _normalize_date("2026/05/20") == "2026-05-20"
    assert _normalize_date("2026年12月3日") == "2026-12-03"


def test_section_tag_mapping():
    assert _map_section_tag("tzgg", "通知公告") == "通知公告"
    assert _map_section_tag("zcfg", "政策法规") == "政策法规"
    assert _map_section_tag("tdcr", "土地出让") == "土地出让"
    assert _map_section_tag("gzdt", "房地产市场") == "房地产市场"
    assert _map_section_tag("other", "其他") == "其他"


def test_field_validation():
    assert _validate_field_value("正常文本") is True
    assert _validate_field_value("text with semicolon;") is False
    assert _validate_field_value("text with backtick`") is False
    assert _validate_field_value("") is True
    assert _validate_field_value(None) is True
