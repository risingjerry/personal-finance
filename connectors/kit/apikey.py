"""Helpers for connectors that read with an API key (exchanges: binance, bitget, okx api).

Read-only API keys are looked up by name (kit/secrets.py: env, Keychain, a configured secret manager); values stay in memory. Nothing here can trade or withdraw.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Dict, Iterator, List, Sequence, Tuple

from .errors import AuthRequired
from .secrets import SETUP_HINT, available, read_secret

DAY_MS = 86_400_000


def secret(name: str) -> str:
    value = read_secret(name)
    if not value:
        raise AuthRequired(f"没有找到 {name}", "请存入只读 API Key：" + SETUP_HINT.format(name=name))
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the API-key headers to wherever it points: refuse it (urllib raises HTTPError)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def opener() -> urllib.request.OpenerDirector:
    """Opener for signed API calls: no redirects; urllib honours HTTPS_PROXY from the environment."""

    return urllib.request.build_opener(_NoRedirect)


def windows(start: int, end: int, span: int) -> Iterator[Tuple[int, int]]:
    while start <= end:
        stop = min(start + span - 1, end)
        yield start, stop
        start = stop + 1


def since_ms(value: str) -> int:
    """'YYYY-MM-DD' in Beijing time -> epoch milliseconds."""

    day = datetime.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    return int(day.timestamp() * 1000)


def write_private(path: Path, payload, *, replace: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if replace else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=None if not replace else 2)
    return path


def secret_names(names: Sequence[str]) -> List[str]:
    """Which of these names are stored (names only; values are never read)."""

    return available(names)


def key_status(label: str, required: Sequence[str]) -> Tuple[List[str], int]:
    """status for API-key connectors: are the keys stored? exit 3 if any is missing."""

    have = set(secret_names(required))
    lines = [f"{label}：{name} {'已存' if name in have else '缺失'}" for name in required]
    missing = [n for n in required if n not in have]
    if missing:
        lines.append(f"缺少 {', '.join(missing)}：请存入只读 API Key，" + SETUP_HINT.format(name="NAME"))
    return lines, 3 if missing else 0
