#!/usr/bin/env python3
"""
Cambia la contraseña de un usuario existente en ScrappSA.
Uso: python3 change_password.py <username>
"""

import sys
import os
import getpass
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient
from werkzeug.security import generate_password_hash

load_dotenv(Path(__file__).parent / '.env')


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 change_password.py <username>")
        sys.exit(1)

    username = sys.argv[1].strip().lower()

    uri = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/')
    col = MongoClient(uri)['scrappsa']['users']

    user = col.find_one({'username': username})
    if not user:
        print(f"Error: el usuario '{username}' no existe")
        sys.exit(1)

    nueva = getpass.getpass("Nueva contraseña: ")
    if not nueva:
        print("Error: la contraseña no puede estar vacía")
        sys.exit(1)

    confirmar = getpass.getpass("Confirmar contraseña: ")
    if nueva != confirmar:
        print("Error: las contraseñas no coinciden")
        sys.exit(1)

    col.update_one(
        {'username': username},
        {'$set': {'password_hash': generate_password_hash(nueva, method='pbkdf2:sha256')}},
    )
    print(f"Contraseña de '{username}' actualizada exitosamente")


if __name__ == '__main__':
    main()
