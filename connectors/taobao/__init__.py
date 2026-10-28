"""淘宝连接器：「已买到的宝贝」全部订单（mtop.taobao.order.queryboughtlistv2），只读。

Login comes from the browser cookies; the mtop _m_h5_tk token is refreshed automatically.
"""

from __future__ import annotations

from typing import Dict, List

from ..kit.output import format_summary
from ..kit.rows import _first_item_title, make_record, stable_id
from ..kit.runner import Connector
from ..kit.spec import ProbeResult, SiteSpec
from .reader import (
    TAOBAO_APP_KEY,
    TAOBAO_ENDPOINT,
    TAOBAO_HOST,
    TAOBAO_LOGIN_COOKIES,
    TAOBAO_REQUIRED_LOGIN_COOKIES,
    TAOBAO_TTID,
    TaobaoFetcher,
    build_page_data,
    canonical_json,
    parse_orders,
    summarize,
)
from ..kit.http import HttpClient, Pacer

PREFIX = "taobao-boughtlist"

SPEC = SiteSpec(
    key="taobao",
    label="淘宝",
    host_suffixes=("taobao.com", "tmall.com"),
    cookie_host=TAOBAO_HOST,
    login_cookies=TAOBAO_LOGIN_COOKIES,
    required_cookies=TAOBAO_REQUIRED_LOGIN_COOKIES,
    login_url="https://www.taobao.com/",
    token_cookies=("_m_h5_tk", "_m_h5_tk_enc"),
)


def add_fetch_args(parser) -> None:
    parser.add_argument("--tab-code", default="all", help="tabCode，默认 all（全部订单）")


def fetch(args, jar):
    client = HttpClient(cookie_jar=jar, pacer=Pacer(max(1.0, args.delay)), timeout=args.timeout)
    fetcher = TaobaoFetcher(client, jar, max_pages=args.max_pages, tab_code=args.tab_code)
    payload = fetcher.fetch()
    return payload, fetcher.warnings


def rows(payload: Dict) -> List[Dict]:
    out = []
    for order in parse_orders(payload.get("pages") or []):
        items = order.get("items") or []
        out.append(make_record(
            source="taobao", record_type="order", id=stable_id("taobao", "order", order.get("order_id")),
            occurred=order.get("occurred"), amount=order.get("paid"), direction="out", merchant=order.get("shop"),
            title=_first_item_title(items), status=order.get("status"),
            extra={"items": items, "order_status_code": order.get("order_status_code"),
                   "order_type": order.get("order_type"), "archived": order.get("archived")},
        ))
    return out


def check(spec, client, jar) -> ProbeResult:
    info = TaobaoFetcher(client, jar, max_pages=1).probe()
    return ProbeResult(spec, "ok", "“已买到的宝贝”第 1 页可读",
                       token_rotations=int(info.get("token_rotations") or 0), token_retries=int(info.get("token_retries") or 0))


def plan(args) -> str:
    return "\n".join([
        "plan: taobao fetch (dry-run, no network, no Keychain)",
        f"endpoint: POST {TAOBAO_ENDPOINT}",
        "api: mtop.taobao.order.queryboughtlistv2 v1.0",
        f"appKey: {TAOBAO_APP_KEY}",
        f"ttid: {TAOBAO_TTID}",
        'sign: md5(token + "&" + t + "&" + appKey + "&" + data)',
        "token: first part of the _m_h5_tk cookie (never printed)",
        f"data(page=1): {canonical_json(build_page_data(1, args.tab_code))}",
        "pacing: >= 1.0s between requests",
        "stop: no MainGroup_ keys, or same MainGroup_ set as previous page",
        f"output: {args.out or 'OUT'}/{PREFIX}-YYYYmmdd-HHMMSS.json + taobao-rows-YYYYmmdd-HHMMSS.jsonl (0600)",
    ])


CONNECTOR = Connector(
    key="taobao", label="淘宝", prefix=PREFIX, spec=SPEC, describe="「已买到的宝贝」全部订单",
    fetch=fetch, summary=lambda payload: format_summary("taobao", summarize(payload)), rows=rows,
    add_fetch_args=add_fetch_args, plan=plan, check=check, keepalive_hours=4.0, max_pages=100,
)
