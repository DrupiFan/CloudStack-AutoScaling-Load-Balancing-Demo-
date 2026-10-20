import time
import json
import threading
import os
import random
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
    "mode": "light",        # "light" -> hits /info, "heavy" -> hits /burn (spins CPU)
    "intensity": 1,
    "concurrency": 1,
    "duration_seconds": 60,
    "burn_seconds": 2,      # only used in heavy mode: how long each /burn call spins CPU
    "delay_ms": 0,
    "ramp_seconds": 0,
    "sent": 0,
    "success": 0,
    "errors": 0,
    "last_error": None,
    "stop_flag": False,
    "started_at": None,
    "end_time": None,
    "instance_baseline": 0,  # active backends at test start (from heartbeats)
}

HEARTBEAT_TIMEOUT = 12          # seconds without a heartbeat -> mark instance removed
PURGE_AFTER = 300               # seconds after removal -> drop from list entirely
# Original backends may heartbeat in a few seconds after "Start"; after this, extra
# VMs are scale-out and must not increase total demand.
BASELINE_GRACE_SECONDS = 8
MAX_LOG_ENTRIES = 300
THRESHOLD_LOGGED = False


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
    global THRESHOLD_LOGGED
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
        THRESHOLD_LOGGED = False
        log_event("instance_added", f"🚀 AutoScale Provisioned: New server {name} ({ip}) joined the cluster")
    elif was_removed:
        log_event("instance_added", f"🔄 Server Rejoined: {name} ({ip}) is active again")

    return jsonify({"ok": True})


def sweeper():
    global THRESHOLD_LOGGED
    while True:
        time.sleep(3)
        now = time.time()
        newly_removed = []
        threshold_event = None

        with LOCK:
            for ip, inst in list(INSTANCES.items()):
                if inst["status"] == "active" and now - inst["last_seen"] > HEARTBEAT_TIMEOUT:
                    inst["status"] = "removed"
                    newly_removed.append((inst["name"], ip))
                elif inst["status"] == "removed" and now - inst["last_seen"] > PURGE_AFTER:
                    del INSTANCES[ip]

            # Detect scale threshold crossing (>80% avg CPU)
            active_cpus = [inst["cpu"] for inst in INSTANCES.values() if inst["status"] == "active"]
            if active_cpus:
                avg_cpu = sum(active_cpus) / len(active_cpus)
                if avg_cpu >= 80.0 and not THRESHOLD_LOGGED:
                    THRESHOLD_LOGGED = True
                    threshold_event = round(avg_cpu, 1)
                elif avg_cpu < 65.0:
                    THRESHOLD_LOGGED = False

        # log AFTER releasing LOCK - log_event() acquires LOCK itself,
        # calling it while still holding LOCK above would deadlock
        for name, ip in newly_removed:
            log_event("instance_removed", f"📉 AutoScale Scale-Down: Server {name} ({ip}) decommissioned")

        if threshold_event is not None:
            log_event("threshold_reached", f"⚡ AutoScale Threshold Exceeded: Average cluster CPU reached {threshold_event}% (>80%). Scale-out initiated!")


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


def _active_instances():
    return [inst for inst in INSTANCES.values() if inst["status"] == "active"]


def worker(worker_id, base_target, mode, burn_seconds, delay_ms, ramp_seconds, target_max_load):
    while True:
        with LOCK:
            if LOADTEST_STATE["stop_flag"] or time.time() >= LOADTEST_STATE["end_time"]:
                return
            now = time.time()
            started_at = LOADTEST_STATE["started_at"] or now
            concurrency = LOADTEST_STATE["concurrency"]
            active = _active_instances()
            n_active = len(active)
            baseline = LOADTEST_STATE.get("instance_baseline") or 0
            # Only grow the original set during the first heartbeats. A new VM starts
            # at ~0% CPU and would pull the cluster average down — that used to look
            # like "still warming up" and we raised demand back to 90% per VM.
            if now <= started_at + BASELINE_GRACE_SECONDS:
                baseline = max(baseline, n_active)
                LOADTEST_STATE["instance_baseline"] = baseline
            # LB responses reveal a new backend before (or without) using CPU average
            n_live = max(n_active, len(DISTRIBUTION))
        n_live = max(1, n_live)
        n_baseline = max(1, baseline)

        # Calculate current ramp factor (0.05 to 1.0) for gradual CPU climb
        elapsed = time.time() - (started_at or time.time())
        if ramp_seconds > 0 and elapsed < ramp_seconds:
            progress = elapsed / float(ramp_seconds)
            ramp_factor = max(0.05, min(1.0, progress ** 1.15))
        else:
            ramp_factor = 1.0

        # Size worker count for the original cluster; ramp still activates them gradually
        active_workers = max(1, int(concurrency * n_baseline * ramp_factor))
        if worker_id >= active_workers:
            time.sleep(0.25)
            continue

        if mode == "heavy":
            # Per-VM target if the original set never grew (capped at 90% for headroom)
            cluster_load = max(10, min(90, int(15 + (target_max_load - 15) * ramp_factor)))
            # Total demand = that target × original backends, shared across whoever is
            # alive now — so min=1/2/3 still hit the scale-out threshold, then average
            # CPU drops when CloudStack adds a VM.
            curr_target_load = max(5, min(90, int(cluster_load * n_baseline / n_live)))

            # Probabilistic burn gating: at early ramp stages, send mostly /info (0% CPU)
            # so the cluster warms up smoothly instead of immediately pinning CPU
            burn_probability = min(1.0, max(0.2, ramp_factor * 1.05))
            if random.random() < burn_probability:
                current_burn = max(1, int(burn_seconds * (0.5 + 0.5 * ramp_factor)))
                url = base_target + f"/burn?seconds={current_burn}&load={curr_target_load}"
                req_timeout = current_burn + 8
                extra_delay = int(350 * (1.0 - ramp_factor))
                current_delay = delay_ms + extra_delay
            else:
                # Interspersed light request to keep average CPU within target range
                url = base_target + "/info"
                req_timeout = 5
                extra_delay = int(600 * (1.0 - ramp_factor))
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


