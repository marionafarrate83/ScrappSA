"""Authenticated, paginated access to the local DENUE catalogue."""
import csv
from contextlib import closing
from functools import lru_cache
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3

from flask import Blueprint, Response, jsonify, request, stream_with_context

SCHEMA_VERSION = 1
EXPORT_LIMIT = 50000
PAGE_SIZE = 50
FIELDS = ['id', 'name', 'legal_name', 'sector', 'size', 'staff', 'state_name',
          'municipality', 'postal_code', 'address', 'phone', 'email', 'website',
          'scian', 'activity', 'score', 'registered', 'latitude', 'longitude']
HEADERS = ['ID', 'Empresa', 'Razón social', 'Sector', 'Tamaño', 'Personal', 'Estado',
           'Municipio', 'Código postal', 'Dirección', 'Teléfono', 'Email', 'Sitio web',
           'SCIAN', 'Actividad', 'Calidad', 'Registro DENUE', 'Latitud', 'Longitud']


def database_path():
    return Path(os.environ.get('DIRECTORY_DB_PATH', str(Path(__file__).resolve().parent / 'instance' / 'directory.sqlite3')))


def connect():
    path = database_path()
    if not path.is_file():
        raise FileNotFoundError('El catálogo todavía no está preparado. Ejecuta python scripts/import_directory.py.')
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    return con


def integer(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(value)))
    except (ValueError, TypeError):
        return default


def filters(args):
    conditions, values = [], []
    for key in ['state', 'municipality', 'sector', 'size', 'scian']:
        value = args.get(key, '').strip()[:160]
        if value:
            conditions.append(f'b.{key} = ?')
            values.append(value)
    for key in ['phone', 'email', 'website']:
        if args.get(key) == '1':
            conditions.append(f"b.{key} != ''")
    score = integer(args.get('score'), 1, 1, 5)
    if score > 1:
        conditions.append('b.score >= ?')
        values.append(score)
    postal = args.get('postal_code', '').strip()[:5]
    if postal:
        if not postal.isascii() or not postal.isdigit():
            raise ValueError('El código postal debe contener solo números.')
        conditions.append('b.postal_code LIKE ?')
        values.append(postal + '%')
    tokens = re.findall(r'[^\W_]+', args.get('q', '')[:200], re.UNICODE)[:12]
    if tokens:
        match = ' AND '.join('"' + token + '"*' for token in tokens)
        if args.get('scope') == 'name':
            match = 'name : (' + match + ')'
        conditions.append('b.id IN (SELECT rowid FROM business_search WHERE business_search MATCH ?)')
        values.append(match)
    ids = args.get('ids', '')
    if ids:
        parts = ids.split(',')
        if len(parts) > 2000 or any(not re.fullmatch(r'\d{1,9}', part) for part in parts):
            raise ValueError('La selección debe contener como máximo 2,000 empresas válidas.')
        conditions.append('b.id IN (' + ','.join('?' for _ in parts) + ')')
        values.extend(map(int, parts))
    return (' WHERE ' + ' AND '.join(conditions) if conditions else ''), values


def order_by(args):
    return {'name': 'b.name_key, b.id', 'municipality': 'b.municipality, b.name_key, b.id'}.get(
        args.get('sort'), 'b.score DESC, b.name_key, b.id')


@lru_cache(maxsize=4)
def _metadata(path, modified):
    with closing(connect()) as con:
        return json.loads(con.execute("SELECT value FROM catalogue_metadata WHERE key='catalogue'").fetchone()[0])


def metadata():
    path = database_path()
    if not path.is_file():
        connect()  # same actionable error as the other endpoints
    return _metadata(str(path), path.stat().st_mtime_ns)


def csv_value(value):
    text = '' if value is None else str(value)
    # CSV quoting does not prevent formulas when a spreadsheet opens the file.
    if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + text
    return text


def create_blueprint(login_required):
    bp = Blueprint('business_directory', __name__)

    @bp.errorhandler(FileNotFoundError)
    def unavailable(error):
        return jsonify(error=str(error)), 503

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @bp.errorhandler(sqlite3.Error)
    def database_error(error):
        return jsonify(error='No se pudo consultar el catálogo. Intenta de nuevo.'), 503

    @bp.get('/api/directory/meta')
    @login_required
    def get_metadata():
        data = dict(metadata())
        data['export_limit'] = EXPORT_LIMIT
        state = request.args.get('state', '')[:2]
        with closing(connect()) as con:
            data['municipalities'] = [dict(row) for row in con.execute(
                'SELECT name, count FROM municipalities WHERE state=? ORDER BY name', (state,))] if state else []
        return jsonify(data)

    @bp.get('/api/directory/search')
    @login_required
    def search():
        where, values = filters(request.args)
        page = integer(request.args.get('page'), 1, 1, 100000)
        with closing(connect()) as con:
            total = (con.execute('SELECT COUNT(*) FROM businesses b' + where, values).fetchone()[0]
                     if where else metadata()['total'])
            pages = max(1, math.ceil(total / PAGE_SIZE))
            page = min(page, pages)
            rows = con.execute('SELECT b.* FROM businesses b' + where + ' ORDER BY ' + order_by(request.args)
                               + ' LIMIT ? OFFSET ?', [*values, PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        return jsonify(results=[{k: row[k] for k in row.keys() if k != 'name_key'} for row in rows],
                       total=total, page=page, pages=pages, page_size=PAGE_SIZE)

    @bp.get('/api/directory/business/<int:identifier>')
    @login_required
    def detail(identifier):
        with closing(connect()) as con:
            row = con.execute('SELECT * FROM businesses WHERE id=?', (identifier,)).fetchone()
        if row is None:
            return jsonify(error='Empresa no encontrada.'), 404
        return jsonify({k: row[k] for k in row.keys() if k != 'name_key'})

    @bp.get('/api/directory/export')
    @login_required
    def export():
        where, values = filters(request.args)
        fmt = request.args.get('format', 'csv')
        if fmt not in ['csv', 'json']:
            raise ValueError('Usa CSV o JSON para exportar.')
        con = connect()
        try:
            total = con.execute('SELECT COUNT(*) FROM businesses b' + where, values).fetchone()[0]
            if total > EXPORT_LIMIT:
                con.close()
                return jsonify(error=f'Filtra los resultados a un máximo de {EXPORT_LIMIT:,} empresas para exportar.'), 400
            cursor = con.execute('SELECT b.* FROM businesses b' + where + ' ORDER BY ' + order_by(request.args), values)
        except BaseException:
            con.close()
            raise

        def generate():
            try:
                if fmt == 'json':
                    yield '['
                    for index, row in enumerate(cursor):
                        yield (',' if index else '') + json.dumps({key: row[key] for key in FIELDS}, ensure_ascii=False)
                    yield ']'
                else:
                    output = io.StringIO()
                    writer = csv.writer(output)
                    writer.writerow(HEADERS)
                    yield '\ufeff' + output.getvalue()
                    for row in cursor:
                        output.seek(0)
                        output.truncate(0)
                        writer.writerow([csv_value(row[key]) for key in FIELDS])
                        yield output.getvalue()
            finally:
                con.close()
        return Response(stream_with_context(generate()), mimetype='text/csv' if fmt == 'csv' else 'application/json',
                        headers={'Content-Disposition': f'attachment; filename="ScrappSA_directorio.{fmt}"', 'Cache-Control': 'no-store'})

    return bp
