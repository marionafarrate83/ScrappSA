import json
import csv
import uuid
import threading
import queue
import io
import os
import functools
from html import escape as escape_html
import re
import time
from datetime import datetime
from urllib.parse import quote
from dotenv import load_dotenv

load_dotenv()
from flask import (
    Flask, request, Response, render_template, jsonify,
    stream_with_context, session, redirect, url_for,
)
from pymongo import MongoClient, DESCENDING
from werkzeug.security import check_password_hash, generate_password_hash
from bson import ObjectId
import requests
import stripe
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import scraper as sc
import price_scraper as ps
import maps_scraper as ms
import economic_activity as ea
from business_directory import create_blueprint

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cambia-esto-en-produccion')

# ── MongoDB ─────────────────────────────────────────────────
_mongo_client = None


def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(
            os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/'),
            serverSelectionTimeoutMS=5000,
        )
    return _mongo_client['scrappsa']


class LazyCollection:
    def __init__(self, name: str):
        self.name = name

    def _collection(self):
        return get_db()[self.name]

    def __getattr__(self, attr):
        return getattr(self._collection(), attr)


users_col = LazyCollection('users')
audit_col = LazyCollection('audit_logs')
saved_searches_col = LazyCollection('saved_searches')
purchases_col = LazyCollection('purchases')

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY') or os.environ.get('STRIPE_API_KEY') or ''
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
STRIPE_PLANS = {
    'estandar': {
        'name': 'ScrappSA Estándar', 'amount': 54900,
        'description': 'Acceso individual de por vida a ScrappSA. Pago único, sin mensualidades.',
        'access_type': 'lifetime',
    },
}

@app.context_processor
def standard_offer_context():
    selected = STRIPE_PLANS['estandar']
    return {'standard_offer': {
        **selected, 'price': f"${selected['amount'] / 100:,.0f} MXN",
        'access': 'Acceso de por vida', 'availability': 'Oferta por tiempo limitado',
    }}


def ensure_indexes():
    try:
        users_col.create_index('username', unique=True)
        audit_col.create_index([('timestamp', DESCENDING)])
        audit_col.create_index('user')
        saved_searches_col.create_index([('created_at', DESCENDING)])
        saved_searches_col.create_index('user')
        saved_searches_col.create_index('kind')
    except Exception:
        pass


threading.Thread(target=ensure_indexes, daemon=True).start()

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


app.register_blueprint(create_blueprint(login_required))


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


def result_count_for_job(job: dict) -> int:
    results = job.get("results", [])
    if job.get("kind") == "prices":
        return len([item for item in results if item.get("precio") is not None])
    if job.get("kind") == "economic_activity":
        return sum(int(item.get("conteo") or 0) for item in results)
    return len(results)


def persist_search_job(job_id: str):
    try:
        job = jobs.get(job_id)
        if not job or job.get("persisted_search_id"):
            return
        results = job.get("results", [])
        doc = {
            "job_id": job_id,
            "kind": job.get("kind", "directory"),
            "query": job.get("query", ""),
            "location": job.get("location", ""),
            "filters": job.get("filters", {}),
            "status": job.get("status", ""),
            "error": job.get("error", ""),
            "result_count": result_count_for_job(job),
            "results": results,
            "user": job.get("user", ""),
            "user_id": job.get("user_id", ""),
            "created_at": job.get("created_at") or datetime.utcnow(),
            "completed_at": datetime.utcnow(),
        }
        inserted = saved_searches_col.insert_one(doc)
        job["persisted_search_id"] = str(inserted.inserted_id)
    except Exception:
        pass


_sendpulse_token = {"access_token": "", "expires_at": 0}


def sendpulse_configured() -> bool:
    return bool(
        (os.environ.get("SENDPULSE_API_ID") or os.environ.get("SP_API_ID"))
        and (os.environ.get("SENDPULSE_API_SECRET") or os.environ.get("SP_API_SECRET"))
        and os.environ.get("SENDPULSE_WHATSAPP_BOT_ID")
    )


