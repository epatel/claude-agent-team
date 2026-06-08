"""Multi-user accounts for the web console (see cards/control-transports.md).

Passwords are hashed with stdlib ``hashlib.scrypt`` + a per-user salt — no extra
crypto dependency. Sessions are carried in a signed cookie by Starlette's
``SessionMiddleware`` (configured in web.py); this module only owns user records.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time

_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def create_user(conn: sqlite3.Connection, username: str, password: str) -> int:
    if get_user_by_name(conn, username) is not None:
        raise ValueError("username already taken")
    salt = os.urandom(16)
    cur = conn.execute(
        "INSERT INTO users (username, pw_hash, pw_salt, created_at) VALUES (?, ?, ?, ?)",
        (username, _hash(password, salt).hex(), salt.hex(), time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def verify_user(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    row = get_user_by_name(conn, username)
    if row is None:
        return None
    expected = bytes.fromhex(row["pw_hash"])
    if hmac.compare_digest(_hash(password, bytes.fromhex(row["pw_salt"])), expected):
        return row
    return None
