#!/usr/bin/env python3
"""
gov_crawler.py — 深圳住建局 + 规自局官网爬虫
Phase 1: 核心抓取、反爬、截图、去重、PDF 处理、结构变化检测
"""
import os
import sys
import re
import json
import time
import random
import hashlib
import subprocess
import signal
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

import db

# ========== Paths ==========
PROJECT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = PROJECT_DIR / "config" / "gov_targets.yaml"
SCREENSHOTS_DIR = PROJECT_DIR / "screenshots"
SCREENSHOTS_FULL_DIR = SCREENSHOTS_DIR / "full"
SCREENSHOTS_BODY_DIR = SCREENSHOTS_DIR / "body"
PDFS_DIR = PROJECT_DIR / "pdfs"
LOG_DIR = PROJECT_DIR / "logs"

# ========== Anti-bot ==========
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]
CAPTCHA_KEYWORDS = ["验证", "滑块", "点击验证", "人机验证", "captcha", "verification"]

# ========== Playwright browser lifecycle ==========
# 连接用户已有 Chrome（端口 9223），利用真实浏览历史和 cookies 突破反爬
_BROWSER = None
_CONTEXT = None
_CDP_URL = "http://127.0.0.1:9223"
_PW = None  # Playwright instance


def _ensure_dirs():
    for d in [SCREENSHOTS_FULL_DIR, SCREENSHOTS_BODY_DIR, PDFS_DIR, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def _load_config():
    if not CONFIG_PATH.exists():
        print(f"ERROR: config file not found: {CONFIG_PATH}")
        sys.exit(1)
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        if not cfg or "sites" not in cfg:
            print(f"ERROR: config file missing 'sites' key: {CONFIG_PATH}")
            sys.exit(1)
        return cfg
    except yaml.YAMLError as e:
        print(f"ERROR: invalid YAML in config: {e}")
        sys.exit(1)


def _launch_browser():
    global _BROWSER, _CONTEXT, _PW
    _PW = sync_playwright().start()
    _BROWSER = _PW.chromium.connect_over_cdp(_CDP_URL)
    _CONTEXT = _BROWSER.contexts[0]


def _kill_orphaned_chromium():
    """Kill zombie chromium processes from previous crashed runs."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "chromium.*property_news"],
            capture_output=True, text=True, timeout=5
        )
        for pid in result.stdout.strip().split():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
    except Exception:
        pass


def _random_delay(min_s=5, max_s=15):
    time.sleep(random.uniform(min_s, max_s))


def _check_captcha(page) -> bool:
    """Detect CAPTCHA/maint page. Returns True if blocked."""
    try:
        body_text = page.inner_text("body")[:2000].lower()
        for kw in CAPTCHA_KEYWORDS:
            if kw in body_text:
                return True
    except Exception:
        pass
    try:
        title = page.title().lower()
        for kw in CAPTCHA_KEYWORDS:
            if kw in title:
                return True
    except Exception:
        pass
    return False


def _check_anti_bot(page) -> bool:
    """Check if the page is a JS challenge shell (like pnr)."""
    try:
        body_text = page.inner_text("body").strip()
        body_html = page.inner_html("body").strip()
        # Heuristic 1: visible text too short (real gov pages have >100 chars)
        if len(body_text) < 80:
            return True
        # Heuristic 2: mostly script with minimal content
        if len(body_html) < 200 and "script" in body_html.lower():
            return True
    except Exception:
        pass
    return False


# ========== Text & content extraction ==========

def _extract_articles_from_list(html: str, base_url: str, section_cfg: dict) -> list[dict]:
    """Parse a section list page HTML, return list of article dicts."""
    soup = BeautifulSoup(html, "lxml")
    articles = []
    seen_on_page = set()

    # Strategy 1: Look for <a> tags matching article pattern
    pattern = section_cfg.get("article_pattern", "")
    # Extract the prefix from pattern like "/xxgk/tzgg/content/post_{id}.html"
    prefix = pattern.replace("{id}", "").rsplit("/", 1)[0] if pattern else ""

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)

        # Must contain the content prefix
        if prefix and prefix not in full_url:
            continue
        # Must have visible text (the title)
        title = a.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        # Must not be a nav/skip link
        if title in ("skip", "返回", "首页", "上一页", "下一页", "浏览指引"):
            continue

        if full_url in seen_on_page:
            continue
        seen_on_page.add(full_url)

        # Try to find date near the link
        date_text = _find_date_near(a)

        articles.append({
            "url": full_url,
            "title": title,
            "date_published": date_text,
        })

    return articles


def _find_date_near(element) -> str | None:
    """Try to find a date near an <a> element."""
    parent = element.find_parent("li") or element.find_parent("div") or element.parent
    if parent:
        text = parent.get_text()
        m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
        if m:
            return m.group(1)
        m = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)", text)
        if m:
            return m.group(1)
    return None


def _extract_detail_title(html: str, list_title: str) -> str | None:
    """从详情页提取完整标题（列表页标题常被截断为...）。"""
    soup = BeautifulSoup(html, "lxml")

    # Try <h1> or other heading first
    for sel in ["h1", ".title", "#title", "[class*=title]", "[class*=news-title]"]:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if len(text) > len(list_title) and text[:20] in list_title[:25]:
                return text

    # Fallback: find the longest text node that contains the list title prefix
    prefix = list_title[:15]
    body = soup.find("body")
    if body:
        texts = [t.strip() for t in body.stripped_strings]
        for t in texts:
            if t.startswith(prefix) and len(t) > len(list_title):
                return t

    return None


def _clean_gov_text(text: str) -> str:
    """清洗政府页面文本：去导航、元信息、分享按钮、页脚等非正文内容。"""
    lines = [l.strip() for l in text.split("\n")]
    cleaned = []

    # 垃圾行模式
    junk_patterns = [
        r"^(当前位置|信息公开|政务服务|互动交流|业务主页|数据开放)",
        r"^(来源：|日期：|发布时间：|发布日期：|文章来源：)",
        r"^(字号|视力保护|无障碍|进入关怀|IPv[46])",
        r"^(微信扫一扫|扫一扫|分享到|微博|微信|邮箱|政务应答)",
        r"^(网站声明|隐私声明|使用帮助|站点地图|版权|关于我们)",
        r"^【字号",
        r"^文档附件：",
        r"^\d+\..*\.pdf$",
        r"^[A-Za-z0-9_]+\.pdf$",
        r"^今天是\d{4}年\d{1,2}月\d{1,2}日.*欢迎",
        r"^老旧街区.*改造.*｜",
        r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+来源：",
    ]
    # 孤立导航词（单行仅一个词，无标点）
    nav_words = {"首页", "信息公开", "公告公示", "大", "中", "小", "繁体版", "手机版",
                 "无障碍阅读", "进入关怀版", "数据开放", "政务应答", "网站声明",
                 "隐私声明", "使用帮助", "站点地图"}

    for line in lines:
        if line in nav_words:
            continue
        skip = False
        for pat in junk_patterns:
            if re.match(pat, line):
                skip = True
                break
        if not skip and len(line) >= 4:
            cleaned.append(line)

    return "\n".join(cleaned).strip()


def _extract_article_body(html: str, url: str) -> str:
    """Extract main body text from an article detail page."""
    soup = BeautifulSoup(html, "lxml")

    # Try common gov content containers
    for sel in ["#zoom", "#content", ".article-con", ".TRS_Editor", ".news-content",
                ".xxym", ".content", "article", ".main-content", ".container"]:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 100:
                # 清洗页头页脚垃圾
                text = _clean_gov_text(text)
                if len(text) > 50:
                    return text[:4000]

    # Fallback: grab the biggest text block from body
    body = soup.find("body")
    if body:
        text = body.get_text(separator="\n", strip=True)
        text = _clean_gov_text(text)
        # Remove very short lines
        lines = [l for l in text.split("\n") if len(l.strip()) > 10]
        return "\n".join(lines)[:4000]

    return ""


def _detect_pdf_links(html: str, base_url: str) -> list[str]:
    """Detect PDF attachment links on a page."""
    soup = BeautifulSoup(html, "lxml")
    pdfs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf") or ".pdf?" in href.lower():
            pdfs.append(urljoin(base_url, href))
    return pdfs


# ========== Screenshots ==========

def _take_screenshots(page, url: str, title: str, site: str) -> tuple[str, str]:
    """Take full-page and body screenshots. Returns (full_path, body_path)."""
    safe_name = re.sub(r"[^\w\-]", "_", title)[:60]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{site}_{ts}_{safe_name}"

    full_path = str(SCREENSHOTS_FULL_DIR / f"{stem}_full.png")
    body_path = str(SCREENSHOTS_BODY_DIR / f"{stem}_body.png")

    try:
        page.screenshot(path=full_path, full_page=True)
    except Exception:
        full_path = None

    # Try to screenshot just the content area
    try:
        for sel in ["#zoom", ".article-con", ".TRS_Editor", ".news-content", ".content", "article"]:
            el = page.query_selector(sel)
            if el:
                el.screenshot(path=body_path)
                break
        else:
            page.screenshot(path=body_path)
    except Exception:
        body_path = None

    return full_path, body_path


# ========== PDF handling ==========

def _handle_pdf(url: str, site: str, title: str, page) -> dict | None:
    """Download PDF and attempt text extraction. Returns info dict or None."""
    try:
        safe_name = re.sub(r"[^\w\-]", "_", title)[:60]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{site}_{ts}_{safe_name}.pdf"
        local_path = PDFS_DIR / filename

        # Download via Playwright (handles session cookies)
        response = page.request.get(url)
        if response.status == 200:
            local_path.write_bytes(response.body())

            # Try pdfplumber text extraction
            try:
                import pdfplumber
                texts = []
                with pdfplumber.open(str(local_path)) as pdf:
                    for p in pdf.pages:
                        t = p.extract_text()
                        if t:
                            texts.append(t)
                full_text = "\n".join(texts)
                return {
                    "pdf_path": str(local_path),
                    "is_scanned": len(full_text.strip()) < 50,
                    "extracted_text": full_text[:4000],
                }
            except ImportError:
                return {"pdf_path": str(local_path), "is_scanned": True, "extracted_text": ""}
        else:
            return {"pdf_path": None, "is_scanned": True, "extracted_text": ""}
    except Exception as e:
        return {"pdf_path": None, "is_scanned": True, "extracted_text": "", "error": str(e)[:200]}


# ========== Structure change detection ==========

def _compute_page_fingerprints(html: str) -> tuple[str, int]:
    """Compute page text hash and DOM element count for structure monitoring."""
    soup = BeautifulSoup(html, "lxml")
    # Strip timestamps/dates from text before hashing
    body_text = soup.get_text()
    body_text = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", "DATE", body_text)
    body_text = re.sub(r"\d{1,2}:\d{2}(:\d{2})?", "TIME", body_text)
    text_hash = hashlib.md5(body_text.encode("utf-8")).hexdigest()
    dom_count = len(soup.find_all())
    return text_hash, dom_count


def _detect_structure_change(conn, site: str, section: str, html: str, item_count: int) -> bool:
    """Multi-signal structure change detection. 2-of-3 trigger."""
    text_hash, dom_count = _compute_page_fingerprints(html)
    baseline = db.get_baseline_stats(conn, site, section, days=30)

    signals = 0
    total_signals = 0

    if baseline:
        avg_items = sum(r["item_count"] for r in baseline) / len(baseline)
        # Signal 1: item count outside 60% of rolling avg
        total_signals += 1
        if avg_items > 0 and (item_count < avg_items * 0.4 or item_count > avg_items * 2.5):
            signals += 1

        # Signal 2: page text hash changed
        total_signals += 1
        recent_hashes = {r["page_text_hash"] for r in baseline[:5]}
        if text_hash not in recent_hashes:
            signals += 1

        # Signal 3: DOM element count changed >30%
        total_signals += 1
        avg_dom = sum(r["dom_element_count"] for r in baseline) / len(baseline)
        if avg_dom > 0 and abs(dom_count - avg_dom) / avg_dom > 0.3:
            signals += 1
    else:
        # No baseline yet — store but don't trigger
        total_signals = 3
        signals = 0

    # Update baseline
    db.update_baseline(conn, site, section, item_count, text_hash, dom_count)

    return signals >= 2 and total_signals >= 3


# ========== AI vision fallback ==========

def _encode_image(path: str) -> str:
    """Read image file and return base64 data URI."""
    import base64
    p = Path(path)
    if not p.exists():
        return ""
    data = p.read_bytes()
    ext = p.suffix.lower().replace(".", "")
    mime = "png" if ext == "png" else "jpeg"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/{mime};base64,{b64}"


def _call_vision_api(image_data_uri: str, prompt: str, api_key: str = None) -> str | None:
    """Call an AI vision API (DeepSeek or Claude) to extract text from an image."""
    key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("  [vision] no API key available")
        return None

    model = os.environ.get("DEEPSEEK_VISION_MODEL", os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_data_uri}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": 0.3,
        "max_tokens": 1000,
    }

    try:
        result = subprocess.run(
            ["curl", "-s", "https://api.deepseek.com/chat/completions",
             "-H", "Content-Type: application/json",
             "-H", f"Authorization: Bearer {key}",
             "-d", json.dumps(payload, ensure_ascii=False),
             "--connect-timeout", "30", "--max-time", "90"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return content.strip()
    except Exception as e:
        print(f"  [vision] API error: {e}")

    return None


def _ai_vision_extract_body(screenshot_path: str) -> str:
    """Extract body text from a detail-page screenshot via AI vision."""
    b64 = _encode_image(screenshot_path)
    if not b64:
        return ""
    prompt = "请从这张截图中提取网页正文的所有文字内容。只输出正文文本，不要加任何说明。"
    return _call_vision_api(b64, prompt) or ""


def _ai_vision_extract_list(screenshot_path: str) -> list[dict]:
    """Extract article list from a list-page screenshot via AI vision."""
    b64 = _encode_image(screenshot_path)
    if not b64:
        return []
    prompt = (
        "这张截图是一个政府网站的栏目列表页。请从中提取所有公告条目的信息，"
        "以JSON数组格式输出：[{\"title\":\"公告标题\",\"url\":\"公告链接\",\"date_published\":\"日期\"}]。"
        "只输出JSON数组，不要加任何说明。"
    )
    text = _call_vision_api(b64, prompt)
    if not text:
        return []
    # Parse JSON from AI response
    try:
        # Find JSON array in response
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except (json.JSONDecodeError, ValueError):
        pass
    return []


# ========== Main crawl orchestration ==========

def crawl_section(conn, page, site_cfg: dict, section_cfg: dict) -> dict:
    """Crawl one section of one site. Returns stats dict."""
    site_name = site_cfg["name"]
    site_key = site_cfg.get("key", site_name)
    section_name = section_cfg["name"]
    list_url = section_cfg["list_url"]
    base_url = site_cfg["base_url"]
    start_time = time.time()

    stats = {"found": 0, "new": 0, "errors": 0, "captcha": False}

    try:
        # Navigate to list page
        page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)  # Let JS finish rendering

        # Check anti-bot (JS challenge wall, e.g. pnr)
        if _check_anti_bot(page):
            # 等待 JS challenge 完成（真人浏览器会自然等待），重试一次
            print(f"  [antibot] {site_key}/{section_name} JS challenge detected, waiting & retrying...")
            page.wait_for_timeout(random.randint(5000, 10000))
            page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            if _check_anti_bot(page):
                stats["errors"] += 1
                db.log_run(conn, site_key, section_name, "antibot",
                           error="JS challenge wall detected (retry failed)")
                return stats

        # Check CAPTCHA
        if _check_captcha(page):
            stats["captcha"] = True
            stats["errors"] += 1
            db.log_run(conn, site_key, section_name, "captcha", error="CAPTCHA detected")
            return stats

        html = page.content()
        articles = _extract_articles_from_list(html, base_url, section_cfg)
        stats["found"] = len(articles)

        # Structure change detection
        structure_changed = _detect_structure_change(conn, site_key, section_name, html, len(articles))

        for art in articles:
            # Dedup
            if db.is_url_seen(conn, art["url"]):
                continue

            try:
                db.mark_url_seen(conn, art["url"], art["title"], site_key, section_name)

                # Fetch article detail
                page.goto(art["url"], wait_until="domcontentloaded", timeout=30000)
                # PNR 需要更长渲染时间（JS challenge + 正文加载）
                wait_ms = 3000 if site_key == 'pnr' else 1500
                page.wait_for_timeout(wait_ms)

                # PNR 详情页反爬检测：首次加载常返回空 body，重试最多 3 次
                if site_key == 'pnr':
                    for retry in range(3):
                        if not _check_anti_bot(page):
                            break
                        print(f"  [antibot-detail] {site_key} 详情页反爬，重试 {retry+1}/3...")
                        page.wait_for_timeout(5000)
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(3000)

                _random_delay(3, 8)

                article_html = page.content()

                # 从详情页提取完整标题（列表页标题可能截断）
                try:
                    page_title = page.title().strip()
                    if page_title and len(page_title) > len(art["title"]):
                        art["title"] = page_title
                except Exception:
                    pass

                # PDF detection on article page
                pdf_links = _detect_pdf_links(article_html, base_url)
                pdf_info = None
                body_text = ""
                if pdf_links:
                    pdf_info = _handle_pdf(pdf_links[0], site_key, art["title"], page)
                    if pdf_info and pdf_info.get("extracted_text"):
                        body_text = pdf_info["extracted_text"]
                    is_pdf = 1
                    pdf_path = pdf_info.get("pdf_path") if pdf_info else None
                else:
                    body_text = _extract_article_body(article_html, art["url"])
                    is_pdf = 0
                    pdf_path = None

                # PDF 文本提取失败时，回退到 HTML 正文提取
                if not body_text:
                    html_text = _extract_article_body(article_html, art["url"])
                    if html_text:
                        body_text = html_text

                # If body text still empty, try AI vision on the detail page screenshot
                ai_fallback = 0
                full_p, body_p = _take_screenshots(page, art["url"], art["title"], site_key)

                if not body_text and full_p:
                    vision_text = _ai_vision_extract_body(full_p)
                    if vision_text:
                        ai_fallback = 1
                        body_text = vision_text

                # Determine tags
                tags = ",".join(section_cfg.get("tags", [section_name]))

                # Resolve date: try article page date first, fall back to list page date
                date_pub = _extract_date_from_page(article_html) or art.get("date_published")

                # Stage record
                db.stage_record(
                    conn,
                    url=art["url"],
                    title=art["title"],
                    date_published=date_pub,
                    site=site_key,
                    section=section_name,
                    tags=tags,
                    body_text=body_text,
                    screenshot_full_path=full_p,
                    screenshot_body_path=body_p,
                    is_pdf=is_pdf,
                    pdf_path=pdf_path,
                    ai_fallback=ai_fallback,
                )
                stats["new"] += 1

            except Exception as e:
                stats["errors"] += 1
                db.log_run(conn, site_key, section_name, "item_error", error=str(e)[:500])

        db.log_run(
            conn, site_key, section_name, "ok",
            items_found=stats["found"], items_new=stats["new"],
            duration_ms=int((time.time() - start_time) * 1000),
        )

    except PwTimeout:
        stats["errors"] += 1
        db.log_run(conn, site_key, section_name, "timeout", error="Page load timeout")
    except Exception as e:
        stats["errors"] += 1
        db.log_run(conn, site_key, section_name, "error", error=str(e)[:500])

    return stats


def _extract_date_from_page(html: str) -> str | None:
    """Extract publish date from an article detail page."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text()[:3000]

    # Try common gov date patterns
    for pat in [
        r"发布日期[：:]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
        r"发布时间[：:]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
        r"(\d{4}年\d{1,2}月\d{1,2}日)",
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
    ]:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def _cleanup_old_assets(days: int = 30):
    """Delete screenshots and PDFs older than N days."""
    import time as _time
    cutoff = _time.time() - days * 86400
    for d in [SCREENSHOTS_FULL_DIR, SCREENSHOTS_BODY_DIR, PDFS_DIR]:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                print(f"  [cleanup] deleted {f.name}")


def main():
    _ensure_dirs()
    db.init_db()
    conn = db.get_db()
    config = _load_config()

    _kill_orphaned_chromium()
    _cleanup_old_assets(days=30)

    total_found = 0
    total_new = 0
    total_errors = 0
    sites_checked = []

    try:
        p = _launch_browser()
        page = _CONTEXT.new_page()

        for site_key, site_cfg in config["sites"].items():
            site_cfg["key"] = site_key
            sites_checked.append(site_key)

            for section_cfg in site_cfg["sections"]:
                _random_delay(5, 15)
                stats = crawl_section(conn, page, site_cfg, section_cfg)
                total_found += stats["found"]
                total_new += stats["new"]
                total_errors += stats["errors"]

                if stats["captcha"]:
                    _send_feishu_alert(f"CAPTCHA 检测: {site_cfg['name']} - {section_cfg['name']}")

        # Write local heartbeat
        db.write_heartbeat(conn, total_found, total_new, total_errors,
                           ",".join(sites_checked))
        db.cleanup_old(conn, days=30)

        # 发送飞书心跳通知
        sites_str = ",".join(sites_checked)
        _send_notification(total_found, total_new, total_errors, sites_str)
        print(f"Done. Found {total_found}, new {total_new}, errors {total_errors}")

    finally:
        # CDP 模式不关闭浏览器（是用户的 Chrome）
        if _PW:
            try:
                _PW.stop()
            except Exception:
                pass
        conn.commit()
        conn.close()

    return 0 if total_errors == 0 else 1


def _send_feishu_alert(msg: str):
    """Send a Feishu alert."""
    try:
        import subprocess as sp
        sp.run(
            ["lark-cli", "im", "send", "--text", msg],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        print(f"[ALERT FAILED] {msg}: {e}")


def _send_notification(found: int, new: int, errors: int, sites: str):
    """飞书心跳通知（替代 feishu_writer.send_notification）。"""
    from datetime import datetime
    emoji = "✅" if errors == 0 else "⚠️"
    msg = f"{emoji} 住建局监控 {datetime.now().strftime('%H:%M')}\n扫描 {sites}\n新增 {new}/{found} 条\n错误 {errors} 条"
    try:
        import subprocess as sp
        sp.run(
            ["lark-cli", "im", "send", "--text", msg],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        print(f"  [notify] failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
