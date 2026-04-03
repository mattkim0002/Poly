#!/bin/bash
# Start bot + dashboard together
# Usage: bash start.sh [live|paper]
#   live  = real money trading
#   paper = simulated trades (default)

cd "$(dirname "$0")"

# Kill any existing instances
echo "Stopping existing processes..."
pkill -f 'python3.*main.py' 2>/dev/null
pkill -f 'python3.*dashboard.py' 2>/dev/null
sleep 1

# Set paper/live mode
MODE="${1:-paper}"
if [ "$MODE" = "live" ]; then
    export PAPER_TRADING=false
    echo "*** LIVE TRADING MODE — REAL MONEY ***"
else
    export PAPER_TRADING=true
    echo "*** PAPER TRADING MODE — NO REAL MONEY ***"
fi

# Install deps if needed
pip3 install -q flask tradingview-ta 2>/dev/null

# Start bot
echo "Starting bot..."
nohup python3 main.py > bot.log 2>&1 &
BOT_PID=$!
echo "Bot PID: $BOT_PID"

# Start dashboard
echo "Starting dashboard on port 8080..."
nohup python3 dashboard.py > dash.log 2>&1 &
DASH_PID=$!
echo "Dashboard PID: $DASH_PID"

sleep 2

# Verify
if kill -0 $BOT_PID 2>/dev/null; then
    echo "Bot is running"
else
    echo "ERROR: Bot failed to start. Check bot.log:"
    tail -20 bot.log
fi

if kill -0 $DASH_PID 2>/dev/null; then
    echo "Dashboard is running at http://$(hostname -I | awk '{print $1}'):8080"
else
    echo "ERROR: Dashboard failed to start. Check dash.log:"
    tail -20 dash.log
fi

echo ""
echo "Commands:"
echo "  tail -f bot.log      # Watch bot logs"
echo "  tail -f dash.log     # Watch dashboard logs"
echo "  bash start.sh live   # Restart in live mode"
echo "  bash start.sh paper  # Restart in paper mode"