def get_sendpulse_token() -> str:
    now = time.time()
    if _sendpulse_token["access_token"] and _sendpulse_token["expires_at"] > now + 60:
        return _sendpulse_token["access_token"]

    api_id = os.environ.get("SENDPULSE_API_ID") or os.environ.get("SP_API_ID")
    api_secret = os.environ.get("SENDPULSE_API_SECRET") or os.environ.get("SP_API_SECRET")
    if not api_id or not api_secret:
        raise RuntimeError("Faltan SENDPULSE_API_ID/SENDPULSE_API_SECRET.")

    response = requests.post(
        "https://api.sendpulse.com/oauth/access_token",
        json={
            "grant_type": "client_credentials",
            "client_id": api_id,
            "client_secret": api_secret,
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError("SendPulse no devolvió access_token.")
    _sendpulse_token["access_token"] = access_token
    _sendpulse_token["expires_at"] = now + int(data.get("expires_in") or 3600)
    return access_token


def normalize_whatsapp_phone(value: str) -> str:
    raw = (value or "").strip()
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw)
        return f"+{digits}" if digits else ""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if len(digits) == 10:
        return f"+52{digits}"
    if len(digits) == 12 and digits.startswith("52"):
        return f"+{digits}"
    if len(digits) > 10 and not digits.startswith("00"):
        return f"+{digits}"
    return ""


def extract_phone(value: str) -> str:
    text = value or ""
    match = re.search(r"(?:\+?\d[\d\s().-]{7,}\d)", text)
    return match.group(0).strip() if match else ""


def extract_search_contacts(entry: dict) -> list[dict]:
    contacts = []
    for index, item in enumerate(entry.get("results") or []):
        phone = extract_phone(item.get("telefono") or item.get("phone") or "")
        wa_phone = normalize_whatsapp_phone(phone)
        wa_digits = re.sub(r"\D", "", wa_phone) if wa_phone else ""
        wa_message = "Hola 👋"
        contacts.append({
            "index": index,
            "nombre": item.get("nombre") or item.get("tienda") or item.get("giro") or "Sin nombre",
            "telefono": phone,
            "whatsapp_phone": wa_phone,
            "wa_me_url": f"https://wa.me/{wa_digits}?text={quote(wa_message)}" if wa_digits else "",
            "wa_message": wa_message,
            "email": item.get("email") or "",
            "direccion": item.get("direccion") or "",
            "sitio_web": item.get("sitio_web") or item.get("url") or "",
            "categoria": item.get("categoria") or item.get("categorias_detectadas") or "",
            "raw": item,
        })
    return contacts


def first_whatsapp_contact(entry: dict) -> dict:
    for contact in extract_search_contacts(entry):
        if contact.get("wa_me_url"):
            return contact
    return {}


def render_message_template(template: str, contact: dict, entry: dict) -> str:
    values = {
        "nombre": contact.get("nombre", ""),
        "telefono": contact.get("telefono", ""),
        "categoria": contact.get("categoria", ""),
        "direccion": contact.get("direccion", ""),
        "sitio_web": contact.get("sitio_web", ""),
        "query": entry.get("query", ""),
        "location": entry.get("location", ""),
    }
    message = template or ""
    for key, value in values.items():
        message = message.replace("{{" + key + "}}", str(value or ""))
    return message.strip()


def sendpulse_send_text(phone: str, message: str) -> dict:
    bot_id = os.environ.get("SENDPULSE_WHATSAPP_BOT_ID")
    if not bot_id:
        raise RuntimeError("Falta SENDPULSE_WHATSAPP_BOT_ID.")
    token = get_sendpulse_token()
    response = requests.post(
        "https://api.sendpulse.com/whatsapp/contacts/sendByPhone",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "bot_id": bot_id,
            "phone": phone,
            "message": {
                "type": "text",
                "text": {"body": message[:1024]},
            },
        },
        timeout=25,
    )
    return {"status_code": response.status_code, "body": response.json() if response.content else {}}


