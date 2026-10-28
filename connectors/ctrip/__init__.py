"""携程连接器：携程全部订单（机票/酒店/火车票）+ 拿去花账单（消费/还款/退款/分期），只读。

Login comes from the browser cookies. 拿去花 shares the Ctrip login and is read by a headless
Chromium (naquhua.py + naquhua.cjs) in the same run; if that part fails it only warns.
"""

from __future__ import annotations

from typing import Dict, List

from ..kit.errors import ConnectorError, UsageError
from ..kit.http import HttpClient, Pacer
from ..kit.output import format_summary
from ..kit.rows import _first_item_title, make_record, stable_id
from ..kit.runner import Connector
from ..kit.spec import ProbeResult, SiteSpec
from .naquhua import fetch as fetch_naquhua, records as naquhua_records, summarize as summarize_naquhua
from .reader import (
    CTRIP_ENDPOINT,
    CTRIP_HISTORY_ENDPOINT,
    CTRIP_HOST,
    CTRIP_LOGIN_COOKIES,
    CTRIP_MAX_PAGE_SIZE,
    CTRIP_PAGE_SIZE,
    CTRIP_REFERER,
    CTRIP_REQUIRED_LOGIN_COOKIES,
    CtripFetcher,
    summarize,
)

PREFIX = "ctrip-orders"

SPEC = SiteSpec(
    key="ctrip",
    label="携程",
    host_suffixes=("ctrip.com", "trip.com"),
    cookie_host=CTRIP_HOST,
    login_cookies=CTRIP_LOGIN_COOKIES,
    required_cookies=CTRIP_REQUIRED_LOGIN_COOKIES,
    login_url="https://my.ctrip.com/myinfo/all",
)


def add_fetch_args(parser) -> None:
    parser.add_argument(
        "--page-size", type=int, default=CTRIP_PAGE_SIZE,
        help=f"最近一年订单每页条数（1-{CTRIP_MAX_PAGE_SIZE}），默认 {CTRIP_PAGE_SIZE}；历史订单固定按 {CTRIP_MAX_PAGE_SIZE} 条分页",
    )
    parser.add_argument("--no-naquhua", action="store_true", help="只拉订单，不读拿去花账单（拿去花要用无界面浏览器）")


def validate(args) -> None:
    if not 1 <= args.page_size <= CTRIP_MAX_PAGE_SIZE:
        raise UsageError(f"--page-size 必须在 1-{CTRIP_MAX_PAGE_SIZE} 之间")


def fetch(args, jar):
    client = HttpClient(cookie_jar=jar, pacer=Pacer(max(1.0, args.delay)), timeout=args.timeout)
    fetcher = CtripFetcher(client, jar, page_size=args.page_size, max_pages=args.max_pages)
    payload = fetcher.fetch()
    warnings = list(fetcher.warnings)
    # 拿去花 shares the Ctrip login: read it in the same run, so one command covers what was bought and how it was paid.
    if not getattr(args, "no_naquhua", False):
        try:
            payload["naquhua"] = fetch_naquhua(jar)
        except ConnectorError as exc:
            warnings.append(f"拿去花未读取：{exc}")
    return payload, warnings


def summary(payload: Dict) -> str:
    data = summarize(payload)
    types = data.get("by_type") or {}
    sources = data.get("by_source") or {}
    text = "\n".join([
        format_summary("ctrip", data),
        "by_type: flight=%d hotel=%d train=%d other=%d"
        % (types.get("flight", 0), types.get("hotel", 0), types.get("train", 0), types.get("other", 0)),
        "by_source: recent=%d history=%d" % (sources.get("recent", 0), sources.get("history", 0)),
    ])
    if payload.get("naquhua"):
        n = summarize_naquhua(payload["naquhua"])
        text += "\n拿去花: " + " ".join(f"{k}={v}" for k, v in n.items())
    return text


def _rows_ctrip(payload: Dict) -> List[Dict]:
    """One normalized row per Ctrip order (recent and history merged)."""

    rows = []
    seen = set()
    for page in payload.get("pages") or []:
        for order in page.get("orders") or []:
            order_id = order.get("order_id")
            key = order_id or (order.get("occurred"), order.get("paid"), order.get("title"))
            if key in seen:
                continue
            seen.add(key)
            items = order.get("items") or []
            rows.append(
                make_record(
                    source="ctrip",
                    record_type="order",
                    id=stable_id("ctrip", "order", order_id, order.get("occurred"), order.get("paid"), order.get("title")),
                    occurred=order.get("occurred"),
                    amount=order.get("paid"),
                    direction="out",
                    merchant="携程",
                    title=order.get("title") or _first_item_title(items),
                    status=order.get("status"),
                    extra={
                        "category": order.get("category"),
                        "biz_type": order.get("biz_type"),
                        "order_type": order.get("order_type"),
                        "currency": order.get("currency"),
                        "items": items,
                        "list_source": page.get("kind"),
                    },
                )
            )
    return rows


