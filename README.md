# CloudStack Auto Scaling & Load Balancing Demo

A small self-contained demo that shows CloudStack's Auto Scaling Group (ASG)
and Load Balancing features working live: instances scaling up/down, and
traffic being spread across whichever instances are currently active.

## What it does

**monitor-app** — a dashboard, meant to run on its own VM:
- Lists every ASG instance currently alive, with live CPU% and RAM%
- Shows a pie chart of how load-generated requests were distributed
  across instances (one color per instance)
- Logs events in real time: instance added, instance removed
  (i.e. scaled up / scaled down), load tests starting/stopping
- Includes a built-in load generator with sliders — concurrency,
  duration, request intensity, and a "heavy" mode that spikes CPU
  on whichever instance answers, so you can actually trigger
  CloudStack's autoscale policy live during a demo

**asg-app** — a tiny app baked into the ASG template, running on every
ASG instance:
- Serves a page identifying itself (instance name, private IP, CPU%,
  RAM%) — refreshing it manually shows the load balancer rotating
  between instances
- `/info` — same data as JSON, used by the load generator to tell
  which instance answered each request
- `/burn?seconds=N` — spins the CPU for N seconds on that instance,
  used by the load generator's "heavy" mode to force a real scale-up
- Pushes a heartbeat (name, IP, CPU%, RAM%) to monitor-app every 4
  seconds

## How instance discovery works (no CloudStack API needed)

This whole demo works without any CloudStack API key. Each ASG instance
*pushes* a heartbeat to the monitor VM. The monitor VM marks an instance
"removed" if no heartbeat arrives for 12 seconds, and fully forgets it
after 5 minutes. That's what makes scale-up/scale-down show up on the
dashboard automatically — no polling CloudStack required.

Instance identity is based on private IP (not hostname), since
cloud-init/userdata isn't relied on anywhere in this setup.

## Repo layout

```
monitor-app/       Dashboard app (Flask) — runs on the monitor VM
  app.py
  requirements.txt
  static/           index.html, logo.png, chart.umd.min.js (all local, no CDN)
  monitor-app.service
asg-app/            Per-instance demo app (Flask) — baked into the ASG template
  app.py
  requirements.txt
  asg-app.service
scripts/
  setup-monitor.sh  One-time install, monitor VM
  setup-asg.sh      One-time install, ASG golden VM
  update-monitor.sh Day-to-day update, monitor VM
  update-asg.sh     Day-to-day update, ASG golden VM
```

## Prerequisites

Before deploying, you should already have in CloudStack:
- A VPC with a tier/network for the ASG (e.g. `10.10.1.0/24`)
- A CloudStack Auto Scaling Group configured on that tier
- A Load Balancing rule (port 80) with a public IP pointed at the ASG
- Ubuntu 24.04 LTS as the base template/image

You'll additionally acquire **one more public IP** for the monitor VM
(separate from the ASG's LB IP), since you need to reach the dashboard
and SSH into it independently.

## Deployment

### 1. Monitor VM — create and expose it

1. **Instances → Add Instance**: same zone, Ubuntu 24.04 LTS template,
   network = the same tier as your ASG (e.g. `10.10.1.0/24`).
2. **Acquire a public IP** for it (Network → IP Addresses → Acquire New IP).
3. **Port forwarding** on that IP, pointed at the monitor VM:
   - Public `2222` → Private `22` (TCP) — SSH
   - Public `8080` → Private `5000` (TCP) — dashboard
4. **Network ACL**: allow ingress TCP `2222` and `8080` on the tier
   (from your IP, or `0.0.0.0/0` if you don't need to restrict it).
5. **Get SSH working** (since cloud-init/userdata isn't used here):
   open the VM's console (View Console), log in, install and enable SSH:
   ```bash
   sudo apt update
   sudo apt install -y openssh-server
   sudo systemctl enable --now ssh
   sudo passwd ubuntu
   sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
   sudo systemctl restart ssh
   ```
6. Confirm from your machine (or jump server, if that's your access path):
   ```bash
   ssh -p 2222 ubuntu@<MONITOR_PUBLIC_IP>
   ```

### 2. Monitor VM — deploy the app

```bash
sudo apt-get install -y git
cd /opt
sudo git clone <YOUR_REPO_URL> cloudstack-asg-demo
cd cloudstack-asg-demo
sudo bash scripts/setup-monitor.sh
```

This installs Python deps into a venv, registers `monitor-app` as a
systemd service, caps journald so weeks of logs can't fill the disk,
and starts it.

Check it's up: `http://<MONITOR_PUBLIC_IP>:8080/`
Note the monitor VM's **private IP** (`ip a`) — you'll need it next.

### 3. Build the ASG template

1. Deploy a temporary "golden" VM on the same tier, same Ubuntu 24.04
   base template your ASG uses. Get SSH access the same way as step 1.5.
2. Deploy the app:
   ```bash
   sudo apt-get install -y git
   cd /opt
   sudo git clone <YOUR_REPO_URL> cloudstack-asg-demo
   cd cloudstack-asg-demo
   sudo bash scripts/setup-asg.sh <MONITOR_VM_PRIVATE_IP>
   ```
3. Verify it's heartbeating — check `http://<MONITOR_PUBLIC_IP>:8080/`,
   this VM should appear as an active instance.
4. **Stop** the VM, then **Create Template** from it (Instances → Stop
   → Create Template, or via its root volume under Storage → Volumes).
5. Once the template is `Ready`, destroy the temp VM.

### 4. Point the ASG at the new template

CloudStack won't let you edit a template on an existing AutoScale VM
Profile in place:
1. Disable the AutoScale VM Group.
2. Delete the old VM Profile, create a new one using your new template
   (same service offering/network), attach it to the group.
3. Re-enable the AutoScale VM Group.

New instances will appear on the dashboard automatically as CloudStack
spins them up.

## Day-to-day updates

Edit code locally → push to GitHub → then on the relevant VM:

```bash
cd /opt/cloudstack-asg-demo
sudo bash scripts/update-monitor.sh   # monitor VM
# or
sudo bash scripts/update-asg.sh       # ASG golden VM, before re-templating
```

Each script does `git pull`, reinstalls deps if `requirements.txt`
changed, and restarts the service. Note: this only updates the golden
VM you run it on — already-running ASG instances are unaffected until
you re-template and swap the VM Profile (step 4 above).

## Using the dashboard

- **Instance cards** — live CPU/RAM per instance, fades when an
  instance stops heartbeating, disappears 5 minutes after removal.
- **Load Generator** — set the Target field to your ASG's public LB IP
  (no path, e.g. `http://203.0.113.50`), pick Light (`/info`, cheap,
  good for pure LB-distribution demos) or Heavy (`/burn`, pins CPU,
  use this to actually trigger autoscale), tune concurrency/duration/
  burn intensity, hit Start.
- **Distribution pie chart** — updates live as requests land, colored
  per instance.
- **Event log** — scroll to see instance added/removed and load test
  start/stop events as they happen.

## Environment-specific quirks baked into this setup

- Routes deliberately avoid the `/api/` prefix — this network's edge
  filtering blocks that pattern on inbound traffic.
- `chart.umd.min.js` is vendored locally in `monitor-app/static/`
  rather than loaded from a CDN, since outbound access to cdnjs is
  also blocked here.
- Werkzeug's per-request access log is silenced and journald is capped
  at 200MB in both apps, so this can run unattended for weeks without
  filling disk.
