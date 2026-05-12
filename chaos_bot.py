"""
chaos_bot.py
────────────
External chaos traffic generator for the Flask Timesheet App.
Targets all /chaos/* endpoints + regular app endpoints to simulate:
  - Slow response times
  - HTTP 4xx / 5xx errors
  - Database-level chaos (slow queries, connection failures, deadlocks, floods)
  - CPU and memory spikes
  - Auth abuse (bad logins, duplicate signups)
  - Malformed payloads → type errors in DB inserts

Run:
    python chaos_bot.py [--url http://host] [--intensity low|medium|high] [--workers 3]

Stop with Ctrl+C — prints a summary report.
"""

import requests
import time
import threading
import random
import argparse
import sys
from datetime import datetime
from collections import defaultdict

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
DEFAULT_BASE_URL   = "http://192.168.6.65"
ADMIN_USER         = "admin"
ADMIN_PASS         = "admin"
NORMAL_USER        = "testuser"
NORMAL_PASS        = "testpass"

# Stats tracker
stats = defaultdict(int)
stats_lock = threading.Lock()

def record(event: str):
    with stats_lock:
        stats[event] += 1

# ─────────────────────────────────────────────────────────────
# Shared authenticated sessions
# ─────────────────────────────────────────────────────────────
_admin_session  = None
_user_session   = None
_session_lock   = threading.Lock()

def get_admin_session(base_url: str) -> requests.Session:
    global _admin_session
    with _session_lock:
        if _admin_session is None:
            s = requests.Session()
            try:
                s.post(f"{base_url}/login",
                       data={"username": ADMIN_USER, "password": ADMIN_PASS},
                       timeout=10)
                _admin_session = s
            except Exception as e:
                print(f"[WARN] Admin login failed: {e}")
                _admin_session = s
    return _admin_session

def get_user_session(base_url: str) -> requests.Session:
    global _user_session
    with _session_lock:
        if _user_session is None:
            s = requests.Session()
            try:
                # Try to create the user first
                s.post(f"{base_url}/signup",
                       data={"username": NORMAL_USER, "password": NORMAL_PASS},
                       timeout=10)
                s.post(f"{base_url}/login",
                       data={"username": NORMAL_USER, "password": NORMAL_PASS},
                       timeout=10)
                _user_session = s
            except Exception as e:
                print(f"[WARN] User session setup failed: {e}")
                _user_session = s
    return _user_session


# ─────────────────────────────────────────────────────────────
# ░░ APP-LEVEL CHAOS SCENARIOS ░░
# ─────────────────────────────────────────────────────────────

def scenario_slow_response(base_url: str):
    """Hit /chaos/delay to inject 3–8 second response latency."""
    ms = random.randint(3000, 8000)
    try:
        r = requests.get(f"{base_url}/chaos/delay?ms={ms}", timeout=15)
        record("slow_response_ok" if r.status_code == 200 else "slow_response_error")
        print(f"  [SLOW]    /chaos/delay?ms={ms} → {r.status_code}")
    except requests.exceptions.Timeout:
        record("slow_response_timeout")
        print(f"  [SLOW]    /chaos/delay?ms={ms} → TIMEOUT (expected)")
    except Exception as e:
        record("slow_response_exception")
        print(f"  [SLOW]    error: {e}")


def scenario_enable_global_latency(base_url: str):
    """Enables persistent 1–4s latency on ALL requests."""
    ms = random.choice([1000, 1500, 2000, 3000, 4000])
    try:
        r = requests.get(f"{base_url}/chaos/latency?ms={ms}", timeout=10)
        record("global_latency_set")
        print(f"  [LATENCY] Global latency set to {ms}ms → {r.status_code}")
    except Exception as e:
        record("global_latency_exception")
        print(f"  [LATENCY] error: {e}")


def scenario_disable_global_latency(base_url: str):
    """Turns off global latency."""
    try:
        r = requests.get(f"{base_url}/chaos/latency?disable=true", timeout=10)
        record("global_latency_disabled")
        print(f"  [LATENCY] Global latency disabled → {r.status_code}")
    except Exception as e:
        print(f"  [LATENCY] disable error: {e}")


def scenario_random_5xx_rate(base_url: str):
    """Enables random 5xx injection at 20–60% rate."""
    rate = round(random.uniform(0.2, 0.6), 2)
    try:
        r = requests.get(f"{base_url}/chaos/error?rate={rate}", timeout=10)
        record("error_rate_set")
        print(f"  [5XX]     Error rate set to {rate*100:.0f}% → {r.status_code}")
    except Exception as e:
        record("error_rate_exception")
        print(f"  [5XX]     error: {e}")


