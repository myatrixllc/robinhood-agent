#!/bin/bash
# Emergency close ALL orders AND positions
echo "🚨 EMERGENCY CLOSE ALL POSITIONS AND ORDERS"

# Stop agent first
sudo systemctl stop trading-agent
echo "✅ Agent stopped"

cd /home/ubuntu/robinhood-agent
source venv/bin/activate

python3 -c "
import sys
sys.path.insert(0, 'agent')
from dotenv import load_dotenv
load_dotenv('.env')
import robin_stocks.robinhood as rh
import os

rh.login(os.environ['ROBINHOOD_USERNAME'], os.environ['ROBINHOOD_PASSWORD'])

# 1. Cancel all open orders
orders = rh.orders.get_all_open_option_orders()
print(f'Open orders: {len(orders)}')
for o in orders:
    rh.orders.cancel_option_order(o.get('id'))
    print(f'  Cancelled order: {o.get(\"id\")}')

# 2. Close all open positions
positions = rh.options.get_open_option_positions()
print(f'Open positions: {len(positions)}')
for p in positions:
    symbol = p.get('chain_symbol')
    expiry = p.get('expiration_date')
    strike = p.get('strike_price')
    otype  = p.get('option_type')
    qty    = int(float(p.get('quantity', 1)))

    # Get mark price
    opts  = rh.options.find_options_by_expiration_and_strike(
        symbol, expirationDate=expiry,
        strikePrice=str(strike), optionType=otype,
    )
    mark  = float(opts[0].get('mark_price', 0)) if opts else 0
    limit = round(mark * 0.95, 2) if mark > 0 else 0.01

    result = rh.orders.order_sell_option_limit(
        positionEffect='close',
        creditOrDebit='credit',
        price=limit,
        symbol=symbol,
        quantity=qty,
        expirationDate=expiry,
        strike=strike,
        optionType=otype,
    )
    print(f'  Closed: {symbol} {otype} \${strike} @ \${limit}')

print()
print('✅ Emergency close complete!')
"
