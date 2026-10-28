"""币安连接器（只读 API Key，见 kit/secrets.py）：账户、充提、C2C、合约收益、Pay 转账，另有合约历史官方归档。

fetch writes the per-request JSON files straight into --out (they are the raw output) and, through the runner,
a small coverage payload. rows are empty for now.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Dict, List

from ..kit.errors import NetworkError
from ..kit.runner import Connector
from . import reader
from ..kit.apikey import key_status, since_ms
from ..kit.secrets import SETUP_HINT

SECRETS = ("BINANCE_API_KEY", "BINANCE_SECRET_KEY")


def add_fetch_args(parser) -> None:
    parser.add_argument("--since", default=(datetime.date.today() - datetime.timedelta(days=365)).isoformat(), help="起始日期 YYYY-MM-DD（北京时间），默认最近 365 天")


def fetch(args, jar):
    try:
        coverage = reader.fetch(args.out, since_ms(args.since))
    except RuntimeError as exc:
        raise NetworkError(f"币安请求失败: {exc}") from None
    warnings = [f"{c['category']}: {c.get('reason')}" for c in coverage if c.get("status") == "gap"]
    return {"kind": "binance_api", "since": args.since, "coverage": coverage}, warnings


def summary(payload: Dict) -> str:
    cov = payload.get("coverage") or []
    return f"binance: 类别 {len(cov)}，已收集 {sum(1 for c in cov if c.get('status') == 'collected')}，缺口 {sum(1 for c in cov if c.get('status') == 'gap')}"


def plan(args) -> str:
    return f"plan: binance fetch --since {args.since}：只读 GET（账户、充提、C2C、合约收益、Pay 转账）（dry-run：不读密钥、不联网）"


def status(args):
    return key_status("币安", SECRETS)


def login(args) -> int:
    print("币安用只读 API Key：" + " / ".join(SECRETS) + "，" + SETUP_HINT.format(name="NAME"))
    return key_status("币安", SECRETS)[1]


def keepalive(args) -> int:
    lines, code = status(args)
    print(json.dumps({"connector": "binance", "status": "ok" if code == 0 else "missing"}) if getattr(args, "json", False) else "\n".join(lines))
    return code


def add_extra(actions) -> None:
    arc = actions.add_parser("futures-archive", help="合约历史官方归档：第一次申请，之后再运行下载已生成的文件")
    arc.add_argument("--out", required=True, type=Path, help="输出目录")
    arc.add_argument("--since", default=(datetime.date.today() - datetime.timedelta(days=365)).isoformat(), help="起始日期 YYYY-MM-DD，默认最近 365 天")
    arc.add_argument("--split", default=datetime.date.today().isoformat(), help="分段日期，默认今天（单个归档最长一年）")


def run_extra(args):
    if args.action != "futures-archive":
        return None
    try:
        result = reader.futures_archive(args.out, since_ms(args.since), since_ms(args.split))
    except RuntimeError as exc:
        raise NetworkError(f"币安请求失败: {exc}") from None
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if not any(i.get("status") == "gap" for i in result) else 5


def rows(payload: Dict) -> List[Dict]:
    return []


CONNECTOR = Connector(
    key="binance", label="币安", prefix="binance-api", describe="账户、充提、C2C、合约收益、Pay 转账（只读 API Key）",
    fetch=fetch, summary=summary, rows=rows, add_fetch_args=add_fetch_args, plan=plan,
    login=login, status=status, keepalive=keepalive, extra=add_extra, run_extra=run_extra,
    login_kind="api-key", keepalive_hours=24.0, record_types=("transaction",), secrets=SECRETS,
)
