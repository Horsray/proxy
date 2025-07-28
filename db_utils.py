import os
import sqlite3
import json
from threading import Lock

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'users.db')

db_lock = Lock()


def init_db():
    """Initialize the SQLite database and create table if not exists."""
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, data TEXT)'
        )
        conn.commit()


def load_users() -> dict:
    """Load all users from the SQLite database as a dictionary."""
    init_db()
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute('SELECT username, data FROM users')
        users = {}
        for username, data in cursor.fetchall():
            try:
                users[username] = json.loads(data)
            except Exception:
                users[username] = {}
        return users


def save_users(users: dict) -> None:
    """Persist the provided users dictionary to the SQLite database."""
    init_db()
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        existing = {row[0] for row in conn.execute('SELECT username FROM users')}
        for username, info in users.items():
            conn.execute(
                'REPLACE INTO users (username, data) VALUES (?, ?)',
                (username, json.dumps(info, ensure_ascii=False))
            )
            existing.discard(username)
        for username in existing:
            conn.execute('DELETE FROM users WHERE username = ?', (username,))
        conn.commit()