def scenario_disable_error_rate(base_url: str):
    """Turns off random error injection."""
    try:
        requests.get(f"{base_url}/chaos/error?disable=true", timeout=10)
        record("error_rate_disabled")
        print(f"  [5XX]     Error rate disabled")
    except Exception as e:
        print(f"  [5XX]     disable error: {e}")


def scenario_force_http_error(base_url: str):
    """Hits forced error endpoints: /chaos/400, 404, 500, 503, /chaos/exception."""
    endpoints = [
        ("/chaos/400", 400),
        ("/chaos/404", 404),
        ("/chaos/500", 500),
        ("/chaos/503", 503),
        ("/chaos/exception", 500),
    ]
    ep, expected = random.choice(endpoints)
    try:
        r = requests.get(f"{base_url}{ep}", timeout=10)
        key = f"force_{expected}_{'ok' if r.status_code == expected else 'unexpected'}"
        record(key)
        print(f"  [HTTP]    {ep} → {r.status_code} (expected {expected})")
    except Exception as e:
        record("force_http_exception")
        print(f"  [HTTP]    {ep} → exception: {e}")


def scenario_cpu_spike(base_url: str):
    """Burns CPU server-side for 5–15 seconds."""
    secs = random.randint(5, 15)
    try:
        r = requests.get(f"{base_url}/chaos/cpu?seconds={secs}", timeout=secs + 10)
        record("cpu_spike_ok")
        print(f"  [CPU]     cpu spike {secs}s → {r.status_code}")
    except requests.exceptions.Timeout:
        record("cpu_spike_timeout")
        print(f"  [CPU]     cpu spike → TIMEOUT after {secs+10}s")
    except Exception as e:
        record("cpu_spike_exception")
        print(f"  [CPU]     error: {e}")


def scenario_memory_spike(base_url: str):
    """Allocates 128–512MB server-side."""
    mb = random.choice([128, 256, 384, 512])
    hold = random.randint(3, 8)
    try:
        r = requests.get(f"{base_url}/chaos/memory?mb={mb}&hold={hold}", timeout=hold + 15)
        record("memory_spike_ok")
        print(f"  [MEM]     memory spike {mb}MB held {hold}s → {r.status_code}")
    except requests.exceptions.Timeout:
        record("memory_spike_timeout")
        print(f"  [MEM]     memory spike → TIMEOUT")
    except Exception as e:
        record("memory_spike_exception")
        print(f"  [MEM]     error: {e}")


def scenario_malformed_save(base_url: str):
    """Posts a bad payload to /save_timesheet → type error in DB → 500."""
    sess = get_admin_session(base_url)
    payload = {
        "week": f"2025-W{random.randint(1,52):02d}",
        "rows": [{
            "project": "ChaosProject",
            "task": "ChaosTask",
            "test_case_id": "TC-CHAOS",
            "mon": "not_a_number",   # intentionally bad type
            "tue": "NaN",
            "wed": None,
            "thu": 0, "fri": 0, "sat": 0, "sun": 0,
            "total": "INVALID"
        }]
    }
    try:
        r = sess.post(f"{base_url}/save_timesheet", json=payload, timeout=10)
        record(f"malformed_save_{r.status_code}")
        print(f"  [BAD]     malformed save → {r.status_code}")
    except Exception as e:
        record("malformed_save_exception")
        print(f"  [BAD]     error: {e}")


def scenario_auth_abuse(base_url: str):
    """Spams wrong credentials and duplicate signup → 4xx-like responses."""
    fake_users = [f"hacker_{random.randint(1000,9999)}" for _ in range(3)]
    for u in fake_users:
        try:
            r = requests.post(f"{base_url}/login",
                              data={"username": u, "password": "wrongpass"},
                              timeout=5)
            record(f"auth_abuse_{r.status_code}")
        except Exception:
            record("auth_abuse_exception")
    # Try to register 'admin' (duplicate)
    try:
        r = requests.post(f"{base_url}/signup",
                          data={"username": "admin", "password": "admin"},
                          timeout=5)
        record(f"dup_signup_{r.status_code}")
        print(f"  [AUTH]    {len(fake_users)} bad logins + 1 dup signup fired")
    except Exception:
        pass


def scenario_rapid_requests(base_url: str):
    """Fires 20–50 rapid GET requests to the home page to spike request rate."""
    n = random.randint(20, 50)
    sess = get_user_session(base_url)
    week = f"2025-W{random.randint(1,52):02d}"
    ok = err = 0
    for _ in range(n):
        try:
            r = sess.get(f"{base_url}/?week={week}", timeout=5)
            if r.status_code == 200:
                ok += 1
            else:
                err += 1
        except Exception:
            err += 1
    record("rapid_requests_ok")
    print(f"  [FLOOD]   {n} rapid GETs → {ok} ok / {err} err")


