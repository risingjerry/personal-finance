"""美团月付连接器：月付账单（消费/退款）和还款记录，只读。

Login comes from the browser cookies. Row ``source`` stays ``meituan-yuefu`` (the ledger depends on it);
the saved Keychain session is also keyed ``meituan-yuefu``.
"""

from __future__ import annotations

from typing import Dict, List

from ..kit.http import HttpClient, Pacer
from ..kit.output import format_date_range, format_summary
from ..kit.rows import make_record, stable_id
from ..kit.runner import Connector
from ..kit.spec import ProbeResult, SiteSpec
from .reader import (
    MEITUAN_BILL_ENDPOINT,
    MEITUAN_ENTRY,
    MEITUAN_HOST,
    MEITUAN_LOGIN_COOKIES,
    MEITUAN_PAY_ATTR,
    MEITUAN_REPAYMENT_FILTER,
    MEITUAN_REPAYMENT_START,
    MEITUAN_REQUIRED_LOGIN_COOKIES,
    MeituanYuefuFetcher,
    parse_page_size,
    parse_repayments as _parse_meituan_repayments,
    parse_transactions as _parse_meituan,
    repayment_mode as _meituan_repayment_mode,
    summarize,
)

PREFIX = "meituan-yuefu"

SPEC = SiteSpec(
    key="meituan-yuefu",  # keep: saved sessions are stored per SiteSpec.key
    label="美团月付",
    host_suffixes=("meituan.com",),
    cookie_host=MEITUAN_HOST,
    login_cookies=MEITUAN_LOGIN_COOKIES,
    required_cookies=MEITUAN_REQUIRED_LOGIN_COOKIES,
    login_url=MEITUAN_ENTRY,
)


def add_fetch_args(parser) -> None:
    parser.add_argument("--page-size", type=int, default=None, help="每页条数（1-100），默认 20，与公开页面一致")
    parser.add_argument("--no-repayments", action="store_true", help="不拉取还款记录（只读消费/退款账单）")


def validate(args) -> None:
    if args.page_size is not None:
        parse_page_size(str(args.page_size))


def fetch(args, jar):
    client = HttpClient(cookie_jar=jar, pacer=Pacer(max(1.0, args.delay)), timeout=args.timeout)
    page_size = parse_page_size(None if args.page_size is None else str(args.page_size))
    fetcher = MeituanYuefuFetcher(
        client, jar, max_pages=args.max_pages, page_size=page_size,
        include_repayments=not getattr(args, "no_repayments", False),
    )
    payload = fetcher.fetch()
    return payload, fetcher.warnings


def summary(payload: Dict) -> str:
    data = summarize(payload)
    text = format_summary("meituan-yuefu", data)
    if data.get("repayments"):
        text += (
            f"\n  repayments: count={data['repayments']} "
            f"date_range={format_date_range(data.get('repayment_date_range'))}"
        )
    return text


def _meituan_direction(item: Dict) -> str:
    if str(item.get("money_flow")) == "4" or str(item.get("trade_type")) == "2":
        return "in"
    return "out"