def sendpulse_send_template(phone: str, template_name: str, language_code: str, parameters: list[str]) -> dict:
    bot_id = os.environ.get("SENDPULSE_WHATSAPP_BOT_ID")
    if not bot_id:
        raise RuntimeError("Falta SENDPULSE_WHATSAPP_BOT_ID.")
    token = get_sendpulse_token()
    body_parameters = [{"type": "text", "text": str(value)} for value in parameters if str(value)]
    payload = {
        "bot_id": bot_id,
        "phone": phone,
        "template": {
            "name": template_name,
            "language": {
                "policy": "deterministic",
                "code": language_code,
            },
        },
    }
    if body_parameters:
        payload["template"]["components"] = [{
            "type": "body",
            "parameters": body_parameters,
        }]
    response = requests.post(
        "https://api.sendpulse.com/whatsapp/contacts/sendTemplateByPhone",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=25,
    )
    return {"status_code": response.status_code, "body": response.json() if response.content else {}}


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


@app.route('/register', methods=['GET', 'POST'])
def register():
    purchase = session.get('purchase_verified')

    token = (request.args.get('purchase') or '').strip()
    if token:
        token_data = load_purchase_token(token)
        if token_data:
            paid_purchase = purchases_col.find_one({
                'stripe_session_id': token_data.get('stripe_session_id'),
                'status': 'paid',
            })
            if paid_purchase:
                purchase = {
                    'plan': paid_purchase.get('plan', 'estandar'),
                    'name': paid_purchase.get('name', ''),
                    'email': paid_purchase.get('email', ''),
                    'verified': True,
                    'stripe_session_id': paid_purchase.get('stripe_session_id'),
                }
                session['purchase_verified'] = purchase

    if not purchase or purchase.get('demo'):
        session.pop('purchase_verified', None)
        return redirect(url_for('checkout'))

    if stripe.api_key:
        stripe_session_id = purchase.get('stripe_session_id', '')
        paid_purchase = purchases_col.find_one({
            'stripe_session_id': stripe_session_id,
            'status': 'paid',
        })
        if not paid_purchase:
            session.pop('purchase_verified', None)
            return redirect(url_for('landing'))

    error = None
    if request.method == 'POST':
        username = (request.form.get('email') or purchase.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        confirm_password = request.form.get('confirm_password') or ''
        email = username

        if not username or not password:
            error = 'Completa el correo electrónico y la contraseña'
        elif '@' not in username or '.' not in username:
            error = 'Ingresa un correo electrónico válido'
        elif len(password) < 8:
            error = 'La contraseña debe tener al menos 8 caracteres'
        elif password != confirm_password:
            error = 'Las contraseñas no coinciden'
        elif users_col.find_one({'username': username}):
            error = 'Ese correo electrónico ya está registrado'
        else:
            try:
                inserted = users_col.insert_one({
                    'username': username,
                    'email': email,
                    'password_hash': generate_password_hash(password, method='pbkdf2:sha256'),
                    'created_at': datetime.utcnow(),
                    'is_active': True,
                    'is_admin': False,
                    'plan': 'estandar',
                    'access_type': 'lifetime',
                    'purchase_verified': True,
                })
                session['user_id'] = str(inserted.inserted_id)
                session['username'] = username
                session['is_admin'] = False
                session.pop('purchase_verified', None)
                log_audit('register')
                return redirect(url_for('index'))
            except Exception:
                error = 'No fue posible crear la cuenta. Inténtalo nuevamente.'

    return render_template(
        'register.html',
        error=error,
        plan=purchase.get('plan', 'estandar'),
        name=purchase.get('name', ''),
        email=purchase.get('email', ''),
    )


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
        persist_search_job(job_id)
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        persist_search_job(job_id)
        log_q.put(f"ERROR:{e}")
    finally:
        log_q.put(None)


def run_price_job(job_id: str, query: str, use_browser: bool = False, include_google_shopping: bool = False):
    job = jobs[job_id]
    log_q: queue.Queue = job["log"]

    def progress(msg: str):
        log_q.put(msg)

    try:
        results = ps.compare_prices(query, progress=progress, use_browser=use_browser, include_google_shopping=include_google_shopping)
        with jobs_lock:
            jobs[job_id]["results"] = results
            jobs[job_id]["status"] = "done"
        persist_search_job(job_id)
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        persist_search_job(job_id)
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
        persist_search_job(job_id)
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        persist_search_job(job_id)
        log_q.put(f"ERROR:{e}")
    finally:
        log_q.put(None)


def run_economic_activity_job(job_id: str, location: str, filters: dict):
    job = jobs[job_id]
    log_q: queue.Queue = job["log"]

    def progress(msg: str):
        log_q.put(msg)

    try:
        results = ea.scan_activity(location, filters=filters, progress=progress)
        with jobs_lock:
            jobs[job_id]["results"] = results
            jobs[job_id]["status"] = "done"
        persist_search_job(job_id)
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)
        persist_search_job(job_id)
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


