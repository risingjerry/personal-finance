"""Binance, read-only: account snapshot, deposits/withdrawals, recent C2C, futures income, Pay transfers,
and the official futures history archives (request once, download when ready).

Responses are cached by request identity under ``<out>``, so a re-run only asks for what it has not seen.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Dict, List

from ..kit.apikey import DAY_MS, opener, secret, windows, write_private

ALLOW = {
    "api.binance.com": {
        "/sapi/v1/pay/transactions", "/api/v3/myTrades", "/api/v3/account", "/sapi/v1/account/apiRestrictions",
        "/sapi/v1/capital/deposit/hisrec", "/sapi/v1/capital/withdraw/history", "/sapi/v1/c2c/orderMatch/listUserOrderHistory",
    },
    "fapi.binance.com": {
        "/fapi/v3/account", "/fapi/v1/income",
        *[f"/fapi/v1/{k}/asyn" for k in ("order", "trade", "income")],
        *[f"/fapi/v1/{k}/asyn/id" for k in ("order", "trade", "income")],
    },
}


class Client:
    def __init__(self, out: Path) -> None:
        self.out = Path(out)
        self.op = opener()
        self.key = secret("BINANCE_API_KEY")
        self.secret = secret("BINANCE_SECRET_KEY")
        self.now = json.load(self.op.open("https://api.binance.com/api/v3/time", timeout=30))["serverTime"]
        self.offset = self.now - int(time.time() * 1000)

    def get(self, host: str, path: str, params: Dict):
        if path not in ALLOW.get(host, set()):
            raise ValueError("Endpoint not allowed")
        ident = hashlib.sha256(json.dumps([host, path, params], sort_keys=True).encode()).hexdigest()[:20]
        target = self.out / (path.split("/")[-1] + "-" + ident + ".json")
        if not path.endswith("/asyn/id") and target.exists():
            return json.loads(target.read_text())["data"]
        for attempt in range(3):
            query = urllib.parse.urlencode({**params, "timestamp": int(time.time() * 1000) + self.offset, "recvWindow": 20000})
            sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            req = urllib.request.Request(f"https://{host}{path}?{query}&signature={sig}", headers={"X-MBX-APIKEY": self.key})
            try:
                with self.op.open(req, timeout=35) as response:
                    data = json.load(response)
                if not path.endswith("/asyn/id"):
                    write_private(target, {"host": host, "path": path, "params": params, "fetched_ms": int(time.time() * 1000), "data": data})
                return data
            except urllib.error.HTTPError as exc:
                try:
                    code = json.loads(exc.read()).get("code")
                except Exception:
                    code = None
                if exc.code == 429:
                    time.sleep(min(int(exc.headers.get("Retry-After", "5")), 60))
                    continue
                raise RuntimeError(f"HTTP {exc.code}; exchange code {code}") from None
            except (TimeoutError, urllib.error.URLError):
                if attempt == 2:
                    raise RuntimeError("Network request failed") from None
                time.sleep(2)
        raise RuntimeError("Rate limited; retry later")


def fetch(out: Path, start: int) -> List[Dict]:
    """Account snapshot and histories since ``start`` (ms); returns one coverage entry per category."""

    c = Client(out)
    summary: List[Dict] = []

    def collect(name, fn):
        try:
            item = {"category": name, "status": "collected", "records": fn()}
        except RuntimeError as exc:
            item = {"category": name, "status": "gap", "reason": str(exc)}
        summary.append(item)
        write_private(c.out / "coverage.json", {"requested_start_ms": start, "end_ms": c.now, "categories": summary}, replace=True)

    def pages(path, host, begin, span, kind="offset", extra=None):
        count = 0
        for a, b in windows(begin, c.now, span):
            page, offset = 1, 0
            while True:
                p = {"startTime": a, "endTime": b, "limit": 1000, **(extra or {})}
                if kind == "c2c":
                    p = {"startTimestamp": a, "endTimestamp": b, "page": page, "rows": 100, **(extra or {})}
                elif kind == "offset":
                    p["offset"] = offset
                else:
                    p["page"] = page
                d = c.get(host, path, p)
                rows = d.get("data", []) if kind == "c2c" else d
                if not isinstance(rows, list):
                    raise RuntimeError("Unexpected response shape")
                count += len(rows)
                if len(rows) < (100 if kind == "c2c" else 1000):
                    break
                page += 1
                offset += 1000
                if page > 100:
                    raise RuntimeError("Pagination safety bound reached")
        return count

    collect("permissions", lambda: 1 if c.get("api.binance.com", "/sapi/v1/account/apiRestrictions", {}) else 0)
    collect("spot_account", lambda: len(c.get("api.binance.com", "/api/v3/account", {"omitZeroBalances": "true"}).get("balances", [])))
    for name, path in (("deposits", "/sapi/v1/capital/deposit/hisrec"), ("withdrawals", "/sapi/v1/capital/withdraw/history")):
        collect(name, lambda path=path: pages(path, "api.binance.com", start, 80 * DAY_MS))
    for side in ("BUY", "SELL"):
        collect("c2c_recent_" + side, lambda side=side: pages(
            "/sapi/v1/c2c/orderMatch/listUserOrderHistory", "api.binance.com", max(start, c.now - 180 * DAY_MS), 29 * DAY_MS, "c2c", {"tradeType": side}))
    collect("usd_futures_account", lambda: len(c.get("fapi.binance.com", "/fapi/v3/account", {}).get("positions", [])))
    collect("usd_futures_income_recent", lambda: pages("/fapi/v1/income", "fapi.binance.com", max(start, c.now - 89 * DAY_MS), 29 * DAY_MS, "page"))
    collect("pay_transfers", lambda: fetch_pay(c, start))
    return summary


def fetch_pay(c: Client, start: int, *, max_requests: int = 200) -> int:
    """Binance Pay transfers (including internal UID transfers); windows split when a page is full."""

    found: Dict[str, Dict] = {}
    requests = [0]

    def walk(a, b):
        if requests[0] >= max_requests:
            raise RuntimeError("Bounded request limit reached; coverage incomplete")
        d = c.get("api.binance.com", "/sapi/v1/pay/transactions", {"startTime": a, "endTime": b, "limit": 100})
        requests[0] += 1
        if d.get("code") != "000000" or d.get("success") is not True:
            raise RuntimeError("Provider did not return success")
        rows = d.get("data") or []
        if len(rows) == 100:
            if a == b:
                raise RuntimeError("100 transfers at one timestamp; needs the official export")
            mid = (a + b) // 2
            walk(a, mid)
            walk(mid + 1, b)
            return
        for row in rows:
            found[row["transactionId"]] = row

    for a, b in windows(start, c.now, 80 * DAY_MS):
        walk(a, b)
    totals: Dict[str, Decimal] = {}
    for row in found.values():
        totals[row["currency"]] = totals.get(row["currency"], Decimal(0)) + Decimal(row["amount"])
    write_private(c.out / "pay-coverage.json", {"start_ms": start, "end_ms": c.now, "unique_transfers": len(found),
                                                "net_by_asset": {k: str(v) for k, v in totals.items()}}, replace=True)
    return len(found)


def futures_archive(out: Path, start: int, split: int) -> List[Dict]:
    """Official USD-M futures history archives: request them once, then download the finished files.

    Binance builds each archive asynchronously; run again later to download. Signed URLs are never stored.
    """

    c = Client(out)
    jobs_path = c.out / "futures-export-jobs.json"
    if not jobs_path.exists():
        jobs = []
        for kind in ("order", "trade", "income"):
            for a, b in ((start, split - 1), (split, c.now)):
                try:
                    d = c.get("fapi.binance.com", f"/fapi/v1/{kind}/asyn", {"startTime": a, "endTime": b})
                    jobs.append({"kind": kind, "start": a, "end": b, "downloadId": d["downloadId"]})
                except RuntimeError as exc:
                    jobs.append({"kind": kind, "start": a, "end": b, "status": "gap", "reason": str(exc)})
                time.sleep(35)  # the export endpoint is heavily rate limited
        write_private(jobs_path, jobs, replace=True)
        return [{"kind": j["kind"], "status": "requested" if "downloadId" in j else "gap"} for j in jobs]
    summary = []
    for job in json.loads(jobs_path.read_text()):
        if "downloadId" not in job:
            continue
        name = f"futures-{job['kind']}-{job['start']}-{job['end']}.download"
        dest = c.out / name
        if dest.exists():
            summary.append({"kind": job["kind"], "status": "already_downloaded"})
            continue
        d = c.get("fapi.binance.com", f"/fapi/v1/{job['kind']}/asyn/id", {"downloadId": job["downloadId"]})
        url = d.get("url")
        if not url:
            summary.append({"kind": job["kind"], "status": d.get("status") or "pending"})
            continue
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.username or not parts.hostname:
            raise RuntimeError("Invalid download URL")
        with c.op.open(url, timeout=90) as response:  # no exchange API headers on the file download
            payload = response.read()
        if payload[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                if archive.testzip():
                    raise RuntimeError("ZIP integrity failed")
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(dest, "xb") as handle:
            handle.write(payload)
        dest.chmod(0o600)
        summary.append({"kind": job["kind"], "status": "downloaded", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    return summary
