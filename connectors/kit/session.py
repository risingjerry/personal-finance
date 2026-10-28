"""CLI-owned rotated cookies. One login-Keychain generic password per site.

The worker receives JSON over stdin and returns it over a captured pipe only;
secrets never occur in argv, temporary files, diagnostics or security(1) input.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .errors import CookieError

SERVICE = 'pfin-session'
DEFAULT_STATE_DIR = '~/Library/Application Support/pfin-connectors'  # connectors' own state (browser profiles, archives)
ROOT = Path(__file__).resolve().parents[2]  # the checkout that contains the connectors package


class SessionStore:
    def __init__(self, site, host_suffixes):
        self.site = site
        self.host_suffixes = host_suffixes

    def _call(self, action, payload=b''):
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'connectors.kit.session', action, self.site],
                input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=30, check=False, env={**os.environ, 'PYTHONPATH': str(ROOT)},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CookieError('会话钥匙串不可用或授权超时') from exc
        if result.returncode:
            raise CookieError('会话钥匙串读写失败（未保存；请检查登录钥匙串授权）')
        return result.stdout

    def accepts(self, domain):
        domain = domain.lower().lstrip('.')
        return any(domain == s or domain.endswith('.' + s) for s in self.host_suffixes)

    def load(self):
        from .cookies import Cookie
        try:
            data = json.loads(self._call('read') or b'[]')
            if not isinstance(data, list):
                raise ValueError()
            cookies = [Cookie(**row) for row in data]
            for c in cookies:
                if (not self.accepts(c.host_key) or not c.name or
                        not isinstance(c.value, str) or not c.path.startswith('/') or
                        not isinstance(c.updated_utc, int) or not isinstance(c.expires_utc, int)):
                    raise ValueError()
            return cookies
        except (ValueError, TypeError, AttributeError) as exc:
            raise CookieError('会话钥匙串数据格式无效；可用 login <站点> --clear 清除') from exc

    def save(self, cookies):
        payload = json.dumps([asdict(c) for c in cookies], separators=(',', ':')).encode()
        self._call('write', payload)

    def clear(self):
        self._call('clear')


def _keychain(action, site, payload):
    """Security.framework ABI; no shell, security -w argument or plaintext file."""
    import ctypes as C
    sec = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
    cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    ptr, uint = C.c_void_p, C.c_uint32
    signatures = {
        'SecKeychainOpen': [C.c_char_p, C.POINTER(ptr)],
        'SecKeychainFindGenericPassword': [ptr, uint, C.c_char_p, uint, C.c_char_p, C.POINTER(uint), C.POINTER(ptr), C.POINTER(ptr)],
        'SecKeychainAddGenericPassword': [ptr, uint, C.c_char_p, uint, C.c_char_p, uint, C.c_char_p, C.POINTER(ptr)],
        'SecKeychainItemModifyAttributesAndData': [ptr, ptr, uint, C.c_char_p],
        'SecKeychainItemDelete': [ptr],
        'SecKeychainItemFreeContent': [ptr, ptr],
    }
    for name, args in signatures.items():
        getattr(sec, name).argtypes = args
        getattr(sec, name).restype = C.c_int32
    cf.CFRelease.argtypes = [ptr]
    cf.CFRelease.restype = None
    keychain, item, data, size = ptr(), ptr(), ptr(), uint()
    service, account = SERVICE.encode(), site.encode()
    def check(status):
        if status:
            raise CookieError('会话钥匙串操作失败')
    check(sec.SecKeychainOpen(str(Path.home() / 'Library/Keychains/login.keychain-db').encode(), C.byref(keychain)))
    try:
        status = sec.SecKeychainFindGenericPassword(keychain, len(service), service, len(account), account, C.byref(size), C.byref(data), C.byref(item))
        if status not in (0, -25300):  # errSecItemNotFound is the only empty case
            check(status)
        if action == 'read':
            return C.string_at(data, size.value) if status == 0 else b'[]'
        if action == 'clear':
            if status == 0:
                check(sec.SecKeychainItemDelete(item))
        elif action == 'write':
            if status == 0:
                check(sec.SecKeychainItemModifyAttributesAndData(item, None, len(payload), payload))
            else:
                check(sec.SecKeychainAddGenericPassword(keychain, len(service), service, len(account), account, len(payload), payload, None))
        else:
            raise ValueError()
        return b''
    finally:
        if data:
            sec.SecKeychainItemFreeContent(None, data)
        if item:
            cf.CFRelease(item)
        cf.CFRelease(keychain)


if __name__ == '__main__':
    try:
        action, site = sys.argv[1:]
        if action not in ('read', 'write', 'clear') or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', site):
            raise ValueError()
        output = _keychain(action, site, sys.stdin.buffer.read())
        # Only an anonymous pipe is allowed to receive the secret response.
        import stat
        if not stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode):
            raise ValueError()
        sys.stdout.buffer.write(output)
    except Exception:
        sys.exit(1)
