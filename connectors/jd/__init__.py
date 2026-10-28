"""京东连接器：订单列表 + 京东白条（月度账单 / 消费明细 / 未出账），只读。

白条和订单共用同一个京东登录（浏览器的 cookie）：一次 fetch 同时读两样，白条放在 payload["baitiao"]。
"""

from __future__ import annotations

import argparse
import datetime
from typing import Dict, List, Tuple

from ..kit.errors import ConnectorError
from ..kit.http import HttpClient, Pacer
from ..kit.output import format_baitiao_summary, format_summary
from ..kit.rows import _first_item_title, make_record, stable_id
from ..kit.runner import Connector
from ..kit.spec import ProbeResult, SiteSpec, _select_login_set
from .baitiao import (
    BAITIAO_HOST,
    BILL_ENDPOINT,
    TRANSACTION_ENDPOINT,
    UNBILLED_ENDPOINT,
    BaitiaoFetcher,
    summarize as summarize_baitiao,
)
from .reader import (
    JD_HOST,
    JD_LOGIN_COOKIES,
    JD_LOGIN_SETS,
    JD_REQUIRED_LOGIN_COOKIES,
    JdFetcher,
    d_for_year,
    html_url,
    parse_years,
    summarize as summarize_jd,
)

PREFIX = "jd-orders"

SPEC = SiteSpec(
    key="jd",
    label="京东",
    host_suffixes=("jd.com", "jd.hk", "360buy.com"),
    cookie_host=JD_HOST,
    login_cookies=JD_LOGIN_COOKIES,
    required_cookies=JD_REQUIRED_LOGIN_COOKIES,
    login_url="https://www.jd.com/",
    login_sets=JD_LOGIN_SETS,
)


def add_fetch_args(parser) -> None:
    parser.add_argument("--years", default=None, help="年份或范围，例如 2018-2026 或 2018,2020,2022-2024；默认今年")
    parser.add_argument("--no-baitiao", action="store_true", help="只拉订单，不读白条账单")
    parser.add_argument("--html-only", action="store_true", help="只读 HTML，不尝试只读 API（即旧的 API 兜底关闭）")
    parser.add_argument(
        "--concurrency", type=int, default=1, metavar="N",
        help=("按年份并行读取的 worker 数（默认 1=顺序）。共享连接池与 Cookie jar，"
              "请求起始间隔仍由 --delay 限速，因此并发只重叠延迟、不提高请求频率"),
    )


def validate(args) -> None:
    parse_years(args.years)


def fetch(args, jar):
    years = parse_years(args.years)
    # Politeness floor: 1.0 s between request *starts*.  A bulk backfill of many years may opt into a faster
    # rate explicitly (--concurrency>1 and --delay below the floor).  Normal single-year runs keep 1.0 s.
    floor = 1.0
    if getattr(args, "concurrency", 1) > 1 and getattr(args, "delay", 1.0) < 1.0:
        floor = 0.25
    client = HttpClient(cookie_jar=jar, pacer=Pacer(max(floor, args.delay)), timeout=args.timeout)
    fetcher = JdFetcher(
        client, jar, years,
        concurrency=getattr(args, "concurrency", 1),
        max_pages=args.max_pages,
        api_fallback=not args.html_only,
    )
    payload = fetcher.fetch()
    warnings = list(fetcher.warnings)
    # 白条 shares the JD login: read it in the same run (one command for what was bought and how it was paid).
    if not getattr(args, "no_baitiao", False):
        baitiao_args = argparse.Namespace(**{**vars(args), "page_size": 10, "no_unbilled": False})
        try:
            payload["baitiao"], baitiao_warnings = fetch_baitiao(baitiao_args, jar)
            warnings += baitiao_warnings
        except ConnectorError as exc:
            warnings.append(f"白条未读取：{exc}")
    return payload, warnings


def fetch_baitiao(args, jar, client=None) -> Tuple[Dict, List[str]]:
    client = client or HttpClient(cookie_jar=jar, pacer=Pacer(max(1.0, args.delay)), timeout=args.timeout)
    fetcher = BaitiaoFetcher(
        client, jar,
        max_pages=args.max_pages,
        page_size=args.page_size,
        include_unbilled=not args.no_unbilled,
    )
    payload = fetcher.fetch()
    return payload, fetcher.warnings


