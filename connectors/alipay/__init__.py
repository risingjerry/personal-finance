"""支付宝连接器：个人账单（官方年度导出，扫码登录）和商户账户流水（开放平台接口，免扫码）。

The work is done by the Node CLI in this folder (bin/alipay, its own off-screen Chrome profile under
~/.alipay-cli). This module gives it the same five verbs as every other connector:
  login      -> alipay auth login            (a person scans the QR code)
  status     -> alipay status
  keepalive  -> alipay auth status            (personal session; the merchant API needs none)
  fetch      -> [alipay sync] + alipay export --format ndjson     (personal)
                [alipay merchant sync] + alipay merchant list --kind funds   (--merchant)
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from typing import Dict, List

from ..kit.errors import AuthRequired, NetworkError, UsageError
from ..kit.rows import make_record, stable_id
from ..kit.runner import Connector

BIN = Path(__file__).resolve().parent / "bin" / "alipay"
BEIJING = datetime.timezone(datetime.timedelta(hours=8))


def _node(args, *argv: str, timeout: float = 3600, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = [str(BIN), *argv]
    if getattr(args, "home", None):
        cmd += ["--home", str(args.home)]
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout, check=False)


def _json(proc: subprocess.CompletedProcess) -> Dict:
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": {"code": "no_output", "message": (proc.stderr or "")[-200:]}}


def _raise(result: Dict, step: str) -> None:
    error = result.get("error") or {}
    code = error.get("code") or "failed"
    if code in ("need_login", "security_challenge", "browser_down"):
        raise AuthRequired(f"支付宝 {step}: {code}", "请运行 python3 -m connectors.alipay login 扫码登录后重试")
    raise NetworkError(f"支付宝 {step} 失败: {code}", str(error.get("message") or "")[:200])


def add_fetch_args(parser) -> None:
    parser.add_argument("--home", type=Path, default=None, help="alipay-cli 数据目录（第二个账号用另一个目录，默认 ~/.alipay-cli）")
    parser.add_argument("--sync", action="store_true", help="先从支付宝拉最新的官方导出（需要登录有效；只补缺的年份）")
    parser.add_argument("--merchant", action="store_true", help="改读商户账户的资金流水（开放平台接口，T+1，不用扫码）")
    parser.add_argument("--days", type=int, default=7, help="配合 --merchant：同步最近几天，默认 7")


def fetch(args, jar):
    warnings: List[str] = []
    if args.merchant:
        today = datetime.datetime.now(BEIJING).date()
        start = today - datetime.timedelta(days=max(1, args.days))
        result = _json(_node(args, "merchant", "sync", "--from", start.isoformat(), "--to", today.isoformat()))
        if not result.get("ok"):
            warnings.append(f"商户流水同步失败，只导出本机已有的：{(result.get('error') or {}).get('code')}")
        listed = _json(_node(args, "merchant", "list", "--kind", "funds", "--limit", "1000000"))
        if not listed.get("ok"):
            _raise(listed, "merchant list")
        data = listed.get("data") or {}
        return {"kind": "alipay_merchant_funds", "records": data.get("items") or data.get("records") or [],
                "_prefix": "alipay-merchant-funds", "_rows": "alipay-merchant"}, warnings
    if args.sync:
        result = _json(_node(args, "sync"))
        if not result.get("ok"):
            _raise(result, "sync")
    proc = _node(args, "export", "--format", "ndjson", "--out", "-")
    if proc.returncode:
        _raise(_json(proc), "export")
    records = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return {"kind": "alipay_bills", "records": records}, warnings


def rows(payload: Dict) -> List[Dict]:
    out = []
    if payload.get("kind") == "alipay_merchant_funds":
        for r in payload.get("records") or []:
            income, expense = r.get("income") or "0", r.get("expense") or "0"
            incoming = income not in ("0", "0.00", "")
            out.append(make_record(
                source="alipay-merchant", record_type="transaction", id=stable_id("alipay-merchant", "transaction", r.get("id")),
                occurred=r.get("occurred_at"), amount=income if incoming else str(expense).lstrip("-"), direction="in" if incoming else "out",
                merchant=r.get("counterparty"), title=(r.get("raw") or {}).get("备注") or r.get("item") or r.get("type"), status=r.get("type"),
                extra={k: r.get(k) for k in ("business_id", "order_id", "channel", "balance")},
            ))
        return out
    for r in payload.get("records") or []:
        out.append(make_record(
            source="alipay", record_type="transaction", id=stable_id("alipay", "transaction", r.get("trade_no")),
            occurred=r.get("paid_at") or r.get("created_at"), amount=r.get("amount"),
            direction=r.get("direction") if r.get("direction") in ("in", "out") else None,
            merchant=r.get("counterparty"), title=r.get("item"), status=r.get("status"),
            extra={k: r.get(k) for k in ("trade_no", "merchant_order_no", "type", "fee", "refund", "fund_state", "created_at", "paid_at", "note")},
        ))
    return out


def summary(payload: Dict) -> str:
    records = payload.get("records") or []
    when = sorted(str(r.get("paid_at") or r.get("created_at") or r.get("occurred_at") or "")[:10] for r in records)
    kind = "商户流水" if payload.get("kind") == "alipay_merchant_funds" else "个人账单"
    return f"alipay {kind}: {len(records)} 条" + (f" {when[0]} .. {when[-1]}" if when else "")


def login(args) -> int:
    return _node(args, "auth", "login", "--timeout", str(max(60, args.wait)), capture=False, timeout=args.wait + 120).returncode


def status(args):
    result = _json(_node(args, "status", timeout=120))
    if not result.get("ok"):
        return [f"支付宝：状态读取失败 {(result.get('error') or {}).get('code')}"], 3
    data = result.get("data") or {}
    personal, business, session = data.get("personal") or {}, data.get("business") or {}, data.get("session") or {}
    lines = [
        f"支付宝个人账单：{personal.get('records', 0)} 条（{personal.get('firstYear')}–{personal.get('lastYear')}）",
        f"商户流水：{(business.get('funds') or {}).get('records', 0)} 条，最新 {(business.get('funds') or {}).get('last')}",
        f"登录：{'浏览器未运行（需要时 login 会启动）' if not session.get('chromeUp') else '浏览器在运行'}",
    ]
    return lines, 0


def keepalive(args) -> int:
    result = _json(_node(args, "auth", "status", timeout=120))
    state = ((result.get("data") or {}).get("state")) or (result.get("error") or {}).get("code") or "unknown"
    print(json.dumps({"connector": "alipay", "status": state}) if getattr(args, "json", False) else f"支付宝登录：{state}")
    return 0 if state == "ok" else 3


def validate(args) -> None:
    if args.merchant and args.sync:
        raise UsageError("--merchant 和 --sync 不能同时用（商户流水自带同步）")


CONNECTOR = Connector(
    key="alipay", label="支付宝", prefix="alipay-bills", describe="个人账单（官方导出）和商户账户流水",
    fetch=fetch, summary=summary, rows=rows, add_fetch_args=add_fetch_args, validate=validate,
    login=login, status=status, keepalive=keepalive, login_kind="qr", keepalive_hours=24.0,
    record_types=("transaction",),
)
