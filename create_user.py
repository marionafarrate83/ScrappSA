#!/usr/bin/env python3
"""
Crea un usuario en MongoDB para ScrappSA.
Uso: python create_user.py <username>
"""

import sys
import os
import getpass
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient
from werkzeug.security import generate_password_hash

load_dotenv(Path(__file__).parent / '.env')


def main():
    if len(sys.argv) < 2:
        print("Uso: python create_user.py <username>")
        sys.exit(1)

    username = sys.argv[1].strip().lower()
    if not username:
        print("Error: el nombre de usuario no puede estar vacío")
        sys.exit(1)

    password = getpass.getpass("Contraseña: ")
    if not password:
        print("Error: la contraseña no puede estar vacía")
        sys.exit(1)

    confirm = getpass.getpass("Confirmar contraseña: ")
    if password != confirm:
        print("Error: las contraseñas no coinciden")
        sys.exit(1)

    uri = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/')
    client = MongoClient(uri)
    col = client['scrappsa']['users']

    if col.find_one({'username': username}):
        print(f"Error: el usuario '{username}' ya existe")
        sys.exit(1)

    col.insert_one({
        'username': username,
        'password_hash': generate_password_hash(password, method='pbkdf2:sha256'),
        'created_at': datetime.utcnow(),
        'is_active': True,
    })

    print(f"Usuario '{username}' creado exitosamente")


if __name__ == '__main__':
    main()
