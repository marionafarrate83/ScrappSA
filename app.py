import json
import csv
import uuid
import threading
import queue
import io
import os
import functools
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
from flask import (
    Flask, request, Response, render_template, jsonify,
    stream_with_context, session, redirect, url_for,
)
from pymongo import MongoClient, DESCENDING
from werkzeug.security import check_password_hash, generate_password_hash
from bson import ObjectId

import scraper as sc
import price_scraper as ps
import maps_scraper as ms

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cambia-esto-en-produccion')

# ── MongoDB ─────────────────────────────────────────────────
_mongo = MongoClient(os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/'))
_db = _mongo['scrappsa']
users_col = _db['users']
audit_col = _db['audit_logs']

try:
    users_col.create_index('username', unique=True)
    audit_col.create_index([('timestamp', DESCENDING)])
    audit_col.create_index('user')
except Exception:
    pass

# ── Jobs ─────────────────────────────────────────────────────
jobs: dict = {}
jobs_lock = threading.Lock()


# ── Auth helpers ─────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def _wrap(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/') or 'text/event-stream' in request.headers.get('Accept', ''):
                return jsonify({'error': 'No autorizado'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return _wrap


def admin_required(f):
    @functools.wraps(f)
    def _wrap(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return _wrap


def log_audit(action, details=None):
    try:
        audit_col.insert_one({
            'user': session.get('username'),
            'user_id': session.get('user_id'),
            'action': action,
            'details': details or {},
            'ip': request.remote_addr,
            'user_agent': request.headers.get('User-Agent', ''),
            'timestamp': datetime.utcnow(),
        })
    except Exception:
        pass


# ── Auth routes ──────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip().lower()
        password = request.form.get('password') or ''
        user = users_col.find_one({'username': username, 'is_active': True})
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = str(user['_id'])
            session['username'] = user['username']
            session['is_admin'] = bool(user.get('is_admin', False))
            log_audit('login')
            return redirect(url_for('index'))
        try:
            audit_col.insert_one({
                'user': username,
                'user_id': None,
                'action': 'login_failed',
                'details': {},
                'ip': request.remote_addr,
                'user_agent': request.headers.get('User-Agent', ''),
                'timestamp': datetime.utcnow(),
            })
        except Exception:
            pass
        error = 'Usuario o contraseña incorrectos'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    if 'user_id' in session:
        log_audit('logout')
        session.clear()
    return redirect(url_for('login'))


# ── Job runners ──────────────────────────────────────────────
def run_scrape_job(job_id: str, query: str, location: str):
    job = jobs[job_id]
    log_q: queue.Queue = job["log"]

    def progress(msg: str):
        log_q.put(msg)

    try:
        results = sc.scrape_all(query, location, progress=progress)
        with jobs_lock:
            jobs[job_id]["results"] = results
            jobs[job_id]["status"] = "done"
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        log_q.put(f"ERROR:{e}")
    finally:
        log_q.put(None)


def run_price_job(job_id: str, query: str):
    job = jobs[job_id]
    log_q: queue.Queue = job["log"]

    def progress(msg: str):
        log_q.put(msg)

    try:
        results = ps.compare_prices(query, progress=progress)
        with jobs_lock:
            jobs[job_id]["results"] = results
            jobs[job_id]["status"] = "done"
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        log_q.put(f"ERROR:{e}")
    finally:
        log_q.put(None)


def run_maps_job(job_id: str, query: str, location: str, filters: dict):
    job = jobs[job_id]
    log_q: queue.Queue = job["log"]

    def progress(msg: str):
        log_q.put(msg)

    try:
        results = ms.scrape_maps(query, location, filters=filters, progress=progress)
        with jobs_lock:
            jobs[job_id]["results"] = results
            jobs[job_id]["status"] = "done"
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        log_q.put(f"ERROR:{e}")
    finally:
        log_q.put(None)


# ── Routes ───────────────────────────────────────────────────
def _fmt_audit_details(entry):
    action = entry.get('action', '')
    d = entry.get('details', {})
    if action == 'search':
        parts = [d.get('mode', ''), d.get('query', '')]
        if d.get('location'):
            parts.append(f"en {d['location']}")
        return ' › '.join(p for p in parts if p)
    if action == 'login_failed':
        return 'intento fallido'
    if action.startswith('admin_'):
        t = d.get('target_user', '')
        return f'→ {t}' if t else ''
    return ''


# ── Admin routes ─────────────────────────────────────────────
@app.route('/admin')
@admin_required
def admin_panel():
    users = list(users_col.find({}, {'password_hash': 0}).sort('created_at', -1))
    for u in users:
        u['_id'] = str(u['_id'])
        ts = u.get('created_at')
        u['created_str'] = ts.strftime('%d/%m/%Y') if ts else '—'
        last = audit_col.find_one({'user': u['username'], 'action': 'login'}, sort=[('timestamp', -1)])
        u['last_login'] = last['timestamp'].strftime('%d/%m/%Y %H:%M') if last else 'Nunca'

    filter_user = request.args.get('user', '')
    filter_action = request.args.get('action', '')
    page = max(1, int(request.args.get('page', 1) or 1))
    per_page = 50
    tab = request.args.get('tab', 'users')

    q = {}
    if filter_user:
        q['user'] = filter_user
    if filter_action:
        q['action'] = filter_action

    total_logs = audit_col.count_documents(q)
    logs = list(audit_col.find(q).sort('timestamp', -1).skip((page - 1) * per_page).limit(per_page))
    for entry in logs:
        entry['_id'] = str(entry['_id'])
        ts = entry.get('timestamp')
        entry['ts_str'] = ts.strftime('%d/%m/%Y %H:%M:%S') if ts else '—'
        entry['details_fmt'] = _fmt_audit_details(entry)

    return render_template('admin.html',
        username=session.get('username'),
        current_user_id=session.get('user_id'),
        users=users,
        tab=tab,
        audit_logs=logs,
        total_logs=total_logs,
        page=page,
        pages=max(1, (total_logs + per_page - 1) // per_page),
        filter_user=filter_user,
        filter_action=filter_action,
        user_list=[u['username'] for u in users],
    )


@app.route('/admin/users/create', methods=['POST'])
@admin_required
def admin_create_user():
    username = (request.form.get('username') or '').strip().lower()
    password = request.form.get('password') or ''
    is_admin = bool(request.form.get('is_admin'))
    if not username or not password:
        return redirect(url_for('admin_panel'))
    if not users_col.find_one({'username': username}):
        users_col.insert_one({
            'username': username,
            'password_hash': generate_password_hash(password, method='pbkdf2:sha256'),
            'created_at': datetime.utcnow(),
            'is_active': True,
            'is_admin': is_admin,
        })
        log_audit('admin_create_user', {'target_user': username, 'is_admin': is_admin})
    return redirect(url_for('admin_panel'))


@app.route('/admin/users/<user_id>/toggle-admin', methods=['POST'])
@admin_required
def admin_toggle_admin(user_id):
    user = users_col.find_one({'_id': ObjectId(user_id)})
    if user and str(user['_id']) != session.get('user_id'):
        new_val = not user.get('is_admin', False)
        users_col.update_one({'_id': ObjectId(user_id)}, {'$set': {'is_admin': new_val}})
        log_audit('admin_toggle_admin', {'target_user': user['username'], 'is_admin': new_val})
    return redirect(url_for('admin_panel'))


@app.route('/admin/users/<user_id>/toggle-active', methods=['POST'])
@admin_required
def admin_toggle_active(user_id):
    user = users_col.find_one({'_id': ObjectId(user_id)})
    if user and str(user['_id']) != session.get('user_id'):
        new_val = not user.get('is_active', True)
        users_col.update_one({'_id': ObjectId(user_id)}, {'$set': {'is_active': new_val}})
        log_audit('admin_toggle_active', {'target_user': user['username'], 'is_active': new_val})
    return redirect(url_for('admin_panel'))


@app.route('/admin/users/<user_id>/password', methods=['POST'])
@admin_required
def admin_change_password(user_id):
    password = request.form.get('password') or ''
    if password:
        user = users_col.find_one({'_id': ObjectId(user_id)})
        if user:
            users_col.update_one(
                {'_id': ObjectId(user_id)},
                {'$set': {'password_hash': generate_password_hash(password, method='pbkdf2:sha256')}},
            )
            log_audit('admin_change_password', {'target_user': user.get('username')})
    return redirect(url_for('admin_panel'))


@app.route("/")
@login_required
def index():
    return render_template("index.html",
        username=session.get('username'),
        is_admin=session.get('is_admin', False),
    )


@app.route("/api/scrape", methods=["POST"])
@login_required
def start_scrape():
    data = request.get_json()
    query = (data.get("query") or "").strip()
    location = (data.get("location") or "").strip()

    if not query or not location:
        return jsonify({"error": "Faltan parámetros query y location"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "query": query,
            "location": location,
            "results": [],
            "log": queue.Queue(),
        }

    log_audit('search', {'mode': 'directory', 'query': query, 'location': location})

    thread = threading.Thread(
        target=run_scrape_job, args=(job_id, query, location), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/maps", methods=["POST"])
@login_required
def start_maps():
    data = request.get_json()
    query = (data.get("query") or "").strip()
    location = (data.get("location") or "").strip()
    filters = data.get("filters") or {}

    if not query or not location:
        return jsonify({"error": "Faltan parámetros query y location"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "kind": "maps",
            "query": query,
            "location": location,
            "filters": filters,
            "results": [],
            "log": queue.Queue(),
        }

    log_audit('search', {'mode': 'maps', 'query': query, 'location': location, 'filters': filters})

    thread = threading.Thread(
        target=run_maps_job, args=(job_id, query, location, filters), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/compare", methods=["POST"])
@login_required
def start_compare():
    data = request.get_json()
    query = (data.get("query") or "").strip()

    if not query:
        return jsonify({"error": "Falta parámetro query"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "kind": "prices",
            "query": query,
            "location": "",
            "results": [],
            "log": queue.Queue(),
        }

    log_audit('search', {'mode': 'prices', 'query': query})

    thread = threading.Thread(target=run_price_job, args=(job_id, query), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
@login_required
def progress_stream(job_id: str):
    if job_id not in jobs:
        return jsonify({"error": "Job no encontrado"}), 404

    def generate():
        log_q = jobs[job_id]["log"]
        while True:
            try:
                msg = log_q.get(timeout=30)
            except queue.Empty:
                yield "data: ping\n\n"
                continue

            if msg is None:
                break
            yield f"data: {json.dumps(msg)}\n\n"

        status = jobs[job_id].get("status", "done")
        results = jobs[job_id].get("results", [])
        if jobs[job_id].get("kind") == "prices":
            count = len([item for item in results if item.get("precio") is not None])
        else:
            count = len(results)
        yield f"data: {json.dumps({'__status__': status, '__count__': count})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/results/<job_id>")
@login_required
def get_results(job_id: str):
    if job_id not in jobs:
        return jsonify({"error": "Job no encontrado"}), 404
    job = jobs[job_id]
    return jsonify({
        "status": job["status"],
        "kind": job.get("kind", "directory"),
        "query": job["query"],
        "location": job["location"],
        "count": len(job["results"]),
        "results": job["results"],
    })


@app.route("/api/download/<job_id>/<fmt>")
@login_required
def download(job_id: str, fmt: str):
    if job_id not in jobs:
        return jsonify({"error": "Job no encontrado"}), 404

    job = jobs[job_id]
    results = job["results"]
    if job.get("kind") == "maps":
        slug = f"google_maps_{job['query']}_{job['location']}".replace(" ", "_")
    elif job.get("kind") == "prices":
        slug = f"comparador_{job['query']}".replace(" ", "_")
    else:
        slug = f"{job['query']}_{job['location']}".replace(" ", "_")

    if fmt == "json":
        content = json.dumps(results, ensure_ascii=False, indent=2)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{slug}.json"'},
        )

    if fmt == "csv":
        output = io.StringIO()
        if job.get("kind") == "maps":
            fields = [
                "nombre", "categoria", "calificacion", "resenas", "telefono",
                "direccion", "sitio_web", "google_maps_url", "latitud", "longitud",
                "linkedin_url", "lider_nombre", "lider_cargo", "lider_fuente",
                "lider_confianza", "resumen_actividad", "estado"
            ]
            # opiniones_muestra es lista — convertir a texto para CSV
            results = [
                {**r, "opiniones_muestra": " | ".join(r.get("opiniones_muestra") or [])}
                for r in results
            ]
        elif job.get("kind") == "prices":
            fields = ["tienda", "producto", "precio", "precio_texto", "url", "fuente", "estado"]
        else:
            fields = ["nombre", "telefono", "whatsapp", "direccion", "sitio_web", "email"]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
        content = "﻿" + output.getvalue()  # UTF-8 BOM para Excel
        return Response(
            content,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{slug}.csv"'},
        )

    return jsonify({"error": "Formato inválido. Usa json o csv"}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", debug=True, port=port, threaded=True)
