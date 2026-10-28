"""中国铁路 12306 连接器：历史订单（增量归档）+ 电子发票只读列表（invoice 动作），只读。

Login comes from the browser cookies; the QR scan is done by the person in the browser. Row ``source`` stays
``12306`` (the ledger depends on it) and the saved Keychain session is keyed ``12306``. The incremental state file
lives in the shared application-support directory (override ``PFIN_STATE_DIR``); see reader.default_state_path().
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Dict, List

from ..kit.errors import AuthRequired, UsageError
from ..kit.http import HttpClient, Pacer
from ..kit.output import ensure_out_dir, format_summary, write_json
from ..kit.rows import make_record, stable_id, stamp_from_raw, write_rows
from ..kit.runner import Connector, browser_args, net_args, provider
from ..kit.spec import ProbeResult, SiteSpec
from .reader import (
    RAIL_HOST,
    RAIL_INVOICE_QUERY_URL,
    RAIL_INVOICE_TYPE_ISSUED,
    RAIL_INVOICE_TYPE_PENDING,
    RAIL_LOGIN_COOKIES,
    RAIL_LOGIN_URL,
    RAIL_MAX_PAGES,
    RAIL_NOT_COMPLETE_ENDPOINT,
    RAIL_ORDER_ENDPOINT,
    RAIL_PAGE_SIZE,
    RAIL_QUERY_TYPE,
    RAIL_QUERY_WHERE,
    RAIL_REQUIRED_LOGIN_COOKIES,
    RAIL_RETENTION_DAYS,
    RAIL_MAX_SPAN_DAYS,
    Rail12306Fetcher,
    RailInvoiceFetcher,
    build_order_form,
    invoice_rows,
    parse_orders as _parse_rail_orders,
    plan_query_windows,
    resolve_date_range,
    summarize,
)

PREFIX = "12306-orders"
INVOICE_PREFIX = "12306-invoice"

SPEC = SiteSpec(
    key="12306",  # keep: saved sessions are stored per SiteSpec.key
    label="中国铁路 12306",
    host_suffixes=("12306.cn",),
    cookie_host=RAIL_HOST,
    login_cookies=RAIL_LOGIN_COOKIES,
    required_cookies=RAIL_REQUIRED_LOGIN_COOKIES,
    login_url=RAIL_LOGIN_URL,
)


def add_fetch_args(parser) -> None:
    parser.add_argument(
        "--start-date", default=None,
        help="起始订票日期 YYYY-MM-DD（默认 12306 最早可查日期，即今天-30；更早的日期会自动截断）",
    )
    parser.add_argument("--end-date", default=None, help="结束订票日期 YYYY-MM-DD（默认昨天；今天/未来会自动截断或报错）")
    parser.add_argument("--page-size", type=int, default=RAIL_PAGE_SIZE, help="每页订单数，默认 8（与页面一致）")
    parser.add_argument(
        "--query-type", choices=("1", "2"), default=RAIL_QUERY_TYPE,
        help="1=按订票日期（历史订单页硬编码为 1），2=按乘车日期（页面只在“未出行订单”用 2）",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="忽略增量状态，重查整个可查区间（30 天）。默认只补查尚未覆盖的日期，每天通常只有 1 个请求",
    )
    parser.add_argument("--no-incremental", action="store_true", help="不用状态文件规划窗口（每次都查 start-date..end-date 区间）")


def validate(args) -> None:
    if args.page_size < 1:
        raise UsageError("--page-size 必须 >= 1")


def fetch(args, jar):
    client = HttpClient(cookie_jar=jar, pacer=Pacer(max(1.0, args.delay)), timeout=args.timeout)
    fetcher = Rail12306Fetcher(
        client, jar,
        max_pages=args.max_pages,
        page_size=args.page_size,
        start_date=args.start_date,
        end_date=args.end_date,
        query_type=args.query_type,
        force_refresh=getattr(args, "refresh", False),
        incremental=not getattr(args, "no_incremental", False),
    )
    payload = fetcher.fetch()
    return payload, fetcher.warnings


def summary(payload: Dict) -> str:
    return format_summary("12306", summarize(payload))


def _rail_title(order: Dict) -> str:
    tickets = order.get("tickets") or []
    if tickets and isinstance(tickets[0], dict):
        ticket = tickets[0]
        parts = [
            ticket.get("train_code"),
            (
                f"{ticket.get('from_station')}→{ticket.get('to_station')}"
                if ticket.get("from_station") and ticket.get("to_station")
                else None
            ),
        ]
        text = " ".join(part for part in parts if part)
        if text:
            return text
    return "12306 火车票订单"


def rows(payload: Dict) -> List[Dict]:
    rows = []
    for order in _parse_rail_orders(payload.get("pages") or []):
        tickets = order.get("tickets") or []
        when = order.get("occurred")
        if not when and tickets:
            when = (tickets[0] or {}).get("travel_date")
        rows.append(
            make_record(
                source="12306",
                record_type="trip",
                id=stable_id(
                    "12306",
                    "trip",
                    order.get("order_id") or order.get("sequence_no"),
                ),
                occurred=when,
                amount=order.get("paid"),
                direction="out",
                merchant="中国铁路 12306",
                title=_rail_title(order),
                status=order.get("status"),
                extra={
                    "sequence_no": order.get("sequence_no"),
                    "order_date": order.get("order_date"),
                    "tickets": tickets,
                },
            )
        )
    return rows


def check(spec, client, jar) -> ProbeResult:
    info = Rail12306Fetcher(client, jar, max_pages=1).probe()
    return ProbeResult(spec, "ok", f"queryMyOrder.html 可读（HTTP {info.get('status')}）")


def plan(args) -> str:
    requested_start, requested_end = resolve_date_range(args.start_date, args.end_date)
    plan = plan_query_windows(requested_start, requested_end)
    windows = plan["windows"]
    if windows:
        first = windows[0]
        form = build_order_form(
            0,
            first["start_date"],
            first["end_date"],
            page_size=args.page_size,
            query_where=RAIL_QUERY_WHERE,
            query_type=args.query_type,
        )
        form_text = json.dumps(form, ensure_ascii=False)
    else:
        form_text = "(none: no overlap with the 12306 30-day horizon)"
    window_text = (
        ", ".join(f"{w['start_date']}..{w['end_date']}" for w in windows)
        or "(none)"
    )
    lines = [
        "plan: 12306 fetch (dry-run, no network, no Keychain)",
        f"endpoint: POST {RAIL_ORDER_ENDPOINT}",
        "api: queryMyOrder (query_where=H 历史订单, queryType=1 按订票日期, "
        "come_from_flag=my_order)",
        f"not used: {RAIL_NOT_COMPLETE_ENDPOINT} (未完成订单, only {{_json_att:''}}; "
        "different response shape)",
        f"requested dates: {requested_start} .. {requested_end}",
        f"12306 allowed: {plan['oldest_allowed']} .. {plan['newest_allowed']} "
        f"(retention={RAIL_RETENTION_DAYS}d, per-query span<="
        f"{RAIL_MAX_SPAN_DAYS}d)",
        f"query windows ({len(windows)}, newest first): {window_text}",
        f"form(window 1, page=1): {form_text}",
        "clamping: dates older than today-30 or newer than yesterday are "
        "clamped; a range with no overlap sends no request",
        "pagination: pageIndex is zero-based per window; stop at "
        "order_total_number or the first empty page",
        "auth: browser cookies, tk is required; uamtk/JSESSIONID/RAIL_* optional",
        f"login page: {RAIL_LOGIN_URL}",
        "signature: none found in the public order bundle; this tool sends "
        "no forged sign",
        "risk: any verification/signature demand fails with "
        "'needs risk signature' (no bypass)",
        "read-only: only queryMyOrder; no cancel/refund/pay/write API",
        "pacing: >= 1.0s between requests",
        "cookies: read-only from browser Cookies SQLite, in memory only",
        f"output: {args.out or 'OUT'}/{PREFIX}-YYYYmmdd-HHMMSS.json (0600)",
        "rows: rail12306-rows-YYYYmmdd-HHMMSS.jsonl (0600)",
    ]
    for warning in plan["warnings"]:
        lines.insert(1, f"warning: {warning}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# invoice action: read-only 180-day e-invoice list
# ---------------------------------------------------------------------------


def extra(actions) -> None:
    invoice = actions.add_parser("invoice", help="电子发票只读列表（180 天，需先在铁路12306 APP 完成一次授权查询）")
    invoice.add_argument("--out", type=Path, help="输出目录（非 --dry-run 必填）")
    invoice.add_argument("--dry-run", action="store_true", help="只打印请求计划，不读登录信息、不联网")
    invoice.add_argument("--max-pages", type=int, default=RAIL_MAX_PAGES, help=f"最多翻页数，默认 {RAIL_MAX_PAGES}")
    invoice.add_argument(
        "--type", dest="invoice_type", choices=("pending", "issued"), default="pending",
        help="pending=待开票（默认），issued=已开票",
    )
    invoice.add_argument("--start-date", default=None, help="乘车起始日期 YYYY-MM-DD（默认今天-179）")
    invoice.add_argument(
        "--end-date", default=None,
        help="乘车结束日期 YYYY-MM-DD（默认今天-31，与 30 天订单接口不重叠，避免账本对同一趟车重复计钱）",
    )
    invoice.add_argument("--include-recent", action="store_true", help="把最近 30 天也纳入发票查询（会与 12306 fetch 的行重叠）")
    invoice.add_argument("--max-requests", type=int, default=8, help="单次最多请求数（含门禁），默认 8；行数超过预算时需要调大")
    invoice.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    browser_args(invoice)
    net_args(invoice, CONNECTOR)


def run_extra(args):
    if args.action == "invoice":
        return run_invoice(args)
    return None


def run_invoice(args) -> int:
    """Read-only 180-day e-invoice list (gate checked before any list request)."""

    if args.dry_run:
        print("\n".join([
            "plan: 12306 invoice (dry-run, no network, no Keychain)",
            "endpoint: gate POST https://kyfw.12306.cn/otn/login/conf"
            f" -> list POST {RAIL_INVOICE_QUERY_URL}",
            'gate: {"is_login":"Y"} and psr_qr_code_result != "N" (铁路12306 APP 授权查询)',
            "window: 乘车日期 今天-179 .. 今天-31 (clamped to >= 2024-11-01)",
            "  与 12306 fetch 的 30 天窗口不重叠；--include-recent 才会覆盖最近 30 天",
            f"type: {args.invoice_type} (pending=待开票 / issued=已开票)",
            "read-only: 只查询；不调用开具/红冲/下载/发邮件任何接口",
            "pacing: >= 2.0s, 单次最多 8 个请求; error.html 立即停止不重试",
        ]))
        return 0
    if args.max_pages < 1:
        raise UsageError("--max-pages 必须 >= 1")
    if not RAIL_INVOICE_QUERY_URL:
        raise UsageError("12306 发票端点未配置")
    if args.out is None:
        raise UsageError("--out DIR 是必填项（dry-run 除外）")
    try:
        out_dir = ensure_out_dir(args.out)
    except OSError as exc:
        raise UsageError(f"无法创建输出目录: {args.out}") from exc
    jar = provider(CONNECTOR, args).load()
    client = HttpClient(cookie_jar=jar, pacer=Pacer(max(2.0, args.delay)), timeout=args.timeout)
    try:
        fetcher = RailInvoiceFetcher(
            client, jar,
            invoice_type=RAIL_INVOICE_TYPE_PENDING if args.invoice_type == "pending" else RAIL_INVOICE_TYPE_ISSUED,
            max_pages=args.max_pages,
            max_requests=args.max_requests,
            min_interval=max(2.0, args.delay),
        )
        if args.end_date is None and not args.include_recent:
            args.end_date = (
                datetime.datetime.now(datetime.timezone.utc).astimezone().date() - datetime.timedelta(days=31)
            ).isoformat()
        gate = fetcher.check_gate()
        if not gate.get("psr_authorized"):
            raise AuthRequired(
                "12306 电子发票需要先在铁路12306 APP 完成一次“授权查询”",
                "打开铁路12306 APP → 我的 → 电子发票（或“授权查询”）完成一次授权，"
                "然后重跑 `python3 -m connectors.rail12306 invoice --out out`。"
                "本工具不会触发授权、开具、红冲或下载，也不会绕过核验。",
            )
        payload = fetcher.fetch(args.start_date, args.end_date, gate=gate)
        records = invoice_rows(payload)
        raw_path = write_json(out_dir, INVOICE_PREFIX, payload)
        rows_path = write_rows(out_dir, INVOICE_PREFIX, records, stamp=stamp_from_raw(raw_path, INVOICE_PREFIX))
        query = payload.get("query") or {}
        if args.json:
            print(json.dumps({
                "command": "12306 invoice",
                "type": args.invoice_type,
                "total": query.get("total"),
                "rows": len(records),
                "requests": query.get("requests_sent"),
                "window": query.get("window"),
                "written": str(raw_path),
                "rows_file": str(rows_path),
            }, ensure_ascii=False, indent=1))
        else:
            print(
                f"12306 invoice: type={args.invoice_type} total={query.get('total')} "
                f"rows={len(records)} requests={query.get('requests_sent')} "
                f"window={query.get('window')}"
            )
            print(f"written: {raw_path}")
            print(f"rows: {len(records)} -> {rows_path}")
        return 0
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


CONNECTOR = Connector(
    key="rail12306", label="中国铁路 12306", prefix=PREFIX, spec=SPEC,
    describe="12306 历史订单（增量归档，30 天窗口）和电子发票只读列表（invoice 动作）",
    fetch=fetch, summary=summary, rows=rows, add_fetch_args=add_fetch_args, validate=validate, plan=plan, check=check,
    extra=extra, run_extra=run_extra, login_kind="qr", keepalive_hours=1.0, max_pages=RAIL_MAX_PAGES,
    record_types=("trip",), min_delay=1.0,
)
