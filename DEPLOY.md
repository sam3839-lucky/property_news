# Deploy

## 前置条件

```bash
# 1. 确认依赖
python3 -c "import playwright,yaml,bs4,lxml; print('OK')"
playwright install chromium
pip3 install --break-system-packages pdfplumber  # PDF optional

# 2. 确认 lark-cli
which lark-cli

# 3. 配置 .env
cp .env.example .env
# 编辑 .env 填入:
#   FEISHU_BASE_TOKEN=  (已预设)
#   FEISHU_TABLE_ID=    (已预设)
#   DEEPSEEK_API_KEY=   (Phase 3 素材生成用)
#   FEISHU_CHAT_ID=     (watchdog 告警接收 chat)

# 4. 预检
python3 gov_health_check.py
```

## Crontab

```bash
# 编辑 crontab
crontab -e
```

添加以下两行：

```
# gov_crawler — 深圳住建局+规自局 每天 8:00 + 18:00
SHELL=/bin/zsh
0 8,18 * * * /Users/sam/Projects/property_news/cron/daily_crawl.sh

# watchdog — 检查爬虫是否按时执行
15 8,18 * * * /Users/sam/Projects/property_news/cron/watchdog.sh
```

## 手动运行

```bash
# 完整爬取（同步到飞书）
cd ~/Projects/property_news
source .env
python3 gov_crawler.py

# 仅生成创作素材（对已入选的记录）
python3 gov_script_generator.py

# 仅同步本地暂存（重试失败的记录）
python3 feishu_writer.py

# 健康检查
python3 gov_health_check.py
```

## 日志

```bash
# 查看最近日志
tail -100 ~/Projects/property_news/logs/crawl_*.log

# 查看 watchdog 状态
python3 -c "
import sys; sys.path.insert(0, '.')
import db
conn = db.get_db()
print('Today heartbeat:', 'yes' if db.has_heartbeat_today(conn) else 'NO')
rows = conn.execute('SELECT * FROM run_log ORDER BY id DESC LIMIT 10').fetchall()
for r in rows:
    print(f'  {r[\"run_time\"]} {r[\"site\"]}/{r[\"section\"]}: {r[\"status\"]} ({r[\"items_new\"]} new)')
conn.close()
"
```

## 监控

- **每天 8:15 / 18:15**：watchdog 检查心跳
- **心跳缺失** → 飞书消息告警（需配置 FEISHU_CHAT_ID）
- **CAPTCHA 检测** → 实时飞书告警
- **截图/PDF**：自动清理 30 天前的文件
- **数据库**：30 天前的基线数据和运行日志自动清理
