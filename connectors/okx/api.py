"""OKX, read-only API key: account config/balance/positions, bills archive, order and fill history.

Complements `okx fetch` (C2C orders through the okx-bills web session): this is the trading side.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List

from ..kit.apikey import opener, secret, write_private

ALLOW = {
    "/api/v5/asset/withdrawal-history", "/api/v5/asset/deposit-history", "/api/v5/account/config", "/api/v5/account/balance",
    "/api/v5/account/positions", "/api/v5/account/bills-archive", "/api/v5/trade/orders-history-archive", "/api/v5/trade/fills-history",
}


def sign(secret_key: str, timestamp: str, target: str) -> str:
    return base64.b64encode(hmac.new(secret_key.encode(), (timestamp + "GET" + target).encode(), hashlib.sha256).digest()).decode()


def fetch(out: Path) -> List[Dict]:
    out = Path(out)
    key, sec, pas = secret("OKX_FINANCE_API_KEY"), secret("OKX_FINANCE_SECRET_KEY"), secret("OKX_FINANCE_PASSPHRASE")
    op = opener()

    def get(path, params):
        if path not in ALLOW:
            raise ValueError("Endpoint not allowed")
        query = urllib.parse.urlencode(params)
        target = path + ("?" + query if query else "")
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        req = urllib.request.Request("https://www.okx.com" + target, headers={
            "User-Agent": "pfin-readonly", "OK-ACCESS-KEY": key, "OK-ACCESS-SIGN": sign(sec, ts, target),
            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": pas})
        try:
            with op.open(req, timeout=30) as response:
                d = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read()).get("code")
            except Exception:
                code = "non-json-response"
            raise RuntimeError(f"HTTP {exc.code}; exchange code {code}") from None
        if d.get("code") != "0":
            raise RuntimeError("exchange code " + str(d.get("code")))
        write_private(out / f"{path.split('/')[-1]}-{time.time_ns()}.json", {"path": path, "params": params, "data": d})
        return d["data"]

    jobs = [(p, {}, None) for p in ("/api/v5/account/config", "/api/v5/account/balance", "/api/v5/account/positions")]
    jobs.append(("/api/v5/account/bills-archive", {}, "billId"))
    for inst in ("SPOT", "MARGIN", "SWAP", "FUTURES", "OPTION"):
        jobs += [("/api/v5/trade/orders-history-archive", {"instType": inst}, "ordId"), ("/api/v5/trade/fills-history", {"instType": inst}, "billId")]
    # Deposit / withdrawal history page by timestamp, not by id: the first page (latest 100) only.
    jobs += [("/api/v5/asset/deposit-history", {}, None), ("/api/v5/asset/withdrawal-history", {}, None)]
    summary: List[Dict] = []
    for path, params, cursor_field in jobs:
        total, seen, p = 0, set(), dict(params)
        if cursor_field:
            p["limit"] = 100
        try:
            while True:
                rows = get(path, p)
                total += len(rows)
                if not cursor_field or len(rows) < 100:
                    break
                cursor = rows[-1][cursor_field]
                if cursor in seen:
                    raise RuntimeError("Repeated cursor")
                seen.add(cursor)
                p["after"] = cursor
                time.sleep(0.5)
            item = {"path": path, **params, "status": "collected", "records": total}
        except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
            item = {"path": path, **params, "status": "gap", "reason": str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}
        summary.append(item)
        write_private(out / "coverage.json", summary, replace=True)
        if item["status"] == "gap" and path.endswith("/config"):
            break  # the key itself is not usable
    return summary
