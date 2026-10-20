import socket
import time
import threading
import os
import multiprocessing
import math
from flask import Flask, jsonify, request as flask_req
import psutil
import requests

app = Flask(__name__)

# The monitor VM's PRIVATE IP + port, injected via systemd Environment=
MONITOR_URL = os.environ.get("MONITOR_URL", "http://10.10.1.10:5000/heartbeat")


def get_private_ip():
    """Discover this VM's own private IP at runtime.
    We do NOT rely on hostname because cloud-init isn't setting unique
    hostnames per clone - the private IP (DHCP-assigned by the
    virtual router) is guaranteed unique per instance."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.10.1.1", 80))  # tier gateway, doesn't need to answer
        ip = s.getsockname()[0]
    except Exception:
        ip = socket.gethostbyname(socket.gethostname())
    finally:
        s.close()
    return ip


INSTANCE_IP = get_private_ip()
INSTANCE_NAME = f"vm-{INSTANCE_IP.split('.')[-1]}"
REQUEST_COUNT = 0
LOCK = threading.Lock()

LATEST_CPU = 0.0
LATEST_RAM = 0.0

# ---------------------------------------------------------------------------
# Multi-core CPU Burner
# Burns across all available CPU cores (e.g. 2 cores, 4 cores) using dedicated
# child processes to bypass Python's GIL. Duty-cycles in 100ms periods so CPU
# hits the requested percentage (e.g. ~88-92%) without pinning 100%.
# ---------------------------------------------------------------------------
CPU_CORES = os.cpu_count() or 1
BURN_LOAD = multiprocessing.Value('d', 0.0)
BURN_UNTIL = multiprocessing.Value('d', 0.0)


def _core_burner_worker(burn_load, burn_until):
    """Worker process that duty-cycles a single CPU core."""
    period = 0.1  # 100ms slice
    while True:
        target = burn_load.value
        until = burn_until.value
        now = time.time()
        if now < until and target > 0:
            target_pct = min(92.0, max(5.0, target))
            spin_time = period * (target_pct / 100.0)
            sleep_time = period * (1.0 - target_pct / 100.0)
            t_end = time.time() + spin_time
            while time.time() < t_end:
                for _ in range(500):
                    math.sqrt(987654321.123)
            if sleep_time > 0.002:
                time.sleep(sleep_time)
        else:
            time.sleep(0.05)


def start_burners():
    for _ in range(CPU_CORES):
        p = multiprocessing.Process(target=_core_burner_worker, args=(BURN_LOAD, BURN_UNTIL))
        p.daemon = True
        p.start()


try:
    start_burners()
except Exception:
    pass


@app.route('/')
def index():
    global REQUEST_COUNT
    with LOCK:
        REQUEST_COUNT += 1
        count = REQUEST_COUNT
        cpu = LATEST_CPU
        ram = LATEST_RAM

    cpu_offset = max(0, min(110, 110 - (cpu / 100.0) * 110))
    ram_offset = max(0, min(110, 110 - (ram / 100.0) * 110))
    cpu_color = "#168351" if cpu < 60 else ("#d98728" if cpu < 80 else "#0085ed")
    ram_color = "#0085ed" if ram < 70 else ("#d98728" if ram < 85 else "#173db3")

    html = f"""<!doctype html>
<html><head><title>UGT Cloud — {INSTANCE_NAME}</title>
<meta http-equiv="refresh" content="3">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f5f8fc;color:#12203b;
     display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;box-sizing:border-box}}
