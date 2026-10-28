"""Owner-owned Didi web session: the entry URL and rotated cookies.

The Didi web app signs its own requests, but it only does so when it has a
valid *entry URL* (the ``#/order-list?...&token=...`` page the app opens) and
a live login session.  Reading the browser's cookies on every run is not enough: the
page rotates its cookies, so the freshest ones live in the browser context
that just ran, not in the browser.

This module stores that state for the owner only:

* ``entry.url``  - the last order-list URL (contains a login token, 0600);
* ``session.json`` - cookies captured after the last run (0600).

Nothing here is ever printed or passed on a command line.  If no stored state
exists, the entry URL is taken from the browser's own history (the owner opened the
page there) and the cookies from the browser's cookie database.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from ..kit.cookies import DEFAULT_COOKIE_DB
from ..kit.env import getenv
from ..kit.session import DEFAULT_STATE_DIR

ENTRY_HOST = "common.diditaxi.com.cn"
ENTRY_PATH = "/general/webEntry"
ENTRY_MARKER = "#/order-list"
SESSION_VERSION = 1

# Only Didi hosts are persisted; unrelated browser cookies must never be copied.
DIDI_HOST_SUFFIXES = (".diditaxi.com.cn", "diditaxi.com.cn", ".udache.com",
                      "udache.com", ".xiaojukeji.com", "xiaojukeji.com")


def state_dir() -> Path:
    override = getenv("PFIN_DIDI_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path(DEFAULT_STATE_DIR).expanduser() / "didi"


def entry_url_path() -> Path:
    return state_dir() / "entry.url"


def session_path() -> Path:
    return state_dir() / "session.json"


def fares_cache_path() -> Path:
    return state_dir() / "fares.json"


def profile_dir() -> Path:
    """Chromium profile owned by this CLI (cookies + localStorage + IDB)."""

    return state_dir() / "profile"


def login_marker_path() -> Path:
    return profile_dir() / ".pfin-login-ok"


def profile_ready() -> bool:
    """True once `didi login` has completed at least one real login."""

    return login_marker_path().is_file()


def mark_profile_ready() -> Path:
    _ensure_private_dir(profile_dir())
    return write_private_text(login_marker_path(), "ok\n")


def default_browser_history_path() -> Path:
    return DEFAULT_COOKIE_DB.with_name("History")


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def write_private_text(path: Path, text: str) -> Path:
    """Atomically write a 0600 file (parent directory 0700)."""

    _ensure_private_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise
    return path


def read_private_text(path: Path) -> Optional[str]:
    try:
        if not path.is_file():
            return None
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            return None  # refuse group/world-readable credential files
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Entry URL
# ---------------------------------------------------------------------------


def validate_entry_url(url: str) -> str:
    """Return ``url`` if it is a Didi order-list entry URL, else raise."""

    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != ENTRY_HOST:
        raise ValueError("entry URL must be https://common.diditaxi.com.cn/...")
    if parts.path != ENTRY_PATH:
        raise ValueError("entry URL must point at /general/webEntry")
    if ENTRY_MARKER not in url:
        raise ValueError("entry URL must contain #/order-list")
    return url


def latest_browser_entry_url(history_path: Optional[Path] = None) -> Optional[str]:
    """Newest Didi order-list URL with a token from the browser's history database.

    The live database is never opened: it is copied to a private temp dir first,
    exactly like the cookie reader does.
    """

    path = Path(history_path) if history_path else default_browser_history_path()
    if not path.is_file():
        return None
    temp_dir = tempfile.mkdtemp(prefix="pfin-browser-hist-")
    try:
        os.chmod(temp_dir, 0o700)
        copy = Path(temp_dir) / "History"
        shutil.copy2(path, copy)
        uri = f"file:{copy}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            rows = conn.execute(
                "SELECT url FROM urls WHERE url LIKE ? "
                "ORDER BY last_visit_time DESC LIMIT 40",
                (f"%{ENTRY_HOST}%webEntry%",),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    for (raw,) in rows:
        url = str(raw or "")
        if "token=" not in url or len(url) < 60:
            continue
        try:
            return validate_entry_url(url)
        except ValueError:
            continue
    return None


def load_entry_url(explicit_file: Optional[Path] = None) -> Optional[str]:
    candidates: List[Path] = []
    if explicit_file is not None:
        candidates.append(Path(explicit_file).expanduser())
    env_file = getenv("PFIN_DIDI_ENTRY_FILE")
    if env_file:
        candidates.append(Path(env_file).expanduser())
    candidates.append(entry_url_path())
    for candidate in candidates:
        raw = read_private_text(candidate)
        if not raw:
            continue
        try:
            return validate_entry_url(raw)
        except ValueError:
            continue
    return None


def save_entry_url(url: str, path: Optional[Path] = None) -> Path:
    return write_private_text(path or entry_url_path(), validate_entry_url(url) + "\n")


def resolve_entry_url(
    explicit_file: Optional[Path] = None,
    *,
    fallback: str = "",
) -> "tuple[str, str]":
    """Return ``(url, source)`` for the order-list page.

    Order: explicit file -> ``$PFIN_DIDI_ENTRY_FILE`` -> stored
    ``entry.url`` -> the URL stored with the last session -> browser history ->
    ``fallback``.  A URL found in browser history is cached to ``entry.url``.
    """

    url = load_entry_url(explicit_file)
    if url:
        return url, "file"
    session = load_session()
    if session and session.get("entry_url"):
        try:
            return validate_entry_url(str(session["entry_url"])), "session"
        except ValueError:
            pass
    latest = latest_browser_entry_url()
    if latest:
        try:
            save_entry_url(latest)
        except OSError:
            pass
        return latest, "browser-history"
    return fallback, "default"


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


def _is_didi_domain(domain: str) -> bool:
    domain = (domain or "").lstrip(".").lower()
    return any(
        domain == suffix.lstrip(".") or domain.endswith(suffix)
        for suffix in DIDI_HOST_SUFFIXES
    )


def clean_cookies(cookies: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only Didi cookies, in Playwright's cookie format."""

    cleaned: List[Dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        value = cookie.get("value")
        if not name or value is None or not _is_didi_domain(domain):
            continue
        item: Dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(cookie.get("path") or "/"),
        }
        if cookie.get("expires") is not None:
            item["expires"] = cookie.get("expires")
        if "httpOnly" in cookie:
            item["httpOnly"] = bool(cookie.get("httpOnly"))
        if "secure" in cookie:
            item["secure"] = bool(cookie.get("secure"))
        if cookie.get("sameSite") in ("Strict", "Lax", "None"):
            item["sameSite"] = cookie.get("sameSite")
        cleaned.append(item)
    return cleaned


