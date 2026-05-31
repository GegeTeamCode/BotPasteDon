#!/bin/bash
# Start all BotPasteDon services (6 processes)
# Order: Auth → Workers → Coordinator → Scanners
systemctl start bot-auth
sleep 3
systemctl start bot-eldo-worker
systemctl start bot-g2g-worker
sleep 2
systemctl start bot-coordinator
sleep 2
systemctl start bot-eldo-scanner
systemctl start bot-g2g-scanner
echo "All services started. Status:"
systemctl status bot-auth bot-coordinator bot-*-scanner bot-*-worker --no-pager -l
