#!/bin/bash
# Run ONCE on the monitoring VM, from inside /opt/cloudstack-asg-demo
set -e

apt-get update -y
apt-get install -y python3 python3-venv python3-pip

# cap journald so weeks of request logging can't fill the disk
mkdir -p /etc/systemd/journald.conf.d
echo -e "[Journal]\nSystemMaxUse=200M" > /etc/systemd/journald.conf.d/limit-size.conf
systemctl restart systemd-journald

cd monitor-app
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
cd ..

cp monitor-app/monitor-app.service /etc/systemd/system/monitor-app.service
systemctl daemon-reload
systemctl enable monitor-app
systemctl restart monitor-app

echo "----------------------------------------------------"
echo "Monitoring dashboard installed and started."
echo "Status:  systemctl status monitor-app"
echo "Logs:    journalctl -u monitor-app -f"
echo "From now on, update with: ./scripts/update-monitor.sh"
echo "----------------------------------------------------"
