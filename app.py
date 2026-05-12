from flask import Flask, render_template, request, redirect, session, jsonify
import psycopg2
import psycopg2.extras
import logging
from logging.handlers import RotatingFileHandler
import os
import json
import traceback
from collections import defaultdict

# --- CHAOS ENGINEERING ---
from chaos_endpoints import chaos_bp, chaos_middleware

app = Flask(__name__)
app.secret_key = "secret123"
app.register_blueprint(chaos_bp)  # Chaos engineering endpoints (/chaos/*)

# --- LOGGING SETUP ---
if not os.path.exists('logs'):
    os.makedirs('logs')

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "pathname": record.pathname,
            "lineno": record.lineno,
        }
        if record.exc_info:
            log_record["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log_record)

logger = logging.getLogger('timesheet_app')
logger.setLevel(logging.INFO)
handler = RotatingFileHandler('logs/app.log', maxBytes=10000000, backupCount=5)
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)

@app.before_request
def log_request_info():
    logger.info(f"API Request started: {request.method} {request.url}")

@app.before_request
def run_chaos_middleware():
    """Inject chaos (latency / random errors) on every request if enabled."""
    if request.path.startswith('/chaos/'):
        return  # never block chaos-control endpoints themselves
    return chaos_middleware()

@app.after_request
def log_response_info(response):
    logger.info(f"API Request completed: {request.method} {request.url} - Status: {response.status_code}")
    return response

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Application breakdown / Unhandled Exception: {str(e)}", exc_info=True)
    return jsonify({
        "error": "Internal Server Error",
        "message": "An unexpected error occurred. Error logged for Dynatrace monitoring."
    }), 500
# --- END LOGGING SETUP ---

DB_URL = os.environ.get('DATABASE_URL','postgresql://timesheet:admin@192.168.6.65:5433/timesheet')

def get_db():
    conn = psycopg2.connect(DB_URL)
    return conn

# INIT DB
def init_db():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT DEFAULT 'user'
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS timesheet (
            id SERIAL PRIMARY KEY,
            resource TEXT,
            project TEXT,
            task TEXT,
            week TEXT,
            mon REAL,
            tue REAL,
            wed REAL,
            thu REAL,
            fri REAL,
            sat REAL,
            sun REAL,
            total REAL
        )
        """)
        # Safely add test_case_id column if it doesn't exist
        try:
            cur.execute("ALTER TABLE timesheet ADD COLUMN test_case_id TEXT")
        except Exception:
            conn.rollback()  # Column already exists, rollback and continue

        cur.execute("INSERT INTO users (username, password, role) VALUES ('admin', 'admin', 'admin') ON CONFLICT (username) DO NOTHING")
    conn.commit()
    conn.close()

try:
    init_db()
except Exception as e:
    logger.error(f"Failed to initialize db: {e}")

# SIGNUP
@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        try:
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (username,password) VALUES (%s,%s)",
                    (request.form['username'], request.form['password'])
                )
            conn.commit()
            conn.close()
            logger.info(f"New user successfully signed up: {request.form['username']}")
            return redirect('/login')
        except psycopg2.IntegrityError:
            logger.warning(f"Signup failed. User already exists: {request.form['username']}")
            return render_template('signup.html', error="User already exists")
        except Exception as e:
            logger.error(f"Error during signup: {str(e)}", exc_info=True)
            raise
    return render_template('signup.html')

# LOGIN
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        conn = get_db()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM users WHERE username=%s AND password=%s",
                (username, request.form['password'])
            )
            user = cur.fetchone()
        conn.close()

        if user:
            session['user'] = user['username']
            session['role'] = user['role']
            logger.info(f"User logged in successfully: {username} (Role: {user['role']})")
            return redirect('/admin' if user['role']=='admin' else '/')

        logger.warning(f"Failed login attempt for username: {username}")
        return render_template('login.html', error="Invalid username or password")
    return render_template('login.html')

# LOGOUT
@app.route('/logout')
def logout():
    user = session.get('user', 'Unknown')
    session.clear()
    logger.info(f"User logged out: {user}")
    return redirect('/login')

# USER DASHBOARD — no server-side project filter; all data grouped by project
@app.route('/')
def index():
    if not session.get('user'):
        return redirect('/login')
    if session.get('role') == 'admin':
        return redirect('/admin')

    user = session.get('user')
    week = request.args.get('week', '')

    conn = get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT * FROM timesheet WHERE resource=%s AND week=%s ORDER BY project, id",
            (user, week)
        )
        rows = cur.fetchall()
    conn.close()

    # Group by project
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['project']].append(dict(row))

    return render_template('index.html', grouped=dict(grouped), week=week)

# ADMIN DASHBOARD
@app.route('/admin')
def admin():
    if session.get('role') != 'admin':
        logger.warning(f"Unauthorized admin access attempt by user: {session.get('user')}")
        return redirect('/login')

    resource = request.args.get('resource', '')
    week = request.args.get('week', '')
    project = request.args.get('project', '')

    conn = get_db()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT username FROM users WHERE role='user'")
        users = cur.fetchall()

        query = "SELECT * FROM timesheet WHERE 1=1"
        params = []

        if resource:
            query += " AND resource=%s"
            params.append(resource)
        if week:
            query += " AND week=%s"
            params.append(week)
        if project:
            query += " AND project ILIKE %s"
            params.append(f'%{project}%')

        query += " ORDER BY resource, project, id"
        cur.execute(query, params)
        data = cur.fetchall()
    conn.close()

    return render_template('admin.html', data=data, users=users,
                           selected_resource=resource,
                           selected_week=week,
                           selected_project=project)

# SAVE TIMESHEET
@app.route('/save_timesheet', methods=['POST'])
def save_timesheet():
    if not session.get('user'):
        logger.warning("Unauthenticated save_timesheet attempt.")
        return jsonify({"status":"error", "message":"Unauthenticated"}), 401

    try:
        data = request.get_json()
        user = session.get('user')
        week = data.get('week')

        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM timesheet WHERE resource=%s AND week=%s", (user, week))

            row_count = 0
            for row in data.get('rows', []):
                cur.execute("""
                INSERT INTO timesheet
                (resource, project, task, test_case_id, week, mon, tue, wed, thu, fri, sat, sun, total)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    user,
                    row['project'],
                    row['task'],
                    row.get('test_case_id', ''),
                    week,
                    row['mon'],
                    row['tue'],
                    row['wed'],
                    row['thu'],
                    row['fri'],
                    row['sat'],
                    row['sun'],
                    row['total']
                ))
                row_count += 1

        conn.commit()
        conn.close()

        logger.info(f"Timesheet saved successfully for user: {user}, week: {week}. Total entries: {row_count}")
        return jsonify({"status":"saved"})

    except Exception as e:
        logger.error(f"Error saving timesheet for user {session.get('user')}: {str(e)}", exc_info=True)
        return jsonify({"status":"error", "message": "Failed to save timesheet"}), 500

