"""大众点评连接器：「我的订单 → 新餐饮订单」（服务端渲染 HTML，只读）。

Login comes from the browser cookies. 大众点评/美团风控严格：任意两次请求至少间隔 10 秒。
"""

from __future__ import annotations

from typing import Dict, List

from ..kit.errors import UsageError
from ..kit.http import HttpClient, Pacer
from ..kit.output import format_summary
from ..kit.rows import make_record, stable_id
from ..kit.runner import Connector
from ..kit.spec import ProbeResult, SiteSpec
from .reader import (
    DIANPING_DAOCAN_PAGE,
    DIANPING_DEFAULT_MAX_PAGES,
    DIANPING_HOST,
    DIANPING_LOGIN_COOKIES,
    DIANPING_MTGSIG_HEADER,
    DianpingDaocanFetcher,
    build_daocan_url,
    check_dianping_login,
    no_redirect_opener,
    summarize,
)

PREFIX = "dianping-orders"

SPEC = SiteSpec(
    key="dianping",
    label="大众点评",
    host_suffixes=("dianping.com",),
    cookie_host=DIANPING_HOST,
    login_cookies=DIANPING_LOGIN_COOKIES,
    required_cookies=DIANPING_LOGIN_COOKIES,
    login_url=DIANPING_DAOCAN_PAGE,
    required_mode="any",
)


def add_fetch_args(parser) -> None:
    parser.add_argument("--page-size", type=int, default=20,
                        help="兼容旧参数；新餐饮订单页固定每页条数，服务端忽略该值")


def validate(args) -> None:
    if args.page_size < 1:
        raise UsageError("--page-size 必须 >= 1")
    if args.timeout <= 0:
        raise UsageError("--timeout 必须 > 0")
    if args.delay < 0:
        raise UsageError("--delay 必须 >= 0")


def fetch(args, jar):
    check_dianping_login(jar)
    # 大众点评/美团风控严格：一次运行内任意两次请求至少间隔 10 秒。
    client = HttpClient(cookie_jar=jar, pacer=Pacer(max(10.0, args.delay)), timeout=args.timeout,
                        opener=no_redirect_opener())
    fetcher = DianpingDaocanFetcher(client, jar, max_pages=args.max_pages)
    payload = fetcher.fetch()
    return payload, fetcher.warnings


def rows(payload: Dict) -> List[Dict]:
    """One normalized row per 新餐饮订单 row on the server-rendered page."""

    out = []
    seen = set()
    for page in payload.get("pages") or []:
        for order in page.get("orders") or []:
            order_id = order.get("order_id")
            key = order_id or (order.get("occurred"), order.get("title"), order.get("amount"))
            if key in seen:
                continue
            seen.add(key)
            out.append(make_record(
                source="dianping", record_type="order",
                id=stable_id("dianping", "order", order_id, order.get("occurred"), order.get("title"), order.get("amount")),
                occurred=order.get("occurred"), amount=order.get("amount"), direction="out",
                merchant="大众点评", title=order.get("title"), status=order.get("status"),
                extra={"expire_at": order.get("expire_at"), "quantity": order.get("quantity"),
                       "coupon_codes": order.get("coupon_codes") or [],
                       "list_source": page.get("source"), "page": page.get("page")},
            ))
    return out


def check(spec, client, jar) -> ProbeResult:
    payload = DianpingDaocanFetcher(client, jar, max_pages=1).fetch()
    count = sum(len(page.get("orders") or []) for page in payload.get("pages") or [])
    return ProbeResult(spec, "ok", f"新餐饮订单 HTML 第 1 页可读（订单 {count} 条）")


def plan(args) -> str:
    return "\n".join([
        "plan: dianping fetch (dry-run, no network, no Keychain)",
        f"entry: {DIANPING_DAOCAN_PAGE}",
        "  (大众点评 -> 我的订单 -> 新餐饮订单)",
        f"endpoint: GET {build_daocan_url(1)} (read-only HTML)",
        f"  pagination: {build_daocan_url(2)} ...; stop on empty/repeated page",
        f"  default max_pages={args.max_pages} (15 rows/page; enough for a full pull)",
        "  the list is server-rendered; page bundle `page::order` has only",
        "  write XHRs (/ajax/order, /ajaxcore/order) which this tool never calls",
        "auth: browser cookies for .dianping.com (dper/dpscope/dpassport), in memory only",
        f"signature: none needed for this HTML page; H5guard {DIANPING_MTGSIG_HEADER}",
        "  is still never computed, copied or bypassed",
        "risk: verify.meituan.com / captcha / 406 / 412 / forced logout => stop",
        "read-only: no write/cancel/refund/review/invoice/payment API",
        "pacing: >= 10.0s between requests (Dianping risk control)",
        "cookies: read-only from browser Cookies SQLite, in memory only",
        "remaining: 外卖 (i.waimai.meituan.com) and 酒店/订座 are",
        "  separate login-gated apps; this command covers 新餐饮订单",
        f"output: {args.out or 'OUT'}/{PREFIX}-YYYYmmdd-HHMMSS.json (0600)",
        "rows: dianping-rows-YYYYmmdd-HHMMSS.jsonl (0600)",
    ])


CONNECTOR = Connector(
    key="dianping", label="大众点评", prefix=PREFIX, spec=SPEC, describe="新餐饮订单（服务端渲染 HTML）",
    fetch=fetch, summary=lambda payload: format_summary("dianping", summarize(payload)), rows=rows,
    add_fetch_args=add_fetch_args, validate=validate, plan=plan, check=check,
    keepalive_hours=24.0, min_delay=10.0, max_pages=DIANPING_DEFAULT_MAX_PAGES,
)
