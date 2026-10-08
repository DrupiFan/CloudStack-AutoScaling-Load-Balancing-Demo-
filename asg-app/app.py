import socket
import time
import threading
import os
from flask import Flask, jsonify
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


@app.route('/')
def index():
    global REQUEST_COUNT
    with LOCK:
        REQUEST_COUNT += 1
        count = REQUEST_COUNT
    cpu = psutil.cpu_percent(interval=0.15)
    ram = psutil.virtual_memory().percent

    cpu_offset = max(0, min(110, 110 - (cpu / 100.0) * 110))
    ram_offset = max(0, min(110, 110 - (ram / 100.0) * 110))
    cpu_color = "#198754" if cpu < 60 else ("#f59e0b" if cpu < 80 else "#0080eb")
    ram_color = "#0080eb" if ram < 70 else ("#f59e0b" if ram < 85 else "#153bb6")

    html = f"""<!doctype html>
<html><head><title>UGT Cloud — {INSTANCE_NAME}</title>
<meta http-equiv="refresh" content="3">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f4f6fa;color:#1e293b;
     display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;box-sizing:border-box}}
.card{{background:#ffffff;padding:36px 44px;border-radius:18px;text-align:center;
      box-shadow:0 10px 30px rgba(15,23,42,.08);border:1px solid #dee4f3;max-width:440px;width:100%}}
.brand{{font-size:12px;font-weight:700;color:#0080eb;letter-spacing:.1em;text-transform:uppercase;margin-bottom:8px}}
h1{{color:#1e293b;margin:0 0 8px;font-size:26px;font-weight:800}}
.badge{{display:inline-block;background:#eef6ff;color:#0080eb;border:1px solid #bfdbfe;padding:4px 14px;
       border-radius:20px;font-weight:700;font-size:13px;margin-bottom:24px}}
.gauges{{display:flex;justify-content:center;gap:20px;margin-bottom:20px}}
.gauge-box{{flex:1;background:#f8f9fd;border:1px solid #dee4f3;border-radius:14px;padding:12px 8px}}
.gauge-svg{{width:100%;max-width:120px;height:auto;display:block;margin:0 auto}}
.reqs-box{{background:#f0f3fa;border-radius:10px;padding:12px;font-size:14px;color:#57647c;font-weight:500}}
.reqs-box b{{color:#0080eb;font-size:18px;font-weight:800}}
.pulse{{display:inline-block;width:8px;height:8px;border-radius:50%;background:#198754;margin-right:6px;box-shadow:0 0 8px #198754}}
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
      <text x="50" y="48" text-anchor="middle" font-size="15" font-weight="800" fill="#1e293b">{cpu}%</text>
      <text x="50" y="60" text-anchor="middle" font-size="7.5" font-weight="700" fill="#64748b">CPU SPEED</text>
    </svg>
  </div>
  <div class="gauge-box">
    <svg viewBox="0 0 100 65" class="gauge-svg">
      <path d="M 15 55 A 35 35 0 0 1 85 55" fill="none" stroke="#e2e8f0" stroke-width="8" stroke-linecap="round" />
      <path d="M 15 55 A 35 35 0 0 1 85 55" fill="none" stroke="{ram_color}" stroke-width="8" stroke-linecap="round"
            stroke-dasharray="110" stroke-dashoffset="{ram_offset}" style="transition:stroke-dashoffset .4s ease" />
      <text x="50" y="48" text-anchor="middle" font-size="15" font-weight="800" fill="#1e293b">{ram}%</text>
      <text x="50" y="60" text-anchor="middle" font-size="7.5" font-weight="700" fill="#64748b">RAM USAGE</text>
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
    cpu = psutil.cpu_percent(interval=0.15)
    ram = psutil.virtual_memory().percent
    return jsonify({
        "instance_ip": INSTANCE_IP,
        "instance_name": INSTANCE_NAME,
        "cpu": cpu,
        "ram": ram,
        "request_count": count
    })


@app.route('/burn')
def api_burn():
    """Spin CPU with duty-cycling so CPU load matches the requested level."""
    from flask import request as freq
    try:
        seconds = min(15, max(1, int(freq.args.get("seconds", 2))))
    except Exception:
        seconds = 2
    try:
        load = min(100, max(5, int(freq.args.get("load", 100))))
    except Exception:
        load = 100

    end = time.time() + seconds
    # Duty-cycling over 50ms periods to cleanly target desired CPU% on low-spec cores
    slice_spin = 0.05 * (load / 100.0)
    slice_sleep = 0.05 * (1.0 - (load / 100.0))

    while time.time() < end:
        chunk_end = time.time() + slice_spin
        while time.time() < chunk_end:
            pow(999999, 999, 7919)
        if slice_sleep > 0.002:
            time.sleep(slice_sleep)

    return jsonify({"burned_seconds": seconds, "load": load, "instance_name": INSTANCE_NAME})


def heartbeat_loop():
    psutil.cpu_percent(interval=None)  # prime psutil initial counter
    while True:
        time.sleep(3)
        try:
            # interval=None accurately averages CPU utilization across the full 3s interval
            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory().percent
            with LOCK:
                count = REQUEST_COUNT
            requests.post(MONITOR_URL, json={
                "ip": INSTANCE_IP,
                "name": INSTANCE_NAME,
                "cpu": round(cpu, 1),
                "ram": round(ram, 1),
                "request_count": count
            }, timeout=3)
        except Exception:
            pass


threading.Thread(target=heartbeat_loop, daemon=True).start()

if __name__ == '__main__':
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # quiet per-request access log
    app.run(host='0.0.0.0', port=80, threaded=True)
