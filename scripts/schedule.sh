#!/bin/bash
(crontab -l 2>/dev/null; cat << 'CRON'
0 1 * * 2-6 /home/ubuntu/robinhood-agent/cleanup.sh >> /home/ubuntu/robinhood-agent/logs/cleanup.log 2>&1
0 14 * * 1-5 sudo systemctl start trading-agent
0 14 * * 1-5 /home/ubuntu/robinhood-agent/cleanup.sh >> /home/ubuntu/robinhood-agent/logs/cleanup.log 2>&1
0 21 * * 5 sudo systemctl stop trading-agent
CRON
) | sort -u | crontab -
echo "✅ Cron jobs installed!"
crontab -l