def rows(payload: Dict) -> List[Dict]:
    rows = []
    seen_bill = set()
    seen_native = set()
    for item in _parse_meituan(payload):
        key = (item.get("trade_no"), item.get("pay_id"))
        if key in seen_bill:
            continue
        seen_bill.add(key)
        for native in key:
            if native:
                seen_native.add(str(native))
        rows.append(
            make_record(
                source="meituan-yuefu",
                record_type="bill",
                id=stable_id(
                    "meituan-yuefu",
                    "bill",
                    item.get("trade_no") or item.get("pay_id"),
                    item.get("pay_id"),
                ),
                occurred=item.get("occurred"),
                amount=item.get("amount_yuan"),
                direction=_meituan_direction(item),
                merchant=item.get("business"),
                title=item.get("title"),
                status=item.get("month"),
                extra={
                    "trade_no": item.get("trade_no"),
                    "pay_id": item.get("pay_id"),
                    "month": item.get("month"),
                    "money_flow": item.get("money_flow"),
                    "trade_type": item.get("trade_type"),
                    "order_type": item.get("order_type"),
                    "promo_fen": item.get("promo_fen"),
                },
            )
        )

    # Repayments are a separate read-only pass (financialTransType=4, no
    # payAttr).  They are real transactions: money goes back into the month-pay
    # account to reduce the outstanding balance, so direction is always "in".
    for item in _parse_meituan_repayments(payload):
        native = item.get("trade_no") or item.get("pay_id")
        if not native:
            continue
        if (
            str(native) in seen_native
            or (item.get("pay_id") and str(item["pay_id"]) in seen_native)
        ):
            continue
        seen_native.add(str(native))
        if item.get("pay_id"):
            seen_native.add(str(item["pay_id"]))
        mode = _meituan_repayment_mode(item)
        extra = {
            "trade_no": item.get("trade_no"),
            "pay_id": item.get("pay_id"),
            "month": item.get("month"),
            "money_flow": item.get("money_flow"),
            "trade_type": item.get("trade_type"),
            "order_type": item.get("order_type"),
            "order_title": item.get("order_title"),
            "repay_mode": mode,
            "pay_detail_tag": item.get("pay_detail_tag"),
            "pay_details": item.get("pay_details"),
            "business_name": item.get("business"),
        }
        if item.get("pay_details_raw"):
            extra["pay_details_raw"] = item["pay_details_raw"]
        rows.append(
            make_record(
                source="meituan-yuefu",
                record_type="transaction",
                id=stable_id(
                    "meituan-yuefu",
                    "transaction",
                    native,
                    item.get("pay_id"),
                ),
                occurred=item.get("occurred"),
                amount=item.get("amount_yuan"),
                direction="in",
                merchant=item.get("business"),
                title=mode,
                status=item.get("month"),
                extra=extra,
            )
        )
    return rows


def check(spec, client, jar) -> ProbeResult:
    info = MeituanYuefuFetcher(client, jar, max_pages=1).probe()
    return ProbeResult(spec, "ok", "美团月付用户信息可读" if info.get("has_data") else "美团月付账单接口可读")


def plan(args) -> str:
    page_size = parse_page_size(
        None if args.page_size is None else str(args.page_size)
    )
    example = (
        '{"userid":"<from cookie>","nb_app":"web","nb_platform":"touch",'
        f'"currentPage":0,"payAttr":{MEITUAN_PAY_ATTR},'
        f'"pageSize":{page_size},"ctype":""}}'
    )
    repayment_example = (
        '{"userid":"<from cookie>","nb_app":"web","nb_platform":"touch",'
        f'"currentPage":0,"pageSize":{page_size},"ctype":"",'
        f'"financialTransType":{MEITUAN_REPAYMENT_FILTER},'
        f'"startTime":"{MEITUAN_REPAYMENT_START}","endTime":"YYYYmmdd"}}'
    )
    return "\n".join(
        [
            "plan: meituan-yuefu fetch (dry-run, no network, no Keychain)",
            f"entry: GET {MEITUAN_ENTRY}",
            f"endpoint: POST {MEITUAN_BILL_ENDPOINT}",
            "pay type: 4 (PayType.monthPay / 美团月付)",
            f"body(page=0): {example}",
            "repayments: same endpoint, no payAttr, financialTransType=4",
            f"  body(page=0): {repayment_example}",
            "  window: " + MEITUAN_REPAYMENT_START + "..today (YYYYMMDD)",
            "auth: browser Cookie 'token' (session credential); 'userId'/'u' optional account id",
            "signature: none in the public bundle; this tool does not compute mtgsig",
            "risk: a server request for mtgsig stops with a 'needs risk signature' error",
            "pagination: follow data.nextPage; stop when it does not advance",
            "pacing: >= 1.0s between requests",
            "cookies: read-only from browser Cookies SQLite, in memory only",
            f"output: {args.out or 'OUT'}/{PREFIX}-YYYYmmdd-HHMMSS.json (0600)",
            "rows: meituan-rows-YYYYmmdd-HHMMSS.jsonl (0600)",
        ]
    )


CONNECTOR = Connector(
    key="meituan", label="美团月付", prefix=PREFIX, spec=SPEC, describe="美团月付账单（消费/退款）与还款记录",
    fetch=fetch, summary=summary, rows=rows, add_fetch_args=add_fetch_args, validate=validate, plan=plan, check=check,
    keepalive_hours=24.0, max_pages=50, record_types=("bill", "transaction"),
)
