import time
import json
import threading
import os
from urllib.parse import urlsplit
from flask import Flask, request, jsonify, send_from_directory
import requests

app = Flask(__name__, static_folder='static', static_url_path='')

LOCK = threading.Lock()

# instances registry: keyed by private IP
INSTANCES = {}

EVENT_LOG = []
DISTRIBUTION = {}   # instance_ip -> count of requests answered during load tests

LOADTEST_STATE = {
    "running": False,
    "target": "",
    "mode": "light",        # "light" -> hits /info, "heavy" -> hits /burn (pins CPU)
    "intensity": 1,
    "concurrency": 1,
    "duration_seconds": 60,
    "burn_seconds": 3,      # only used in heavy mode: how long each /burn call spins CPU
    "delay_ms": 0,
    "ramp_seconds": 0,
    "sent": 0,
    "success": 0,
    "errors": 0,
    "last_error": None,
    "stop_flag": False,
    "started_at": None,
    "end_time": None,
}

HEARTBEAT_TIMEOUT = 12          # seconds without a heartbeat -> mark instance removed
PURGE_AFTER = 300               # seconds after removal -> drop from list entirely
MAX_LOG_ENTRIES = 300


def log_event(event_type, message):
    with LOCK:
        EVENT_LOG.append({"ts": time.time(), "type": event_type, "message": message})
        if len(EVENT_LOG) > MAX_LOG_ENTRIES:
            del EVENT_LOG[0: len(EVENT_LOG) - MAX_LOG_ENTRIES]


# ---------------------------------------------------------------------------
# Heartbeat ingestion (pushed by each ASG instance every few seconds)
# ---------------------------------------------------------------------------
@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    data = request.get_json(force=True, silent=True) or {}
    ip = data.get('ip')
    name = data.get('name', ip)
    cpu = float(data.get('cpu', 0))
    ram = float(data.get('ram', 0))
    reqs = int(data.get('request_count', 0))
    if not ip:
        return jsonify({"error": "ip required"}), 400

    is_new = False
    was_removed = False
    with LOCK:
        existing = INSTANCES.get(ip)
        if existing is None:
            is_new = True
        elif existing.get("status") == "removed":
            was_removed = True
        INSTANCES[ip] = {
            "ip": ip,
            "name": name,
            "cpu": round(cpu, 1),
            "ram": round(ram, 1),
            "request_count": reqs,
            "last_seen": time.time(),
            "first_seen": existing.get("first_seen", time.time()) if existing else time.time(),
            "status": "active",
        }

    if is_new:
        log_event("instance_added", f"🚀 AutoScale Provisioned: New server {name} ({ip}) joined the cluster")
    elif was_removed:
        log_event("instance_added", f"🔄 Server Rejoined: {name} ({ip}) is active again")

    return jsonify({"ok": True})


def sweeper():
    while True:
        time.sleep(3)
        now = time.time()
        newly_removed = []
        with LOCK:
            for ip, inst in list(INSTANCES.items()):
                if inst["status"] == "active" and now - inst["last_seen"] > HEARTBEAT_TIMEOUT:
                    inst["status"] = "removed"
                    newly_removed.append((inst["name"], ip))
                elif inst["status"] == "removed" and now - inst["last_seen"] > PURGE_AFTER:
                    del INSTANCES[ip]
        # log AFTER releasing LOCK - log_event() acquires LOCK itself,
        # calling it while still holding LOCK above would deadlock
        for name, ip in newly_removed:
            log_event("instance_removed", f"📉 AutoScale Scale-Down: Server {name} ({ip}) decommissioned")


threading.Thread(target=sweeper, daemon=True).start()


# ---------------------------------------------------------------------------
# Read endpoints for the dashboard UI
# ---------------------------------------------------------------------------
@app.route('/instances')
def get_instances():
    with LOCK:
        return jsonify(sorted(INSTANCES.values(), key=lambda i: i["ip"]))


@app.route('/logs')
def get_logs():
    with LOCK:
        return jsonify(list(reversed(EVENT_LOG[-150:])))


