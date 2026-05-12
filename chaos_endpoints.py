"""
chaos_endpoints.py
──────────────────
Flask Blueprint that exposes /chaos/* routes for APM chaos testing.
Register in app.py with:

    from chaos_endpoints import chaos_bp
    app.register_blueprint(chaos_bp)

WARNING: Never expose these routes in production.
"""

from flask import Blueprint, jsonify, request
import time
import random
import threading
import psycopg2
import os

chaos_bp = Blueprint('chaos', __name__)

# ─── Shared state ──────────────────────────────────────────────
_chaos_state = {
    "slow_mode":      False,   # globally slow all DB queries
    "error_rate":     0.0,     # 0.0–1.0 fraction of requests to randomly 500
    "latency_ms":     0,       # extra ms injected into every response
    "db_fail":        False,   # simulate DB connection failure
    "db_slow_ms":     0,       # add sleep to every DB query
}

DB_URL = os.environ.get('DATABASE_URL','postgresql://timesheet:admin@192.168.6.65:5433/timesheet')


# ─── Middleware hook (call this from app.py before_request) ────
def chaos_middleware():
    """
    Call inside @app.before_request in app.py:
        app.before_request(chaos_middleware)
    """
    state = _chaos_state
    # Random error injection
    if state["error_rate"] > 0 and random.random() < state["error_rate"]:
        code = random.choice([500, 502, 503, 504])
        return jsonify({
            "error": "Chaos-injected failure",
            "chaos": True,
            "code": code
        }), code

    # Response latency injection
    if state["latency_ms"] > 0:
        time.sleep(state["latency_ms"] / 1000)


# ─── /chaos/status ────────────────────────────────────────────
@chaos_bp.route('/chaos/status')
def chaos_status():
    """Returns current chaos configuration."""
    return jsonify(_chaos_state)


# ─── /chaos/reset ─────────────────────────────────────────────
@chaos_bp.route('/chaos/reset')
def chaos_reset():
    """Resets all chaos to off."""
    _chaos_state.update({
        "slow_mode":  False,
        "error_rate": 0.0,
        "latency_ms": 0,
        "db_fail":    False,
        "db_slow_ms": 0,
    })
    return jsonify({"status": "reset", "state": _chaos_state})


# ─── /chaos/delay?ms=3000 ─────────────────────────────────────
@chaos_bp.route('/chaos/delay')
def chaos_delay():
    """
    Injects a one-shot sleep into THIS request.
    ?ms=<milliseconds>   default = 5000
    Also sets global latency_ms for subsequent requests.
    """
    ms = int(request.args.get('ms', 5000))
    _chaos_state["latency_ms"] = ms
    time.sleep(ms / 1000)
    return jsonify({
        "chaos": "delay",
        "injected_ms": ms,
        "message": f"Slept {ms}ms. Global latency now set to {ms}ms."
    })


# ─── /chaos/latency?ms=1500&disable=false ────────────────────
@chaos_bp.route('/chaos/latency')
def chaos_latency():
    """
    Persistently adds latency to every response.
    ?ms=<ms>       set global latency
    ?disable=true  turn off
    """
    if request.args.get('disable', 'false').lower() == 'true':
        _chaos_state["latency_ms"] = 0
        return jsonify({"chaos": "latency_disabled"})
    ms = int(request.args.get('ms', 2000))
    _chaos_state["latency_ms"] = ms
    return jsonify({"chaos": "latency_enabled", "latency_ms": ms})


# ─── /chaos/error?rate=0.5 ────────────────────────────────────
@chaos_bp.route('/chaos/error')
def chaos_error():
    """
    Sets global random 5xx injection rate.
    ?rate=0.5  → 50% of requests fail with random 5xx
    ?disable   → turn off
    """
    if request.args.get('disable', 'false').lower() == 'true':
        _chaos_state["error_rate"] = 0.0
        return jsonify({"chaos": "error_rate_disabled"})
    rate = float(request.args.get('rate', 0.3))
    rate = max(0.0, min(1.0, rate))
    _chaos_state["error_rate"] = rate
    return jsonify({"chaos": "error_rate_set", "rate": rate})


# ─── /chaos/500 ───────────────────────────────────────────────
@chaos_bp.route('/chaos/500')
def chaos_500():
    """Always returns HTTP 500."""
    return jsonify({"chaos": "forced_500", "error": "Internal Server Error"}), 500


# ─── /chaos/503 ───────────────────────────────────────────────
@chaos_bp.route('/chaos/503')
def chaos_503():
    """Simulates service unavailable."""
    return jsonify({"chaos": "forced_503", "error": "Service Unavailable"}), 503


