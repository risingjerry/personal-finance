"""Read credentials (API keys, merchant keys) without ever exposing their values.

A secret is looked up by name, first hit wins:

1. the environment variable of that name;
2. the macOS login Keychain, generic password with service ``pfin`` and account NAME
   (store one with ``security add-generic-password -s pfin -a NAME -w``: it prompts for the value);
3. a secret manager you name in ``~/.config/pfin/secrets.json``::

       {"get": ["my-vault", "get", "--fd", "{fd}", "{name}"], "list": ["my-vault", "list"]}

   ``get`` must write the value to the file descriptor it is given as ``{fd}`` (never stdout or argv);
   ``list`` (optional) prints one stored name per line, so ``status`` can check names without reading values.

Values are read through a pipe into this process and kept in memory.  Nothing is logged, written to disk,
or placed in a command-line argument; stderr of the helpers is discarded.  Error messages only ever contain
secret *names*.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .errors import AuthRequired

KEYCHAIN_SERVICE = "pfin"
MANAGER_CONFIG = Path("~/.config/pfin/secrets.json")
SECURITY = "/usr/bin/security"
SETUP_HINT = (
    "设环境变量 {name}，或存进钥匙串：security add-generic-password -s pfin -a {name} -w（回车后输入值）；"
    "本工具不会打印、记录或猜测任何凭据值"
)


def _keychain(name: str, value: bool = True) -> Optional[str]:
    if not os.path.exists(SECURITY):
        return None
    argv = [SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name] + (["-w"] if value else [])
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").rstrip("\r\n") if value else "present"


def _manager() -> Dict:
    try:
        return json.loads(MANAGER_CONFIG.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _from_manager(name: str) -> Optional[str]:
    argv = _manager().get("get")
    if not isinstance(argv, list) or not argv:
        return None
    read_fd, write_fd = os.pipe()
    try:
        try:
            result = subprocess.run(
                [str(a).replace("{fd}", str(write_fd)).replace("{name}", name) for a in argv],
                pass_fds=(write_fd,),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        # The child inherited write_fd.  Close our copy before reading so the
        # pipe reaches EOF as soon as the child exits.
        os.close(write_fd)
        write_fd = -1
        chunks = []
        while True:
            chunk = os.read(read_fd, 8192)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)
    if result.returncode != 0:
        return None
    return b"".join(chunks).decode("utf-8", "replace").rstrip("\r\n")


def read_secret(name: str) -> Optional[str]:
    """Return one secret value, or ``None`` when no backend has it."""

    if not name:
        return None
    return os.environ.get(name) or _keychain(name) or _from_manager(name) or None


def available(names: Sequence[str]) -> List[str]:
    """The names some backend holds, checked without reading values where the backend allows it."""

    listed: Optional[set] = None
    found = []
    for name in names:
        if os.environ.get(name) or _keychain(name, value=False):
            found.append(name)
            continue
        if listed is None:
            listed = set()
            argv = _manager().get("list")
            if isinstance(argv, list) and argv:
                try:
                    proc = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=30, check=False)
                    if proc.returncode == 0:
                        listed = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
                except (OSError, subprocess.SubprocessError):
                    pass
        if name in listed:
            found.append(name)
    return found


def load_secrets(names: Sequence[str]) -> Dict[str, Optional[str]]:
    """Read the named credentials once; missing names map to ``None``."""

    return {name: read_secret(name) for name in names}


def missing_names(values: Dict[str, Optional[str]], required: Iterable[str]) -> List[str]:
    return [name for name in required if not values.get(name)]


def require_secrets(values: Dict[str, Optional[str]], required: Sequence[str],
                    platform: str) -> None:
    """Raise a value-free error listing only the missing credential names."""

    missing = missing_names(values, required)
    if missing:
        raise AuthRequired(f"{platform}: 缺少凭据 " + ", ".join(missing), SETUP_HINT.format(name="NAME"))
