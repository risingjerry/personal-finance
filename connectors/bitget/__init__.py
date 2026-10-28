"""Bitget 连接器（只读 API Key，见 kit/secrets.py）：资产、设置、近 90 天委托和成交。不是只读的 Key 一律拒绝。

fetch writes the per-request JSON files straight into --out (raw output) and a small coverage payload through
the runner. rows are empty for now.
"""

from __future__ import annotations

import json
from typing import Dict, List

from ..kit.apikey import key_status
from ..kit.secrets import SETUP_HINT
from ..kit.errors import NetworkError, UsageError
from ..kit.runner import Connector
from . import reader

SECRETS = ("BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE")


def add_fetch_args(parser) -> None:
    parser.add_argument("--days", type=int, default=89, help="回看天数，默认 89")


def validate(args) -> None:
    if args.days < 1:
        raise UsageError("--days 必须 >= 1")


def fetch(args, jar):
    try:
        coverage = reader.fetch(args.out, args.days)
    except RuntimeError as exc:
        if str(exc).startswith("Refusing"):
            raise UsageError("拒绝使用不是只读的 Bitget API Key", "请在 Bitget 新建只读 Key 再存入") from None
        raise NetworkError(f"Bitget 请求失败: {exc}") from None
    warnings = [f"{c['path']} {c['category']}: {c.get('reason')}" for c in coverage if c.get("status") == "gap"]
    return {"kind": "bitget_api", "days": args.days, "coverage": coverage}, warnings


def summary(payload: Dict) -> str:
    cov = payload.get("coverage") or []
    return f"bitget: 类别 {len(cov)}，已收集 {sum(1 for c in cov if c.get('status') == 'collected')}，缺口 {sum(1 for c in cov if c.get('status') == 'gap')}"


def plan(args) -> str:
    return f"plan: bitget fetch --days {args.days}：只读 GET（资产、设置、委托、成交），非只读 Key 拒绝（dry-run：不读密钥、不联网）"


def status(args):
    return key_status("Bitget", SECRETS)


def login(args) -> int:
    print("Bitget 用只读 API Key：" + " / ".join(SECRETS) + "，" + SETUP_HINT.format(name="NAME"))
    return key_status("Bitget", SECRETS)[1]


def keepalive(args) -> int:
    lines, code = status(args)
    print(json.dumps({"connector": "bitget", "status": "ok" if code == 0 else "missing"}) if getattr(args, "json", False) else "\n".join(lines))
    return code


def rows(payload: Dict) -> List[Dict]:
    return []


CONNECTOR = Connector(
    key="bitget", label="Bitget", prefix="bitget-api", describe="资产、设置、近 90 天委托和成交（只读 API Key）",
    fetch=fetch, summary=summary, rows=rows, add_fetch_args=add_fetch_args, validate=validate, plan=plan,
    login=login, status=status, keepalive=keepalive,
    login_kind="api-key", keepalive_hours=24.0, record_types=("transaction",), secrets=SECRETS,
)