def load_session(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    raw = read_private_text(path or session_path())
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list):
        return None
    return data


def session_usable(
    data: Optional[Dict[str, Any]], *, now: Optional[float] = None
) -> bool:
    """True when the stored session has at least one unexpired cookie."""

    if not data:
        return False
    cookies = data.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return False
    import time as _time

    current = _time.time() if now is None else now
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        expires = cookie.get("expires")
        if expires is None:
            return True
        if not isinstance(expires, (int, float)):
            return True
        if expires <= 0 or expires > current:
            return True
    return False


def save_session(
    cookies: Iterable[Dict[str, Any]],
    *,
    entry_url: Optional[str] = None,
    path: Optional[Path] = None,
    now: Optional[str] = None,
) -> Path:
    import datetime

    payload: Dict[str, Any] = {
        "version": SESSION_VERSION,
        "saved_at": now
        or datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        "cookies": clean_cookies(cookies),
    }
    if entry_url:
        payload["entry_url"] = validate_entry_url(entry_url)
    return write_private_text(
        path or session_path(),
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def merge_cookies(
    primary: Iterable[Dict[str, Any]],
    secondary: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """``primary`` wins per (domain, name); ``secondary`` fills the gaps."""

    merged: Dict[tuple, Dict[str, Any]] = {}
    order: List[tuple] = []
    for source in (primary, secondary):
        for cookie in source:
            if not isinstance(cookie, dict):
                continue
            key = (str(cookie.get("domain") or ""), str(cookie.get("name") or ""))
            if key[0] == "" or key[1] == "":
                continue
            if key not in merged:
                order.append(key)
                merged[key] = cookie
    return [merged[key] for key in order]


def session_summary(path: Optional[Path] = None) -> Dict[str, Any]:
    """Non-secret description of the stored session (for ``didi status``)."""

    data = load_session(path)
    if not data:
        return {"stored": False}
    cookies = data.get("cookies") or []
    names = sorted({str(cookie.get("name")) for cookie in cookies if cookie.get("name")})
    expires = [
        cookie.get("expires")
        for cookie in cookies
        if isinstance(cookie.get("expires"), (int, float)) and cookie.get("expires") > 0
    ]
    return {
        "stored": True,
        "saved_at": data.get("saved_at"),
        "cookies": len(cookies),
        "names": names,
        "entry_url": bool(data.get("entry_url")),
        "expires": int(max(expires)) if expires else None,
    }


# ---------------------------------------------------------------------------
# Fare cache (so monthly refreshes only fetch new orders)
# ---------------------------------------------------------------------------


def load_fare_cache(path: Optional[Path] = None) -> Dict[str, str]:
    raw = read_private_text(path or fares_cache_path())
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in data.items()
        if isinstance(value, (str, int, float)) and str(value)
    }


def save_fare_cache(fares: Dict[str, str], path: Optional[Path] = None) -> Path:
    clean = {
        str(key): str(value)
        for key, value in (fares or {}).items()
        if key and value not in (None, "")
    }
    return write_private_text(
        path or fares_cache_path(),
        json.dumps(clean, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
    )
