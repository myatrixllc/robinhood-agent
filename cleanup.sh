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
