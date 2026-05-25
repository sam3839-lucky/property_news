#!/usr/bin/env python3
"""
gov_script_generator.py — 短视频创作素材生成
从飞书 Base 读取已入选的记录，调用 AI 生成素材（事实+影响+问题），写回 Base。
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ========== Config ==========
BASE_TOKEN = os.environ.get("FEISHU_BASE_TOKEN", "")
TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "tblsJ9n8uUyvX9tj")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
LARK_CLI = os.environ.get("LARK_CLI_PATH", "lark-cli")
MAX_INPUT_CHARS = 4000
MAX_RETRIES = 3

# ========== Prompt ==========
SYSTEM_PROMPT = """你是一个深圳房产短视频博主的研究助手。你的任务是帮博主整理创作素材，而不是直接写稿子。

基于提供的官方公告信息，请提取：

1. **3个最值得讲的事实** — 具体数据、具体条款、具体地块名称
2. **2个有争议或反直觉的影响分析** — 这件事让人意外的点在哪？对买房人/业主意味着什么？
3. **3个可以展开讨论的开放问题** — 适合在视频结尾引导观众在评论区互动

规则：
- 全文不超过400字
- 用要点而非段落
- 不要念文件口吻
- 如果原文信息不足（少于50字），输出「信息不足，建议人工查看原文」"""


def _find_lark_cli() -> str:
    for p in [
        os.path.expanduser("~/.npm-global/bin/lark-cli"),
        "/opt/homebrew/bin/lark-cli",
        "/usr/local/bin/lark-cli",
    ]:
        if os.path.exists(p):
            return p
    return LARK_CLI


def _sanitize_input(text: str) -> str:
    """Truncate and strip potentially dangerous patterns from input text."""
    if not text:
        return ""
    t = text[:MAX_INPUT_CHARS]
    # Strip lines that look like system prompts or instructions
    t = re.sub(r"(?im)^[ \t]*(you are|system:|assistant:|human:|user:)[^\n]*", "", t)
    return t.strip()


def _call_deepseek(title: str, source: str, body: str) -> str | None:
    """Call DeepSeek API to generate creative material. Returns text or None."""
    if not DEEPSEEK_API_KEY:
        print("  [ai] DEEPSEEK_API_KEY not set — skipping")
        return None

    user_msg = f"标题：{title}\n来源：{source}\n正文：{body}"

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.7,
        "max_tokens": 800,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                ["curl", "-s", "https://api.deepseek.com/chat/completions",
                 "-H", "Content-Type: application/json",
                 "-H", f"Authorization: Bearer {DEEPSEEK_API_KEY}",
                 "-d", json.dumps(payload, ensure_ascii=False),
                 "--connect-timeout", "30", "--max-time", "60"],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    return content.strip()

            delay = 5 * (2 ** (attempt - 1))
            print(f"  [ai] retry {attempt}/{MAX_RETRIES} — waiting {delay}s")
            time.sleep(delay)
        except Exception as e:
            print(f"  [ai] error: {e}")

    return None


def _list_selected_records() -> list[dict]:
    """Query Feishu Base for records where 选题决策=入选 AND 文案状态=待生成."""
    # We use lark-cli to read all records, then filter client-side
    # because Base API doesn't support compound filter queries easily
    try:
        result = subprocess.run(
            [LARK_CLI, "base", "+record-list",
             "--base-token", BASE_TOKEN,
             "--table-id", TABLE_ID,
             "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"  [base] list failed: {result.stderr[:200]}")
            return []

        data = json.loads(result.stdout)
        if not data.get("ok"):
            return []

        records = []
        field_ids = data["data"].get("field_id_list", [])
        fields = data["data"].get("fields", [])

        for i, row in enumerate(data["data"].get("data", [])):
            record = {"record_id": data["data"]["record_id_list"][i]}
            for j, fid in enumerate(field_ids):
                fname = fields[j] if j < len(fields) else fid
                record[fname] = row[j] if j < len(row) else None
            records.append(record)

        # Filter: 选题决策=入选 AND 文案状态=待生成
        selected = [
            r for r in records
            if r.get("选题决策") == "入选" and r.get("文案状态") == "待生成"
        ]
        return selected

    except Exception as e:
        print(f"  [base] error: {e}")
        return []


def _update_record(record_id: str, creative_material: str) -> bool:
    """Write creative material back to the Base record."""
    safe_material = _sanitize_input(creative_material)
    payload = json.dumps({
        "创作素材": safe_material,
        "文案状态": "已生成",
    }, ensure_ascii=False)

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
            return data.get("ok", False)
    except Exception as e:
        print(f"  [base] update failed: {e}")
    return False


def main():
    if not BASE_TOKEN:
        print("ERROR: FEISHU_BASE_TOKEN not set")
        return 1
    if not DEEPSEEK_API_KEY:
        print("WARNING: DEEPSEEK_API_KEY not set — AI generation disabled")
        print("Set it in your .env file or export DEEPSEEK_API_KEY=sk-xxx")

    LARK_CLI = _find_lark_cli()
    records = _list_selected_records()
    print(f"Found {len(records)} selected records to generate material for")

    generated = 0
    for r in records:
        title = r.get("标题") or ""
        body = r.get("正文全文") or r.get("正文摘要") or ""
        section = r.get("来源栏目") or ""

        if not body or len(body.strip()) < 10:
            _update_record(r["record_id"], "信息不足，建议人工查看原文")
            print(f"  [skip] {title[:40]} — insufficient body text")
            continue

        safe_body = _sanitize_input(body)
        print(f"  [generating] {title[:50]}")
        material = _call_deepseek(title, section, safe_body)

        if material:
            if _update_record(r["record_id"], material):
                generated += 1
                print(f"  [done] {title[:40]}")
            else:
                print(f"  [fail] update failed for {title[:40]}")
        else:
            print(f"  [fail] AI generation failed for {title[:40]}")

    print(f"\nDone. Generated {generated}/{len(records)}")
    return 0 if generated == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
