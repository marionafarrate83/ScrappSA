"""Build ScrapSA's indexed catalogue from the upstream DENUE JavaScript data.

Usage: python scripts/import_directory.py [--source /path/to/upstream/data]
The source is parsed as JSON; JavaScript is never executed.
"""
import argparse
import gzip
import json
import os
from pathlib import Path
import sqlite3
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from business_directory import SCHEMA_VERSION, database_path


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFD', value.casefold()) if unicodedata.category(c) != 'Mn')


def unwrap(text, prefix, suffix):
    text = text.strip()
    if not text.startswith(prefix) or not text.endswith(suffix):
        raise ValueError('Formato de datos de origen inesperado')
    return json.loads(text[len(prefix):-len(suffix)])


def build(source=None):
    assets = ROOT / 'directory_data'
    assets.mkdir(exist_ok=True)
    if source:
        source = Path(source)
        meta = unwrap((source / 'index.js').read_text(encoding='utf-8'), 'window.DIR_META=', ';')
        (assets / 'metadata.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        for state in meta['states']:
            with gzip.open(assets / (state['st'] + '.json.gz'), 'wt', encoding='utf-8', compresslevel=9) as out:
                text = (source / (state['st'] + '.js')).read_text(encoding='utf-8')
                json.dump(unwrap(text, 'DIR_LOAD(', ');'), out, ensure_ascii=False, separators=(',', ':'))
    else:
        meta = json.loads((assets / 'metadata.json').read_text(encoding='utf-8'))

    target = database_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.building')
    temporary.unlink(missing_ok=True)
    con = sqlite3.connect(temporary)
    try:
        con.executescript('''
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=MEMORY;
            PRAGMA cache_size=-65536;
            CREATE TABLE businesses (
                id INTEGER PRIMARY KEY, state TEXT NOT NULL, state_name TEXT NOT NULL,
                municipality TEXT NOT NULL, sector TEXT NOT NULL, size TEXT NOT NULL,
                staff TEXT NOT NULL, scian TEXT NOT NULL, activity TEXT NOT NULL,
                name TEXT NOT NULL, legal_name TEXT NOT NULL, phone TEXT NOT NULL,
                email TEXT NOT NULL, website TEXT NOT NULL, address TEXT NOT NULL,
                postal_code TEXT NOT NULL, score INTEGER NOT NULL, registered TEXT NOT NULL,
                latitude REAL, longitude REAL, name_key TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE business_search USING fts5(
                name, details, content='', tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE catalogue_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE municipalities (state TEXT, name TEXT, count INTEGER, PRIMARY KEY(state, name));
        ''')
        total = 0
        size_names = ['Micro', 'Micro', 'Pequeña', 'Pequeña', 'Mediana', 'Mediana', 'Grande']
        for state in meta['states']:
            with gzip.open(assets / (state['st'] + '.json.gz'), 'rt', encoding='utf-8') as f:
                payload = json.load(f)
            rows, search_rows, municipality_counts = [], [], {}
            for index, line in enumerate(payload['rows'].splitlines()):
                columns = line.split('\t')
                numbers = columns[0].split(',')
                if len(numbers) != 10 or len(columns) != 7:
                    raise ValueError(f"Registro inválido: {state['st']}-{index}")
                sec, rank, mun, score, activity, registered = map(int, numbers[:6])
                flags = int(numbers[9])
                legal, commercial, phone, email, website, address = columns[1:]
                # Same field semantics as the reference catalogue (flags 1 and 2).
                name = (legal if flags & 1 else commercial or legal) or '(Sin nombre)'
                municipality = payload['mun'][mun]
                postal_code = numbers[8].zfill(5) if numbers[8] and int(numbers[8]) else ''
                if not flags & 2:
                    address = ', '.join(x for x in [address, 'C.P. ' + postal_code if postal_code else '', municipality] if x)
                code, description = meta['scian'][activity]
                identifier = int(state['st']) * 1000000 + index
                record = (identifier, state['st'], state['name'], municipality, meta['sect'][sec],
                          size_names[rank], meta['rang'][rank], code, description, name, legal,
                          phone, email, website, address, postal_code, score,
                          meta['alta'][registered].replace(' ', '-'),
                          float(numbers[6]) if numbers[6] else None,
                          float(numbers[7]) if numbers[7] else None, normalize(name))
                rows.append(record)
                search_rows.append((identifier, name + ' ' + legal,
                                    ' '.join([description, code, address, phone, email, municipality, state['name'], meta['sect'][sec]])))
                municipality_counts[municipality] = municipality_counts.get(municipality, 0) + 1
                if len(rows) >= 5000:
                    con.executemany('INSERT INTO businesses VALUES (' + ','.join('?' * 21) + ')', rows)
                    con.executemany('INSERT INTO business_search(rowid,name,details) VALUES (?,?,?)', search_rows)
                    rows, search_rows = [], []
            con.executemany('INSERT INTO businesses VALUES (' + ','.join('?' * 21) + ')', rows)
            con.executemany('INSERT INTO business_search(rowid,name,details) VALUES (?,?,?)', search_rows)
            count = sum(municipality_counts.values())
            if count != state['n']:
                raise ValueError(f"Conteo inesperado para {state['name']}: {count} != {state['n']}")
            con.executemany('INSERT INTO municipalities VALUES (?,?,?)', [(state['st'], name, n) for name, n in municipality_counts.items()])
            total += count
            con.commit()
            print(f"{state['name']}: {count:,} empresas", flush=True)
        if total != meta['total']:
            raise ValueError('El total no coincide con el manifiesto de origen')
        con.executescript('''
            CREATE INDEX business_quality ON businesses(score DESC, name_key, id);
            CREATE INDEX business_name ON businesses(name_key, id);
            CREATE INDEX business_state ON businesses(state, score DESC, name_key, id);
            CREATE INDEX business_municipality ON businesses(state, municipality, score DESC);
            CREATE INDEX business_sector ON businesses(sector, score DESC);
            CREATE INDEX business_size ON businesses(size, score DESC);
            ANALYZE;
        ''')
        metadata = {'total': total, 'edition': meta['edition'], 'states': meta['states'],
                    'sectors': meta['sect'], 'activities': meta['scian'],
                    'source': 'DENUE / INEGI', 'schema_version': SCHEMA_VERSION}
        con.execute('INSERT INTO catalogue_metadata VALUES (?,?)', ('catalogue', json.dumps(metadata, ensure_ascii=False)))
        con.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        con.commit()
    except BaseException:
        con.close()
        temporary.unlink(missing_ok=True)
        raise
    con.close()
    os.replace(temporary, target)
    print(f'Catálogo listo: {total:,} empresas en {target}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    build(args.source)
