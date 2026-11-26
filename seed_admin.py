#!/usr/bin/env python3
"""
Seed idempotente para crear o actualizar un usuario administrador.

Uso interactivo:
    python3 seed_admin.py
    python3 seed_admin.py --username mario

Uso automatizado:
    ADMIN_USERNAME=mario ADMIN_PASSWORD='cambia-esta-clave' python3 seed_admin.py

Credenciales por defecto del seed:
    usuario: Admin
    password:   
"""

import argparse
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path

from pymongo import MongoClient
from werkzeug.security import generate_password_hash

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv(Path(__file__).parent / '.env')

DEFAULT_ADMIN_USERNAME = 'Admin'
DEFAULT_ADMIN_PASSWORD = 'Alfanumerico$2026'


def parse_args():
    parser = argparse.ArgumentParser(
        description='Crea o actualiza un usuario administrador en ScrapSA.'
    )
    parser.add_argument(
        '--username',
        default=os.environ.get('ADMIN_USERNAME', DEFAULT_ADMIN_USERNAME),
        help='Nombre de usuario admin. Default: ADMIN_USERNAME o Admin.',
    )
    parser.add_argument(
        '--password',
        default=os.environ.get('ADMIN_PASSWORD', DEFAULT_ADMIN_PASSWORD),
        help='Contraseña admin. Default: ADMIN_PASSWORD o la contraseña seed.',
    )
    parser.add_argument(
        '--keep-password',
        action='store_true',
        help='Si el usuario ya existe, no cambia su contraseña.',
    )
    return parser.parse_args()


def get_password(args, user_exists):
    if args.keep_password and user_exists:
        return None
    if args.password:
        return args.password

    label = 'Contraseña admin'
    password = getpass.getpass(f'{label}: ')
    if not password:
        print('Error: la contraseña no puede estar vacía')
        sys.exit(1)

    confirm = getpass.getpass(f'Confirmar {label.lower()}: ')
    if password != confirm:
        print('Error: las contraseñas no coinciden')
        sys.exit(1)

    return password


def main():
    args = parse_args()
    username = (args.username or '').strip().lower()
    if not username:
        print('Error: el nombre de usuario no puede estar vacío')
        sys.exit(1)

    uri = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/')
    db_name = os.environ.get('MONGODB_DB', 'scrappsa')
    users_col = MongoClient(uri)[db_name]['users']
    users_col.create_index('username', unique=True)

    existing = users_col.find_one({'username': username})
    password = get_password(args, bool(existing))

    if existing:
        update = {
            'is_admin': True,
            'is_active': True,
            'updated_at': datetime.utcnow(),
        }
        if password:
            update['password_hash'] = generate_password_hash(
                password,
                method='pbkdf2:sha256',
            )

        users_col.update_one({'_id': existing['_id']}, {'$set': update})
        print(f"Usuario admin '{username}' actualizado")
        return

    users_col.insert_one({
        'username': username,
        'password_hash': generate_password_hash(password, method='pbkdf2:sha256'),
        'created_at': datetime.utcnow(),
        'is_active': True,
        'is_admin': True,
    })
    print(f"Usuario admin '{username}' creado")


if __name__ == '__main__':
    main()
