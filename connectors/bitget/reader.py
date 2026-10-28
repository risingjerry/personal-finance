"""Bitget, read-only API key: assets, settings, and 90 days of orders and fills per product line.
Refuses to run with a key that is not read-only."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List

from ..kit.apikey import DAY_MS, opener, secret, windows, write_private

ALLOW = {
    "/api/v3/account/info", "/api/v3/account/settings", "/api/v3/account/assets", "/api/v3/trade/history-orders",
    "/api/v3/trade/fills", "/api/v3/account/financial-records", "/api/v3/account/funding-financial-records", "/api/v3/position/current-position",
}
CATEGORIES = ("SPOT", "MARGIN", "USDT-FUTURES", "COIN-FUTURES", "USDC-FUTURES")


class Client:
    def __init__(self, out: Path) -> None:
        self.out = Path(out)
        self.key, self.sec, self.pas = secret("BITGET_API_KEY"), secret("BITGET_SECRET_KEY"), secret("BITGET_PASSPHRASE")
        self.op = opener()

    def get(self, path: str, params=None):
        if path not in ALLOW:
            raise ValueError("Endpoint not allowed")
        params = params or {}
        query = urllib.parse.urlencode(sorted(params.items()))
        target = path + ("?" + query if query else "")
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(hmac.new(self.sec.encode(), (ts + "GET" + target).encode(), hashlib.sha256).digest()).decode()
        req = urllib.request.Request("https://api.bitget.com" + target, headers={
            "ACCESS-KEY": self.key, "ACCESS-SIGN": sig, "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": self.pas, "Content-Type": "application/json"})
        try:
            with self.op.open(req, timeout=30) as response:
                d = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read()).get("code")
            except Exception:
                code = None
            raise RuntimeError(f"HTTP {exc.code}; exchange code {code}") from None
        if d.get("code") != "00000":
            raise RuntimeError("exchange code " + str(d.get("code")))
        write_private(self.out / f"{path.split('/')[-1]}-{time.time_ns()}.json", {"path": path, "params": params, "data": d})
        return d["data"]


def fetch(out: Path, days: int = 89) -> List[Dict]:
    c = Client(out)
    info = c.get("/api/v3/account/info")
    if info.get("permType") not in ("readonly", "read-only"):
        raise RuntimeError("Refusing a Bitget API key that is not read-only")
    for path in ("/api/v3/account/assets", "/api/v3/account/settings"):
        c.get(path)
    end = int(time.time() * 1000)
    start = end - days * DAY_MS
    summary: List[Dict] = []
    for path in ("/api/v3/trade/history-orders", "/api/v3/trade/fills"):
        for category in CATEGORIES:
            count = 0
            try:
                for a, b in windows(start, end, 29 * DAY_MS):
                    cursor, seen = None, set()
                    while True:
                        params = {"category": category, "startTime": a, "endTime": b, "limit": 100}
                        if cursor:
                            params["cursor"] = cursor
                        d = c.get(path, params)
                        rows = d["list"] or []
                        count += len(rows)
                        nxt = d.get("cursor")
                        if not rows or not nxt:
                            break
                        if nxt in seen:
                            raise RuntimeError("Repeated cursor")
                        seen.add(nxt)
                        cursor = nxt
                        if len(seen) > 1000:
                            raise RuntimeError("Pagination safety bound")
                item = {"path": path, "category": category, "status": "collected", "rows": count}
            except RuntimeError as exc:
                item = {"path": path, "category": category, "status": "gap", "reason": str(exc)}
            summary.append(item)
            write_private(c.out / "coverage.json", {"start_ms": start, "end_ms": end, "categories": summary}, replace=True)
    return summary