def scenario_cascade_failure(base_url: str):
    """Triggers the cascade endpoint (DB slow → app delay → 503)."""
    try:
        r = requests.get(f"{base_url}/chaos/cascade", timeout=15)
        record(f"cascade_{r.status_code}")
        print(f"  [CASCADE] cascade failure → {r.status_code}")
    except requests.exceptions.Timeout:
        record("cascade_timeout")
        print(f"  [CASCADE] cascade → TIMEOUT")
    except Exception as e:
        print(f"  [CASCADE] error: {e}")


# ─────────────────────────────────────────────────────────────
# ░░ DATABASE-LEVEL CHAOS SCENARIOS ░░
# ─────────────────────────────────────────────────────────────

def scenario_db_slow_query(base_url: str):
    """Runs pg_sleep() on the real DB to simulate slow query."""
    ms = random.choice([1500, 2000, 3000, 4000, 5000])
    try:
        r = requests.get(f"{base_url}/chaos/db/slow?ms={ms}", timeout=ms/1000 + 10)
        record(f"db_slow_{r.status_code}")
        print(f"  [DB-SLOW] pg_sleep({ms}ms) → {r.status_code}")
    except requests.exceptions.Timeout:
        record("db_slow_timeout")
        print(f"  [DB-SLOW] pg_sleep({ms}ms) → TIMEOUT")
    except Exception as e:
        record("db_slow_exception")
        print(f"  [DB-SLOW] error: {e}")


def scenario_db_connection_fail(base_url: str):
    """Forces a failed DB connection attempt → OperationalError."""
    try:
        r = requests.get(f"{base_url}/chaos/db/fail", timeout=10)
        record(f"db_connfail_{r.status_code}")
        print(f"  [DB-FAIL] connection failure → {r.status_code}")
    except Exception as e:
        record("db_connfail_exception")
        print(f"  [DB-FAIL] error: {e}")


def scenario_db_connection_leak(base_url: str):
    """Leaks DB connections to exhaust the pool."""
    count = random.randint(10, 25)
    try:
        r = requests.get(f"{base_url}/chaos/db/leak?count={count}", timeout=20)
        record(f"db_leak_{r.status_code}")
        print(f"  [DB-LEAK] {count} connections leaked → {r.status_code}")
    except requests.exceptions.Timeout:
        record("db_leak_timeout")
        print(f"  [DB-LEAK] {count} connections → TIMEOUT")
    except Exception as e:
        record("db_leak_exception")
        print(f"  [DB-LEAK] error: {e}")


def scenario_db_query_flood(base_url: str):
    """Fires 100–300 SELECT queries rapidly to stress the DB."""
    n = random.randint(100, 300)
    try:
        r = requests.get(f"{base_url}/chaos/db/flood?queries={n}", timeout=30)
        record(f"db_flood_{r.status_code}")
        print(f"  [DB-FLOOD] {n} queries → {r.status_code}")
    except requests.exceptions.Timeout:
        record("db_flood_timeout")
        print(f"  [DB-FLOOD] {n} queries → TIMEOUT")
    except Exception as e:
        record("db_flood_exception")
        print(f"  [DB-FLOOD] error: {e}")


def scenario_db_deadlock(base_url: str):
    """Triggers a deliberate DB deadlock scenario."""
    try:
        r = requests.get(f"{base_url}/chaos/db/deadlock", timeout=20)
        record(f"db_deadlock_{r.status_code}")
        print(f"  [DEADLOCK] deadlock attempt → {r.status_code}")
    except Exception as e:
        record("db_deadlock_exception")
        print(f"  [DEADLOCK] error: {e}")


def scenario_db_bad_query(base_url: str):
    """Fires a malformed SQL query → ProgrammingError."""
    try:
        r = requests.get(f"{base_url}/chaos/db/badquery", timeout=10)
        record(f"db_badquery_{r.status_code}")
        print(f"  [DB-SQL]  bad query → {r.status_code}")
    except Exception as e:
        record("db_badquery_exception")
        print(f"  [DB-SQL]  error: {e}")


# ─────────────────────────────────────────────────────────────
# ░░ CHAOS RESET ░░
# ─────────────────────────────────────────────────────────────

def scenario_reset(base_url: str):
    """Resets all global chaos state."""
    try:
        r = requests.get(f"{base_url}/chaos/reset", timeout=10)
        record("reset_ok")
        print(f"  [RESET]   chaos state reset → {r.status_code}")
    except Exception as e:
        print(f"  [RESET]   error: {e}")


# ─────────────────────────────────────────────────────────────
# Scenario catalogue by intensity
# ─────────────────────────────────────────────────────────────

