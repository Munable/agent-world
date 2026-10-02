"""Read-only liveness and optional local SQLite health probe; no identity needed."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import httpx


def check_health(url, *, universe='commons', db=None):
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
        response = client.get(url.rstrip('/') + '/health')
        response.raise_for_status()
        status = response.json()
        if status.get('ok') is not True or status.get('universe') != universe:
            raise ValueError('unexpected world health response')
    if db is not None:
        path = Path(db).expanduser().resolve()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
            if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite quick_check failed')
            if connection.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('SQLite foreign_key_check failed')
            if connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='roles'").fetchone()[0] != 1:
                raise ValueError('not an Agent World database')
    return {'ok': True, 'universe': universe, 'http': 'passed', 'sqlite': 'passed' if db else 'not_checked'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--universe', default='commons')
    parser.add_argument('--db', help='optional local database, opened read-only')
    args = parser.parse_args()
    try:
        print(json.dumps(check_health(args.url, universe=args.universe, db=args.db)))
    except (httpx.HTTPError, OSError, ValueError, sqlite3.Error) as exc:
        print('Health check failed: ' + type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
