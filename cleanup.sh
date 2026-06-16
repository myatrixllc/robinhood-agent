#!/bin/bash
# cleanup.sh — Daily disk cleanup after market close
# Runs at 4:15pm ET (21:15 UTC)

echo "$(date) — Starting daily cleanup..."

# Keep only last 7 days of logs
find /home/ubuntu/robinhood-agent/logs -name "*.log" -mtime +7 -delete

# Clean Python cache
find /home/ubuntu/robinhood-agent -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
find /home/ubuntu/robinhood-agent -name "*.pyc" -delete 2>/dev/null

# Clean apt cache
sudo apt-get clean -y

# Clean journalctl — keep last 3 days only
sudo journalctl --vacuum-time=3d

# Clean temp files
sudo rm -rf /tmp/* 2>/dev/null

# Show disk usage after cleanup
df -h /

echo "$(date) — Cleanup complete!"

# Truncate syslog if over 500MB
SYSLOG_SIZE=$(du -sm /var/log/syslog 2>/dev/null | cut -f1)
if [ "${SYSLOG_SIZE:-0}" -gt 500 ]; then
    echo "$(date) — Truncating syslog (${SYSLOG_SIZE}MB)"
    sudo truncate -s 0 /var/log/syslog
    sudo truncate -s 0 /var/log/syslog.1
fi