.card{{background:#ffffff;padding:36px 44px;border-radius:18px;text-align:center;
      box-shadow:0 10px 30px rgba(15,23,42,.08);border:1px solid #e1e8f2;max-width:440px;width:100%}}
.brand{{font-size:12px;font-weight:700;color:#0085ed;letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px}}
h1{{color:#12203b;margin:0 0 8px;font-size:26px;font-weight:800}}
.badge{{display:inline-block;background:#eef6ff;color:#0085ed;border:1px solid #bfdbfe;padding:4px 14px;
       border-radius:20px;font-weight:700;font-size:13px;margin-bottom:24px}}
.gauges{{display:flex;justify-content:center;gap:20px;margin-bottom:20px}}
.gauge-box{{flex:1;background:#fafbfd;border:1px solid #e1e8f2;border-radius:14px;padding:12px 8px}}
.gauge-svg{{width:100%;max-width:120px;height:auto;display:block;margin:0 auto}}
.reqs-box{{background:#f0f4fa;border-radius:10px;padding:12px;font-size:14px;color:#62718a;font-weight:500}}
.reqs-box b{{color:#0085ed;font-size:18px;font-weight:800}}
.pulse{{display:inline-block;width:8px;height:8px;border-radius:50%;background:#168351;margin-right:6px;box-shadow:0 0 8px #168351}}
</style></head>
<body><div class="card">
<div class="brand">UGT Cloud — Auto Scaling Instance</div>
<h1>{INSTANCE_NAME}</h1>
<div class="badge">{INSTANCE_IP}</div>

<div class="gauges">
  <div class="gauge-box">
    <svg viewBox="0 0 100 65" class="gauge-svg">
      <path d="M 15 55 A 35 35 0 0 1 85 55" fill="none" stroke="#e2e8f0" stroke-width="8" stroke-linecap="round" />
      <path d="M 15 55 A 35 35 0 0 1 85 55" fill="none" stroke="{cpu_color}" stroke-width="8" stroke-linecap="round"
            stroke-dasharray="110" stroke-dashoffset="{cpu_offset}" style="transition:stroke-dashoffset .4s ease" />
      <text x="50" y="48" text-anchor="middle" font-size="15" font-weight="800" fill="#12203b">{cpu}%</text>
      <text x="50" y="60" text-anchor="middle" font-size="7.5" font-weight="700" fill="#62718a">CPU SPEED</text>
    </svg>
  </div>
  <div class="gauge-box">
    <svg viewBox="0 0 100 65" class="gauge-svg">
      <path d="M 15 55 A 35 35 0 0 1 85 55" fill="none" stroke="#e2e8f0" stroke-width="8" stroke-linecap="round" />
      <path d="M 15 55 A 35 35 0 0 1 85 55" fill="none" stroke="{ram_color}" stroke-width="8" stroke-linecap="round"
            stroke-dasharray="110" stroke-dashoffset="{ram_offset}" style="transition:stroke-dashoffset .4s ease" />
      <text x="50" y="48" text-anchor="middle" font-size="15" font-weight="800" fill="#12203b">{ram}%</text>
      <text x="50" y="60" text-anchor="middle" font-size="7.5" font-weight="700" fill="#62718a">RAM USAGE</text>
    </svg>
  </div>
</div>

<div class="reqs-box">
  <div><span class="pulse"></span>Total Requests Served</div>
  <div style="margin-top:4px"><b>{count}</b></div>
</div>
</div></body></html>"""
    return html


@app.route('/info')
def api_info():
    """JSON endpoint - used by the monitoring VM's load generator to identify
    which backend instance answered each request."""
    global REQUEST_COUNT
    with LOCK:
        REQUEST_COUNT += 1
        count = REQUEST_COUNT
        cpu = LATEST_CPU
        ram = LATEST_RAM
    return jsonify({
        "instance_ip": INSTANCE_IP,
        "instance_name": INSTANCE_NAME,
        "cpu": cpu,
        "ram": ram,
        "request_count": count
    })


@app.route('/burn')
def api_burn():
    """Spin CPU with duty-cycling across all cores so CPU load matches the requested level."""
    try:
        seconds = min(15, max(1, int(flask_req.args.get("seconds", 2))))
    except Exception:
        seconds = 2
    try:
        load = min(92, max(5, int(flask_req.args.get("load", 88))))
    except Exception:
        load = 88

    with LOCK:
        BURN_LOAD.value = float(load)
        BURN_UNTIL.value = max(BURN_UNTIL.value, time.time() + seconds)

    time.sleep(seconds)
    return jsonify({"burned_seconds": seconds, "load": load, "instance_name": INSTANCE_NAME})


def heartbeat_loop():
    global LATEST_CPU, LATEST_RAM
    psutil.cpu_percent(interval=None)  # prime psutil initial counter
    while True:
        time.sleep(3)
        try:
            # interval=None accurately averages CPU utilization across the full 3s interval
            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory().percent
            with LOCK:
                LATEST_CPU = round(cpu, 1)
                LATEST_RAM = round(ram, 1)
                count = REQUEST_COUNT
            requests.post(MONITOR_URL, json={
                "ip": INSTANCE_IP,
                "name": INSTANCE_NAME,
                "cpu": LATEST_CPU,
                "ram": LATEST_RAM,
                "request_count": count
            }, timeout=3)
        except Exception:
            pass


threading.Thread(target=heartbeat_loop, daemon=True).start()

if __name__ == '__main__':
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # quiet per-request access log
    app.run(host='0.0.0.0', port=80, threaded=True)
