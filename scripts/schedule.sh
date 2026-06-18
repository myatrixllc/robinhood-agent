#!/bin/bash
# Install all cron jobs for trading agent

(crontab -l 2>/dev/null; cat << 'CRON'
# 9:00 AM ET cleanup (Mon-Fri)
0 14 * * 1-5 /home/ubuntu/robinhood-agent/cleanup.sh >> /home/ubuntu/robinhood-agent/logs/cleanup.log 2>&1
# 8:00 PM ET cleanup (Tue-Sat)
0 1 * * 2-6 /home/ubuntu/robinhood-agent/cleanup.sh >> /home/ubuntu/robinhood-agent/logs/cleanup.log 2>&1
# Stop agent Friday 4:00pm ET (21:00 UTC)
0 21 * * 5 sudo systemctl stop trading-agent
# Start agent Monday 9:00am ET (14:00 UTC)
0 14 * * 1 sudo systemctl start trading-agent
CRON
) | sort -u | crontab -

echo "Cron jobs installed!"
crontab -l
