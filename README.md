# CloudStack ASG / LB Demo

Two small Flask apps:

- **monitor-app** — dashboard VM. Shows live ASG instances (CPU/RAM), a
  pie chart of request distribution across instances, an event log
  (instance added/removed), and a load generator with sliders.
- **asg-app** — runs on every ASG instance (baked into the template).
  Serves a page identifying itself (name/IP/CPU/RAM), plus `/info` (JSON)
  and `/burn?seconds=N` (CPU spike, for triggering autoscale) endpoints.
  Pushes a heartbeat to monitor-app every 4 seconds.

No CloudStack API access is used - instance discovery is entirely via
heartbeat push, so scale up/down shows up on the dashboard automatically
whether triggered by CloudStack's AutoScale policy or done manually.

## One-time setup

### Monitoring VM
```bash
cd /opt
sudo git clone <YOUR_REPO_URL> cloudstack-asg-demo
cd cloudstack-asg-demo
sudo ./scripts/setup-monitor.sh
```
Dashboard will be reachable at `http://<MONITOR_PUBLIC_IP>:<your forwarded port>/`.

### ASG golden VM (before creating the template)
```bash
cd /opt
sudo git clone <YOUR_REPO_URL> cloudstack-asg-demo
cd cloudstack-asg-demo
sudo ./scripts/setup-asg.sh <MONITOR_VM_PRIVATE_IP>
```
Verify it's heartbeating (`http://<MONITOR_PUBLIC_IP>:PORT/instances` should
show it), then stop the VM and create your CloudStack template from it as usual.

## Day-to-day updates (the new workflow)

Whenever you push a change to GitHub:

**Monitor VM:**
```bash
cd /opt/cloudstack-asg-demo
sudo ./scripts/update-monitor.sh
```

**ASG golden VM** (only matters if you're about to re-template - live
running ASG instances won't pick this up on their own, CloudStack doesn't
push code to running VMs):
```bash
cd /opt/cloudstack-asg-demo
sudo ./scripts/update-asg.sh
```
Then stop the VM, create a new template from it, and swap the AutoScale
VM Profile over to the new template (profile's template can't be edited
in place - you recreate the profile pointing at the new template).

## Notes

- Routes intentionally avoid the `/api/` prefix - this environment's
  network path blocks that pattern at the edge.
- `chart.umd.min.js` is vendored locally in `monitor-app/static/` (not
  loaded from a CDN) because outbound access to cdnjs is also blocked
  on this network.
- journald is capped and Werkzeug's per-request access log is silenced
  in `monitor-app/app.py` / `asg-app/app.py` to avoid disk growth over
  weeks of uptime.
