"""OKX 连接器：C2C 订单和充提流水（Node 的 okx-bills，扫码登录），以及只读 API Key 的交易侧数据。

okx-bills (okx-bills/, Node) owns login, request-template capture and sync; this module gives it the five
verbs and turns its exports into rows. Never use the npm `okx` command: that is OKX's trading CLI.
  login      -> okx-bills auth login            (a person scans the QR code)
  status     -> okx-bills doctor --offline + auth status
  keepalive  -> the same two checks
  fetch      -> [okx-bills sync] + okx-bills export (orders, flows)
  api        -> read-only API key collection (okx.api), extra action
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Dict, List, Sequence

from ..kit.errors import AuthRequired, ConnectorError
from ..kit.rows import make_record, stable_id
from ..kit.runner import Connector

OKX_BILLS = Path(__file__).resolve().parent / "okx-bills" / "bin" / "okx-bills"


def _call(*args: str, timeout: float = 600.0) -> str:
    proc = subprocess.run([str(OKX_BILLS), *args], capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        try:
            error = json.loads(proc.stderr.strip().splitlines()[-1]).get("error") or {}
        except (ValueError, IndexError):
            error = {}
        code = error.get("code") or "failed"
        if code in ("need_login", "security_challenge"):
            raise AuthRequired(f"okx-bills: {code}", "请本人运行 python3 -m connectors.okx login 扫码登录后重试")
        raise ConnectorError(f"okx-bills {args[0]} 失败: {code}", str(error.get("message") or "")[:200])
    return proc.stdout


def _ndjson(*args: str) -> List[Dict]:
    return [json.loads(line) for line in _call(*args).splitlines() if line.strip()]


def order_rows(orders: Sequence[Dict]) -> List[Dict]:
    rows = []
    for o in orders:
        side = o.get("side")
        # Times are taken from epoch ms: the ISO strings are UTC and would be misread as Beijing time.
        rows.append(make_record(
            source="okx-c2c",
            record_type="order",
            id=stable_id("okx-c2c", "order", o.get("id")),
            occurred=o.get("created_ms"),
            amount=o.get("quote_amount"),
            direction="out" if side == "buy" else "in" if side == "sell" else None,
            merchant=o.get("counterparty"),
            title=f"OKX C2C {'买入' if side == 'buy' else '卖出'} {o.get('base_amount')} {o.get('base_currency')}",
            status=o.get("status"),
            extra={k: o.get(k) for k in ("public_id", "base_currency", "base_amount", "quote_currency", "price",
                                          "payment_method", "payment_status", "completed_ms", "paid_at")},
        ))
    return rows


def flow_rows(flows: Sequence[Dict]) -> List[Dict]:
    rows = []
    for f in flows:
        kind = f.get("type")
        rows.append(make_record(
            source="okx-flows",
            record_type="transaction",
            id=stable_id("okx-flows", "transaction", f.get("key") or f.get("id")),
            occurred=f.get("time_ms"),
            amount=f.get("amount"),  # in the coin below, not CNY
            direction="in" if kind == "deposit" else "out" if kind == "withdraw" else None,
            merchant=f.get("network"),
            title=f"OKX {'充值' if kind == 'deposit' else '提币'} {f.get('amount')} {f.get('currency')}",
            status=f.get("status"),
            extra={k: f.get(k) for k in ("currency", "network", "address", "txid", "fee", "fee_currency", "internal")},
        ))
    return rows


def add_fetch_args(parser) -> None:
    parser.add_argument("--sync", action="store_true", help="先运行 okx-bills sync 拉最新数据（需要有效会话）")


def fetch(args, jar):
    if args.sync:
        _call("sync")
    orders = _ndjson("export", "--format", "ndjson", "--include-cancelled", "--out", "-")
    flows = _ndjson("export", "--kind", "flows", "--format", "ndjson", "--out", "-")
    return {"kind": "okx_bills", "orders": orders, "flows": flows}, []


def rows(payload: Dict) -> List[Dict]:
    return order_rows(payload.get("orders") or []) + flow_rows(payload.get("flows") or [])


def summary(payload: Dict) -> str:
    return f"okx: c2c={len(payload.get('orders') or [])} flows={len(payload.get('flows') or [])}"


def plan(args) -> str:
    return "plan: okx fetch -> okx-bills export（C2C 订单 + 充提流水）" + ("，先 okx-bills sync" if args.sync else "") + "（dry-run：不联网）"


def login(args) -> int:
    return subprocess.run([str(OKX_BILLS), "auth", "login"], timeout=args.wait + 120, check=False).returncode


def status(args):
    return [_call("doctor", "--offline", timeout=60).strip(), _call("auth", "status", timeout=120).strip()], 0


def keepalive(args) -> int:
    lines, code = status(args)
    print(json.dumps({"connector": "okx", "lines": lines}, ensure_ascii=False) if getattr(args, "json", False) else "\n".join(lines))
    return code


def add_extra(actions) -> None:
    api = actions.add_parser("api", help="用只读 API Key 读交易侧数据：余额、持仓、账单归档、委托和成交历史")
    api.add_argument("--out", required=True, type=Path, help="输出目录（每个响应一个 0600 JSON）")


def run_extra(args):
    if args.action != "api":
        return None
    from . import api

    result = api.fetch(args.out)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if all(i["status"] == "collected" for i in result) else 5


CONNECTOR = Connector(
    key="okx", label="OKX", prefix="okx-bills", describe="C2C 订单与充提流水（okx-bills），另有只读 API Key 的交易侧数据（api）",
    fetch=fetch, summary=summary, rows=rows, add_fetch_args=add_fetch_args, plan=plan,
    login=login, status=status, keepalive=keepalive, extra=add_extra, run_extra=run_extra,
    login_kind="qr", keepalive_hours=24.0, record_types=("order", "transaction"),
    secrets=("OKX_FINANCE_API_KEY", "OKX_FINANCE_SECRET_KEY", "OKX_FINANCE_PASSPHRASE"),
)
