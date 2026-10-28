"""Read-only Chromium cookie provider.

The real cookie database is never opened in place.  It is copied to a
private temporary directory first, the copy is opened read-only, and the
copy is deleted afterwards.  Cookie values are never printed or logged. Server rotations alone may be
persisted via the CLI-owned Keychain session store.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie, CookieError as ParseCookieError
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import aes
from .errors import CookieError, UsageError

# Profile roots relative to ~/Library/Application Support, in detection order.
BROWSERS = {
    "dia": ("Dia/User Data", "Dia Safe Storage", "Dia"),
    "chrome": ("Google/Chrome", "Chrome Safe Storage", "Google Chrome"),
    "edge": ("Microsoft Edge", "Microsoft Edge Safe Storage", "Microsoft Edge"),
    "brave": ("BraveSoftware/Brave-Browser", "Brave Safe Storage", "Brave Browser"),
    "arc": ("Arc/User Data", "Arc Safe Storage", "Arc"),
    "chromium": ("Chromium", "Chromium Safe Storage", "Chromium"),
}


def select_browser() -> Tuple[Path, str, str]:
    root = Path.home() / "Library" / "Application Support"
    name = os.environ.get("PFIN_BROWSER")
    if name is not None:
        if name not in BROWSERS:
            raise UsageError("PFIN_BROWSER must be one of: " + ", ".join(BROWSERS))
    else:
        name = next(
            (key for key, (directory, _, _) in BROWSERS.items()
             if (root / directory / "Default" / "Cookies").exists()),
            "dia",  # Preserve the default when no profile exists yet.
        )
    directory, service, app = BROWSERS[name]
    return root / directory / "Default" / "Cookies", service, app


DEFAULT_COOKIE_DB, DEFAULT_KEYCHAIN_SERVICE, BROWSER_APP = select_browser()


def browser_label() -> str:
    return BROWSER_APP


# Chromium on macOS uses PBKDF2-HMAC-SHA1 with these constants for v10.
PBKDF2_SALT = b"saltysalt"
PBKDF2_ITERATIONS = 1003
PBKDF2_LENGTH = 16
AES_IV = b" " * 16


def derive_key(password: bytes) -> bytes:
    if not password:
        raise CookieError("钥匙串返回了空密码，无法解密 Cookie")
    return hashlib.pbkdf2_hmac(
        "sha1", password, PBKDF2_SALT, PBKDF2_ITERATIONS, dklen=PBKDF2_LENGTH
    )


def macos_keychain_password(service: str = DEFAULT_KEYCHAIN_SERVICE) -> bytes:
    """Read the Safe Storage password from the login Keychain.

    This is deliberately only called by a live fetch, never by --dry-run or
    by the unit tests.  The child's stderr is discarded because it may contain
    account details; no cookie value is ever passed to the child.
    """

    try:
        completed = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-w", "-s", service],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CookieError("找不到 macOS 的 security 命令") from exc
    except subprocess.TimeoutExpired as exc:
        raise CookieError(
            "读取钥匙串超时（可能需要在弹窗中授权）",
            f"请在浏览器运行时允许本工具访问 '{service}'",
        ) from exc
    if completed.returncode != 0 or not completed.stdout.strip():
        raise CookieError(
            f"无法从钥匙串读取 '{service}'",
            "请确认浏览器正在使用该钥匙串项；首次运行可能需要在系统弹窗中允许访问",
        )
    return completed.stdout.rstrip(b"\r\n")


def expiry_datetime(expires_utc: int) -> Optional[datetime]:
    """Convert Chromium's 1601-based microseconds to an aware datetime.

    ``0`` means a session cookie, which has no absolute expiry; ``None`` is
    returned for that case or for an unrepresentable value.
    """

    try:
        value = int(expires_utc or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=value
        )
    except (OverflowError, ValueError):
        return None


def _is_expired(expires_utc: int, now: Optional[datetime] = None) -> bool:
    # Chromium stores microseconds since 1601-01-01 UTC; 0 means session cookie.
    expires = expiry_datetime(expires_utc)
    if expires is None:
        return False
    return expires <= (now or datetime.now(timezone.utc))


def _strip_host_prefix(plaintext: bytes, host_key: str) -> bytes:
    """Strip the 32-byte SHA256(host_key) prefix used by newer DBs."""

    candidates = {host_key}
    if host_key.startswith("."):
        candidates.add(host_key[1:])
    for candidate in candidates:
        digest = hashlib.sha256(candidate.encode("utf-8")).digest()
        if plaintext.startswith(digest):
            return plaintext[len(digest):]
    return plaintext


def decrypt_v10_value(encrypted: bytes, host_key: str, key: bytes) -> str:
    """Decrypt a Chromium v10 value and return the cookie text.

    Only the v10 scheme is supported.  App-bound v20 values intentionally
    fail closed rather than being guessed at.
    """

    if not encrypted.startswith(b"v10"):
        raise CookieError("不支持的 Cookie 加密版本（仅支持 Chromium v10）")
    payload = encrypted[3:]
    if not payload or len(payload) % aes.BLOCK_SIZE:
        raise CookieError("Cookie 密文长度异常")
    try:
        plaintext = aes.cbc_decrypt(key, AES_IV, payload)
    except ValueError as exc:
        raise CookieError(
            "Cookie 解密失败（钥匙串密码可能不匹配，或数据库格式已变化）"
        ) from exc
    plaintext = _strip_host_prefix(plaintext, host_key)
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        # Chromium cookie values should be UTF-8.  Keep a stable value rather
        # than crashing on a single unrelated cookie.
        return plaintext.decode("utf-8", "replace")


def domain_matches(host_key: str, host: str) -> bool:
    host = host.lower().rstrip(".")
    key = host_key.lower().rstrip(".")
    if not key:
        return False
    if key.startswith("."):
        bare = key[1:]
        return host == bare or host.endswith("." + bare)
    return host == key


def _path_matches(cookie_path: str, request_path: str) -> bool:
    """RFC 6265 path-match: the cookie path must be a prefix of the request."""

    cookie_path = cookie_path or "/"
    request_path = request_path or "/"
    if request_path == cookie_path:
        return True
    if not request_path.startswith(cookie_path):
        return False
    return cookie_path.endswith("/") or request_path[len(cookie_path)] == "/"


@dataclass
class Cookie:
    host_key: str
    name: str
    value: str
    path: str = "/"
    expires_utc: int = 0
    updated_utc: int = 0
    secure: bool = False

    @property
    def key(self):
        return (self.host_key.lower(), self.name, self.path)

    def __repr__(self) -> str:  # never include the value
        return f"Cookie(host_key={self.host_key!r}, name={self.name!r})"


@dataclass
class StoredCookie:
    """Cookie metadata read without decrypting or exposing its value.

    ``storage`` is one of ``plain``, ``v10``, ``unsupported`` or ``empty``.
    This is what ``status`` uses to report presence and expiry
    without calling the Keychain.
    """

    host_key: str
    name: str
    path: str
    expires_utc: int
    has_value: bool
    storage: str
    updated_utc: int = 0

    def expires_at(self) -> Optional[datetime]:
        return expiry_datetime(self.expires_utc)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return _is_expired(self.expires_utc, now)

    def is_session(self) -> bool:
        return not self.expires_utc or self.expires_utc <= 0

    def __repr__(self) -> str:  # never include a value
        return (
            f"StoredCookie(host_key={self.host_key!r}, name={self.name!r}, "
            f"storage={self.storage!r})"
        )


class CookieJar:
    """In-memory cookie set.  Its repr only lists names."""

    def __init__(self, cookies: Optional[Iterable[Cookie]] = None):
        # Concurrent year workers share one jar: every mutation and every
        # Cookie-header build takes this lock, so a rotated value can never be
        # read half-applied.
        self._lock = threading.RLock()
        self._cookies: List[Cookie] = list(cookies or [])
        self.session_store = None
        self.rotated = {}
        self.skipped_unsupported = 0
        self.skipped_decrypt_failed = 0

    def add(self, cookie: Cookie) -> None:
        with self._lock:
            self._cookies.append(cookie)

    def set_cookie(self, name: str, value: str, host_key: str, path: str = "/") -> None:
        """Update an in-memory cookie (e.g. a refreshed _m_h5_tk)."""

        # Legacy fetchers call this after HttpClient has already processed the
        # response. Do not discard its expiry, Secure flag or timestamp.
        key = (host_key.lower(), name, path)
        with self._lock:
            if any(c.key == key and c.value == value for c in self._cookies):
                return
            self._cookies = [c for c in self._cookies if c.key != key]
            self._cookies.append(Cookie(host_key, name, value, path))

    def apply_response(self, response):
        """Apply RFC-scoped Set-Cookie, including deletion tombstones."""
        with self._lock:
            return self._apply_response_locked(response)

    def _apply_response_locked(self, response):
        if getattr(response, '_applied_jar', None) is self:
            return response._applied_names
        origin = urllib.parse.urlsplit(response.url)
        host = origin.hostname or ""
        now = time.time()
        updated = int((now + 11644473600) * 1_000_000)
        changes = []
        for line in getattr(response, "set_cookies", ()):
            parsed = SimpleCookie()
            try:
                parsed.load(line)
            except ParseCookieError:
                continue
            for name, morsel in parsed.items():
                domain = "." + morsel['domain'].lower().lstrip('.') if morsel['domain'] else host
                # No unrelated/public-suffix injection into a site's session.
                if not domain_matches(domain, host):
                    continue
                if self.session_store and not self.session_store.accepts(domain):
                    continue
                if domain.startswith('.') and '.' not in domain[1:]:
                    continue
                path = morsel['path']
                if not path.startswith('/'):
                    path = origin.path.rsplit('/', 1)[0] or '/'
                expires = 0
                try:
                    if morsel['max-age']:
                        expires = int((now + int(morsel['max-age']) + 11644473600) * 1_000_000)
                    elif morsel['expires']:
                        expires = int((parsedate_to_datetime(morsel['expires']).timestamp() + 11644473600) * 1_000_000)
                except (ValueError, OverflowError, TypeError):
                    continue
                cookie = Cookie(domain, name, morsel.value, path, expires, updated, bool(morsel['secure']))
                if cookie.secure and origin.scheme != 'https':
                    continue
                if name.startswith('__Secure-') and not cookie.secure:
                    continue
                if name.startswith('__Host-') and (not cookie.secure or morsel['domain'] or path != '/'):
                    continue
                if _is_expired(expires):
                    cookie.value = ''  # retain only tombstone, never an expired secret
                self._cookies = [c for c in self._cookies if c.key != cookie.key]
                self._cookies.append(cookie)
                self.rotated[cookie.key] = cookie
                changes.append(name)
        if changes and self.session_store:
            self.session_store.save(self.rotated.values())
        response._applied_jar = self
        response._applied_names = changes
        return changes

    def get_cookie(
        self,
        name: str,
        host: Optional[str] = None,
        *,
        path: Optional[str] = None,
    ) -> Optional["Cookie"]:
        """Return the best matching :class:`Cookie` without exposing it in repr."""

        with self._lock:
            candidates = [
                c for c in self._cookies
                if c.name == name and c.value and not _is_expired(c.expires_utc)
            ]
        if host is not None:
            matching = [c for c in candidates if domain_matches(c.host_key, host)]
            if not matching:
                return None
            matching.sort(
                key=lambda c: (
                    -len(c.path),
                    -c.updated_utc,
                    0 if not c.host_key.startswith(".") else 1,
                    -len(c.host_key),
                    c.path,
                )
            )
            if path is not None:
                for cookie in matching:
                    if _path_matches(cookie.path, path):
                        return cookie
                return None
            return matching[0]
        return candidates[0] if candidates else None

    def get(
        self, name: str, host: Optional[str] = None, *, path: Optional[str] = None
    ) -> Optional[str]:
        cookie = self.get_cookie(name, host, path=path)
        return cookie.value if cookie is not None else None

    def entries(self) -> List["Cookie"]:
        with self._lock:
            return list(self._cookies)

    def listing(self) -> List[Tuple[str, str, str]]:
        """(host_key, path, name) tuples; values are deliberately omitted."""

        with self._lock:
            return sorted({(c.host_key, c.path, c.name) for c in self._cookies})

    def has_any(self, names: Sequence[str], host: Optional[str] = None) -> bool:
        return any(self.get(name, host) is not None for name in names)

    def names(self) -> List[str]:
        with self._lock:
            return sorted({c.name for c in self._cookies})

    def header_for(self, host: str, *, path: Optional[str] = None, secure: bool = True) -> str:
        """Build a Cookie header for *host* without ever logging values."""

        with self._lock:
            cookies = list(self._cookies)
        matching = [
            c
            for c in cookies
            if domain_matches(c.host_key, host)
            and c.value and not _is_expired(c.expires_utc)
            and (secure or not c.secure)
            and (path is None or _path_matches(c.path, path))
        ]
        # One cookie per name: prefer host-only, then longer/more specific.
        by_name = {}
        for cookie in sorted(
            matching,
            key=lambda c: (
                -len(c.path),
                -c.updated_utc if c.name.startswith("_m_h5_") else 0,
                0 if not c.host_key.startswith(".") else 1,
                -len(c.host_key),
            ),
        ):
            by_name.setdefault(cookie.name, cookie)
        return "; ".join(
            f"{name}={cookie.value}" for name, cookie in sorted(by_name.items())
        )

    def count(self) -> int:
        with self._lock:
            return len(self._cookies)

    def count_for(self, host: str) -> int:
        with self._lock:
            return sum(1 for c in self._cookies if domain_matches(c.host_key, host))

    def domain_summary(self) -> List[Dict[str, object]]:
        """Group cookie *names* by stored host_key.  Values never leave the jar."""

        grouped: Dict[str, List[str]] = {}
        with self._lock:
            cookies = list(self._cookies)
        for cookie in cookies:
            grouped.setdefault(cookie.host_key, []).append(cookie.name)
        return [
            {
                "host_key": host_key,
                "cookies": len(names),
                "names": sorted(set(names)),
            }
            for host_key, names in sorted(grouped.items())
        ]

    def __repr__(self) -> str:
        return f"CookieJar(names={self.names()!r})"


class CookieProvider:
    """Injectable cookie source for tests and for live browser access."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        password: Optional[bytes] = None,
        password_loader: Optional[Callable[[], bytes]] = None,
        keychain_service: str = DEFAULT_KEYCHAIN_SERVICE,
        host_suffixes: Optional[Sequence[str]] = None,
        session_store=None,
    ):
        self.session_store = session_store
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_COOKIE_DB
        self.password = password
        self.password_loader = password_loader
        self.keychain_service = keychain_service
        self.host_suffixes = list(host_suffixes or [])

    def _password(self) -> bytes:
        if self.password is not None:
            return self.password
        if self.password_loader is not None:
            return self.password_loader()
        return macos_keychain_password(self.keychain_service)

    def _copy_database(self, temp_dir: Path) -> Path:
        if not self.db_path.exists():
            raise CookieError(
                f"找不到 Cookie 数据库: {self.db_path}",
                "请确认浏览器已安装并至少登录过一次",
            )
        target = temp_dir / "Cookies"
        try:
            shutil.copy2(self.db_path, target)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.db_path) + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, Path(str(target) + suffix))
        except OSError as exc:
            raise CookieError("无法读取 Cookie 数据库（可能正被浏览器占用）") from exc
        os.chmod(target, 0o600)
        return target

    def _select_rows(self, connection: sqlite3.Connection) -> List[Tuple]:
        available = {row[1] for row in connection.execute('PRAGMA table_info(cookies)')}
        timestamp = 'last_update_utc' if 'last_update_utc' in available else ('creation_utc' if 'creation_utc' in available else '0')
        secure = 'is_secure' if 'is_secure' in available else '0'
        columns = ", ".join(
            ["host_key", "name", "value", "encrypted_value", "path", "expires_utc", timestamp, secure]
        )
        suffixes = [s.lstrip(".").lower() for s in self.host_suffixes if s]
        if not suffixes:
            sql = f"SELECT {columns} FROM cookies"
            return list(connection.execute(sql))
        clauses = []
        params: List[str] = []
        for suffix in suffixes:
            clauses.append("(lower(host_key) = ? OR lower(host_key) LIKE ?)")
            params.extend([suffix, "%." + suffix])
        sql = f"SELECT {columns} FROM cookies WHERE {' OR '.join(clauses)}"
        return list(connection.execute(sql, params))

    def _read_rows(self) -> List[Tuple]:
        """Copy the database and return raw rows; the original is never opened."""

        temp_dir = Path(tempfile.mkdtemp(prefix="pfin-cookies-"))
        try:
            copied = self._copy_database(temp_dir)
            try:
                if Path(str(copied) + "-wal").exists():
                    # Let SQLite replay the copied WAL inside the 0700 temp
                    # directory; the original database is never opened.
                    connection = sqlite3.connect(str(copied))
                else:
                    connection = sqlite3.connect(
                        copied.as_uri() + "?mode=ro", uri=True
                    )
            except sqlite3.Error as exc:
                raise CookieError("无法打开 Cookie 数据库副本") from exc
            try:
                return self._select_rows(connection)
            except sqlite3.Error as exc:
                raise CookieError(
                    "Cookie 数据库结构不是预期的 Chromium 格式"
                ) from exc
            finally:
                connection.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def inspect(self) -> List[StoredCookie]:
        """Return cookie metadata without decrypting any value.

        No Keychain access is performed.  Expired cookies are included so
        ``status`` can show their expiry instead of silently dropping them.
        """

        cookies: List[StoredCookie] = []
        for host_key, name, value, encrypted, path, expires_utc, updated_utc, secure in self._read_rows():
            if isinstance(encrypted, str):
                encrypted = encrypted.encode("latin-1", "replace")
            encrypted = bytes(encrypted or b"")
            if encrypted:
                storage = "v10" if encrypted.startswith(b"v10") else "unsupported"
                has_value = True
            elif value:
                storage = "plain"
                has_value = True
            else:
                storage = "empty"
                has_value = False
            cookies.append(
                StoredCookie(
                    host_key=host_key or "",
                    name=name or "",
                    path=path or "/",
                    expires_utc=int(expires_utc or 0),
                    has_value=has_value,
                    storage=storage,
                    updated_utc=int(updated_utc or 0),
                )
            )
        if self.session_store:
            merged = {(c.host_key.lower(), c.name, c.path): c for c in cookies}
            for c in self.session_store.load():
                old = merged.get(c.key)
                if old is None or c.updated_utc > old.updated_utc:
                    merged[c.key] = StoredCookie(c.host_key, c.name, c.path, c.expires_utc, bool(c.value), 'plain', c.updated_utc)
            cookies = list(merged.values())
        return cookies

    def load(self, now: Optional[datetime] = None) -> CookieJar:
        password = self._password()
        key = derive_key(password)
        rows = self._read_rows()

        jar = CookieJar()
        first_decrypt_error: Optional[CookieError] = None
        for host_key, name, value, encrypted, path, expires_utc, updated_utc, secure in rows:
            if encrypted:
                if isinstance(encrypted, str):
                    encrypted = encrypted.encode("latin-1", "replace")
                if not bytes(encrypted).startswith(b"v10"):
                    jar.skipped_unsupported += 1
                    continue
                try:
                    cookie_value = decrypt_v10_value(
                        bytes(encrypted), host_key or "", key
                    )
                except CookieError as exc:
                    first_decrypt_error = first_decrypt_error or exc
                    jar.skipped_decrypt_failed += 1
                    continue
            elif value:
                cookie_value = value
            else:
                continue
            if not cookie_value:
                continue
            jar.add(Cookie(host_key or "", name or "", cookie_value, path or "/", int(expires_utc or 0), int(updated_utc or 0), bool(secure)))
        if self.session_store:
            rotated = self.session_store.load()
            merged = {c.key: c for c in jar.entries()}
            for c in rotated:
                if c.key not in merged or c.updated_utc > merged[c.key].updated_utc:
                    merged[c.key] = c
            jar._cookies = list(merged.values())
            jar.session_store = self.session_store
            jar.rotated = {c.key: c for c in rotated}
        jar._cookies = [c for c in jar._cookies if c.value and not _is_expired(c.expires_utc, now)]
        if jar.count() == 0:
            if first_decrypt_error is not None:
                raise first_decrypt_error
            raise CookieError(
                "Cookie 数据库中没有可用的登录 Cookie",
                "请先在浏览器中登录对应站点",
            )
        return jar
