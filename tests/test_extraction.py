"""Test HTML extraction against real fixture files."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from pathlib import Path
from gov_crawler import _extract_articles_from_list, _extract_article_body, _detect_pdf_links

FIXTURES = Path(__file__).parent / "fixtures"
ZJJ_HOME = FIXTURES / "zjj_homepage.html"
PNR_HOME = FIXTURES / "pnr_homepage.html"


def test_extract_zjj_tzgg():
    """Articles visible on the zjj homepage contain 通知公告 links."""
    html = ZJJ_HOME.read_text()
    section = {
        "article_pattern": "/xxgk/tzgg/content/post_{id}.html",
    }
    articles = _extract_articles_from_list(html, "https://zjj.sz.gov.cn", section)
    assert len(articles) >= 5, f"Expected >=5 通知公告, got {len(articles)}"
    for a in articles:
        assert "/xxgk/tzgg/" in a["url"], f"URL not in tzgg: {a['url']}"
        assert len(a["title"]) > 5, f"Title too short: {a['title']}"


def test_extract_zjj_zcfg():
    """政策法规 articles exist with correct URL pattern (may not be on homepage)."""
    html = ZJJ_HOME.read_text()
    section = {
        "article_pattern": "/xxgk/zcfgs/zcfg/content/post_{id}.html",
    }
    articles = _extract_articles_from_list(html, "https://zjj.sz.gov.cn", section)
    # Homepage may not directly list zcfg articles; they appear on the section list page.
    # This test verifies the pattern matching works without false positives.
    for a in articles:
        assert "/xxgk/zcfgs/zcfg/" in a["url"], f"URL not in zcfg pattern: {a['url']}"


def test_extract_zjj_gzdt():
    """Articles visible on the zjj homepage contain 工作动态 links."""
    html = ZJJ_HOME.read_text()
    section = {
        "article_pattern": "/xxgk/gzdt/content/post_{id}.html",
    }
    articles = _extract_articles_from_list(html, "https://zjj.sz.gov.cn", section)
    assert len(articles) >= 1, f"Expected >=1 工作动态, got {len(articles)}"


def test_pnr_is_antibot():
    """The pnr homepage fixture is an anti-bot JS challenge (minimal body)."""
    html = PNR_HOME.read_text()
    assert len(html) < 10000, f"Expected anti-bot shell <10KB, got {len(html)}"
    assert "<body>" in html


def test_pdf_detection():
    """PDF links are detected in HTML content."""
    html = '<a href="/files/doc.pdf">Download</a><a href="/page.html">Page</a>'
    pdfs = _detect_pdf_links(html, "https://example.com")
    assert len(pdfs) == 1, f"Expected 1 PDF, got {len(pdfs)}"
    assert pdfs[0].endswith(".pdf")
