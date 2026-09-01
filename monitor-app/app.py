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
    "concurrency": 1,
    "duration_seconds": 60,
    "burn_seconds": 3,      # only used in heavy mode: how long each /burn call spins CPU
    "delay_ms": 0,
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
            "status": "active",
        }

    if is_new:
        log_event("instance_added", f"New ASG instance detected: {name} ({ip})")
    elif was_removed:
        log_event("instance_added", f"Instance rejoined: {name} ({ip})")

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
            log_event("instance_removed", f"Instance lost / scaled down: {name} ({ip})")


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


def worker(base_target, mode, burn_seconds, delay_ms):
    if mode == "heavy":
        url = base_target + f"/burn?seconds={burn_seconds}"
        req_timeout = burn_seconds + 8
    else:
        url = base_target + "/info"
        req_timeout = 5

    while True:
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
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)


def run_loadtest(target, mode, concurrency, burn_seconds, delay_ms):
    threads = []
    for _ in range(concurrency):
        t = threading.Thread(target=worker, args=(target, mode, burn_seconds, delay_ms), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    with LOCK:
        LOADTEST_STATE["running"] = False
        s, e = LOADTEST_STATE["success"], LOADTEST_STATE["errors"]
    log_event("loadtest_finished", f"Load test finished: {s} ok / {e} errors")


@app.route('/loadtest/start', methods=['POST'])
def loadtest_start():
    data = request.get_json(force=True, silent=True) or {}
    target = normalize_target(data.get("target"))
    mode = data.get("mode") if data.get("mode") in ("light", "heavy") else "light"
    concurrency = max(1, min(200, int(data.get("concurrency", 1))))
    duration_seconds = max(5, min(3600, int(data.get("duration_seconds", 60))))
    burn_seconds = max(1, min(15, int(data.get("burn_seconds", 3))))
    delay_ms = max(0, min(5000, int(data.get("delay_ms", 0))))

    if not target:
        return jsonify({"error": "target URL required"}), 400

    now = time.time()
    with LOCK:
        if LOADTEST_STATE["running"]:
            return jsonify({"error": "a load test is already running"}), 400
        LOADTEST_STATE.update({
            "running": True, "target": target, "mode": mode, "concurrency": concurrency,
            "duration_seconds": duration_seconds, "burn_seconds": burn_seconds, "delay_ms": delay_ms,
            "sent": 0, "success": 0, "errors": 0, "last_error": None, "stop_flag": False,
            "started_at": now, "end_time": now + duration_seconds,
        })
        DISTRIBUTION.clear()

    mode_desc = f"HEAVY (CPU burn {burn_seconds}s/req)" if mode == "heavy" else "light (/info)"
    log_event("loadtest_started",
              f"Load test started: {duration_seconds}s duration, concurrency {concurrency}, "
              f"mode {mode_desc}, delay {delay_ms}ms -> {target}")
    threading.Thread(target=run_loadtest, args=(target, mode, concurrency, burn_seconds, delay_ms),
                      daemon=True).start()
    return jsonify({"ok": True})


@app.route('/loadtest/stop', methods=['POST'])
def loadtest_stop():
    with LOCK:
        if not LOADTEST_STATE["running"]:
            return jsonify({"error": "no load test running"}), 400
        LOADTEST_STATE["stop_flag"] = True
        LOADTEST_STATE["running"] = False
    log_event("loadtest_stopped", "Load test stopped by user")
    return jsonify({"ok": True})


@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


if __name__ == '__main__':
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # quiet per-request access log
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
