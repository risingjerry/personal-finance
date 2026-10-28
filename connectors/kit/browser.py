"""Playwright engine discovery (Python or Node, never auto-installed) and secret redaction for browser logs.
Shared by the connectors that need a real page (Didi, 拿去花)."""

from __future__ import annotations
import datetime
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from .errors import BrowserUnavailable


_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|_)("
    r"token|ticket|wsgsig|sign|signature|authorization|cookie|"
    r"session|session_id|sec_session_id|openid|open_id|phone|a3"
    r")(?:$|_)"
)


_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|ticket|wsgsig|sign|access_token|authorization|"
    r"openid|phone)=)[^&#]*"
)


_SECRET_INLINE_RE = re.compile(
    r"(?i)([\"']?(?:token|ticket|wsgsig|sign|access_token|authorization|"
    r"openid|phone)[\"']?\s*[:=]\s*[\"']?)[^\"'&\s,}]+"
)


def _key_is_secret(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return bool(_SECRET_KEY_RE.search(key))


def _redact_text(value: str, secrets: Sequence[str]) -> str:
    text = value
    for secret in secrets:
        # Very short cookie values (for example "1") are not credentials and
        # replacing them would mangle unrelated diagnostics.
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
            quoted = urllib.parse.quote(secret, safe="")
            if quoted and quoted != secret:
                text = text.replace(quoted, "[REDACTED]")
    text = _SECRET_QUERY_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_INLINE_RE.sub(r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)(cookie\s*:\s*)[^\r\n]+", r"\1[REDACTED]", text
    )
    return text


def redact_browser(value: Any, secrets: Sequence[str] = ()) -> Any:
    """Recursively remove token/ticket/sign material from a JSON value."""

    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_browser(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_browser(item, secrets) for item in value)
    if isinstance(value, dict):
        output: Dict[Any, Any] = {}
        for key, child in value.items():
            if _key_is_secret(key):
                output[key] = "[REDACTED]"
            else:
                output[key] = redact_browser(child, secrets)
        return output
    return value


def redact_url(url: str, secrets: Sequence[str] = ()) -> str:
    """Keep a diagnostic URL but redact credential query parameters."""

    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return _redact_text(url, secrets)
    query = []
    for key, value in urllib.parse.parse_qsl(
        parts.query, keep_blank_values=True
    ):
        query.append((key, "[REDACTED]" if _key_is_secret(key) else value))
    fragment = _SECRET_QUERY_RE.sub(r"\1[REDACTED]", parts.fragment)
    fragment = _SECRET_INLINE_RE.sub(r"\1[REDACTED]", fragment)
    netloc = parts.netloc
    # Never retain userinfo (user:password@host).
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            netloc,
            parts.path,
            urllib.parse.urlencode(query),
            fragment,
        )
    )


@dataclass
class BrowserEngine:
    kind: str  # "python" or "node"
    executable: Optional[str] = None
    note: str = ""

    def describe(self) -> str:
        label = {
            "python": "Python Playwright",
            "node": "Node Playwright",
        }.get(self.kind, self.kind)
        if self.executable:
            return f"{label} (Chromium: {self.executable})"
        return label


def _browser_cache_dirs() -> List[Path]:
    dirs: List[Path] = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env and env not in ("0", "1"):
        dirs.append(Path(env).expanduser())
    dirs.extend(
        [
            Path.home() / "Library" / "Caches" / "ms-playwright",
            Path.home() / ".cache" / "ms-playwright",
            Path.home() / "AppData" / "Local" / "ms-playwright",
        ]
    )
    return dirs


def _revision_key(path: str) -> int:
    match = re.search(r"(?:chromium|headless_shell)-(\d+)", path)
    return int(match.group(1)) if match else 0


def _binary_score(path: str) -> int:
    # Prefer a full Chromium (works headless and headed) over the headless
    # shell, then prefer a newer revision.
    lowered = path.lower()
    if "headless" in lowered:
        return 1
    return 2


def find_installed_chromium() -> Optional[str]:
    """Find an already-installed Playwright Chromium.  Never downloads."""

    patterns = [
        "chromium-*/chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-mac-*/Chromium.app/Contents/MacOS/Chromium",
        "chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell",
        "chromium-*/chrome-linux/chrome",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
        "chromium-*/chrome-win/chrome.exe",
        "chromium_headless_shell-*/chrome-win/headless_shell.exe",
    ]
    candidates: List[str] = []
    for base in _browser_cache_dirs():
        if not base.exists():
            continue
        for pattern in patterns:
            candidates.extend(glob.glob(str(base / pattern)))
    executable = [
        path
        for path in candidates
        if os.path.isfile(path) and os.access(path, os.X_OK)
    ]
    if not executable:
        return None
    executable.sort(
        key=lambda path: (_binary_score(path), _revision_key(path), path),
        reverse=True,
    )
    return executable[0]


