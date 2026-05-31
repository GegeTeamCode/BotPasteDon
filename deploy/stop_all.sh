#!/bin/bash
# Stop all BotPasteDon services (reverse order)
systemctl stop bot-eldo-scanner
systemctl stop bot-g2g-scanner
systemctl stop bot-coordinator
systemctl stop bot-eldo-worker
systemctl stop bot-g2g-worker
systemctl stop bot-auth
echo "All services stopped."
