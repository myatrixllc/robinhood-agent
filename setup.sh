#!/bin/bash
# setup.sh — One-command install on a fresh Ubuntu VM (DigitalOcean / AWS)
# Run as: bash setup.sh

set -e
echo "🤖 Setting up Robinhood Claude Trading Agent..."

# ── System deps ───────────────────────────────────────────────────────────────
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv git curl

# ── Clone repo ────────────────────────────────────────────────────────────────
cd /home/ubuntu
if [ ! -d "robinhood-agent" ]; then
    git clone https://github.com/YOUR_USERNAME/robinhood-agent.git
fi
cd robinhood-agent

# ── Python venv ───────────────────────────────────────────────────────────────
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# ── Environment file ─────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  IMPORTANT: Edit your API keys now:"
    echo "    nano /home/ubuntu/robinhood-agent/.env"
    echo ""
    echo "Fill in:"
    echo "  ANTHROPIC_API_KEY"
    echo "  ROBINHOOD_TOKEN"
    echo "  TWILIO_* (for SMS alerts)"
    echo "  EMAIL_* (for email alerts)"
    echo ""
    read -p "Press ENTER after you've saved your .env file..."
fi

# ── Logs dir ──────────────────────────────────────────────────────────────────
mkdir -p logs

# ── Systemd service ───────────────────────────────────────────────────────────
sudo cp trading-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable trading-agent
sudo systemctl start trading-agent

echo ""
echo "✅ Agent is running!"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status trading-agent   # check if running"
echo "  sudo systemctl stop trading-agent     # stop the agent"
echo "  sudo systemctl restart trading-agent  # restart after config change"
echo "  tail -f logs/agent_$(date +%Y-%m-%d).log  # watch live logs"
echo ""
echo "The agent will automatically:"
echo "  ✅ Start on VM reboot"
echo "  ✅ Restart if it crashes"
echo "  ✅ Only trade 9:35am–3:45pm ET Mon–Fri"
echo "  ✅ Text + email you on every trade"
echo "  ✅ Send daily P&L summary at 4pm ET"
