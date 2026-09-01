#!/bin/bash
# Run this on the ASG golden VM any time you want to pull the latest code
# before re-creating the AutoScale template. Existing live ASG instances
# won't get this automatically - you still need to re-template + swap the
# AutoScale VM Profile for changes to reach them (see README.md).
set -e

cd /opt/cloudstack-asg-demo
git pull

cd asg-app
./venv/bin/pip install -r requirements.txt --quiet
cd ..

systemctl restart asg-app
echo "Updated and restarted asg-app."
systemctl status asg-app --no-pager -l | head -5
