"""Regression checks for search semantics, access control and safe exports."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask, jsonify, session
import business_directory as directory


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'catalogue.sqlite3'
        self.env = patch.dict(os.environ, {'DIRECTORY_DB_PATH': str(self.path)})
        self.env.start()
        fields = directory.FIELDS + ['state', 'name_key']
        with sqlite3.connect(self.path) as con:
            con.execute('CREATE TABLE businesses (' + ','.join(f'{key} ' + ('INTEGER PRIMARY KEY' if key == 'id' else 'INTEGER' if key == 'score' else 'TEXT') for key in fields) + ')')
            con.execute("CREATE VIRTUAL TABLE business_search USING fts5(name,details,content='',tokenize='unicode61 remove_diacritics 2')")
            con.execute('CREATE TABLE catalogue_metadata (key TEXT PRIMARY KEY,value TEXT)')
            con.execute('CREATE TABLE municipalities (state TEXT,name TEXT,count INTEGER)')
            con.execute('INSERT INTO catalogue_metadata VALUES (?,?)', ('catalogue', json.dumps({'total':3,'edition':'2026','states':[],'sectors':[],'activities':[]})))
            con.execute('INSERT INTO municipalities VALUES (?,?,?)', ('09','Benito Juárez',2))
            samples = [
                {'id':1,'state':'09','name':'Clínica Ñandú','name_key':'clinica nandu','activity':'Servicios dentales','score':5,'phone':'5512345678','email':'hola@example.com','municipality':'Benito Juárez','postal_code':'03100'},
                {'id':2,'state':'03','name':'Ferretería Norte','name_key':'ferreteria norte','activity':'Comercio minorista','score':3,'municipality':'La Paz','postal_code':'23000'},
                {'id':3,'state':'09','name':'=SUM(1,1)','name_key':'sum','activity':'Servicios dentales','score':4,'municipality':'Benito Juárez','postal_code':'03400'},
            ]
            for sample in samples:
                row = {key:'' for key in fields}
                row.update(sample)
                con.execute('INSERT INTO businesses VALUES (' + ','.join('?' for _ in fields) + ')', [row[key] for key in fields])
                con.execute('INSERT INTO business_search(rowid,name,details) VALUES (?,?,?)', (sample['id'],sample['name'],sample['activity']))
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only'
        def login_required(fn):
            from functools import wraps
            @wraps(fn)
            def wrapper(*args, **kwargs):
                if 'user_id' not in session:
                    return jsonify(error='No autorizado'),401
                return fn(*args, **kwargs)
            return wrapper
        self.app.register_blueprint(directory.create_blueprint(login_required))
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'] = 'test-user'

    def tearDown(self):
        directory._metadata.cache_clear()
        self.env.stop()
        self.temp.cleanup()

    def test_authentication_required_for_all_routes(self):
        client = self.app.test_client()
        for path in ['meta','search','business/1','export']:
            self.assertEqual(client.get('/api/directory/' + path).status_code,401)

    def test_accent_insensitive_prefix_search(self):
        data = self.client.get('/api/directory/search?q=clinica+nandu').get_json()
        self.assertEqual([row['id'] for row in data['results']],[1])

    def test_name_scope_excludes_activity(self):
        self.assertEqual(self.client.get('/api/directory/search?q=dental').json['total'],2)
        self.assertEqual(self.client.get('/api/directory/search?q=dental&scope=name').json['total'],0)

    def test_combined_filters_and_pagination(self):
        data = self.client.get('/api/directory/search?state=09&municipality=Benito+Ju%C3%A1rez&score=4&phone=1&email=1&postal_code=03&page=999').json
        self.assertEqual(data['total'],1)
        self.assertEqual(data['page'],1)
        self.assertEqual(data['results'][0]['id'],1)
        self.assertNotIn('name_key',data['results'][0])

    def test_input_is_parameterized_and_fts_operators_are_literals(self):
        self.assertEqual(self.client.get('/api/directory/search?state=%27+OR+1%3D1--').json['total'],0)
        self.assertEqual(self.client.get('/api/directory/search?q=%22+OR+%22').status_code,200)
        self.assertEqual(self.client.get('/api/directory/search?postal_code=abc').status_code,400)
        self.assertEqual(self.client.get('/api/directory/search?ids=1,evil').status_code,400)

    def test_metadata_and_missing_records(self):
        data = self.client.get('/api/directory/meta?state=09').json
        self.assertEqual(data['total'],3)
        self.assertEqual(data['municipalities'][0]['name'],'Benito Juárez')
        self.assertEqual(self.client.get('/api/directory/business/999').status_code,404)

    def test_export_respects_filters_and_neutralizes_formulas(self):
        response = self.client.get('/api/directory/export?ids=3')
        self.assertEqual(response.status_code,200)
        self.assertIn("'=SUM(1,1)",response.get_data(as_text=True))
        self.assertNotIn('Clínica',response.get_data(as_text=True))
        data = self.client.get('/api/directory/export?ids=1&format=json').json
        self.assertEqual([row['id'] for row in data],[1])
        self.assertNotIn('name_key',data[0])

    def test_export_limit_does_not_silently_truncate(self):
        with patch.object(directory,'EXPORT_LIMIT',1):
            self.assertEqual(self.client.get('/api/directory/export').status_code,400)
        self.assertEqual(self.client.get('/api/directory/export?format=xlsx').status_code,400)

    def test_missing_catalogue_has_actionable_error(self):
        self.path.unlink()
        response = self.client.get('/api/directory/search')
        self.assertEqual(response.status_code,503)
        self.assertIn('import_directory.py',response.json['error'])


if __name__ == '__main__':
    unittest.main()