def _fmt_search_details(entry):
    parts = [entry.get('query', '')]
    if entry.get('location'):
        parts.append(f"en {entry['location']}")
    filters = entry.get('filters') or {}
    if filters.get('radius_km'):
        parts.append(f"radio {filters['radius_km']} km")
    return ' › '.join(p for p in parts if p)


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
    search_user = request.args.get('s_user', '')
    search_kind = request.args.get('s_kind', '')
    page = max(1, int(request.args.get('page', 1) or 1))
    search_page = max(1, int(request.args.get('search_page', 1) or 1))
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

    search_q = {}
    if search_user:
        search_q['user'] = search_user
    if search_kind:
        search_q['kind'] = search_kind

    total_searches = saved_searches_col.count_documents(search_q)
    saved_searches = list(
        saved_searches_col
        .find(search_q, {
            'job_id': 1,
            'kind': 1,
            'query': 1,
            'location': 1,
            'filters': 1,
            'status': 1,
            'error': 1,
            'result_count': 1,
            'user': 1,
            'user_id': 1,
            'created_at': 1,
            'completed_at': 1,
            'results.nombre': 1,
            'results.tienda': 1,
            'results.giro': 1,
            'results.telefono': 1,
            'results.phone': 1,
        })
        .sort('created_at', -1)
        .skip((search_page - 1) * per_page)
        .limit(per_page)
    )
    for item in saved_searches:
        item['_id'] = str(item['_id'])
        ts = item.get('completed_at') or item.get('created_at')
        item['completed_str'] = ts.strftime('%d/%m/%Y %H:%M:%S') if ts else '—'
        item['details_fmt'] = _fmt_search_details(item)
        item['first_whatsapp_contact'] = first_whatsapp_contact(item)

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
        search_user=search_user,
        search_kind=search_kind,
        saved_searches=saved_searches,
        total_searches=total_searches,
        search_page=search_page,
        search_pages=max(1, (total_searches + per_page - 1) // per_page),
        user_list=[u['username'] for u in users],
    )