def run_loadtest(target, mode, concurrency, burn_seconds, delay_ms, ramp_seconds, target_max_load):
    # Extra threads stay idle unless heartbeats show a larger original cluster
    worker_cap = max(concurrency, min(200, concurrency * 10))
    threads = []
    for wid in range(worker_cap):
        t = threading.Thread(target=worker, args=(wid, target, mode, burn_seconds, delay_ms, ramp_seconds, target_max_load), daemon=True)
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
        # 2: Moderate Traffic (~45% CPU)
        # 3: Heavy Traffic (~70% CPU)
        # 4: Surge Spike (>80% CPU - Triggers AutoScale with smooth gradual ramp, targets ~90%)
        if intensity == 1:
            mode = "light"
            concurrency = 3
            burn_seconds = 1
            delay_ms = 150
            ramp_seconds = 0
            target_max_load = 20
            intensity_name = "Light (Load Balance Demo)"
        elif intensity == 2:
            mode = "heavy"
            concurrency = 3
            burn_seconds = 2
            delay_ms = 300
            ramp_seconds = 15
            target_max_load = 45
            intensity_name = "Moderate Traffic (~45% CPU)"
        elif intensity == 3:
            mode = "heavy"
            concurrency = 4
            burn_seconds = 2
            delay_ms = 150
            ramp_seconds = 20
            target_max_load = 70
            intensity_name = "Heavy Traffic (~70% CPU)"
        else:
            mode = "heavy"
            concurrency = 6
            burn_seconds = 2
            delay_ms = 0
            ramp_seconds = 28
            target_max_load = 90
            intensity_name = "Surge Spike (AutoScale Demo)"
    else:
        # Direct parameter fallback
        mode = data.get("mode") if data.get("mode") in ("light", "heavy") else "light"
        concurrency = max(1, min(200, int(data.get("concurrency", 1))))
        burn_seconds = max(1, min(15, int(data.get("burn_seconds", 2))))
        delay_ms = max(0, min(5000, int(data.get("delay_ms", 0))))
        ramp_seconds = max(0, min(60, int(data.get("ramp_seconds", 20 if mode == "heavy" else 0))))
        target_max_load = int(data.get("target_max_load", 90 if mode == "heavy" else 20))
        intensity = 4 if mode == "heavy" else 1
        intensity_name = f"Custom ({mode})"

    duration_seconds = max(5, min(3600, int(data.get("duration_seconds", 60))))

    now = time.time()
    with LOCK:
        if LOADTEST_STATE["running"]:
            return jsonify({"error": "a load test is already running"}), 400
        n_baseline = sum(1 for inst in INSTANCES.values() if inst["status"] == "active")
        LOADTEST_STATE.update({
            "running": True, "target": target, "mode": mode, "intensity": intensity,
            "concurrency": concurrency, "duration_seconds": duration_seconds,
            "burn_seconds": burn_seconds, "delay_ms": delay_ms, "ramp_seconds": ramp_seconds,
            "sent": 0, "success": 0, "errors": 0, "last_error": None, "stop_flag": False,
            "started_at": now, "end_time": now + duration_seconds,
            "instance_baseline": n_baseline,
        })
        DISTRIBUTION.clear()

    mode_desc = f"{intensity_name} (ramp: {ramp_seconds}s)" if ramp_seconds > 0 else intensity_name
    log_event("loadtest_started",
              f"🚦 Traffic simulation started: {duration_seconds}s duration, mode {mode_desc} -> {target}")
    threading.Thread(target=run_loadtest, args=(target, mode, concurrency, burn_seconds, delay_ms, ramp_seconds, target_max_load),
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
  