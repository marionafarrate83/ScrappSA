#!/usr/bin/env python3
"""
Otorga rol de administrador a un usuario existente.
Uso: python3 make_admin.py <username>
"""
import sys
import os
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(Path(__file__).parent / '.env')


def main():
    username = sys.argv[1].strip().lower() if len(sys.argv) > 1 else 'mario'
    col = MongoClient(os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/'))['scrappsa']['users']
    result = col.update_one({'username': username}, {'$set': {'is_admin': True}})
    if result.matched_count:
        print(f"✓ '{username}' ahora es administrador")
    else:
        print(f"Error: usuario '{username}' no encontrado")


if __name__ == '__main__':
    main()