@app.route('/admin/searches/<search_id>/json')
@admin_required
def admin_search_json(search_id):
    try:
        entry = saved_searches_col.find_one({'_id': ObjectId(search_id)})
    except Exception:
        entry = None
    if not entry:
        return jsonify({'error': 'Búsqueda no encontrada'}), 404
    entry['_id'] = str(entry['_id'])
    content = json.dumps(entry, ensure_ascii=False, indent=2, default=str)
    filename = f"busqueda_{entry.get('kind', 'search')}_{search_id}.json"
    return Response(
        content,
        mimetype="application/json",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.route('/admin/searches/<search_id>')
@admin_required
def admin_search_detail(search_id):
    try:
        entry = saved_searches_col.find_one({'_id': ObjectId(search_id)})
    except Exception:
        entry = None
    if not entry:
        return jsonify({'error': 'Búsqueda no encontrada'}), 404

    entry['_id'] = str(entry['_id'])
    ts = entry.get('completed_at') or entry.get('created_at')
    entry['completed_str'] = ts.strftime('%d/%m/%Y %H:%M:%S') if ts else '—'
    entry['details_fmt'] = _fmt_search_details(entry)
    contacts = extract_search_contacts(entry)
    default_message = (
        "Hola {{nombre}}, te contacto de Visoor. "
        "Vimos tu negocio en {{location}} y nos gustaría compartirte una propuesta."
    )
    return render_template(
        "admin_search_detail.html",
        username=session.get('username'),
        entry=entry,
        contacts=contacts,
        sendpulse_ready=sendpulse_configured(),
        default_message=default_message,
        default_template=os.environ.get("SENDPULSE_WHATSAPP_TEMPLATE_NAME", ""),
        default_template_lang=os.environ.get("SENDPULSE_WHATSAPP_TEMPLATE_LANG", "es_MX"),
    )


@app.route('/admin/searches/<search_id>/whatsapp/send', methods=['POST'])
@admin_required
def admin_search_whatsapp_send(search_id):
    try:
        entry = saved_searches_col.find_one({'_id': ObjectId(search_id)})
    except Exception:
        entry = None
    if not entry:
        return jsonify({'error': 'Búsqueda no encontrada'}), 404
    if not sendpulse_configured():
        return jsonify({'error': 'Faltan credenciales SendPulse en variables de entorno.'}), 400

    selected = request.form.getlist('selected')
    mode = request.form.get('mode') or 'text'
    message_template = request.form.get('message') or ''
    template_name = (request.form.get('template_name') or os.environ.get("SENDPULSE_WHATSAPP_TEMPLATE_NAME") or '').strip()
    language_code = (request.form.get('template_lang') or os.environ.get("SENDPULSE_WHATSAPP_TEMPLATE_LANG") or 'es_MX').strip()
    contacts = extract_search_contacts(entry)

    selected_indexes = set()
    for value in selected:
        try:
            selected_indexes.add(int(value))
        except ValueError:
            pass
    targets = [contact for contact in contacts if contact["index"] in selected_indexes and contact.get("whatsapp_phone")]
    if not targets:
        return jsonify({'error': 'Selecciona al menos un contacto con teléfono válido.'}), 400
    if len(targets) > 25:
        return jsonify({'error': 'Por seguridad, envía máximo 25 mensajes por lote.'}), 400
    if mode == 'template' and not template_name:
        return jsonify({'error': 'Falta el nombre de la plantilla WABA.'}), 400
    if mode == 'text' and not message_template.strip():
        return jsonify({'error': 'Escribe el mensaje a enviar.'}), 400

    send_results = []
    for contact in targets:
        try:
            if mode == 'template':
                parameters = [
                    contact.get('nombre', ''),
                    entry.get('location', ''),
                    contact.get('categoria', ''),
                ]
                result = sendpulse_send_template(
                    contact["whatsapp_phone"],
                    template_name,
                    language_code,
                    parameters,
                )
                rendered_message = f"template:{template_name}"
            else:
                rendered_message = render_message_template(message_template, contact, entry)
                result = sendpulse_send_text(contact["whatsapp_phone"], rendered_message)
            ok = 200 <= int(result.get("status_code", 0)) < 300
            send_results.append({
                "nombre": contact.get("nombre"),
                "phone": contact.get("whatsapp_phone"),
                "ok": ok,
                "message": rendered_message,
                "sendpulse": result,
            })
        except Exception as exc:
            send_results.append({
                "nombre": contact.get("nombre"),
                "phone": contact.get("whatsapp_phone"),
                "ok": False,
                "message": str(exc),
                "sendpulse": {},
            })

    try:
        saved_searches_col.update_one(
            {'_id': ObjectId(search_id)},
            {'$push': {
                'whatsapp_sends': {
                    'sent_at': datetime.utcnow(),
                    'user': session.get('username'),
                    'mode': mode,
                    'count': len(send_results),
                    'results': send_results,
                }
            }}
        )
        log_audit('admin_whatsapp_send', {
            'search_id': search_id,
            'mode': mode,
            'count': len(send_results),
        })
    except Exception:
        pass

    return render_template(
        "admin_send_result.html",
        username=session.get('username'),
        search_id=search_id,
        results=send_results,
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


# ── Landing pública ─────────────────────────────────────────
@app.route("/")
def landing():
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template("landing.html")


@app.route("/app")
@login_required
def index():
    return render_template("index.html",
        username=session.get('username'),
        is_admin=session.get('is_admin', False),
        ai_available=bool(os.environ.get('ANTHROPIC_API_KEY')),
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
            "kind": "directory",
            "query": query,
            "location": location,
            "filters": {},
            "results": [],
            "log": queue.Queue(),
            "user": session.get('username', ''),
            "user_id": session.get('user_id', ''),
            "created_at": datetime.utcnow(),
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
            "user": session.get('username', ''),
            "user_id": session.get('user_id', ''),
            "created_at": datetime.utcnow(),
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
    use_browser = bool(data.get("use_browser", False))
    include_google_shopping = bool(data.get("include_google_shopping", False))

    if not query:
        return jsonify({"error": "Falta parámetro query"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "kind": "prices",
            "query": query,
            "location": "",
            "filters": {
                "use_browser": use_browser,
                "include_google_shopping": include_google_shopping,
            },
            "results": [],
            "log": queue.Queue(),
            "user": session.get('username', ''),
            "user_id": session.get('user_id', ''),
            "created_at": datetime.utcnow(),
        }

    log_audit('search', {'mode': 'prices', 'query': query, 'use_browser': use_browser, 'google_shopping': include_google_shopping})

    thread = threading.Thread(target=run_price_job, args=(job_id, query, use_browser, include_google_shopping), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/economic-activity", methods=["POST"])
@login_required
def start_economic_activity():
    data = request.get_json()
    location = (data.get("location") or "").strip()
    filters = data.get("filters") or {}

    if not location:
        return jsonify({"error": "Falta parámetro location"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "kind": "economic_activity",
            "query": "actividad economica",
            "location": location,
            "filters": filters,
            "results": [],
            "log": queue.Queue(),
            "user": session.get('username', ''),
            "user_id": session.get('user_id', ''),
            "created_at": datetime.utcnow(),
        }

    log_audit('search', {'mode': 'economic_activity', 'location': location, 'filters': filters})

    thread = threading.Thread(
        target=run_economic_activity_job, args=(job_id, location, filters), daemon=True
    )
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
        elif jobs[job_id].get("kind") == "economic_activity":
            count = sum(int(item.get("conteo") or 0) for item in results)
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
    elif job.get("kind") == "economic_activity":
        slug = f"actividad_economica_{job['location']}".replace(" ", "_")
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
                "email", "direccion", "sitio_web", "google_maps_url", "latitud",
                "longitud", "linkedin_url", "lider_nombre", "lider_cargo",
                "lider_fuente", "lider_confianza", "resumen_actividad", "estado"
            ]
            # opiniones_muestra es lista — convertir a texto para CSV
            results = [
                {**r, "opiniones_muestra": " | ".join(r.get("opiniones_muestra") or [])}
                for r in results
            ]
        elif job.get("kind") == "economic_activity":
            fields = [
                "giro", "conteo", "con_telefono", "con_sitio_web",
                "categorias_detectadas", "negocios", "estado"
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


@app.route("/checkout", methods=['GET', 'POST'])
def checkout():
    # One offer for all new purchases, including links to retired plans.
    plan = 'estandar'
    stripe_configured = bool(stripe.api_key)

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        if not name or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            return render_template('checkout.html', plan=plan, stripe_configured=stripe_configured, error='Completa tu nombre e ingresa un correo electrónico válido.'), 400

        if stripe.api_key:
            try:
                selected = STRIPE_PLANS[plan]
                checkout_session = stripe.checkout.Session.create(
                    mode='payment',
                    customer_email=email,
                    line_items=[{'price_data': {
                        'currency': 'mxn',
                        'product_data': {'name': selected['name'], 'description': selected['description']},
                        'unit_amount': selected['amount'],
                    }, 'quantity': 1}],
                    metadata={'plan': plan, 'name': name, 'email': email, 'access_type': 'lifetime'},
                    payment_intent_data={'metadata': {'plan': plan, 'name': name, 'email': email, 'access_type': 'lifetime'}},
                    success_url=url_for('checkout_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
                    cancel_url=url_for('checkout', _external=True) + '?plan=' + plan,
                )
                return redirect(checkout_session.url, code=303)
            except Exception:
                app.logger.exception('No se pudo iniciar el pago con Stripe')
                return render_template('checkout.html', plan=plan, stripe_configured=True, error='No se pudo abrir el pago. Intenta de nuevo; tu acceso se activa cuando el pago se confirma.'), 502

        return render_template('checkout.html', plan=plan, stripe_configured=False,
                               error='El pago en línea no está disponible en este momento. Intenta más tarde.'), 503

    return render_template("checkout.html", plan=plan, success=False, stripe_configured=bool(stripe.api_key))


@app.route('/checkout/success')
def checkout_success():
    session_id = request.args.get('session_id', '').strip()
    if not session_id or not stripe.api_key:
        return redirect(url_for('checkout'))

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        if checkout_session.payment_status != 'paid':
            return redirect(url_for('checkout'))

        session_data = checkout_session.to_dict() if hasattr(checkout_session, 'to_dict') else dict(checkout_session)
        metadata = session_data.get('metadata') or {}
        customer_details = session_data.get('customer_details') or {}
        plan = metadata.get('plan', 'estandar')
        email = (metadata.get('email') or customer_details.get('email') or '').strip().lower()
        name = metadata.get('name', '')

        purchases_col.update_one(
            {'stripe_session_id': session_id},
            {'$set': {
                'stripe_session_id': session_id,
                'stripe_payment_intent': session_data.get('payment_intent', ''),
                'email': email, 'name': name, 'plan': plan,
                'status': 'paid', 'updated_at': datetime.utcnow(),
            }, '$setOnInsert': {'created_at': datetime.utcnow()}},
            upsert=True,
        )
        session['purchase_verified'] = {
            'plan': plan, 'name': name, 'email': email,
            'verified': True, 'stripe_session_id': session_id,
        }
        register_url = url_for('register', _external=True)
        existing_purchase = purchases_col.find_one({'stripe_session_id': session_id})
        if not existing_purchase or not existing_purchase.get('confirmation_email_sent'):
            base_url = os.environ.get('APP_BASE_URL', '').strip()
            register_path = '/register?purchase=' + create_purchase_token(session_id)
            register_url = f"{base_url.rstrip('/')}/register?purchase={create_purchase_token(checkout_session['id'])}" if base_url else url_for('register', purchase=create_purchase_token(checkout_session['id']), _external=True)
            email_sent = send_purchase_confirmation(email, name, plan, register_url, amount_paid=session_data.get('amount_total'))
            purchases_col.update_one(
                {'stripe_session_id': session_id},
                {'$set': {
                    'confirmation_email_sent': email_sent,
                    'confirmation_email_attempted_at': datetime.utcnow(),
                }},
                upsert=True,
            )
        return redirect(url_for('register'))
    except Exception as exc:
        app.logger.exception("Stripe checkout success failed: %s", exc)
        return redirect(url_for('checkout'))


@app.route('/webhooks/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    signature = request.headers.get('Stripe-Signature', '')
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({'error': 'Webhook no configurado'}), 503

    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return jsonify({'error': 'Firma inválida'}), 400

    if event['type'] == 'checkout.session.completed':
        checkout_session = event['data']['object']
        if checkout_session.get('payment_status') == 'paid':
            metadata = checkout_session.get('metadata') or {}
            customer_details = checkout_session.get('customer_details') or {}
            purchases_col.update_one(
                {'stripe_session_id': checkout_session['id']},
                {'$set': {
                    'stripe_session_id': checkout_session['id'],
                    'stripe_payment_intent': checkout_session.get('payment_intent'),
                    'email': (metadata.get('email') or customer_details.get('email') or '').strip().lower(),
                    'name': metadata.get('name', ''),
                    'plan': metadata.get('plan', 'estandar'),
                    'status': 'paid', 'updated_at': datetime.utcnow(),
                }, '$setOnInsert': {'created_at': datetime.utcnow()}},
                upsert=True,
            )

    return jsonify({'received': True}), 200


def send_purchase_confirmation(email: str, name: str, plan: str, register_url: str = '', amount_paid=None) -> bool:
    api_key = os.environ.get('RESEND_API_KEY', '').strip()
    if not api_key or not email:
        return False

    sender = os.environ.get('RESEND_FROM_EMAIL', 'ScrappSA <onboarding@resend.dev>').strip()
    plan_name = 'Estándar'
    cents = STRIPE_PLANS['estandar']['amount'] if amount_paid is None else amount_paid
    amount = f'${cents / 100:,.2f} MXN'
    safe_name = escape_html(name or 'comprador')
    safe_email = escape_html(email)
    safe_register_url = escape_html(register_url, quote=True)

    subject = f'Pago confirmado — ScrappSA {plan_name}'
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;color:#111827">
      <h1 style="margin-bottom:8px">Pago confirmado</h1>
      <p>Hola {safe_name},</p>
      <p>Tu compra de <strong>ScrappSA {plan_name}</strong> por <strong>{amount}</strong> fue confirmada.</p>
      <p>Tu acceso es de por vida: un solo pago, sin mensualidades ni renovación automática. El fin de la oferta no cambia la vigencia de tu acceso.</p>
      <p>Incluye el directorio de empresas de México, listas de prospectos, exportaciones y las herramientas de Google Maps y actividad económica.</p>
      <p>Ya puedes crear tu cuenta con el correo <strong>{safe_email}</strong>.</p>
      {f'<p><a href="{safe_register_url}" style="display:inline-block;padding:12px 18px;border-radius:8px;background:#f5c800;color:#111827;text-decoration:none;font-weight:700">Crear mi cuenta</a></p>' if register_url else ''}
      <p style="color:#6b7280;font-size:13px">Si no realizaste esta compra, responde a este correo.</p>
    </div>
    """

    try:
        response = requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'from': sender, 'to': [email], 'subject': subject, 'html': html},
            timeout=20,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        app.logger.warning('Purchase confirmation email failed for %s: %s', email, exc)
        return False


def purchase_token_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt='scrappsa-purchase-registration')


def create_purchase_token(stripe_session_id: str) -> str:
    return purchase_token_serializer().dumps({'stripe_session_id': stripe_session_id})


def load_purchase_token(token: str):
    try:
        return purchase_token_serializer().loads(token, max_age=7 * 24 * 60 * 60)
    except (BadSignature, SignatureExpired):
        return None


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", debug=debug, port=port, threaded=True, use_reloader=False)
