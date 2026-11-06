"""User store + auth helpers for the GUI.

A tiny SQLite file holds the seeded accounts. Passwords are hashed with
Werkzeug's pbkdf2:sha256; the file is created and seeded on first import
of ``ensure_user_store`` if it does not already exist.

Each user is bound to a single ``mode`` (``"technical"`` or ``"business"``).
``verify_credentials`` returns that mode on success or ``None`` on failure.

The ``login_required`` and ``mode_required`` decorators are wired by
``ChatWindow`` to gate the chat routes.

Credential sourcing (in priority order, per user):
1. ``{ROLE}_PASSWORD`` env var (e.g. ``MANAGER_PASSWORD``)
2. A securely-generated random password printed to stdout ONCE at seed
   time. No password ever appears in source or git history.

Rotate at any time with:
    python -m gui.auth --rotate manager
    python -m gui.auth --rotate business [--password 'new-pass']
"""

from __future__ import annotations

import argparse
import os
import secrets
import sqlite3
import sys
from functools import wraps
from typing import Callable

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


DEFAULT_USER_STORE = "mock_database/users.db"

# Roles to seed on first run. Passwords are NEVER hard-coded; each role
# pulls its password from the matching env var, or has a strong random
# one generated and printed at seed time.
ROLES = [
    {
        "role_key": "manager",
        "username_env": "MANAGER_USERNAME",
        "password_env": "MANAGER_PASSWORD",
        "default_username": "manager",
        "mode": "technical",
    },
    {
        "role_key": "business",
        "username_env": "BUSINESS_USERNAME",
        "password_env": "BUSINESS_PASSWORD",
        "default_username": "business",
        "mode": "business",
    },
]


def _generate_password() -> str:
    """22-char URL-safe random string (~130 bits of entropy)."""
    return secrets.token_urlsafe(16)


def _print_credentials_banner(records: list[dict]) -> None:
    """Print newly-seeded credentials once, clearly, to stderr.

    stderr is used so the message survives stdout redirection. Each record
    is ``{"username", "password", "mode", "source"}``.
    """
    if not records:
        return
    border = "=" * 70
    print(border, file=sys.stderr)
    print("AGENTIC-DB - INITIAL CREDENTIALS (save these now)", file=sys.stderr)
    print(border, file=sys.stderr)
    for rec in records:
        print(
            f"  {rec['mode']:>10}  user={rec['username']!r:<24} "
            f"password={rec['password']!r}  [{rec['source']}]",
            file=sys.stderr,
        )
    print(border, file=sys.stderr)
    print(
        "These will NOT be shown again. Rotate with:\n"
        "  python -m gui.auth --rotate <role>",
        file=sys.stderr,
    )
    print(border, file=sys.stderr)


# ---------------------------------------------------------------------------
# User store
# ---------------------------------------------------------------------------

def _open(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('technical', 'business'))
        )
        """
    )
    return conn


def ensure_user_store(path: str = DEFAULT_USER_STORE) -> str:
    """Create the users table if missing; seed missing roles.

    For each role not already present in the store:
      - If the matching ``{ROLE}_PASSWORD`` env var is set, use that.
      - Otherwise generate a strong random password and print it to
        stderr ONCE.

    Returns the resolved path so callers can log it.
    """
    conn = _open(path)
    try:
        existing = {
            row[0]: row[0]
            for row in conn.execute("SELECT username FROM users").fetchall()
        }
        newly_seeded: list[dict] = []
        for role in ROLES:
            username = os.environ.get(role["username_env"], role["default_username"])
            if username in existing:
                continue
            env_password = os.environ.get(role["password_env"], "")
            if env_password:
                password = env_password
                source = f"from ${role['password_env']}"
            else:
                password = _generate_password()
                source = "auto-generated"
            conn.execute(
                "INSERT INTO users (username, password_hash, mode) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), role["mode"]),
            )
            newly_seeded.append({
                "username": username,
                "password": password,
                "mode": role["mode"],
                "source": source,
            })
        conn.commit()
        _print_credentials_banner(newly_seeded)
    finally:
        conn.close()
    return path


def rotate_password(
    username: str,
    new_password: str | None = None,
    path: str = DEFAULT_USER_STORE,
) -> str:
    """Replace ``username``'s password hash. Returns the new password.

    If ``new_password`` is ``None``, a strong random one is generated.
    Raises ``ValueError`` if the username does not exist.
    """
    conn = _open(path)
    try:
        row = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,),
        ).fetchone()
        if not row:
            raise ValueError(f"No user with username={username!r}.")
        if not new_password:
            new_password = _generate_password()
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (generate_password_hash(new_password), username),
        )
        conn.commit()
    finally:
        conn.close()
    return new_password


def verify_credentials(
    username: str, password: str, path: str = DEFAULT_USER_STORE
) -> str | None:
    """Return the matching user's mode, or ``None`` on failure."""
    if not username or not password:
        return None
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT password_hash, mode FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    password_hash, mode = row
    if not check_password_hash(password_hash, password):
        return None
    return mode


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def _is_api_request() -> bool:
    return request.path.startswith("/api/")


def login_required(view: Callable):
    """Block routes for unauthenticated callers.

    Browser requests get a 302 to ``/``; API requests get a 401 JSON body.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("username") or not session.get("mode"):
            if _is_api_request():
                return jsonify({"error": "not_signed_in"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def mode_required(mode_name: str):
    """Block routes whose session-mode does not match ``mode_name``.

    Layered on top of ``login_required`` (which runs first). Used to keep
    the ``business`` user out of tech-write endpoints and vice versa.
    """
    def decorator(view: Callable):
        @wraps(view)
        def wrapper(*args, **kwargs):
            current = session.get("mode")
            if current != mode_name:
                if _is_api_request():
                    return jsonify({
                        "error": "wrong_mode",
                        "expected": mode_name,
                        "actual": current,
                    }), 403
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# CLI: rotate a password
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(
        prog="python -m gui.auth",
        description="Rotate a stored user's password.",
    )
    parser.add_argument(
        "--rotate",
        metavar="USERNAME",
        required=True,
        help="Username (e.g. 'manager' or 'business')",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Explicit new password. If omitted, a strong random one is generated.",
    )
    parser.add_argument(
        "--store",
        default=DEFAULT_USER_STORE,
        help=f"Path to the user store (default: {DEFAULT_USER_STORE})",
    )
    args = parser.parse_args()
    try:
        new_pw = rotate_password(args.rotate, args.password, args.store)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Rotated password for {args.rotate!r}.")
    print(f"  new password: {new_pw}")
    print("Save this value now; it cannot be recovered later.")


if __name__ == "__main__":
    _cli()