def summary(payload: Dict) -> str:
    text = format_summary("jd", summarize_jd(payload))
    if payload.get("baitiao"):
        text += "\n" + format_baitiao_summary(summarize_baitiao(payload["baitiao"]))
    return text


def rows(payload: Dict) -> List[Dict]:
    out = _rows_jd(payload)
    if payload.get("baitiao"):
        out += _rows_jd_baitiao(payload["baitiao"])
    return out


def check(spec, client, jar) -> ProbeResult:
    fetcher = JdFetcher(client, jar, [datetime.datetime.now().year], max_pages=1)
    info = fetcher.probe_api()
    detail = f"订单列表 API 可读（第 1 页 {info.get('orders', 0)} 单，totalNum={info.get('total_num')}"
    if spec.login_sets:
        login_set, _ = _select_login_set(spec, jar)
        if login_set is not None and login_set.label:
            detail += f"；登录方式: {login_set.label}"
    return ProbeResult(spec, "ok", detail + "）", cookie_rotations=int(info.get("cookie_rotations") or 0))


def _rows_jd(payload: Dict) -> List[Dict]:
    rows = []
    seen = set()
    for page in payload.get("pages") or []:
        for order in page.get("orders") or []:
            oid = order.get("order_id")
            if oid in seen:
                continue
            seen.add(oid)
            items = order.get("items") or []
            rows.append(
                make_record(
                    source="jd",
                    record_type="order",
                    id=stable_id("jd", "order", oid),
                    occurred=order.get("occurred"),
                    amount=order.get("paid"),
                    direction="out",
                    merchant=order.get("shop"),
                    title=_first_item_title(items),
                    status=order.get("status"),
                    extra={"items": items, "transport": order.get("source")},
                )
            )
    return rows


def _rows_jd_baitiao(payload: Dict) -> List[Dict]:
    rows = []
    for bill in payload.get("bills") or []:
        rows.append(
            make_record(
                source="jd-baitiao",
                record_type="bill",
                id=stable_id(
                    "jd-baitiao",
                    "bill",
                    bill.get("bill_id"),
                    bill.get("bill_date"),
                    bill.get("amount"),
                ),
                occurred=bill.get("bill_date"),
                amount=bill.get("amount") or bill.get("should_pay"),
                direction="out",
                merchant="京东白条",
                title=(
                    f"{bill.get('bill_month')} 账单"
                    if bill.get("bill_month")
                    else "京东白条账单"
                ),
                status=(
                    f"status={bill['status']}"
                    if bill.get("status") is not None
                    else None
                ),
                extra={
                    "bill_month": bill.get("bill_month"),
                    "due_date": bill.get("due_date"),
                    "paid": bill.get("paid"),
                    "refund": bill.get("refund"),
                    "should_pay": bill.get("should_pay"),
                    "min_payment": bill.get("min_payment"),
                    "is_current": bill.get("is_current"),
                    "installment_count": bill.get("installment_count"),
                    "overdue": bill.get("overdue"),
                },
            )
        )
    for transaction in payload.get("transactions") or []:
        rows.append(
            make_record(
                source="jd-baitiao",
                record_type="transaction",
                id=stable_id(
                    "jd-baitiao",
                    "transaction",
                    transaction.get("order_id"),
                    transaction.get("occurred"),
                    transaction.get("amount"),
                    transaction.get("product"),
                ),
                occurred=transaction.get("occurred"),
                amount=transaction.get("amount"),
                direction="out",
                merchant="京东白条",
                title=transaction.get("product"),
                status=None,
                extra={
                    "order_id": transaction.get("order_id"),
                    "discount": transaction.get("discount"),
                    "sys_code": transaction.get("sys_code"),
                    "biz_code": transaction.get("biz_code"),
                    "has_order_url": bool(transaction.get("order_url")),
                },
            )
        )
    unbilled = payload.get("unbilled")
    if isinstance(unbilled, dict):
        rows.append(
            make_record(
                source="jd-baitiao",
                record_type="bill",
                id=stable_id(
                    "jd-baitiao",
                    "bill",
                    unbilled.get("bill_id"),
                    unbilled.get("bill_date"),
                    "unbilled",
                ),
                occurred=unbilled.get("bill_date"),
                amount=unbilled.get("amount") or unbilled.get("should_pay"),
                direction="out",
                merchant="京东白条",
                title="京东白条未出账单",
                status="unbilled",
                extra={
                    "paid": unbilled.get("paid"),
                    "refund": unbilled.get("refund"),
                    "should_pay": unbilled.get("should_pay"),
                    "min_payment": unbilled.get("min_payment"),
                    "can_repay": unbilled.get("can_repay"),
                },
            )
        )
    return rows