APP_SCENARIOS = [
    scenario_slow_response,
    scenario_enable_global_latency,
    scenario_disable_global_latency,
    scenario_random_5xx_rate,
    scenario_disable_error_rate,
    scenario_force_http_error,
    scenario_malformed_save,
    scenario_auth_abuse,
    scenario_rapid_requests,
    scenario_cascade_failure,
]

DB_SCENARIOS = [
    scenario_db_slow_query,
    scenario_db_connection_fail,
    scenario_db_query_flood,
    scenario_db_bad_query,
    scenario_db_deadlock,
    scenario_db_connection_leak,
]

# Heavy scenarios only run in medium/high intensity
HEAVY_SCENARIOS = [
    scenario_cpu_spike,
    scenario_memory_spike,
    scenario_db_connection_leak,
    scenario_db_deadlock,
]

INTENSITY_CONFIG = {
    "low":    {"sleep_min": 2.0, "sleep_max": 5.0,  "heavy": False},
    "medium": {"sleep_min": 0.8, "sleep_max": 2.5,  "heavy": True},
    "high":   {"sleep_min": 0.2, "sleep_max": 0.8,  "heavy": True},
}


# ─────────────────────────────────────────────────────────────
# Orchestrator thread
# ─────────────────────────────────────────────────────────────

def orchestrator(base_url: str, intensity: str, worker_id: int):
    cfg = INTENSITY_CONFIG[intensity]
    pool = APP_SCENARIOS + DB_SCENARIOS
    if cfg["heavy"]:
        pool = pool + HEAVY_SCENARIOS

    # Occasionally reset to give APM breathing room
    reset_every = random.randint(15, 30)
    call_count = 0

    while True:
        scenario = random.choice(pool)
        tag = scenario.__name__.replace("scenario_", "").upper()
        print(f"[W{worker_id}] → {tag}")
        try:
            scenario(base_url)
        except Exception as e:
            print(f"[W{worker_id}]   Unhandled: {e}")
            record("unhandled_exception")

        call_count += 1
        if call_count % reset_every == 0:
            scenario_reset(base_url)
            reset_every = random.randint(15, 30)

        sleep = random.uniform(cfg["sleep_min"], cfg["sleep_max"])
        time.sleep(sleep)


# ─────────────────────────────────────────────────────────────
# Summary report
# ─────────────────────────────────────────────────────────────

def print_summary(start_time: float):
    elapsed = time.time() - start_time
    print("\n" + "═" * 60)
    print(f"  CHAOS BOT SUMMARY  |  Duration: {elapsed:.1f}s")
    print("═" * 60)
    total = sum(stats.values())
    print(f"  Total events recorded: {total}")
    print()
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        bar = "█" * min(v, 40)
        print(f"  {k:<35} {v:>5}  {bar}")
    print("═" * 60)


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Chaos Bot — APM stress generator for Flask Timesheet App"
    )
    parser.add_argument("--url",       default=DEFAULT_BASE_URL,
                        help=f"Base URL of the app (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--intensity", default="medium",
                        choices=["low", "medium", "high"],
                        help="Chaos intensity level (default: medium)")
    parser.add_argument("--workers",   default=3, type=int,
                        help="Number of concurrent chaos worker threads (default: 3)")
    args = parser.parse_args()

    base_url  = args.url.rstrip("/")
    intensity = args.intensity
    workers   = args.workers

    print("╔══════════════════════════════════════════════════════╗")
    print("║        CHAOS BOT — APM Stress Generator              ║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  Target    : {base_url:<40}║")
    print(f"║  Intensity : {intensity:<40}║")
    print(f"║  Workers   : {workers:<40}║")
    print(f"║  Started   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<40}║")
    print("╚══════════════════════════════════════════════════════╝")
    print("  Press Ctrl+C to stop and see summary.\n")

    # Pre-warm sessions
    print("[INIT] Setting up authenticated sessions...")
    get_admin_session(base_url)
    get_user_session(base_url)
    print("[INIT] Sessions ready.\n")

    start_time = time.time()
    threads = []
    for i in range(1, workers + 1):
        t = threading.Thread(
            target=orchestrator,
            args=(base_url, intensity, i),
            daemon=True,
            name=f"ChaosWorker-{i}"
        )
        t.start()
        threads.append(t)
        time.sleep(0.2)  # stagger workers slightly

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[STOP] Stopping chaos...")
        # Reset app state before quitting
        try:
            requests.get(f"{base_url}/chaos/reset", timeout=5)
            print("[STOP] Chaos state reset on server.")
        except Exception:
            pass
        print_summary(start_time)
        sys.exit(0)


if __name__ == "__main__":
    main()
