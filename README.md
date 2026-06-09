# 🤖 Robinhood Claude Trading Agent

Fully autonomous options trading agent for AAPL + MCD using Claude AI brain + Robinhood MCP.

## Architecture

```
VM (always on)
│
├── scanner.py      → Fetches price, RSI, volume, IV every 2 min
├── brain.py        → Asks Claude: should I trade?
├── executor.py     → Places/closes orders via Robinhood MCP
└── notifier.py     → Texts + emails you every trade
```

## What It Does Automatically

- ✅ Scans AAPL + MCD every 2 minutes during market hours
- ✅ Computes RSI + volume signals
- ✅ Skips trades near earnings or when IV is too high
- ✅ Asks Claude AI to approve every trade
- ✅ Max 1 contract open at a time
- ✅ Takes profit at +80%, stops loss at -50%
- ✅ Texts + emails you every trade
- ✅ Daily P&L summary at 4pm ET
- ✅ Stops if daily loss > $200 (configurable)
- ✅ Auto-restarts if it crashes

---

## One-Time Setup (30 minutes total)

### Step 1 — Get a VM ($6/mo)
1. Go to [digitalocean.com](https://digitalocean.com)
2. Create account → Create Droplet
3. Choose: **Ubuntu 24.04**, Basic, $6/mo (1 CPU, 1GB RAM)
4. Add your SSH key or use password auth
5. Copy the VM's IP address

### Step 2 — Get Your API Keys

**Anthropic (Claude):**
- Go to [console.anthropic.com](https://console.anthropic.com)
- API Keys → Create Key
- Copy `sk-ant-...`

**Robinhood MCP Token:**
- Robinhood app → Account → Settings → API Access
- Generate token for `agent.robinhood.com/mcp/trading`

**Twilio SMS (free):**
- Sign up at [twilio.com](https://twilio.com)
- Get a free phone number
- Copy Account SID + Auth Token

**Gmail App Password (for email alerts):**
- Google Account → Security → 2-Step Verification → App Passwords
- Create one for "Mail"

### Step 3 — Deploy

```bash
# SSH into your VM
ssh ubuntu@YOUR_VM_IP

# Download and run setup script
curl -O https://raw.githubusercontent.com/YOUR_USERNAME/robinhood-agent/main/setup.sh
bash setup.sh
```

When prompted, fill in your `.env` file with all the API keys above.

### Step 4 — Verify It's Running

```bash
sudo systemctl status trading-agent
tail -f logs/agent_$(date +%Y-%m-%d).log
```

You should see log lines like:
```
2024-01-15 09:36:00 [INFO] Signal [AAPL]: HOLD — RSI=52.3, VolRatio=0.9
2024-01-15 09:38:00 [INFO] Signal [MCD]: BUY_CALL — RSI=33.1, VolRatio=2.1
2024-01-15 09:38:02 [INFO] Claude decision: {"action": "BUY_CALL", ...}
2024-01-15 09:38:04 [INFO] Order placed: {...}
```

---

## Configuration

Edit `/home/ubuntu/robinhood-agent/.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `DAILY_LOSS_CAP` | 200 | Stop trading if daily loss exceeds this ($) |

To change symbols, edit `agent/main.py`:
```python
SYMBOLS = ["AAPL", "MCD"]   # add more here
```

---

## Useful Commands

```bash
# Check agent status
sudo systemctl status trading-agent

# Stop the agent
sudo systemctl stop trading-agent

# Restart after config changes
sudo systemctl restart trading-agent

# Watch live logs
tail -f logs/agent_$(date +%Y-%m-%d).log

# Check today's trades
grep "Order placed\|Position closed" logs/agent_$(date +%Y-%m-%d).log
```

---

## Signal Logic

| Condition | Action |
|-----------|--------|
| RSI < 35 + Volume 1.5x avg | BUY CALL |
| RSI > 68 + Volume 1.5x avg | BUY PUT |
| IV > 80% | SKIP (options too expensive) |
| Earnings within 5 days | SKIP |
| Option up +80% | EXIT (take profit) |
| Option down -50% | EXIT (stop loss) |
| Already have 1 open position | HOLD |

Claude AI reviews every signal before a trade is placed.

---

## Risk Disclaimer

This is experimental software. Options trading involves significant risk.
Paper trade for at least 30 days before using real money. Never risk more
than you can afford to lose.
