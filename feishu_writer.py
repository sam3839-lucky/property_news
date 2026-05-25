#!/usr/bin/env python3
"""
feishu_writer.py — 飞书同步模块
将本地 staging 表记录推送到飞书多维表格，含重试和通知。
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import db

# ========== Config ==========
BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "")
TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "tblsJ9n8uUyvX9tj")
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds, doubles each retry

# Field name → lark-cli compatible value mapping
# Select fields use the option name text directly
# Datetime fields use "YYYY-MM-DD" format strings


def _find_lark_cli() -> str:
    """Find the lark-cli binary path."""
    for p in [
        os.path.expanduser("~/.npm-global/bin/lark-cli"),
        "/opt/homebrew/bin/lark-cli",
        "/usr/local/bin/lark-cli",
    ]:
        if os.path.exists(p):
            return p
    # Fall back to PATH
    return "lark-cli"


LARK_CLI = _find_lark_cli()


def _validate_field_value(value: str) -> bool:
    """Reject values with shell metacharacters that could cause injection."""
    if not value:
        return True
    dangerous = re.search(r'[;|&`$(){}\[\]<>"\'\n\r]', value)
    return not bool(dangerous)


def _sanitize(value: str) -> str:
    """Strip dangerous characters from field values."""
    if not value:
        return ""
    # Keep only safe chars: alphanumeric, Chinese, common punctuation, URLs
    safe = re.sub(r'[;&`$(){}\[\]<>"\'\n\r]', "", value)
    return safe


def upload_screenshot(file_path: str) -> str | None:
    """Upload a local file to Feishu Drive. Returns the Drive URL or None."""
    if not file_path or not os.path.exists(file_path):
        print(f"  [upload] file not found: {file_path}")
        return None

    try:
        result = subprocess.run(
            [LARK_CLI, "drive", "+upload", "--file", file_path],
            capture_output=True, text=True, timeout=60,
            cwd=str(Path(file_path).parent),
        )
        if result.returncode != 0:
            print(f"  [upload] failed: {result.stderr[:200]}")
            return None

        data = json.loads(result.stdout)
        if data.get("ok") and data.get("data", {}).get("url"):
            return data["data"]["url"]
    except Exception as e:
        print(f"  [upload] error: {e}")

    return None


def build_record(staging_row) -> dict:
    """Build a Base record dict from a staging table row."""
    # Map staging columns → Base field names
    record = {
        "标题": staging_row["title"],
        "原文链接": staging_row["url"],
        "正文全文": staging_row["body_text"] or "",
        "正文摘要": (staging_row["body_text"] or "")[:200],
        "来源栏目": _map_section_tag(staging_row["section"], staging_row["tags"]),
        "选题决策": "待定",
        "文案状态": "待生成",
    }

    # Date fields
    if staging_row["date_published"]:
        record["发布日期"] = _normalize_date(staging_row["date_published"])
    record["采集时间"] = datetime.now().strftime("%Y-%m-%d")

    # Screenshot URLs (upload if not already done)
    if staging_row["screenshot_full_url"]:
        record["全页截图"] = staging_row["screenshot_full_url"]
    if staging_row["screenshot_body_url"]:
        record["正文截图"] = staging_row["screenshot_body_url"]

    # AI extraction mark
    if staging_row["ai_fallback"]:
        record["AI提取标记"] = "ai_vision_fallback"
    else:
        record["AI提取标记"] = "正常"

    # PDF mark
    if staging_row["is_pdf"]:
        if "扫描" in (staging_row.get("pdf_path") or ""):
            record["PDF标记"] = "扫描件PDF"
        else:
            record["PDF标记"] = "PDF附件"
    else:
        record["PDF标记"] = "否"

    return record


def _map_section_tag(section: str, tags: str) -> str:
    """Map section + tags to the 来源栏目 select field."""
    combined = f"{section} {tags or ''}"
    if "土地" in combined:
        return "土地出让"
    if "政策" in combined or "法规" in combined:
        return "政策法规"
    if "通知" in combined or "公告" in combined:
        return "通知公告"
    if "市场" in combined or "数据" in combined:
        return "房地产市场"
    return "其他"


def _normalize_date(date_str: str) -> str:
    """Normalize various date formats to YYYY-MM-DD."""
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")
    # Already YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    # Chinese date: "2024年1月2日"
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # YYYY/MM/DD
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return datetime.now().strftime("%Y-%m-%d")


def write_record_to_base(record: dict) -> tuple[bool, str]:
    """Write a single record to the Feishu Base via lark-cli. Returns (ok, error_msg)."""
    # Sanitize all text values
    safe_record = {}
    for k, v in record.items():
        if isinstance(v, str):
            safe_record[k] = _sanitize(v)
        else:
            safe_record[k] = v

    payload = json.dumps(safe_record, ensure_ascii=False)

    try:
        result = subprocess.run(
            [LARK_CLI, "base", "+record-upsert",
             "--base-token", BASE_TOKEN,
             "--table-id", TABLE_ID,
             "--json", payload],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if data.get("ok"):
                return True, ""
            return False, data.get("error", {}).get("message", str(data))[:500]
        return False, result.stderr[:500]
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)[:500]


def sync_record_with_retry(conn, staging_row) -> bool:
    """Upload screenshots, write to Base, retry on failure. Returns True if synced."""
    row = dict(staging_row)

    # Upload screenshots if not already uploaded
    if not row.get("screenshot_full_url") and row.get("screenshot_full_path"):
        url = upload_screenshot(row["screenshot_full_path"])
        if url:
            row["screenshot_full_url"] = url
            conn.execute(
                "UPDATE staging SET screenshot_full_url=? WHERE url_hash=?",
                (url, row["url_hash"]),
            )

    if not row.get("screenshot_body_url") and row.get("screenshot_body_path"):
        url = upload_screenshot(row["screenshot_body_path"])
        if url:
            row["screenshot_body_url"] = url
            conn.execute(
                "UPDATE staging SET screenshot_body_url=? WHERE url_hash=?",
                (url, row["url_hash"]),
            )

    record = build_record(row)

    for attempt in range(1, MAX_RETRIES + 1):
        ok, error = write_record_to_base(record)
        if ok:
            db.mark_synced(conn, row["url"])
            return True

        delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
        print(f"  [retry {attempt}/{MAX_RETRIES}] {error[:100]} — waiting {delay}s")
        time.sleep(delay)

    db.mark_sync_failed(conn, row["url"], error)
    return False


def sync_all_pending(conn, limit: int = 50) -> dict:
    """Sync all pending records. Returns stats dict."""
    pending = db.get_pending_records(conn, limit=limit)
    stats = {"total": len(pending), "synced": 0, "failed": 0}

    for row in pending:
        if sync_record_with_retry(conn, row):
            stats["synced"] += 1
        else:
            stats["failed"] += 1

    conn.commit()
    return stats


def send_notification(found: int, new: int, errors: int, sites: str):
    """Send a Feishu heartbeat notification."""
    emoji = "✅" if errors == 0 else "⚠️"
    msg = f"{emoji} 住建局监控 {datetime.now().strftime('%H:%M')}\n扫描 {sites}\n新增 {new}/{found} 条\n错误 {errors} 条"

    try:
        subprocess.run(
            ["lark-cli", "im", "send",
             "--text", msg],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        print(f"  [notify] failed: {e}")


def main():
    conn = db.get_db()
    db.init_db()

    stats = sync_all_pending(conn)
    print(f"Sync done: {stats['synced']} synced, {stats['failed']} failed (of {stats['total']})")

    conn.close()
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
