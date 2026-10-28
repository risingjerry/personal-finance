"""Test helpers: fixtures and a synthetic Chromium cookie DB.

No helper reads the real browser database or calls the Keychain.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

import os

from .aes import cbc_encrypt
from .cookies import AES_IV, derive_key
from .session import SessionStore

# Real captures are private: they live outside the repository; tests that need them skip when absent.
FIXTURES = Path(os.environ.get("PFIN_PRIVATE_FIXTURES") or Path.home() / "Library" / "Application Support" / "personal-finance" / "test-fixtures")
PASSWORD = b"test-password"

# ``mock_sessions`` installs a process-wide patch on ``SessionStore._call``.
# CPython 3.9 does not always run module cleanups at end of run (the module
# teardown is skipped when the fixture was handled by a nested suite), so the
# patch can survive into later test modules.  Tests that need the genuine
# keychain transport pin this reference explicitly instead of trusting the
# cleanup ordering.
REAL_SESSION_CALL = SessionStore._call


def load_fixture(name: str):
    path = FIXTURES / name
    if not path.exists():
        raise unittest.SkipTest(f"fixture missing: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def encrypt_cookie(value: str, host_key: str, password: bytes = PASSWORD,
                   *, host_prefix: bool = True) -> bytes:
    key = derive_key(password)
    plaintext = value.encode("utf-8")
    if host_prefix:
        plaintext = hashlib.sha256(host_key.encode("utf-8")).digest() + plaintext
    return b"v10" + cbc_encrypt(key, AES_IV, plaintext)


def make_cookie_db(
    path: Path,
    cookies,
    *,
    password: bytes = PASSWORD,
    host_prefix: bool = True,
) -> Path:
    """cookies: iterable of (host_key, name, value, encrypted_value)."""

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE cookies (
                host_key TEXT NOT NULL,
                name TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                encrypted_value BLOB NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '/',
                expires_utc INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        for host_key, name, value, encrypted in cookies:
            if encrypted == b"v10" and value:
                encrypted = encrypt_cookie(
                    value, host_key, password, host_prefix=host_prefix
                )
            connection.execute(
                "INSERT INTO cookies (host_key, name, encrypted_value) "
                "VALUES (?, ?, ?)",
                (host_key, name, encrypted),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def mock_sessions():
    """CLI tests must never read or write the real login Keychain."""
    from unittest.mock import patch
    stub = patch('connectors.kit.session.SessionStore._call', return_value=b'[]')
    stub.start()
    unittest.addModuleCleanup(stub.stop)