@app.route('/distribution')
def get_distribution():
    with LOCK:
        # only show slices for instances that are still active - once autoscale
        # removes a VM (or it's purged after 5 min), its slice disappears too,
        # instead of lingering forever from earlier in the same load test
        active_names = {inst["name"] for inst in INSTANCES.values() if inst["status"] == "active"}
        filtered = {k: v for k, v in DISTRIBUTION.items() if k in active_names}
        return jsonify(filtered)


@app.route('/loadtest/status')
def loadtest_status():
    with LOCK:
        return jsonify(LOADTEST_STATE)


# ---------------------------------------------------------------------------
# Load generator
# ---------------------------------------------------------------------------
def extract_instance_id(text):
    try:
        d = json.loads(text)
        return d.get("instance_name") or d.get("instance_ip") or "unknown"
    except Exception:
        return "unknown"


def normalize_target(raw):
    """Accepts whatever the user typed - with or without scheme, with or
    without a leftover /info or /burn path - and reduces it to a clean
    scheme://host[:port] base so /info and /burn always resolve correctly."""
    t = (raw or "").strip()
    if not t:
        return t
    if '://' not in t:
        t = 'http://' + t
    parts = urlsplit(t)
    return f"{parts.scheme}://{parts.netloc}"


def worker(worker_id, base_target, mode, burn_seconds, delay_ms, ramp_seconds):
    while True:
        with LOCK:
            if LOADTEST_STATE["stop_flag"] or time.time() >= LOADTEST_STATE["end_time"]:
                return
            started_at = LOADTEST_STATE["started_at"]
            concurrency = LOADTEST_STATE["concurrency"]

        # Calculate current ramp factor (0.05 to 1.0) for gradual CPU climb
        elapsed = time.time() - (started_at or time.time())
        if ramp_seconds > 0 and elapsed < ramp_seconds:
            ramp_factor = max(0.05, min(1.0, elapsed / float(ramp_seconds)))
        else:
            ramp_factor = 1.0

        # Dynamic worker gating: smoothly activate more threads over the ramp period
        active_workers = max(1, int(concurrency * ramp_factor))
        if worker_id >= active_workers:
            time.sleep(0.3)
            continue

        # Dynamic burn time and inter-request delay
        if mode == "heavy":
            # Scale burn from 1s up to burn_seconds
            current_burn = max(1, int(burn_seconds * (0.25 + 0.75 * ramp_factor)))
            url = base_target + f"/burn?seconds={current_burn}"
            req_timeout = current_burn + 8
            # Scale extra delay down from 1000ms to 0ms
            extra_delay = int(1000 * (1.0 - ramp_factor))
            current_delay = delay_ms + extra_delay
        else:
            url = base_target + "/info"
            req_timeout = 5
            current_delay = delay_ms

        with LOCK:
            if LOADTEST_STATE["stop_flag"] or time.time() >= LOADTEST_STATE["end_time"]:
                return
            LOADTEST_STATE["sent"] += 1

        try:
            r = requests.get(url, timeout=req_timeout)
            if r.status_code == 200:
                inst_id = extract_instance_id(r.text)
                with LOCK:
                    LOADTEST_STATE["success"] += 1
                    DISTRIBUTION[inst_id] = DISTRIBUTION.get(inst_id, 0) + 1
            else:
                with LOCK:
                    LOADTEST_STATE["errors"] += 1
                    LOADTEST_STATE["last_error"] = f"HTTP {r.status_code} from {url}"
        except Exception as e:
            with LOCK:
                LOADTEST_STATE["errors"] += 1
                LOADTEST_STATE["last_error"] = f"{type(e).__name__}: {e}"[:200]

        if current_delay > 0:
            time.sleep(current_delay / 1000.0)


