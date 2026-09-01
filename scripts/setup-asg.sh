#!/bin/bash
# Run ONCE on the ASG golden VM (before you snapshot it into a template),
# from inside /opt/cloudstack-asg-demo
#
# USAGE: ./scripts/setup-asg.sh 10.10.1.X   (monitor VM's PRIVATE IP)
set -e

MONITOR_IP="$1"
if [ -z "$MONITOR_IP" ]; then
  echo "Usage: ./scripts/setup-asg.sh <MONITOR_VM_PRIVATE_IP>"
  exit 1
fi

apt-get update -y
apt-get install -y python3 python3-venv python3-pip

mkdir -p /etc/systemd/journald.conf.d
echo -e "[Journal]\nSystemMaxUse=200M" > /etc/systemd/journald.conf.d/limit-size.conf
systemctl restart systemd-journald

cd asg-app
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
cd ..

sed "s/MONITOR_PRIVATE_IP/${MONITOR_IP}/" asg-app/asg-app.service > /etc/systemd/system/asg-app.service

systemctl daemon-reload
systemctl enable asg-app
systemctl restart asg-app

echo "----------------------------------------------------"
echo "ASG demo app installed, reporting to ${MONITOR_IP}:5000"
echo "Status:  systemctl status asg-app"
echo "Logs:    journalctl -u asg-app -f"
echo "----------------------------------------------------"
