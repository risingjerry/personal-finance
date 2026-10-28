"""闲鱼连接器：买到的 / 卖出的订单（mtop 接口），只读。

Login comes from the browser cookies; the mtop _m_h5_tk token is refreshed automatically.
"""

from __future__ import annotations

from typing import Dict, List

from ..kit.errors import UsageError
from ..kit.http import HttpClient, Pacer
from ..kit.output import format_summary
from ..kit.rows import _first_item_title, make_record, stable_id
from ..kit.runner import Connector
from ..kit.spec import ProbeResult, SiteSpec
from .reader import (
    BOUGHT_STATUSES,
    GOOFISH_APP_KEY,
    GOOFISH_BOUGHT_API,
    GOOFISH_BOUGHT_ENDPOINT,
    GOOFISH_HOST,
    GOOFISH_JSV,
    GOOFISH_LOGIN_COOKIES,
    GOOFISH_REQUIRED_LOGIN_COOKIES,
    GOOFISH_SOLD_API,
    GOOFISH_SOLD_ENDPOINT,
    GoofishFetcher,
    build_bought_data,
    build_sold_data,
    canonical_json,
    parse_orders,
    summarize,
)

PREFIX = "goofish-orders"

SPEC = SiteSpec(
    key="goofish",
    label="闲鱼",
    host_suffixes=("goofish.com",),
    cookie_host=GOOFISH_HOST,
    login_cookies=GOOFISH_LOGIN_COOKIES,
    required_cookies=GOOFISH_REQUIRED_LOGIN_COOKIES,
    login_url="https://www.goofish.com/",
)


def add_fetch_args(parser) -> None:
    parser.add_argument(
        "--side", choices=("both", "bought", "sold"), default="both",
        help="抓取买到的（bought）、卖出的（sold）或两者，默认 both",
    )
    parser.add_argument("--bought-status", default="ALL", choices=BOUGHT_STATUSES, help="买到的订单状态筛选，默认 ALL")
    parser.add_argument("--sold-status", default="ALL", help="卖出的订单 queryCode，默认 ALL")
    parser.add_argument("--rows-per-page", type=int, default=20, help="卖出的订单每页数量，默认 20")


def validate(args) -> None:
    if args.rows_per_page < 1:
        raise UsageError("--rows-per-page 必须 >= 1")
    if args.timeout <= 0:
        raise UsageError("--timeout 必须 > 0")
    if args.delay < 0:
        raise UsageError("--delay 必须 >= 0")


def fetch(args, jar):
    sides = ("bought", "sold") if args.side == "both" else (args.side,)
    client = HttpClient(cookie_jar=jar, pacer=Pacer(max(1.0, args.delay)), timeout=args.timeout)
    fetcher = GoofishFetcher(
        client, jar, sides=sides, max_pages=args.max_pages,
        bought_status=args.bought_status, sold_status=args.sold_status, rows_per_page=args.rows_per_page,
    )
    payload = fetcher.fetch()
    return payload, fetcher.warnings


def summary(payload: Dict) -> str:
    s = summarize(payload)
    return "\n".join([
        format_summary("goofish", s),
        f"  bought: orders={s['bought_orders']}",
        f"  sold:   orders={s['sold_orders']}",
    ])


def rows(payload: Dict) -> List[Dict]:
    out = []
    for order in parse_orders(payload.get("pages") or []):
        side = order.get("side") or "bought"
        items = order.get("items") or []
        out.append(make_record(
            source="goofish", record_type="order",
            id=stable_id("goofish", "order",
                         f"{side}:{order.get('order_id')}" if order.get("order_id") else None,
                         side, order.get("order_id")),
            occurred=order.get("occurred"), amount=order.get("paid"),
            direction="in" if side == "sold" else "out",
            merchant=order.get("counterparty"), title=_first_item_title(items), status=order.get("status"),
            extra={"side": side, "items": items, "status_code": order.get("status_code"),
                   "item_id": order.get("item_id"), "in_refund": order.get("in_refund"),
                   "order_detail_url": order.get("order_detail_url")},
        ))
    return out


def check(spec, client, jar) -> ProbeResult:
    info = GoofishFetcher(client, jar, sides=("bought",), max_pages=1).probe()
    detail = "闲鱼用户导航可读"
    purchase = info.get("purchase_count")
    sold = info.get("sold_count")
    if purchase is not None or sold is not None:
        detail += f"（买到 {'?' if purchase is None else purchase} / 卖出 {'?' if sold is None else sold}）"
    return ProbeResult(spec, "ok", detail,
                       token_rotations=int(info.get("token_rotations") or 0), token_retries=int(info.get("token_retries") or 0))


def plan(args) -> str:
    sides = "bought, sold" if args.side == "both" else args.side
    bought_example = canonical_json(build_bought_data(1, args.bought_status))
    sold_example = canonical_json(build_sold_data(1, args.sold_status, rows_per_page=args.rows_per_page))
    return "\n".join([
        "plan: goofish fetch (dry-run, no network, no Keychain)",
        f"sides: {sides}",
        f"endpoint (bought): POST {GOOFISH_BOUGHT_ENDPOINT}",
        f"endpoint (sold):   POST {GOOFISH_SOLD_ENDPOINT}",
        f"api: {GOOFISH_BOUGHT_API} v1.0",
        f"     {GOOFISH_SOLD_API} v1.0",
        f"appKey: {GOOFISH_APP_KEY}",
        f"jsv: {GOOFISH_JSV}",
        'sign: md5(token + "&" + t + "&" + appKey + "&" + data)',
        "token: first part of the _m_h5_tk cookie (never printed)",
        f"data bought(page=1): {bought_example}",
        f"data sold(page=1):   {sold_example}",
        "method: POST form data=<json>&ua=<user-agent>",
        "pacing: >= 1.0s between requests",
        "stop: nextPage != true, empty page, repeated ids, or --max-pages",
        "cookies: read-only from browser Cookies SQLite, in memory only",
        "risk: the page may add ua/umidtoken from window.fyOBJ when data.asac",
        "      is set; this tool never sets asac and never forges a signature",
        f"output: {args.out or 'OUT'}/{PREFIX}-YYYYmmdd-HHMMSS.json (0600)",
        "rows: goofish-rows-YYYYmmdd-HHMMSS.jsonl (0600)",
    ])


CONNECTOR = Connector(
    key="goofish", label="闲鱼", prefix=PREFIX, spec=SPEC, describe="买到的 / 卖出的订单",
    fetch=fetch, summary=summary, rows=rows, add_fetch_args=add_fetch_args, validate=validate,
    plan=plan, check=check, keepalive_hours=4.0, max_pages=50,
)