def run_loadtest(target, mode, concurrency, burn_seconds, delay_ms, ramp_seconds):
    threads = []
    for wid in range(concurrency):
        t = threading.Thread(target=worker, args=(wid, target, mode, burn_seconds, delay_ms, ramp_seconds), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    with LOCK:
        LOADTEST_STATE["running"] = False
        s, e = LOADTEST_STATE["success"], LOADTEST_STATE["errors"]
    log_event("loadtest_finished", f"✅ Traffic simulation ended: {s} ok / {e} errors")


@app.route('/loadtest/start', methods=['POST'])
def loadtest_start():
    data = request.get_json(force=True, silent=True) or {}
    target = normalize_target(data.get("target"))
    if not target:
        return jsonify({"error": "target URL required"}), 400

    intensity = data.get("intensity")
    if intensity is not None:
        try:
            intensity = int(intensity)
        except (ValueError, TypeError):
            intensity = 4
        # 1: Light (Load Balancing Demo)
        # 2: Moderate Traffic (~40% CPU)
        # 3: Heavy Traffic (~70% CPU)
        # 4: Surge Spike (>90% CPU - Triggers AutoScale with gradual ramp)
        if intensity == 1:
            mode = "light"
            concurrency = 6
            burn_seconds = 1
            delay_ms = 150
            ramp_seconds = 0
            intensity_name = "Light (Load Balance Demo)"
        elif intensity == 2:
            mode = "heavy"
            concurrency = 8
            burn_seconds = 2
            delay_ms = 400
            ramp_seconds = 15
            intensity_name = "Moderate Traffic"
        elif intensity == 3:
            mode = "heavy"
            concurrency = 16
            burn_seconds = 3
            delay_ms = 100
            ramp_seconds = 18
            intensity_name = "Heavy Traffic"
        else:
            mode = "heavy"
            concurrency = 28
            burn_seconds = 4
            delay_ms = 0
            ramp_seconds = 22
            intensity_name = "Surge Spike (AutoScale Demo)"
    else:
        # Direct parameter fallback
        mode = data.get("mode") if data.get("mode") in ("light", "heavy") else "light"
        concurrency = max(1, min(200, int(data.get("concurrency", 1))))
        burn_seconds = max(1, min(15, int(data.get("burn_seconds", 3))))
        delay_ms = max(0, min(5000, int(data.get("delay_ms", 0))))
        ramp_seconds = max(0, min(60, int(data.get("ramp_seconds", 20 if mode == "heavy" else 0))))
        intensity = 4 if mode == "heavy" else 1
        intensity_name = f"Custom ({mode})"

    duration_seconds = max(5, min(3600, int(data.get("duration_seconds", 60))))

    now = time.time()
    with LOCK:
        if LOADTEST_STATE["running"]:
            return jsonify({"error": "a load test is already running"}), 400
        LOADTEST_STATE.update({
            "running": True, "target": target, "mode": mode, "intensity": intensity,
            "concurrency": concurrency, "duration_seconds": duration_seconds,
            "burn_seconds": burn_seconds, "delay_ms": delay_ms, "ramp_seconds": ramp_seconds,
            "sent": 0, "success": 0, "errors": 0, "last_error": None, "stop_flag": False,
            "started_at": now, "end_time": now + duration_seconds,
        })
        DISTRIBUTION.clear()

    mode_desc = f"{intensity_name} (ramp: {ramp_seconds}s)" if ramp_seconds > 0 else intensity_name
    log_event("loadtest_started",
              f"🚦 Traffic simulation started: {duration_seconds}s duration, mode {mode_desc} -> {target}")
    threading.Thread(target=run_loadtest, args=(target, mode, concurrency, burn_seconds, delay_ms, ramp_seconds),
                      daemon=True).start()
    return jsonify({"ok": True})


@app.route('/loadtest/stop', methods=['POST'])
def loadtest_stop():
    with LOCK:
        if not LOADTEST_STATE["running"]:
            return jsonify({"error": "no load test running"}), 400
        LOADTEST_STATE["stop_flag"] = True
        LOADTEST_STATE["running"] = False
    log_event("loadtest_stopped", "⏹️ Traffic simulation stopped manually")
    return jsonify({"ok": True})


@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


if __name__ == '__main__':
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # quiet per-request access log
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
