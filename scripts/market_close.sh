#!/bin/bash
# Run at 4pm ET Friday — close all positions then stop agent

echo "$(date) — Market close routine starting"

cd /home/ubuntu/robinhood-agent
source venv/bin/activate

# Close all open positions first
python3 -c "
import sys
sys.path.insert(0, 'agent')
from dotenv import load_dotenv
load_dotenv('.env')
from executor_alpaca import get_option_positions, close_option_position, enrich_position_pnl

positions = get_option_positions()
print(f'Open positions: {len(positions)}')

if positions:
    for pos in positions:
        pos = enrich_position_pnl(pos)
        symbol = pos.get('symbol')
        pnl    = pos.get('pnl_pct', 0)
        print(f'Closing {symbol} P&L={pnl:+.1f}%')
        result = close_option_position(pos)
        if 'error' not in result:
            print(f'✅ Closed {symbol}')
        else:
            print(f'❌ Failed: {result}')
else:
    print('No open positions — safe to stop')
"

# Wait 10 seconds for orders to fill
sleep 10

# Now stop the agent
sudo systemctl stop trading-agent
echo "$(date) — Agent stopped"