def plan(args) -> str:
    years = parse_years(args.years)
    current = datetime.datetime.now().year
    lines = [
        "plan: jd fetch (dry-run, no network, no Keychain)",
        f"years: {', '.join(str(y) for y in years)}",
        "transport: read-only API first (1 POST per page, no HTML request)",
        "  POST https://api.m.jd.com/api"
        "?appid=order-jd-com&functionId=pcorder_center_list&source=pc_order_center",
        '  body={"page":N,"d":"<d>","s":"4096"}  (no pageSize: the server fixes it at 20)',
        "  pagination: stop at data.totalNum (ceil(totalNum/page_size)), no empty-tail probe",
        (
            "  HTML fallback: disabled (--html-only forces the HTML path instead)"
            if args.html_only
            else "  HTML fallback: only if page 1 fails with a network/non-JSON error"
        ),
        (
            f"HTML path: GET {html_url(d_for_year(years[0], current), 1)}"
            " (d=2 for the current year; d=YYYY for past years)"
            if args.html_only
            else "HTML path: not used unless the API fails"
        ),
        "empty-year detection: order dates outside the requested year => fallback, ignore",
        "static analysis: the SPA attempts window.PSign.sign and attaches h5st, but the bundle's own",
        "     call site swallows a signing failure and sends the request unsigned;",
        "     this tool never computes, fetches or bypasses h5st and stops on risk",
        (
            f"concurrency: {args.concurrency} worker(s) over years; request starts "
            f"spaced >= {0.25 if (args.concurrency > 1 and args.delay < 1.0) else 1.0:.2f}s"
        ),
        "pacing: connections are reused (keep-alive); default floor 1.0s per request start",
        "cookies: read-only from browser Cookies SQLite, in memory only",
    ]
    if args.no_baitiao:
        lines.append("baitiao: skipped (--no-baitiao)")
    else:
        lines += [
            "baitiao: same login, read after the orders (page_size=10, with unbilled)",
            f"  bills: POST {BILL_ENDPOINT}",
            f"  transactions: POST {TRANSACTION_ENDPOINT}",
            f"  unbilled: POST {UNBILLED_ENDPOINT} (empty form)",
            f"  host: {BAITIAO_HOST}; read-only: getBillList / billConsumeList / queryNotOutAccount only",
            "  stop: passport redirect, NotLogin, captcha/risk response; never bypass",
        ]
    lines += [
        f"output: {args.out or 'OUT'}/{PREFIX}-YYYYmmdd-HHMMSS.json (0600)",
        "rows: jd-rows-YYYYmmdd-HHMMSS.jsonl (0600; orders + 白条 bills/transactions)",
    ]
    mapping = ", ".join(f"{y}->{d_for_year(y, current)}" for y in years)
    lines.append(f"d map: {mapping}")
    return "\n".join(lines)


CONNECTOR = Connector(
    key="jd", label="京东", prefix=PREFIX, spec=SPEC, describe="京东订单列表 + 京东白条账单（同一个登录）",
    fetch=fetch, summary=summary, rows=rows, add_fetch_args=add_fetch_args, validate=validate,
    plan=plan, check=check, keepalive_hours=1.0, max_pages=50, record_types=("order", "bill", "transaction"),
)
