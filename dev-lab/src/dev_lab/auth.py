"""Multi-user accounts for the web console (see cards/control-transports.md).

Passwords are hashed with stdlib ``hashlib.scrypt`` + a per-user salt — no extra
crypto dependency. Sessions are carried in a signed cookie by Starlette's
``SessionMiddleware`` (configured in web.py); this module only owns user records.

Access model: the FIRST registered user becomes the **super-user** (no invite
needed). Every later registration must redeem an invite code the super-user
mints. Super-users manage the user db — create invites, block, delete, and reset
passwords.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time

_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)


# --- Users ----------------------------------------------------------------

def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def count_users(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM users ORDER BY id"))


def create_user(
    conn: sqlite3.Connection, username: str, password: str, *, is_super: bool = False
) -> int:
    if get_user_by_name(conn, username) is not None:
        raise ValueError("username already taken")
    salt = os.urandom(16)
    cur = conn.execute(
        "INSERT INTO users (username, pw_hash, pw_salt, is_super, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, _hash(password, salt).hex(), salt.hex(), int(is_super), time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def verify_user(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    """Return the user row on a correct password, else None. Blocked users still
    verify here — callers decide whether to reject them (see web.login)."""
    row = get_user_by_name(conn, username)
    if row is None:
        return None
    expected = bytes.fromhex(row["pw_hash"])
    if hmac.compare_digest(_hash(password, bytes.fromhex(row["pw_salt"])), expected):
        return row
    return None


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    salt = os.urandom(16)
    conn.execute(
        "UPDATE users SET pw_hash = ?, pw_salt = ? WHERE id = ?",
        (_hash(password, salt).hex(), salt.hex(), user_id),
    )
    conn.commit()


def set_blocked(conn: sqlite3.Connection, user_id: int, blocked: bool) -> None:
    conn.execute("UPDATE users SET blocked = ? WHERE id = ?", (int(blocked), user_id))
    conn.commit()


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    # Drop any invites the user created/consumed so foreign-key rows don't dangle.
    conn.execute("DELETE FROM invites WHERE created_by = ?", (user_id,))
    conn.execute("UPDATE invites SET used_by = NULL, used_at = NULL WHERE used_by = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()


# --- Invites --------------------------------------------------------------

def create_invite(conn: sqlite3.Connection, created_by: int) -> str:
    code = secrets.token_urlsafe(9)
    conn.execute(
        "INSERT INTO invites (code, created_by, created_at) VALUES (?, ?, ?)",
        (code, created_by, time.time()),
    )
    conn.commit()
    return code


def get_invite(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()


def list_invites(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM invites ORDER BY created_at DESC"))


def consume_invite(conn: sqlite3.Connection, code: str, user_id: int) -> bool:
    """Mark an unused invite as redeemed by ``user_id``. Returns False if the
    code is unknown or already used (an atomic guarded UPDATE)."""
    cur = conn.execute(
        "UPDATE invites SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL",
        (user_id, time.time(), code),
    )
    conn.commit()
    return cur.rowcount > 0
