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
    hostnames per clone - the private IP (DHCP-assigned by the CloudStack
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
    html = f"""<!doctype html>
<html><head><title>{INSTANCE_NAME}</title>
<meta http-equiv="refresh" content="3">
<style>
body{{font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;
     display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{background:#1e293b;padding:40px 60px;border-radius:16px;text-align:center;
      box-shadow:0 8px 24px rgba(0,0,0,.4);border:1px solid #334155}}
h1{{color:#38bdf8;margin-bottom:6px}}
.badge{{display:inline-block;background:#38bdf8;color:#0f172a;padding:4px 14px;
       border-radius:20px;font-weight:bold;font-size:13px;margin-bottom:18px}}
.metric{{margin:10px 0;font-size:17px}}
.metric b{{color:#fbbf24}}
</style></head>
<body><div class="card">
<h1>{INSTANCE_NAME}</h1>
<div class="badge">{INSTANCE_IP}</div>
<div class="metric">CPU load: <b>{cpu}%</b></div>
<div class="metric">RAM load: <b>{ram}%</b></div>
<div class="metric">Requests served by this instance: <b>{count}</b></div>
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
    """Briefly spin the CPU so the demo can show a real load spike.
    ?seconds=N (default 2, capped at 15 to match the dashboard's burn slider)."""
    try:
        seconds = min(15, max(1, int(request_args_seconds())))
    except Exception:
        seconds = 2
    end = time.time() + seconds
    while time.time() < end:
        pow(999999, 999, 7919)
    return jsonify({"burned_seconds": seconds, "instance_name": INSTANCE_NAME})


def request_args_seconds():
    from flask import request as freq
    return freq.args.get("seconds", 2)


def heartbeat_loop():
    while True:
        try:
            cpu = psutil.cpu_percent(interval=0.5)
            ram = psutil.virtual_memory().percent
            with LOCK:
                count = REQUEST_COUNT
            requests.post(MONITOR_URL, json={
                "ip": INSTANCE_IP,
                "name": INSTANCE_NAME,
                "cpu": cpu,
                "ram": ram,
                "request_count": count
            }, timeout=3)
        except Exception:
            pass
        time.sleep(4)


threading.Thread(target=heartbeat_loop, daemon=True).start()

if __name__ == '__main__':
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # quiet per-request access log
    app.run(host='0.0.0.0', port=80, threaded=True)
