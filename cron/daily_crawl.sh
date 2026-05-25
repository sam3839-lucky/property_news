#!/bin/zsh
# daily_crawl.sh — Cron wrapper for gov_crawler
# Hardened against macOS cron's minimal environment

# Full PATH (cron only has /usr/bin:/bin)
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.npm-global/bin:/usr/bin:/bin"

# Python venv (if using one)
# source ~/Projects/property_news/.venv/bin/activate

# Environment variables (read from file for security)
ENV_FILE="$HOME/Projects/property_news/.env"
[ -f "$ENV_FILE" ] && source "$ENV_FILE"

# Absolute paths
PROJECT_DIR="$HOME/Projects/property_news"
LOG_FILE="$PROJECT_DIR/logs/crawl_$(date +%Y%m%d_%H%M).log"

cd "$PROJECT_DIR" || exit 1

# Run crawler
python3 "$PROJECT_DIR/gov_crawler.py" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

# Keep last 60 days of logs
find "$PROJECT_DIR/logs" -name "crawl_*.log" -mtime +60 -delete 2>/dev/null

exit $EXIT_CODE