# ─── /chaos/404 ───────────────────────────────────────────────
@chaos_bp.route('/chaos/404')
def chaos_404():
    """Forces a 404 Not Found."""
    return jsonify({"chaos": "forced_404", "error": "Not Found"}), 404


# ─── /chaos/400 ───────────────────────────────────────────────
@chaos_bp.route('/chaos/400')
def chaos_400():
    """Forces a 400 Bad Request."""
    return jsonify({"chaos": "forced_400", "error": "Bad Request"}), 400


# ─── /chaos/exception ─────────────────────────────────────────
@chaos_bp.route('/chaos/exception')
def chaos_exception():
    """Raises an unhandled Python exception → triggers global error handler → 500."""
    raise RuntimeError("Chaos: deliberately raised unhandled exception for APM detection")


# ─── /chaos/cpu ───────────────────────────────────────────────
@chaos_bp.route('/chaos/cpu')
def chaos_cpu():
    """
    Burns CPU for a configurable duration.
    ?seconds=10  (default 10)
    """
    duration = int(request.args.get('seconds', 10))
    end = time.time() + duration
    count = 0
    while time.time() < end:
        # Pure CPU burn: floating point arithmetic in tight loop
        _ = sum(i * i for i in range(10_000))
        count += 1
    return jsonify({"chaos": "cpu_spike", "duration_seconds": duration, "iterations": count})


# ─── /chaos/memory ────────────────────────────────────────────
@chaos_bp.route('/chaos/memory')
def chaos_memory():
    """
    Allocates a large list in memory and holds it briefly.
    ?mb=256  (default 256 MB)
    ?hold=5  (seconds to hold, default 5)
    """
    mb = int(request.args.get('mb', 256))
    hold = int(request.args.get('hold', 5))
    # Allocate ~mb megabytes (each int ~ 28 bytes, 1MB ~ 37,000 ints)
    chunk_size = mb * 37_000
    blob = [random.randint(0, 2**32) for _ in range(chunk_size)]
    time.sleep(hold)
    size = len(blob)
    del blob  # release
    return jsonify({"chaos": "memory_spike", "allocated_mb": mb, "held_seconds": hold, "items": size})


# ─── /chaos/db/slow?ms=3000 ───────────────────────────────────
@chaos_bp.route('/chaos/db/slow')
def chaos_db_slow():
    """
    Runs a real pg_sleep() on the database to simulate slow queries.
    ?ms=3000    sleep duration in ms (default 3000)
    Also sets global db_slow_ms for the middleware hook.
    """
    ms = int(request.args.get('ms', 3000))
    seconds = ms / 1000
    _chaos_state["db_slow_ms"] = ms
    try:
        conn = psycopg2.connect(DB_URL)
        with conn.cursor() as cur:
            cur.execute(f"SELECT pg_sleep({seconds})")  # Real DB-side sleep
        conn.close()
        return jsonify({
            "chaos": "db_slow_query",
            "pg_sleep_seconds": seconds,
            "global_db_slow_ms": ms
        })
    except Exception as e:
        return jsonify({"chaos": "db_slow_query", "error": str(e)}), 500


# ─── /chaos/db/fail ───────────────────────────────────────────
@chaos_bp.route('/chaos/db/fail')
def chaos_db_fail():
    """
    Attempts to connect to a deliberately wrong DB host to generate
    psycopg2.OperationalError (connection failure / timeout).
    """
    _chaos_state["db_fail"] = True
    try:
        bad_conn = psycopg2.connect(
            "postgresql://nobody:wrong@127.0.0.1:9999/nonexistent",
            connect_timeout=3
        )
        bad_conn.close()
        return jsonify({"chaos": "db_fail", "result": "unexpectedly succeeded"})
    except psycopg2.OperationalError as e:
        return jsonify({
            "chaos": "db_connection_failure",
            "error": str(e),
            "message": "Intentional DB connection failure for APM detection"
        }), 503
    finally:
        _chaos_state["db_fail"] = False


# ─── /chaos/db/leak ───────────────────────────────────────────
@chaos_bp.route('/chaos/db/leak')
def chaos_db_leak():
    """
    Opens multiple DB connections without closing them → connection pool exhaustion.
    ?count=20  (default 20 leaked connections)
    """
    count = int(request.args.get('count', 20))
    leaked = []
    errors = 0
    for i in range(count):
        try:
            conn = psycopg2.connect(DB_URL, connect_timeout=3)
            leaked.append(conn)  # deliberately not closed
        except Exception:
            errors += 1
    # Hold for 5s then release
    time.sleep(5)
    for c in leaked:
        try:
            c.close()
        except Exception:
            pass
    return jsonify({
        "chaos": "db_connection_leak",
        "requested": count,
        "leaked": len(leaked),
        "errors": errors,
        "held_seconds": 5
    })