def _rows_naquhua(payload: Dict) -> List[Dict]:
    """携程拿去花 rows (fetched with the Ctrip orders): a purchase on the credit line is a ``bill`` row
    (money out of the line); a repayment or refund is a ``transaction`` row (money back into it)."""

    found = naquhua_records(payload.get("naquhua") or {})
    rows = []
    for r in found["trade"]:
        rows.append(make_record(
            source="ctrip-naquhua", record_type="bill", id=stable_id("ctrip-naquhua", "bill", r.get("billSerialNo")),
            occurred=r.get("orderDate"), amount=r.get("amount"), direction="out", merchant="携程", title=r.get("productName"),
            status=r.get("amountDesc"),
            extra={"bill_serial_no": r.get("billSerialNo"), "platform": r.get("platText"), "loan_term": r.get("loanTerm"),
                   "tags": [t.get("text") for t in (r.get("tagList") or []) if isinstance(t, dict)]},
        ))
    for kind, title in (("repay", "还款"), ("refund", "退款")):
        for r in found[kind]:
            serial = r.get("serialNo") or r.get("billSerialNo") or r.get("refundSerialNo")
            amount = next((r[k] for k in ("repaySuccAmount", "refundAmount", "refundSuccAmount", "amount") if r.get(k) is not None), None)
            when = next((r[k] for k in ("repayFinishDate", "refundDate", "refundFinishDate", "orderDate") if r.get(k)), None)
            rows.append(make_record(
                source="ctrip-naquhua", record_type="transaction", id=stable_id("ctrip-naquhua", kind, serial),
                occurred=when, amount=amount, direction="in", merchant="携程拿去花",
                title=r.get("launchTypeDesc") or r.get("productName") or title,
                status=r.get("repayStatus") or r.get("refundStatus") or r.get("status"),
                extra={"kind": kind, "serial_no": serial},
            ))
    return rows


def rows(payload: Dict) -> List[Dict]:
    out = _rows_ctrip(payload)
    if payload.get("naquhua"):
        out.extend(_rows_naquhua(payload))
    return out


def check(spec, client, jar) -> ProbeResult:
    info = CtripFetcher(client, jar, page_size=1, max_pages=1).probe()
    return ProbeResult(spec, "ok", f"GetOrderWithBM 第 1 页可读（订单 {info.get('orders', 0)} 条）")


def plan(args) -> str:
    return "\n".join([
        "plan: ctrip fetch (dry-run, no network, no Keychain)",
        f"recent: POST {CTRIP_ENDPOINT}",
        f"history: POST {CTRIP_HISTORY_ENDPOINT}",
        "api: 10098/GetOrderWithBM.json + 10098/GetHistoryOrderList.json",
        "     (the real my.ctrip.com/myinfo/all order centre; getAllOrders is legacy)",
        f"page: PageIndex/PageSize, recent={args.page_size}, history={CTRIP_MAX_PAGE_SIZE}, max_pages={args.max_pages}",
        "login: cticket cookie is required; head.auth stays empty",
        'body: head.cid=GUID; head.auth="" (session comes from cookies);',
        '      Channel=Online; ClientVersion=99.99; Locale="";',
        "      OrderStatusClassify=All; BizTypes=[]; PageIndex/PageSize",
        "history: BeginBookingDateTime=2000-01-01 00:00:00 .. now",
        "signature: the page may compute w-payload-source with c-sign.js;",
        "           this tool never forges/bypasses it and fails with",
        '           "needs risk signature" (HTTP 430/431/432) instead',
        f"referer: {CTRIP_REFERER}",
        "pacing: >= 1.0s between requests",
        "stop: short page, zero page, repeated IDs, total reached, or max pages",
        "cookies: read-only from browser Cookies SQLite, in memory only",
        "rows: one order row per recent/history order, deduplicated by order id",
        "naquhua: " + ("skipped (--no-naquhua)" if getattr(args, "no_naquhua", False)
                       else "headless Chromium reads the 拿去花 record tabs with the same login (read-only); failure only warns"),
        f"output: {args.out or 'OUT'}/{PREFIX}-YYYYmmdd-HHMMSS.json + ctrip-rows-YYYYmmdd-HHMMSS.jsonl (0600)",
    ])


CONNECTOR = Connector(
    key="ctrip", label="携程", prefix=PREFIX, spec=SPEC, describe="携程全部订单（机票/酒店/火车票）和拿去花账单（消费/还款/退款/分期）",
    fetch=fetch, summary=summary, rows=rows, add_fetch_args=add_fetch_args, validate=validate, plan=plan, check=check,
    keepalive_hours=24.0, max_pages=50, record_types=("order", "bill", "transaction"),
)