def _detect_python_playwright() -> Optional[BrowserEngine]:
    try:
        spec = importlib.util.find_spec("playwright.sync_api")
    except (ImportError, ModuleNotFoundError, ValueError):
        return None
    if spec is None:
        return None
    pw = None
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        expected = ""
        try:
            expected = pw.chromium.executable_path
        except Exception:
            expected = ""
        if expected and Path(expected).exists():
            return BrowserEngine("python", expected)
        fallback = find_installed_chromium()
        if fallback:
            note = (
                "Python Playwright 期望的 Chromium 不存在，改用已安装的 "
                f"{fallback}"
            )
            return BrowserEngine("python", fallback, note)
        return None
    except Exception:
        return None
    finally:
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass


def _node_module_paths() -> List[str]:
    paths: List[str] = []
    for raw in (os.environ.get("NODE_PATH") or "").split(os.pathsep):
        if raw:
            paths.append(raw)
    paths.extend(
        [
            str(Path.home() / ".npm-global" / "lib" / "node_modules"),
            "/opt/homebrew/lib/node_modules",
            "/usr/local/lib/node_modules",
            str(Path.home() / ".local" / "lib" / "node_modules"),
        ]
    )
    seen = set()
    unique = []
    for path in paths:
        if path not in seen and Path(path).exists():
            seen.add(path)
            unique.append(path)
    return unique


def _node_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["NODE_PATH"] = os.pathsep.join(_node_module_paths())
    return env


def _detect_node_playwright() -> Optional[BrowserEngine]:
    node = shutil.which("node")
    if not node:
        return None
    script = (
        "const pw=require('playwright');"
        "process.stdout.write(pw.chromium.executablePath());"
    )
    try:
        proc = subprocess.run(
            [node, "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env=_node_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    expected = proc.stdout.decode("utf-8", "replace").strip()
    if expected and Path(expected).exists():
        return BrowserEngine("node", expected)
    fallback = find_installed_chromium()
    if fallback:
        return BrowserEngine(
            "node",
            fallback,
            "Node Playwright 期望的 Chromium 不存在，改用已安装的 "
            f"{fallback}",
        )
    return None


def _python_missing_reason() -> str:
    try:
        spec = importlib.util.find_spec("playwright.sync_api")
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None
    if spec is None:
        return "Python 包 playwright 未安装"
    if find_installed_chromium() is None:
        return "包已安装，但未找到 Chromium（本工具不会下载）"
    return "包已安装，但启动探针失败"


def _node_missing_reason() -> str:
    node = shutil.which("node")
    if not node:
        return "未安装 node"
    try:
        proc = subprocess.run(
            [node, "-e", "process.stdout.write(require.resolve('playwright'))"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
            env=_node_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "node 已安装，但无法执行 playwright 探针"
    if proc.returncode != 0:
        return "node 已安装，但找不到 playwright 模块"
    if find_installed_chromium() is None:
        return "playwright 已安装，但未找到 Chromium（本工具不会下载）"
    return "playwright 已安装，但启动探针失败"


def detect_browser_engine(prefer: str = "auto") -> BrowserEngine:
    """Return an installed Playwright engine, or explain what is missing.

    ``prefer`` is ``auto``, ``python`` or ``node``.  No install/download is
    ever attempted.
    """

    if prefer not in ("auto", "python", "node"):
        raise ValueError("prefer must be auto, python or node")
    missing: List[str] = []
    if prefer in ("auto", "python"):
        engine = _detect_python_playwright()
        if engine is not None:
            return engine
        missing.append("Python Playwright: " + _python_missing_reason())
    if prefer in ("auto", "node"):
        engine = _detect_node_playwright()
        if engine is not None:
            return engine
        missing.append("Node Playwright: " + _node_missing_reason())
    raise BrowserUnavailable(
        "没有可用的 Playwright Chromium，已停止（不会自动下载）",
        "\n".join(missing)
        + "\n请自行安装 Playwright 及 Chromium 后重试；本工具不会执行 "
        "`playwright install`，也不会下载任何浏览器。",
    )
