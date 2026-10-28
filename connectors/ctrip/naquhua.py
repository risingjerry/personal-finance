"""携程拿去花 (Ctrip's credit line) bill records, fetched together with Ctrip orders.

The 拿去花 pages live on jr.ctrip.com and share the Ctrip login.  Their request
bodies are encrypted in the page, so this module does not build requests: it
hands the owner's Ctrip cookies (already loaded for the order fetch) to a
headless Chromium over stdin, lets the real pages load each record tab, and
keeps the plain JSON responses.  Read-only: nothing is clicked or submitted.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from ..kit.cookies import CookieJar
from ..kit.errors import AuthRequired, BrowserUnavailable, NetworkError

TABS = ("trade", "repay", "refund", "stage", "afterPay")
SCRIPT = Path(__file__).with_name("naquhua.cjs")


def playwright_cookies(jar: CookieJar) -> List[Dict]:
    """The jar's Ctrip cookies in Playwright's addCookies shape (values stay in memory)."""

    out = []
    for c in jar.entries():
        host = c.host_key.lower()
        if not (host.endswith("ctrip.com") or host.endswith("c-ctrip.com")):
            continue
        cookie = {"name": c.name, "value": c.value, "domain": c.host_key, "path": c.path or "/", "secure": bool(c.secure)}
        out.append(cookie)
    return out


def fetch(jar: CookieJar, *, timeout: float = 45.0, runner=None) -> Dict:
    """{"tabs": [{tab, ended, responses: [{api, status, body}]}]}; raises AuthRequired when the pages ask to log in."""

    cookies = playwright_cookies(jar)
    if not cookies:
        raise AuthRequired("没有携程登录 cookie", "请先在浏览器里登录携程，再运行 ctrip fetch")
    from ..kit.browser import _node_env, detect_browser_engine

    engine = detect_browser_engine("node")
    request = json.dumps({"cookies": cookies, "executable_path": engine.executable or "", "timeout_ms": int(timeout * 1000), "tabs": list(TABS)})
    run = runner or _run_node
    raw = run(request, timeout=timeout * len(TABS) + 60, env=_node_env())
    try:
        result = json.loads(raw.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise NetworkError("拿去花页面没有返回结果", "稍后重试") from exc
    if not result.get("ok"):
        raise NetworkError(f"拿去花页面读取失败: {result.get('error')}", str(result.get("message") or "")[:200])
    if any(t.get("login_required") for t in result.get("tabs") or []):
        raise AuthRequired("拿去花页面要求登录", "请在浏览器里重新登录携程后重试")
    return {"tabs": result.get("tabs") or []}


def _run_node(request: str, *, timeout: float, env: Dict[str, str]) -> str:
    import shutil

    node = shutil.which("node")
    if not node:
        raise BrowserUnavailable("找不到 node，无法读取拿去花页面", "请安装 Node.js 和 Playwright Chromium")
    proc = subprocess.run([node, str(SCRIPT)], input=request, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    return proc.stdout or "{}"


API_TAB = {"queryConsumerRecords": "trade", "queryRepayRecords": "repay", "queryRefundRecords": "refund", "queryInstallRecords": "stage"}


def records(naquhua: Dict) -> Dict[str, List[Dict]]:
    """Records per tab (trade / repay / refund / stage), de-duplicated across scrolled pages."""

    out: Dict[str, List[Dict]] = {t: [] for t in API_TAB.values()}
    seen = set()
    for tab in naquhua.get("tabs") or []:
        for response in tab.get("responses") or []:
            kind = API_TAB.get(response.get("api"))
            data = (response.get("body") or {}).get("data") or {}
            for record in (data.get("records") or []) if kind else []:
                key = (kind, record.get("billSerialNo") or record.get("serialNo") or json.dumps(record, sort_keys=True, ensure_ascii=False))
                if key in seen:
                    continue
                seen.add(key)
                out[kind].append(record)
    return out


def summarize(naquhua: Dict) -> Dict[str, int]:
    found = records(naquhua)
    return {"消费": len(found["trade"]), "还款": len(found["repay"]), "退款": len(found["refund"]), "分期": len(found["stage"])}