# ─── /chaos/db/flood ──────────────────────────────────────────
@chaos_bp.route('/chaos/db/flood')
def chaos_db_flood():
    """
    Fires many rapid SELECT queries to flood the DB with read load.
    ?queries=200  (default 200 queries)
    """
    n = int(request.args.get('queries', 200))
    errors = 0
    start = time.time()
    try:
        conn = psycopg2.connect(DB_URL)
        with conn.cursor() as cur:
            for _ in range(n):
                try:
                    cur.execute("SELECT * FROM timesheet ORDER BY random() LIMIT 50")
                    cur.fetchall()
                except Exception:
                    errors += 1
        conn.close()
    except Exception as e:
        return jsonify({"chaos": "db_flood", "error": str(e)}), 500

    elapsed = round(time.time() - start, 3)
    return jsonify({
        "chaos": "db_query_flood",
        "queries_fired": n,
        "errors": errors,
        "elapsed_seconds": elapsed
    })


# ─── /chaos/db/deadlock ───────────────────────────────────────
@chaos_bp.route('/chaos/db/deadlock')
def chaos_db_deadlock():
    """
    Creates a deliberate deadlock scenario using two concurrent transactions.
    APM should detect the rollback / lock wait.
    """
    results = {"chaos": "db_deadlock_attempt", "threads": []}

    def txn_a(out):
        try:
            conn = psycopg2.connect(DB_URL)
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("UPDATE timesheet SET total = total + 0 WHERE id = (SELECT MIN(id) FROM timesheet)")
                time.sleep(1)  # Hold lock
                cur.execute("UPDATE timesheet SET total = total + 0 WHERE id = (SELECT MAX(id) FROM timesheet)")
            conn.commit()
            out.append({"txn": "A", "result": "committed"})
        except Exception as e:
            out.append({"txn": "A", "result": "rollback", "error": str(e)})
        finally:
            try: conn.close()
            except: pass

    def txn_b(out):
        try:
            conn = psycopg2.connect(DB_URL)
            conn.autocommit = False
            with conn.cursor() as cur:
                time.sleep(0.3)  # Slight offset to ensure both acquire first lock
                cur.execute("UPDATE timesheet SET total = total + 0 WHERE id = (SELECT MAX(id) FROM timesheet)")
                time.sleep(1)
                cur.execute("UPDATE timesheet SET total = total + 0 WHERE id = (SELECT MIN(id) FROM timesheet)")
            conn.commit()
            out.append({"txn": "B", "result": "committed"})
        except Exception as e:
            out.append({"txn": "B", "result": "rollback", "error": str(e)})
        finally:
            try: conn.close()
            except: pass

    out = []
    ta = threading.Thread(target=txn_a, args=(out,))
    tb = threading.Thread(target=txn_b, args=(out,))
    ta.start(); tb.start()
    ta.join(timeout=10); tb.join(timeout=10)
    results["threads"] = out
    return jsonify(results)


# ─── /chaos/db/badquery ───────────────────────────────────────
@chaos_bp.route('/chaos/db/badquery')
def chaos_db_badquery():
    """Fires an intentionally malformed SQL query → DB error."""
    try:
        conn = psycopg2.connect(DB_URL)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM this_table_does_not_exist_chaos_test")
        conn.close()
        return jsonify({"chaos": "badquery", "result": "unexpected_success"})
    except psycopg2.ProgrammingError as e:
        return jsonify({
            "chaos": "db_bad_query",
            "error": str(e),
            "message": "Intentional SQL error for APM detection"
        }), 500
    except Exception as e:
        return jsonify({"chaos": "db_bad_query", "error": str(e)}), 500


# ─── /chaos/cascade ───────────────────────────────────────────
@chaos_bp.route('/chaos/cascade')
def chaos_cascade():
    """
    Triggers a cascading failure scenario:
    DB slow query → high latency → timeout → 503.
    Mimics real-world cascading service degradation for APM.
    """
    try:
        conn = psycopg2.connect(DB_URL)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_sleep(4)")  # Simulate slow DB
        conn.close()
        time.sleep(2)  # App-level delay on top
        # Simulate the app deciding to return 503 due to latency budget exceeded
        return jsonify({
            "chaos": "cascade_failure",
            "db_sleep_s": 4,
            "app_delay_s": 2,
            "result": "Latency budget exceeded → returning 503"
        }), 503
    except Exception as e:
        return jsonify({"chaos": "cascade_failure", "error": str(e)}), 500
