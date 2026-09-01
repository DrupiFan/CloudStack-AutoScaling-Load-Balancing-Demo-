#!/bin/bash
# Run this on the monitoring VM any time you want to pull the latest code
# from GitHub and restart the service. This is your new update workflow.
set -e

cd /opt/cloudstack-asg-demo
git pull

cd monitor-app
./venv/bin/pip install -r requirements.txt --quiet
cd ..

systemctl restart monitor-app
echo "Updated and restarted monitor-app."
systemctl status monitor-app --no-pager -l | head -5
