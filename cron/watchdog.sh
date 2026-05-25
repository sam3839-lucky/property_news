#!/bin/zsh
# watchdog.sh — Check that gov_crawler ran today
# Run at 8:15 and 18:15 via cron

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

PROJECT_DIR="$HOME/Projects/property_news"
cd "$PROJECT_DIR" || exit 1

# Check heartbeat via Python
HAS_HEARTBEAT=$(python3 -c "
import sys; sys.path.insert(0, '.')
import db
conn = db.get_db()
db.init_db()
print('yes' if db.has_heartbeat_today(conn) else 'no')
conn.close()
" 2>/dev/null)

if [ "$HAS_HEARTBEAT" != "yes" ]; then
    echo "[WATCHDOG] No heartbeat today — crawler may have failed to run"
    # Send Feishu alert (Phase 2 integration)
    # lark-cli im send --chat-id <CHAT_ID> --text "gov_crawler 今日未执行"
    exit 1
fi

echo "[WATCHDOG] Heartbeat OK"
exit 0