# DELETE TIMESHEET RECORD (Admin only)
@app.route('/delete/<int:id>', methods=['POST'])
def delete_record(id):
    if session.get('role') != 'admin':
        return redirect('/login')
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM timesheet WHERE id=%s", (id,))
        conn.commit()
        conn.close()
        logger.info(f"Admin {session.get('user')} deleted timesheet record id={id}")
    except Exception as e:
        logger.error(f"Error deleting record {id}: {str(e)}", exc_info=True)
    return redirect('/admin')

# EDIT TIMESHEET RECORD (Admin only)
@app.route('/edit/<int:id>', methods=['GET','POST'])
def edit_record(id):
    if session.get('role') != 'admin':
        return redirect('/login')

    conn = get_db()
    if request.method == 'POST':
        try:
            with conn.cursor() as cur:
                cur.execute("""
                UPDATE timesheet SET
                    project=%s, task=%s, test_case_id=%s, week=%s,
                    mon=%s, tue=%s, wed=%s, thu=%s, fri=%s, sat=%s, sun=%s, total=%s
                WHERE id=%s
                """, (
                    request.form['project'],
                    request.form['task'],
                    request.form.get('test_case_id', ''),
                    request.form['week'],
                    float(request.form.get('mon', 0) or 0),
                    float(request.form.get('tue', 0) or 0),
                    float(request.form.get('wed', 0) or 0),
                    float(request.form.get('thu', 0) or 0),
                    float(request.form.get('fri', 0) or 0),
                    float(request.form.get('sat', 0) or 0),
                    float(request.form.get('sun', 0) or 0),
                    float(request.form.get('total', 0) or 0),
                    id
                ))
            conn.commit()
            conn.close()
            logger.info(f"Admin {session.get('user')} edited timesheet record id={id}")
            return redirect('/admin')
        except Exception as e:
            logger.error(f"Error editing record {id}: {str(e)}", exc_info=True)
            conn.close()
            return redirect('/admin')
    else:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM timesheet WHERE id=%s", (id,))
            record = cur.fetchone()
        conn.close()
        if not record:
            return redirect('/admin')
        return render_template('edit.html', record=dict(record))

if __name__ == '__main__':
    app.run(debug=True)
