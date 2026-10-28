"""China Railway 12306 historical-order reader (read-only).

Only ``POST https://kyfw.12306.cn/otn/queryOrder/queryMyOrder`` is used.
The request shape follows the public order-page bundle. The public page only offers the last 30 days (the picker is
``GetDateStr(-30) .. GetDateStr(-1)`` and the page says order data is kept
for 30 days).  ``fetch`` therefore clamps a requested range to that horizon
and walks it in <=30-day windows instead of sending a query the server will
reject with an HTML error page.

Because the horizon is a sliding 30-day window, querying "the whole horizon"
every day would re-read 29 already-seen days on every run.  ``fetch`` is
therefore *incremental*: the covered calendar days live in a small local
state file (default
``rail-12306.json`` in the shared application-support directory or ``$PFIN_STATE_DIR``,
mode 0600, atomic replace) and each run only queries the gaps.  A day counts
as covered only when its window was fully paged out; failed windows stay as
gaps and are retried after a backoff instead of being hammered.

No signature is generated here, and no write, cancel, refund, repay,
transfer, invoice or payment API is present.  Out-of-range dates are never
probed: the planner only ever emits windows inside ``[today-30, yesterday]``
(Beijing time), because the server answers a boundary probe with an
``error.html`` redirect and may trigger throttling.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .blocking import check_rail12306_html, check_rail12306_json
from ..kit.cookies import CookieJar
from ..kit.errors import (
    ApiError,
    AuthRequired,
    NeedsRiskSignature,
    NetworkError,
    ConnectorError,
    RiskBlocked,
    UsageError,
)
from ..kit.env import getenv
from ..kit.session import DEFAULT_STATE_DIR
from ..kit.http import HttpClient, Response

RAIL_HOST = "kyfw.12306.cn"
RAIL_BASE = "https://kyfw.12306.cn"
RAIL_ORDER_ENDPOINT = RAIL_BASE + "/otn/queryOrder/queryMyOrder"
# The "未完成订单" tab uses a different endpoint and shape:
# POST data={_json_att:""} -> data.orderDBList / orderCacheDTO.  It is not a
# historical-order query and this tool deliberately does not call it.
RAIL_NOT_COMPLETE_ENDPOINT = RAIL_BASE + "/otn/queryOrder/queryMyOrderNoComplete"
RAIL_ORDER_PAGE_URL = RAIL_BASE + "/otn/queryOrder/queryMyOrder.html"
RAIL_TRAIN_ORDER_PAGE = RAIL_BASE + "/otn/view/train_order.html"
RAIL_LOGIN_CHECK_URL = RAIL_BASE + "/passport/web/auth/uamtk-static"
RAIL_LOGIN_URL = RAIL_BASE + "/otn/resources/login.html"

RAIL_REFERER = RAIL_TRAIN_ORDER_PAGE
RAIL_ORIGIN = RAIL_BASE

# ``tk`` is the ticket issued by /otn/uamauthclient after a successful
# login; it is the decisive logged-in marker.  ``uamtk`` is the passport
# app token, ``JSESSIONID`` carries the /otn session, and the RAIL_* pair
# is the server-issued device fingerprint.  All are sent as ordinary
# cookies; none is a per-request signature.
RAIL_LOGIN_COOKIES = (
    "tk",
    "uamtk",
    "JSESSIONID",
    "RAIL_DEVICEID",
    "RAIL_EXPIRATION",
    "BIGipServerotn",
    "BIGipServerpool_passport",
)
RAIL_REQUIRED_LOGIN_COOKIES = ("tk",)

# Cookies that the server may rotate on any response.  They are refreshed
# in memory only; the browser database is never written back.
RAIL_COOKIE_NAMES = frozenset(RAIL_LOGIN_COOKIES) | {"route", "myversion"}

RAIL_QUERY_WHERE = "H"  # 历史订单
RAIL_QUERY_WHERE_NOT_TRAVEL = "G"  # 未出行订单（本工具不查）
RAIL_QUERY_TYPE = "1"   # 按订票日期（历史订单页硬编码为 1）
RAIL_COME_FROM_FLAG = "my_order"
RAIL_PAGE_SIZE = 8
# Match the public bundle's page size and offset/``order_total_number`` rule.
# An empty result cannot establish that a larger page size is supported.

# The history tab's picker is built with
#   startDate: GetDateStr(r ? -30 : -1)
#   endDate:   GetDateStr(r ? -1 : 29)
# (train_order/main_v30108.js line 50 col 30722), and the page says
# "订单信息在本网站保存期限为30日".  So each query may span at most 30
# calendar days (inclusive) and the oldest queryable day is today-30.
RAIL_MAX_SPAN_DAYS = 30
RAIL_RETENTION_DAYS = 30
RAIL_DEFAULT_DAYS = RAIL_MAX_SPAN_DAYS
RAIL_MAX_PAGES = 20

# --- incremental archive ------------------------------------------------
#
# The state file records, per queried window, when it was fetched, how many
# pages/orders it produced and whether it succeeded, plus the set of fully
# covered calendar days.  It never contains cookie values, request headers
# or ticket/price data — only window metadata and 12306 order ids (which are
# used to deduplicate across runs).
RAIL_STATE_ENV = "PFIN_STATE_DIR"
RAIL_STATE_FILENAME = "rail-12306.json"
RAIL_STATE_VERSION = 1
RAIL_MAX_KNOWN_ORDER_IDS = 2000

# Politeness: the server throttles after a boundary probe, so requests are
# spaced out and capped per run.  Anything left over becomes a gap for the
# next run instead of being retried in a loop.
RAIL_MIN_REQUEST_INTERVAL = 2.0
RAIL_MAX_REQUESTS = 12

# Backoff after a failed window, so a throttled server is left alone.
RAIL_ERROR_BACKOFF_SECONDS = 6 * 3600
RAIL_THROTTLE_BACKOFF_SECONDS = 12 * 3600
RAIL_OUT_OF_RANGE_BACKOFF_SECONDS = 24 * 3600
RAIL_RISK_BACKOFF_SECONDS = 24 * 3600

# Statuses that keep a window as a *gap* (never "covered").
RAIL_FAILED_STATUSES = frozenset(
    {
        "error_page",
        "throttled",
        "out_of_range",
        "risk",
        "auth",
        "network",
        "error",
    }
)

# --- electronic invoice (电子发票) --------------------------------------
#
# The only public read-only entry that keeps a window longer than 30 days and
# still carries date + train + order number + amount.  Both endpoints below are
# called by the public invoice pages themselves; no signature, h5st or device
# fingerprint is involved.
RAIL_INVOICE_QUERY_URL = RAIL_BASE + "/otn/eInvoice/queryPsr"
RAIL_LOGIN_CONF_URL = RAIL_BASE + "/otn/login/conf"
RAIL_INVOICE_REFERER = RAIL_BASE + "/otn/view/invoice_ticket_list.html"
RAIL_INVOICE_INDEX_URL = RAIL_BASE + "/otn/view/invoice_index.html"

# Tab semantics (invoice_ticket_list.html:61-70, js:41:23410):
#   待开票 #invoice_ticket_tab[data-type=1] -> type=2
#   已开票 data-type=2                      -> type=1
RAIL_INVOICE_TYPE_PENDING = "2"
RAIL_INVOICE_TYPE_ISSUED = "1"
RAIL_INVOICE_TYPE_LABELS = {
    RAIL_INVOICE_TYPE_PENDING: "pending",
    RAIL_INVOICE_TYPE_ISSUED: "issued",
}

# The request body carries no pageSize; the list page hard-codes 10 per page
# (it computes pages from ``data.total % 10``), and ``pageIndex`` starts at 1.
RAIL_INVOICE_PAGE_SIZE = 10
RAIL_INVOICE_PAGE_INDEX_BASE = 1

# UI window: boarding date 今天-179 .. 今天+14, clamped by r() to 2024-11-01
# (js:41:13030-13330, js:41:22853-22952).  We never query future dates: an
# expense archive only needs trips that already happened, so the newest legal
# query date is today.
RAIL_INVOICE_LOOKBACK_DAYS = 179
RAIL_INVOICE_FUTURE_DAYS = 14
RAIL_INVOICE_DATE_FLOOR = datetime.date(2024, 11, 1)

# Politeness: the invoice list is a different endpoint than queryMyOrder, but
# out-of-range queries can still trigger account-level throttling.
RAIL_INVOICE_MIN_INTERVAL = 2.0
RAIL_INVOICE_MAX_REQUESTS = 8

# Endpoints in the same bundle that are NOT read-only and MUST never be called
# (invoice_main.js:30:8101-10400).  Listed for auditability only.
RAIL_INVOICE_FORBIDDEN_ENDPOINTS = (
    "npRecordVoucher",
    "npRecordVoucherAsync",
    "npRecordVoucherBatch",
    "arithER60RecordVoucher",
    "npExchangeVoucher",
    "npExchangeVoucherBatch",
    "arithER60ExchangeVoucher",
    "checkRecordVoucher",
    "checkExchangeVoucher",
    "getImg",
    "sendEmail",
    "saveInvoiceHeader",
    "updateInvoiceHeader",
    "deleteInvoiceHeader",
    "arithER60QueryVoucherDownload",
)

RAIL_INVOICE_AUTH_MESSAGE = (
    "12306 电子发票查询被「铁路12306 APP 授权」门禁挡住："
    "当前账号还没有通过 APP 授权查询"
)
RAIL_INVOICE_AUTH_HINT = (
    "请在手机「铁路12306」APP 里完成一次电子发票/授权查询（本人操作，"
    "本工具不会触发、代理或绕过授权），然后在浏览器里确认仍处于登录状态，"
    "再重跑 `python3 -m connectors.rail12306 invoice`。授权是账号级状态，成功一次后"
    "后续运行会自动生效；若刚授权仍失败，重新打开一次发票页让站点刷新"
    "会话后再试。"
)

BEIJING = datetime.timezone(datetime.timedelta(hours=8))

_DATETIME_RE = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})"
    r"(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?"
)


def _text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _flag(value) -> bool:
    """12306 sends flags as ``"Y"``/``"N"`` (sometimes as real booleans)."""

    if isinstance(value, bool):
        return value
    return (_text(value) or "").upper() == "Y"


def _iso_now(moment: datetime.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment.isoformat(timespec="seconds")


def _parse_iso(text) -> Optional[datetime.datetime]:
    """Tolerant ISO-8601 parse for state timestamps (never raises)."""

    if not text:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment


def _parse_day(value) -> Optional[datetime.date]:
    """Tolerant day parse (``YYYY-MM-DD`` or ``YYYYMMDD``) for bookkeeping."""

    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return datetime.date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None


def _as_day(value) -> datetime.date:
    """Day coercion for user-supplied ranges (``YYYY-MM-DD``/``YYYYMMDD``)."""

    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    day = _parse_day(value)
    if day is None:
        raise UsageError(f"日期格式必须是 YYYY-MM-DD: {value!r}")
    return day


def _iter_days(start: datetime.date, end: datetime.date):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += datetime.timedelta(days=1)


def _contiguous_runs(days: Iterable[datetime.date]) -> List[List[datetime.date]]:
    runs: List[List[datetime.date]] = []
    for day in sorted(set(days)):
        if runs and (day - runs[-1][-1]).days == 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


def _split_run(
    run: Sequence[datetime.date], max_span_days: int
) -> List[Tuple[datetime.date, datetime.date]]:
    chunks: List[Tuple[datetime.date, datetime.date]] = []
    index = 0
    while index < len(run):
        piece = run[index : index + max_span_days]
        chunks.append((piece[0], piece[-1]))
        index += max_span_days
    return chunks


def _window_key(start_date: str, end_date: str) -> str:
    return f"{start_date}..{end_date}"


def default_state_dir(environ: Optional[Dict[str, str]] = None) -> Path:
    """``$PFIN_STATE_DIR`` or the macOS application-support default."""

    override = (getenv(RAIL_STATE_ENV, environ) or "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return Path(DEFAULT_STATE_DIR).expanduser()


def default_state_path(environ: Optional[Dict[str, str]] = None) -> Path:
    return default_state_dir(environ) / RAIL_STATE_FILENAME


def empty_state() -> Dict:
    return {
        "version": RAIL_STATE_VERSION,
        "updated_at": None,
        "covered_days": [],
        "known_order_ids": [],
        "windows": [],
        "last_run": None,
    }


def _covered_day_set(state: Dict) -> set:
    covered = state.get("covered_days") if isinstance(state, dict) else None
    return {str(day) for day in (covered or []) if _parse_day(day)}


def _normalize_state(raw) -> Dict:
    """Best-effort normalization; partial garbage is dropped, not fatal."""

    if not isinstance(raw, dict):
        raise ValueError("state root is not an object")
    state = empty_state()
    covered = set()
    for item in raw.get("covered_days") or []:
        day = _parse_day(item)
        if day:
            covered.add(day)
    state["covered_days"] = [day.isoformat() for day in sorted(covered)]

    known: List[str] = []
    seen = set()
    for item in raw.get("known_order_ids") or []:
        text = _text(item)
        if text and text not in seen:
            seen.add(text)
            known.append(text)
    state["known_order_ids"] = known[-RAIL_MAX_KNOWN_ORDER_IDS:]

    windows: Dict[str, Dict] = {}
    for item in raw.get("windows") or []:
        if not isinstance(item, dict):
            continue
        start = _parse_day(item.get("start_date"))
        end = _parse_day(item.get("end_date"))
        if not start or not end or start > end:
            continue
        record = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "fetched_at": _text(item.get("fetched_at")),
            "status": str(item.get("status") or "ok"),
            "pages": _int(item.get("pages")) or 0,
            "orders": _int(item.get("orders")) or 0,
            "order_total_number": _int(item.get("order_total_number")),
            "attempts": _int(item.get("attempts")) or 1,
            "retry_after": _text(item.get("retry_after")),
            "reason": _text(item.get("reason")),
            "error": _text(item.get("error")),
        }
        windows[_window_key(record["start_date"], record["end_date"])] = record
    state["windows"] = sorted(windows.values(), key=lambda item: item["start_date"])
    state["updated_at"] = _text(raw.get("updated_at"))
    state["last_run"] = raw.get("last_run") if isinstance(raw.get("last_run"), dict) else None
    return state


def _quarantine_state(path: Path) -> None:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(path.name + ".corrupt-" + stamp)
    try:
        os.replace(path, target)
    except OSError:
        pass


def load_state(path) -> Tuple[Dict, List[str]]:
    """Read the archive state; a missing or corrupt file yields a fresh one.

    Corruption must never crash a scheduled run: the unreadable file is
    moved aside (``*.corrupt-<UTC>``) so it can be inspected, and the run
    proceeds as a first run (which simply re-queries the legal window).
    """

    path = Path(path)
    warnings: List[str] = []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_state(), warnings
    except OSError as exc:
        warnings.append(
            f"归档状态文件无法读取（{type(exc).__name__}）；本次按首次运行处理"
        )
        return empty_state(), warnings
    try:
        raw = json.loads(raw_text)
        state = _normalize_state(raw)
    except ValueError:
        _quarantine_state(path)
        warnings.append(
            "归档状态文件已损坏；已隔离为 .corrupt 备份并按首次运行处理"
        )
        return empty_state(), warnings
    version = _int(raw.get("version"))
    if version is not None and version != RAIL_STATE_VERSION:
        warnings.append(
            f"归档状态文件版本 {version} 与当前 {RAIL_STATE_VERSION} 不一致；已按兼容模式读取"
        )
    return state, warnings


def save_state(state: Dict, path) -> Path:
    """Write the state file atomically (temp file + fsync + replace, 0600)."""

    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    fd, temp_name = tempfile.mkstemp(
        prefix=".rail-state-", suffix=".tmp", dir=str(directory)
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
    return path


def _backoff_days(
    state: Dict, now: datetime.datetime
) -> Dict[datetime.date, Tuple[datetime.datetime, str]]:
    """Days inside a failed window whose retry backoff has not elapsed yet."""

    blocked: Dict[datetime.date, Tuple[datetime.datetime, str]] = {}
    for record in state.get("windows") or []:
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "")
        if status not in RAIL_FAILED_STATUSES:
            continue
        retry_at = _parse_iso(record.get("retry_after"))
        if retry_at is None or retry_at <= now:
            continue
        start = _parse_day(record.get("start_date"))
        end = _parse_day(record.get("end_date"))
        if not start or not end or start > end:
            continue
        for day in _iter_days(start, end):
            blocked.setdefault(day, (retry_at, status))
    return blocked


def plan_incremental_windows(
    state: Dict,
    today,
    requested_range: Optional[Sequence] = None,
    *,
    retention_days: int = RAIL_RETENTION_DAYS,
    max_span_days: int = RAIL_MAX_SPAN_DAYS,
    now: Optional[datetime.datetime] = None,
    force: bool = False,
) -> Dict:
    """Plan only the queryable gaps inside the 30-day horizon.

    ``state`` is the archive state (see :func:`load_state`), ``today`` is a
    Beijing date (or tz-aware datetime) and ``requested_range`` is an
    optional ``(start, end)`` pair of ``YYYY-MM-DD`` strings that is clamped
    to ``[today-30, yesterday]`` exactly like :func:`plan_query_windows`.
    With ``requested_range=None`` the whole legal horizon is the target.

    Windows are returned **oldest first** and chunked from the oldest day:

    * the oldest day is the one closest to dropping out of 12306's 30-day
      retention, so fetching it first minimises permanent loss when a run is
      cut short by throttling or the request budget;
    * progress then moves monotonically forward in time, which keeps the
      state file readable and lets an interrupted run simply continue;
    * because covered days are persisted after every window, the order does
      not change the final result — each run re-derives the gaps.

    Days belonging to a window that failed earlier stay gaps, but are held
    back until that window's ``retry_after`` (backoff) has passed, so a
    throttled server is not hit again immediately.  Days are never planned
    outside ``[today-30, yesterday]``; a boundary probe is never sent.
    """

    if max_span_days < 1:
        raise ValueError("max_span_days must be >= 1")
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")
    moment = now or datetime.datetime.now(BEIJING)
    today_day = _as_day(today)
    oldest = today_day - datetime.timedelta(days=retention_days)
    newest = today_day - datetime.timedelta(days=1)

    warnings: List[str] = []
    if requested_range is None:
        requested_start, requested_end = oldest, newest
    else:
        try:
            raw_start, raw_end = requested_range  # type: ignore[misc]
        except (TypeError, ValueError) as exc:
            raise UsageError("requested_range 必须是 (start, end)") from exc
        requested_start = _as_day(raw_start)
        requested_end = _as_day(raw_end)
        if requested_start > requested_end:
            raise UsageError("--start-date 不能晚于 --end-date")

    start = max(requested_start, oldest)
    end = min(requested_end, newest)
    result: Dict = {
        "requested_range": (
            [requested_start.isoformat(), requested_end.isoformat()]
            if requested_range is not None
            else None
        ),
        "target_start_date": None,
        "target_end_date": None,
        "oldest_allowed": oldest.isoformat(),
        "newest_allowed": newest.isoformat(),
        "retention_days": retention_days,
        "max_span_days": max_span_days,
        "force": bool(force),
        "windows": [],
        "deferred": [],
        "warnings": warnings,
        "covered_days": 0,
        "target_days": 0,
        "gap_days": 0,
        "ready_days": 0,
        "deferred_days": 0,
    }
    if start > end:
        warnings.append(
            f"请求区间 {requested_start}..{requested_end} 与 12306 可查区间 "
            f"{oldest}..{newest} 没有交集；未发送查询。"
            f"12306 仅保留最近 {retention_days} 天订单。"
        )
        return result

    result["target_start_date"] = start.isoformat()
    result["target_end_date"] = end.isoformat()
    target_days = list(_iter_days(start, end))
    archived = _covered_day_set(state)
    covered = set() if force else archived
    blocked = {} if force else _backoff_days(state, moment)

    covered_in_target = [day for day in target_days if day.isoformat() in archived]
    gap_days = [day for day in target_days if day.isoformat() not in covered]
    ready_days = [day for day in gap_days if day not in blocked]
    deferred_days = [day for day in gap_days if day in blocked]

    windows: List[Dict] = []
    for run in _contiguous_runs(ready_days):
        for run_start, run_end in _split_run(run, max_span_days):
            windows.append(
                {
                    "start_date": run_start.isoformat(),
                    "end_date": run_end.isoformat(),
                    "days": (run_end - run_start).days + 1,
                }
            )
    deferred: List[Dict] = []
    for run in _contiguous_runs(deferred_days):
        for run_start, run_end in _split_run(run, max_span_days):
            retry_at = max(
                blocked[day][0] for day in _iter_days(run_start, run_end)
            )
            statuses = sorted(
                {blocked[day][1] for day in _iter_days(run_start, run_end)}
            )
            deferred.append(
                {
                    "start_date": run_start.isoformat(),
                    "end_date": run_end.isoformat(),
                    "reason": "backoff",
                    "status": ",".join(statuses),
                    "retry_after": _iso_now(retry_at),
                }
            )

    result.update(
        {
            "windows": windows,
            "deferred": deferred,
            "covered_days": len(covered_in_target),
            "target_days": len(target_days),
            "gap_days": len(gap_days),
            "ready_days": len(ready_days),
            "deferred_days": len(deferred_days),
        }
    )
    if force and covered_in_target:
        warnings.append(
            f"已忽略归档覆盖（force refresh）：目标区间 {start}..{end} "
            f"共 {len(target_days)} 天将重新查询"
        )
    if deferred:
        warnings.append(
            f"{len(deferred_days)} 天处于失败退避中"
            f"（最早 {deferred[0]['retry_after']} 之后才重试），本次跳过"
        )
    if not windows and not deferred:
        warnings.append(
            f"目标区间 {start}..{end} 已全部归档"
            f"（{len(covered_in_target)}/{len(target_days)} 天已覆盖），"
            f"本次未发送请求；如需强制重查请使用 --refresh"
        )
    return result


def coverage_summary(state: Dict, target_start, target_end) -> Dict:
    """Earliest/latest covered day, contiguous runs and remaining gap days."""

    summary = {
        "oldest_covered": None,
        "newest_covered": None,
        "contiguous_ranges": 0,
        "covered_days": 0,
        "target_days": 0,
        "gap_days": 0,
        "ranges": [],
    }
    start = _parse_day(target_start)
    end = _parse_day(target_end)
    if not start or not end or start > end:
        return summary
    covered = _covered_day_set(state)
    days = list(_iter_days(start, end))
    covered_days = [day for day in days if day.isoformat() in covered]
    runs = _contiguous_runs(covered_days)
    summary.update(
        {
            "oldest_covered": covered_days[0].isoformat() if covered_days else None,
            "newest_covered": covered_days[-1].isoformat() if covered_days else None,
            "contiguous_ranges": len(runs),
            "covered_days": len(covered_days),
            "target_days": len(days),
            "gap_days": len(days) - len(covered_days),
            "ranges": [
                [run[0].isoformat(), run[-1].isoformat()] for run in runs
            ],
        }
    )
    return summary


def normalize_amount(value) -> Optional[str]:
    """Return a plain decimal string for ``¥553.00`` / ``553`` / ``553.5元``."""

    text = _text(value)
    if not text:
        return None
    cleaned = (
        text.replace(",", "")
        .replace("¥", "")
        .replace("￥", "")
        .replace("元", "")
        .replace("\u00a0", "")
        .strip()
    )
    if not cleaned:
        return None

    def as_text(candidate: str) -> Optional[str]:
        try:
            number = Decimal(candidate)
        except (InvalidOperation, ValueError):
            return None
        return str(number) if number.is_finite() else None

    direct = as_text(cleaned)
    if direct is not None:
        return direct
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return as_text(match.group(0)) if match else None


def normalize_datetime(value) -> Optional[str]:
    """Normalize 12306's date/date-time fields to ``YYYY-MM-DD HH:MM:SS``."""

    text = _text(value)
    if not text:
        return None
    match = _DATETIME_RE.search(text)
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        moment = datetime.datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
        )
    except ValueError:
        return None
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _sum_amounts(values: Iterable) -> Optional[str]:
    total = Decimal("0")
    found = False
    for value in values:
        amount = normalize_amount(value)
        if amount is None:
            continue
        total += Decimal(amount)
        found = True
    return str(total) if found else None


def parse_date(value: str) -> datetime.date:
    try:
        return datetime.datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (AttributeError, ValueError) as exc:
        raise UsageError(f"日期格式必须是 YYYY-MM-DD: {value!r}") from exc


def allowed_date_bounds(
    now: Optional[datetime.datetime] = None,
    *,
    retention_days: int = RAIL_RETENTION_DAYS,
) -> Tuple[datetime.date, datetime.date]:
    """Return the oldest/newest dates the history query accepts.

    The public page's picker is ``GetDateStr(-30) .. GetDateStr(-1)`` and
    states that order information is kept for 30 days.  Dates are Beijing
    dates, because the page builds them from the browser's local clock.
    """

    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")
    moment = now or datetime.datetime.now(BEIJING)
    today = moment.date()
    return (
        today - datetime.timedelta(days=retention_days),
        today - datetime.timedelta(days=1),
    )


def default_date_range(
    now: Optional[datetime.datetime] = None,
) -> Tuple[str, str]:
    """Default to the full 30-day history horizon (oldest .. yesterday)."""

    oldest, newest = allowed_date_bounds(now)
    return oldest.isoformat(), newest.isoformat()


def resolve_date_range(
    start: Optional[str],
    end: Optional[str],
    *,
    now: Optional[datetime.datetime] = None,
) -> Tuple[str, str]:
    """Parse the requested range; clamping happens in ``plan_query_windows``.

    ``end`` may be today here so the planner can turn it into yesterday with
    an explicit warning.  A future date is still a usage error.
    """

    moment = now or datetime.datetime.now(BEIJING)
    today = moment.date()
    if start is None and end is None:
        return default_date_range(moment)
    end_date = parse_date(end) if end else today - datetime.timedelta(days=1)
    if start:
        start_date = parse_date(start)
    else:
        start_date = end_date - datetime.timedelta(days=RAIL_DEFAULT_DAYS - 1)
    if start_date > end_date:
        raise UsageError("--start-date 不能晚于 --end-date")
    if end_date > today:
        raise UsageError("--end-date 不能晚于今天（12306 历史订单只查过去日期）")
    return start_date.isoformat(), end_date.isoformat()


def plan_query_windows(
    start_date: str,
    end_date: str,
    *,
    now: Optional[datetime.datetime] = None,
    max_span_days: int = RAIL_MAX_SPAN_DAYS,
    retention_days: int = RAIL_RETENTION_DAYS,
) -> Dict:
    """Clamp a requested range to the 12306 horizon and split it into windows.

    Returns newest-first windows, so ``fetch`` walks backwards from the
    requested end date until it reaches the oldest allowed date.  Each
    window is at most ``max_span_days`` calendar days inclusive, matching the
    history picker's ``-30 .. -1`` limits.

    This is the *non-incremental* clamp/planner: it is what
    ``--dry-run`` reports and what ``incremental=False`` executes.
    Incremental runs use :func:`plan_incremental_windows` on top of it.
    """

    if max_span_days < 1:
        raise ValueError("max_span_days must be >= 1")
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")
    moment = now or datetime.datetime.now(BEIJING)
    today = moment.date()
    requested_start = parse_date(start_date)
    requested_end = parse_date(end_date)
    if requested_start > requested_end:
        raise UsageError("--start-date 不能晚于 --end-date")

    oldest = today - datetime.timedelta(days=retention_days)
    newest = today - datetime.timedelta(days=1)
    warnings: List[str] = []

    start = max(requested_start, oldest)
    end = min(requested_end, newest)
    overlap = start <= end
    if overlap and requested_end > newest:
        warnings.append(
            f"结束日期 {requested_end} 晚于 12306 可查上限 {newest}（昨天）；"
            f"已截断为 {newest}"
        )
    if overlap and requested_start < oldest:
        warnings.append(
            f"起始日期 {requested_start} 早于 12306 最早可查日期 {oldest}"
            f"（仅保留最近 {retention_days} 天订单）；已截断为 {oldest}"
        )

    windows: List[Dict[str, str]] = []
    if overlap:
        cursor = end
        while cursor >= start:
            window_start = max(
                start, cursor - datetime.timedelta(days=max_span_days - 1)
            )
            windows.append(
                {
                    "start_date": window_start.isoformat(),
                    "end_date": cursor.isoformat(),
                }
            )
            cursor = window_start - datetime.timedelta(days=1)
        if len(windows) > 1:
            warnings.append(
                f"请求范围超过单次 {max_span_days} 天上限；已拆分为 "
                f"{len(windows)} 个窗口，按从新到旧查询"
            )
    else:
        warnings.append(
            f"请求区间 {requested_start}..{requested_end} 与 12306 可查区间 "
            f"{oldest}..{newest} 没有交集；未发送查询。"
            f"12306 仅保留最近 {retention_days} 天订单。"
        )

    return {
        "requested_start_date": requested_start.isoformat(),
        "requested_end_date": requested_end.isoformat(),
        "start_date": start.isoformat() if windows else None,
        "end_date": end.isoformat() if windows else None,
        "oldest_allowed": oldest.isoformat(),
        "newest_allowed": newest.isoformat(),
        "max_span_days": max_span_days,
        "retention_days": retention_days,
        "windows": windows,
        "warnings": warnings,
    }


def build_order_form(
    page_index: int,
    start_date: str,
    end_date: str,
    *,
    page_size: int = RAIL_PAGE_SIZE,
    query_where: str = RAIL_QUERY_WHERE,
    query_type: str = RAIL_QUERY_TYPE,
    train_name: str = "",
) -> Dict[str, str]:
    """The exact jQuery ``data`` object built by train_order_init.

    The bundle sends ``come_from_flag=my_order``, a zero-based ``pageIndex``,
    ``pageSize=8`` and the two date strings.  It does not send a signature,
    nonce or ``_json_att`` field for this particular call.
    """

    return {
        "come_from_flag": RAIL_COME_FROM_FLAG,
        "pageIndex": str(int(page_index)),
        "pageSize": str(int(page_size)),
        "query_where": query_where,
        "queryStartDate": start_date,
        "queryEndDate": end_date,
        "queryType": str(query_type),
        "sequeue_train_name": train_name or "",
    }


def refresh_cookies_from_response(jar: CookieJar, response: Response) -> int:
    """Apply response cookies once; report 12306 rotations. Never write back to browser."""

    return sum(name in RAIL_COOKIE_NAMES for name in jar.apply_response(response))


def missing_login_cookies(jar: CookieJar, host: str = RAIL_HOST) -> List[str]:
    return [
        name
        for name in RAIL_REQUIRED_LOGIN_COOKIES
        if not jar.get(name, host)
    ]


def login_cookie_presence(
    jar: CookieJar, host: str = RAIL_HOST
) -> Dict[str, bool]:
    return {name: bool(jar.get(name, host)) for name in RAIL_LOGIN_COOKIES}


def require_login(jar: CookieJar, host: str = RAIL_HOST) -> None:
    missing = missing_login_cookies(jar, host)
    if missing:
        raise AuthRequired(
            "浏览器中没有找到 12306 登录 Cookie: " + ", ".join(missing),
            "请在浏览器中打开 https://kyfw.12306.cn/otn/resources/login.html "
            "扫码登录后重试；本工具不会尝试绕过登录",
        )


def _parse_ticket(raw: Dict) -> Dict:
    raw = raw if isinstance(raw, dict) else {}
    station = raw.get("stationTrainDTO") or {}
    passenger = raw.get("passengerDTO") or {}
    travel_date = normalize_datetime(
        raw.get("start_train_date_page") or raw.get("train_date")
    )
    return {
        "train_code": _text(station.get("station_train_code")),
        "train_no": _text((station.get("trainDTO") or {}).get("train_no")),
        "from_station": _text(station.get("from_station_name")),
        "to_station": _text(station.get("to_station_name")),
        "travel_date": travel_date,
        "depart_time": _text(station.get("start_time")),
        "arrive_time": _text(station.get("arrive_time")),
        "seat_type": _text(raw.get("seat_type_name")),
        "seat": _text(raw.get("seat_name")),
        "coach_no": _text(raw.get("coach_no")),
        "seat_no": _text(raw.get("seat_no")),
        "price": normalize_amount(raw.get("str_ticket_price_page")),
        "price_raw": _text(raw.get("str_ticket_price_page")),
        "passenger": _text(passenger.get("passenger_name")),
        "passenger_id_type": _text(passenger.get("passenger_id_type_name")),
        "ticket_status": _text(raw.get("ticket_status_name")),
        "ticket_type": _text(raw.get("ticket_type_name")),
        "trade_no": _text(raw.get("trade_no")),
    }


def parse_order(raw: Dict) -> Dict:
    raw = raw if isinstance(raw, dict) else {}
    raw_tickets = raw.get("tickets") or []
    tickets = [
        _parse_ticket(ticket)
        for ticket in raw_tickets
        if isinstance(ticket, dict)
    ]
    sequence = _text(raw.get("sequence_no"))
    order_id = _text(raw.get("order_id")) or sequence
    paid = normalize_amount(raw.get("ticket_total_price_page"))
    if paid is None:
        paid = _sum_amounts(
            (ticket or {}).get("str_ticket_price_page")
            for ticket in raw_tickets
            if isinstance(ticket, dict)
        )
    return {
        "order_id": order_id,
        "sequence_no": sequence,
        "occurred": normalize_datetime(raw.get("order_date")),
        "order_date": _text(raw.get("order_date")),
        "paid": paid,
        "ticket_total_price": _text(raw.get("ticket_total_price_page")),
        "status": _text(raw.get("order_status"))
        or next(
            (ticket.get("ticket_status") for ticket in tickets if ticket.get("ticket_status")),
            None,
        ),
        "tickets": tickets,
    }


def _page_raw_orders(page) -> List[Dict]:
    if not isinstance(page, dict):
        return []
    orders = page.get("orders")
    if isinstance(orders, list):
        return [order for order in orders if isinstance(order, dict)]
    raw = page.get("raw")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
        return _payload_raw_orders(payload)
    return _payload_raw_orders(page)


def _payload_raw_orders(payload) -> List[Dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("OrderDTODataList") or []
    return [row for row in rows if isinstance(row, dict)]


def parse_orders(pages: Iterable) -> List[Dict]:
    """One row per unique 12306 order, with per-ticket fares."""

    output: Dict[str, Dict] = {}
    for page in pages:
        for row in _page_raw_orders(page):
            if (
                "order_id" in row
                and "occurred" in row
                and isinstance(row.get("tickets"), list)
            ):
                parsed = row
            else:
                parsed = parse_order(row)
            key = parsed.get("order_id") or parsed.get("sequence_no")
            if not key or key in output:
                continue
            output[str(key)] = parsed
    return list(output.values())


def date_range(orders: Iterable[Dict]) -> Optional[Tuple[str, str]]:
    dates = [order.get("occurred") for order in orders if order.get("occurred")]
    if not dates:
        return None
    return min(dates), max(dates)


def _page_data(payload) -> Dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def safe_body_preview(text: str, limit: int = 200) -> str:
    """A short, tag-stripped body excerpt for diagnostics (never cookies)."""

    if not text:
        return ""
    cleaned = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", text)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).lstrip("\ufeff").strip()
    # Defensive: a server should not reflect cookies, but never echo one.
    if any(token in cleaned for token in ("JSESSIONID=", "tk=", "uamtk=")):
        return ""
    return cleaned[:limit]


def _safe_error_text(exc: Exception) -> str:
    """Short, cookie-free description of a failure for the state file."""

    text = getattr(exc, "message", None) or str(exc)
    text = " ".join(str(text).split())
    if any(token in text for token in ("JSESSIONID=", "tk=", "uamtk=", "Cookie:")):
        return type(exc).__name__
    return text[:200]


class _RequestBudgetExhausted(Exception):
    """Internal: the per-run request budget is used up (not a server error)."""


def decode_rail_json(response: Response, text: str) -> Dict:
    """Decode one 12306 JSON reply, failing closed on HTML/risk/login pages.

    ``error.html`` starts with a UTF-8 BOM before ``<html>``; it is stripped so
    an HTML body is recognized as HTML rather than "non-JSON".  Recognition is
    delegated to :mod:`connectors.rail12306.blocking` so the typed errors stay shared
    between the order and invoice readers.
    """

    stripped = text.lstrip("\ufeff \t\r\n")
    preview = safe_body_preview(text)
    detail = f"；响应摘要: {preview}" if preview else ""
    if stripped.startswith("<"):
        check_rail12306_html(text, response.url, response.status)
        raise ApiError(
            f"12306 返回了 HTML 而不是 JSON（页面结构可能已变化）{detail}"
        )
    try:
        payload = json.loads(text)
    except ValueError as exc:
        check_rail12306_html(text, response.url, response.status)
        raise ApiError(f"12306 返回了非 JSON 内容{detail}") from exc
    if not isinstance(payload, dict):
        check_rail12306_html(text, response.url, response.status)
        raise ApiError(f"12306 返回了意外的 JSON 类型{detail}")
    check_rail12306_json(payload, response.status)
    return payload


def probe_session(
    client,
    jar: CookieJar,
    *,
    clock=time.monotonic,
) -> Dict:
    """One read-only GET of the order page: is the ``tk`` session still alive?

    Returns ``{logged_in, http_status, redirected_to_passport, elapsed_ms}``
    plus a ``reason`` when not logged in.  No cookie value is ever returned
    or printed.  The browser cookie database is never written.

    A missing ``tk`` short-circuits without any network request.  Transport
    failures raise :class:`NetworkError` (a keepalive scheduler must be able
    to tell "network down" from "please log in again"); HTTP/auth/risk
    outcomes are reported in the returned dict instead of raising.
    """

    started = clock()
    info: Dict = {
        "logged_in": False,
        "http_status": 0,
        "redirected_to_passport": False,
        "elapsed_ms": 0,
        "reason": None,
    }
    missing = missing_login_cookies(jar)
    if missing:
        info["reason"] = "missing_tk"
        info["missing"] = missing
        info["elapsed_ms"] = int(round((clock() - started) * 1000))
        return info

    response = client.get(
        RAIL_ORDER_PAGE_URL,
        cookie_header=jar.header_for(RAIL_HOST),
        headers={
            "Referer": RAIL_TRAIN_ORDER_PAGE,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    )
    refresh_cookies_from_response(jar, response)
    info["http_status"] = response.status
    final_url = (response.url or "").lower()
    if "passport" in final_url or "login.html" in final_url:
        info["redirected_to_passport"] = True
        info["reason"] = "redirected_to_login"
    elif "error.html" in final_url:
        info["reason"] = "error_page"
    else:
        try:
            check_rail12306_html(response.text, response.url, response.status)
        except AuthRequired:
            info["reason"] = "login_page"
        except RiskBlocked:
            info["reason"] = "risk_verification"
        except ApiError:
            info["reason"] = "error_page"
        else:
            if response.status == 200:
                info["logged_in"] = True
            else:
                info["reason"] = f"http_{response.status}"
    info["elapsed_ms"] = int(round((clock() - started) * 1000))
    return info


class Rail12306Fetcher:
    """Fetch historical orders incrementally, mirroring the page's pagination.

    The constructor signature is backwards compatible: every new option is
    keyword-only with a default, and ``incremental=True`` on a fresh state
    file behaves exactly like the old "query the clamped range" mode (the
    clamp range *is* the whole gap on a first run).

    Politeness/limits (all configurable, production defaults in brackets):

    * ``min_interval`` [2.0s] between HTTP requests;
    * ``max_requests`` [12] per run — leftovers become gaps, not retries;
    * the first ``error.html`` / non-JSON / ``validateMessages`` / risk reply
      stops the run immediately (no retry), and the failed window keeps its
      days as gaps with a backoff before the next attempt.
    """

    def __init__(
        self,
        client: HttpClient,
        jar: CookieJar,
        *,
        max_pages: int = RAIL_MAX_PAGES,
        page_size: int = RAIL_PAGE_SIZE,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        query_where: str = RAIL_QUERY_WHERE,
        query_type: str = RAIL_QUERY_TYPE,
        train_name: str = "",
        now: Optional[datetime.datetime] = None,
        max_span_days: int = RAIL_MAX_SPAN_DAYS,
        retention_days: int = RAIL_RETENTION_DAYS,
        incremental: bool = True,
        state_path: Optional[object] = None,
        state_dir: Optional[object] = None,
        min_interval: float = RAIL_MIN_REQUEST_INTERVAL,
        max_requests: Optional[int] = RAIL_MAX_REQUESTS,
        error_backoff_seconds: int = RAIL_ERROR_BACKOFF_SECONDS,
        throttle_backoff_seconds: int = RAIL_THROTTLE_BACKOFF_SECONDS,
        clock=None,
        sleep=None,
        force_refresh: bool = False,
    ):
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        if max_requests is not None and max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if error_backoff_seconds < 0 or throttle_backoff_seconds < 0:
            raise ValueError("backoff seconds must be >= 0")
        self.client = client
        self.jar = jar
        self.max_pages = max_pages
        self.page_size = page_size
        self.query_where = query_where
        self.query_type = query_type
        self.train_name = train_name

        moment = now or datetime.datetime.now(BEIJING)
        self.now = moment
        requested_start, requested_end = resolve_date_range(
            start_date, end_date, now=moment
        )
        self.plan = plan_query_windows(
            requested_start,
            requested_end,
            now=moment,
            max_span_days=max_span_days,
            retention_days=retention_days,
        )
        self.requested_start_date = requested_start
        self.requested_end_date = requested_end
        self.start_date = self.plan["start_date"] or requested_start
        self.end_date = self.plan["end_date"] or requested_end
        self.windows: List[Dict[str, str]] = [
            dict(window) for window in self.plan["windows"]
        ]
        self.pages: List[Dict] = []
        self.window_results: List[Dict] = []
        self.warnings: List[str] = list(self.plan["warnings"])

        self.incremental = bool(incremental)
        if state_path is not None:
            self.state_path = Path(state_path)
        elif state_dir is not None:
            self.state_path = Path(state_dir) / RAIL_STATE_FILENAME
        else:
            self.state_path = default_state_path()
        self.min_interval = float(min_interval)
        self.max_requests = max_requests
        self.error_backoff_seconds = int(error_backoff_seconds)
        self.throttle_backoff_seconds = int(throttle_backoff_seconds)
        self.force_refresh = bool(force_refresh)
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep

        self.requests_sent = 0
        self.elapsed_ms = 0
        self.inc_plan: Optional[Dict] = None
        self._state: Dict = empty_state()
        self._window_status: List[Dict] = []
        self._last_request_at: Optional[float] = None
        self._last_response: Optional[Response] = None
        self._last_body_preview = ""
        self._state_dirty = False
        self._budget_exhausted = False
        self._failure: Optional[Dict] = None

    # -- wiring ---------------------------------------------------------
    def _cookie_header(self) -> str:
        return self.jar.header_for(RAIL_HOST)

    def _remember(self, response: Response) -> None:
        refresh_cookies_from_response(self.jar, response)

    def _add_warning(self, text: str) -> None:
        if text and text not in self.warnings:
            self.warnings.append(text)

    def _budget_left(self) -> int:
        if self.max_requests is None:
            return 1 << 30
        return max(0, self.max_requests - self.requests_sent)

    def _pace(self) -> None:
        """Space out requests (>= ``min_interval``), injectable for tests."""

        moment = self.clock()
        if self.min_interval > 0 and self._last_request_at is not None:
            remaining = self.min_interval - (moment - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
                moment = self.clock()
        self._last_request_at = moment

    def _request_page(
        self, page_index: int, start_date: str, end_date: str
    ) -> Tuple[Response, Dict, str]:
        if self._budget_left() <= 0:
            raise _RequestBudgetExhausted()
        self._pace()
        self.requests_sent += 1
        form = build_order_form(
            page_index,
            start_date,
            end_date,
            page_size=self.page_size,
            query_where=self.query_where,
            query_type=self.query_type,
            train_name=self.train_name,
        )
        response = self.client.post_form(
            RAIL_ORDER_ENDPOINT,
            form,
            cookie_header=self._cookie_header(),
            headers={
                "Referer": RAIL_REFERER,
                "Origin": RAIL_ORIGIN,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        self._last_response = response
        self._last_body_preview = safe_body_preview(response.text)
        self._remember(response)
        text = response.text
        payload = self._decode(response, text)
        return response, payload, text

    def _decode(self, response: Response, text: str) -> Dict:
        return decode_rail_json(response, text)

    # -- incremental state ----------------------------------------------
    def _load_state(self) -> Tuple[Dict, List[str]]:
        if not self.incremental:
            self._state = empty_state()
            return self._state, []
        state, warnings = load_state(self.state_path)
        self._state = state
        return state, warnings

    def _plan_for_run(self, state: Dict) -> Dict:
        if not self.incremental:
            windows = []
            for window in self.plan["windows"]:
                start = parse_date(window["start_date"])
                end = parse_date(window["end_date"])
                windows.append(
                    {
                        "start_date": window["start_date"],
                        "end_date": window["end_date"],
                        "days": (end - start).days + 1,
                    }
                )
            return {
                "requested_range": None,
                "target_start_date": self.start_date,
                "target_end_date": self.end_date,
                "oldest_allowed": self.plan["oldest_allowed"],
                "newest_allowed": self.plan["newest_allowed"],
                "retention_days": self.plan["retention_days"],
                "max_span_days": self.plan["max_span_days"],
                "force": False,
                "windows": windows,
                "deferred": [],
                "warnings": [],
                "covered_days": 0,
                "target_days": 0,
                "gap_days": 0,
                "ready_days": len(windows),
                "deferred_days": 0,
            }
        return plan_incremental_windows(
            state,
            self.now.date(),
            (self.start_date, self.end_date),
            retention_days=self.plan["retention_days"],
            max_span_days=self.plan["max_span_days"],
            now=self.now,
            force=self.force_refresh,
        )

    def _prune_state(self, state: Dict) -> None:
        oldest = _parse_day(self.plan.get("oldest_allowed"))
        if oldest is not None:
            state["covered_days"] = [
                day
                for day in state.get("covered_days") or []
                if (_parse_day(day) or oldest) >= oldest
            ]
            state["windows"] = [
                record
                for record in state.get("windows") or []
                if (_parse_day(record.get("end_date")) or oldest) >= oldest
            ]
        state["known_order_ids"] = (
            state.get("known_order_ids") or []
        )[-RAIL_MAX_KNOWN_ORDER_IDS:]

    def _persist(self, state: Dict, deferred: Optional[List[Dict]] = None) -> None:
        if not self.incremental:
            return
        state["version"] = RAIL_STATE_VERSION
        state["updated_at"] = _iso_now(self.now)
        self._prune_state(state)
        state["last_run"] = {
            "at": _iso_now(self.now),
            "requests": self.requests_sent,
            "windows_planned": len(self.windows),
            "windows_fetched": len(self.window_results),
            "windows_deferred": len(deferred or []),
            "elapsed_ms": self.elapsed_ms,
            "budget_exhausted": self._budget_exhausted,
        }
        try:
            save_state(state, self.state_path)
        except OSError as exc:
            self._add_warning(
                f"归档状态写入失败（{type(exc).__name__}）；"
                f"本次进度未保存，下次运行会重新查询该窗口"
            )

    @staticmethod
    def _state_window(state: Dict, key: str) -> Optional[Dict]:
        for record in state.get("windows") or []:
            if _window_key(record.get("start_date"), record.get("end_date")) == key:
                return record
        return None

    def _upsert_state_window(self, state: Dict, record: Dict) -> None:
        key = _window_key(record["start_date"], record["end_date"])
        state["windows"] = [
            existing
            for existing in state.get("windows") or []
            if _window_key(existing.get("start_date"), existing.get("end_date")) != key
        ]
        state["windows"].append(record)
        state["windows"].sort(key=lambda item: item.get("start_date") or "")

    def _remember_order_ids(self, state: Dict, order_ids: Iterable[str]) -> None:
        known = list(state.get("known_order_ids") or [])
        seen = set(known)
        for order_id in order_ids:
            text = _text(order_id)
            if text and text not in seen:
                seen.add(text)
                known.append(text)
        state["known_order_ids"] = known[-RAIL_MAX_KNOWN_ORDER_IDS:]

    def _record_success(
        self, state: Dict, index: int, window: Dict[str, str], outcome: Dict
    ) -> Tuple[Dict, Dict]:
        start_date = window["start_date"]
        end_date = window["end_date"]
        status = "incomplete" if outcome["incomplete_reason"] else "ok"
        key = _window_key(start_date, end_date)
        previous = self._state_window(state, key)
        record = {
            "start_date": start_date,
            "end_date": end_date,
            "fetched_at": _iso_now(self.now),
            "status": status,
            "pages": outcome["pages"],
            "orders": outcome["orders"],
            "order_total_number": outcome["order_total_number"],
            "attempts": (previous.get("attempts") if previous else 0) or 0,
            "retry_after": None,
            "reason": outcome["incomplete_reason"],
            "error": None,
        }
        record["attempts"] += 1
        self._upsert_state_window(state, record)
        if status == "ok":
            covered = set(state.get("covered_days") or [])
            for day in _iter_days(parse_date(start_date), parse_date(end_date)):
                covered.add(day.isoformat())
            state["covered_days"] = sorted(covered)
        self._remember_order_ids(state, outcome.get("order_ids") or [])
        self._state_dirty = True
        self._window_status.append(
            {
                "start_date": start_date,
                "end_date": end_date,
                "status": status,
                "pages": outcome["pages"],
                "orders": outcome["orders"],
                "retry_after": None,
                "reason": outcome["incomplete_reason"],
            }
        )
        public = {
            "window": index + 1,
            "start_date": start_date,
            "end_date": end_date,
            "pages": outcome["pages"],
            "orders": outcome["orders"],
            "order_total_number": outcome["order_total_number"],
        }
        return public, record

    def _classify_failure(self, exc: Exception) -> Tuple[str, str, int]:
        """Map an exception to (state status, reason, backoff seconds).

        An ``error.html`` redirect is reported as server throttling rather
        than a date-range problem: this code never constructs an
        out-of-range window, so a range complaint would be a planner bug and
        is labelled separately.
        """

        message = getattr(exc, "message", None) or str(exc)
        last_url = (getattr(self._last_response, "url", "") or "").lower()
        # The server's own error page body is the only reliable signal; our
        # ApiError text enumerates several possible causes and must not be
        # used to classify (it always mentions "日期越界").
        preview = self._last_body_preview or ""
        if isinstance(exc, NeedsRiskSignature):
            return "risk", "risk_signature", RAIL_RISK_BACKOFF_SECONDS
        if isinstance(exc, AuthRequired):
            return "auth", "logged_out", 0
        if isinstance(exc, NetworkError):
            return "network", "network_error", 0
        if "error.html" in last_url or "错误页" in message:
            if any(
                marker in preview
                for marker in ("频繁", "稍后", "过多", "节流", "重试", "网络")
            ):
                return "throttled", "server_throttle", self.throttle_backoff_seconds
            if any(
                marker in preview
                for marker in ("日期", "范围", "超出", "越界", "区间")
            ):
                return "out_of_range", "date_range_rejected", RAIL_OUT_OF_RANGE_BACKOFF_SECONDS
            return "error_page", "server_error_page", self.error_backoff_seconds
        if isinstance(exc, ApiError):
            return "error_page", "unexpected_response", self.error_backoff_seconds
        if isinstance(exc, RiskBlocked):
            return "risk", "risk_verification", RAIL_RISK_BACKOFF_SECONDS
        return "error", type(exc).__name__, self.error_backoff_seconds

    _FAILURE_LABELS = {
        "throttled": "服务端错误页/疑似节流（并非日期越界：本工具只构造合法窗口）",
        "out_of_range": "日期越界（不应发生：本工具只构造合法窗口）",
        "error_page": "服务端错误页/响应异常",
        "risk": "风控/签名要求",
        "auth": "登录态失效",
        "network": "网络错误",
        "error": "未预期的失败",
    }

    def _record_failure(
        self, state: Dict, index: int, window: Dict[str, str], exc: Exception
    ) -> Tuple[Dict, Dict]:
        start_date = window["start_date"]
        end_date = window["end_date"]
        status, reason, backoff = self._classify_failure(exc)
        key = _window_key(start_date, end_date)
        previous = self._state_window(state, key)
        pages = sum(1 for page in self.pages if page.get("window") == index + 1)
        orders = sum(
            len(page.get("orders") or [])
            for page in self.pages
            if page.get("window") == index + 1
        )
        retry_after = (
            _iso_now(self.now + datetime.timedelta(seconds=backoff))
            if backoff
            else None
        )
        record = {
            "start_date": start_date,
            "end_date": end_date,
            "fetched_at": _iso_now(self.now),
            "status": status,
            "pages": pages,
            "orders": orders,
            "order_total_number": None,
            "attempts": ((previous.get("attempts") if previous else 0) or 0) + 1,
            "retry_after": retry_after,
            "reason": reason,
            "error": _safe_error_text(exc),
        }
        self._upsert_state_window(state, record)
        self._state_dirty = True
        self._window_status.append(
            {
                "start_date": start_date,
                "end_date": end_date,
                "status": status,
                "pages": pages,
                "orders": orders,
                "retry_after": retry_after,
                "reason": reason,
            }
        )
        if retry_after:
            hint = f"已记录缺口，{retry_after} 之前不会重试"
        else:
            hint = "已记录缺口，下次运行会重试"
        self._add_warning(
            f"窗口 {start_date}..{end_date} 查询失败"
            f"（{self._FAILURE_LABELS.get(status, status)}）；{hint}；"
            f"本次立即停止且不重试"
        )
        detail = {
            "status": status,
            "reason": reason,
            "retry_after": retry_after,
            "window": f"{start_date}..{end_date}",
        }
        public = {
            "window": index + 1,
            "start_date": start_date,
            "end_date": end_date,
            "pages": pages,
            "orders": orders,
            "order_total_number": None,
        }
        return public, detail

    # -- one window -----------------------------------------------------
    def _run_window(self, window_index: int, window: Dict[str, str]) -> Dict:
        start_date = window["start_date"]
        end_date = window["end_date"]
        total: Optional[int] = None
        collected = 0
        page_index = 0
        window_pages = 0
        incomplete_reason: Optional[str] = None
        order_ids: List[str] = []
        while page_index < self.max_pages:
            try:
                response, payload, text = self._request_page(
                    page_index, start_date, end_date
                )
            except _RequestBudgetExhausted:
                incomplete_reason = "budget"
                break
            data = _page_data(payload)
            rows = data.get("OrderDTODataList") or []
            if not isinstance(rows, list):
                rows = []
            if total is None:
                total = _int(data.get("order_total_number"))
            parsed = [parse_order(row) for row in rows if isinstance(row, dict)]
            for order in parsed:
                key = order.get("order_id") or order.get("sequence_no")
                if key:
                    order_ids.append(str(key))
            collected += len(rows)
            self.pages.append(
                {
                    "page": len(self.pages) + 1,
                    "window": window_index + 1,
                    "window_page": window_pages + 1,
                    "page_index": page_index,
                    "query_start_date": start_date,
                    "query_end_date": end_date,
                    "status": response.status,
                    "order_total_number": total,
                    "orders": parsed,
                    "raw": text,
                }
            )
            window_pages += 1
            page_index += 1
            if not rows:
                if total is not None and collected < total:
                    # The server claims more orders than it returned: treat
                    # the window as incomplete so it stays a gap.
                    incomplete_reason = "empty_page_before_total"
                break
            if total is not None and collected >= total:
                break
            if total is None and len(rows) < self.page_size:
                break
        else:
            incomplete_reason = "max_pages"
            self._add_warning(
                f"窗口 {start_date}..{end_date} 已达到单次最大页数 "
                f"{self.max_pages}，12306 订单可能尚未取完"
            )
        return {
            "pages": window_pages,
            "orders": collected,
            "order_total_number": total,
            "incomplete_reason": incomplete_reason,
            "order_ids": order_ids,
        }

    @staticmethod
    def _defer_windows(
        windows: Iterable[Dict[str, str]], reason: str
    ) -> List[Dict]:
        return [
            {
                "start_date": window["start_date"],
                "end_date": window["end_date"],
                "reason": reason,
                "retry_after": None,
            }
            for window in windows
        ]

    # -- public ---------------------------------------------------------
    def fetch(self) -> Dict:
        require_login(self.jar)
        started = self.clock()
        self._last_request_at = None
        self.requests_sent = 0
        self._budget_exhausted = False
        self._failure = None
        state, state_warnings = self._load_state()
        for text in state_warnings:
            self._add_warning(text)
        plan = self._plan_for_run(state)
        self.inc_plan = plan
        self.windows = [
            {"start_date": window["start_date"], "end_date": window["end_date"]}
            for window in plan["windows"]
        ]
        for text in plan["warnings"]:
            self._add_warning(text)
        deferred: List[Dict] = list(plan["deferred"])

        index = 0
        while index < len(self.windows):
            window = self.windows[index]
            if self._budget_left() <= 0:
                deferred.extend(self._defer_windows(self.windows[index:], "budget"))
                self._budget_exhausted = True
                self._add_warning(
                    f"本次运行已达到 {self.max_requests} 个请求上限；"
                    f"剩余 {len(self.windows) - index} 个窗口推迟到下次"
                )
                break
            try:
                outcome = self._run_window(index, window)
            except ConnectorError as exc:
                public, detail = self._record_failure(state, index, window, exc)
                self.window_results.append(public)
                self._failure = detail
                deferred.extend(
                    self._defer_windows(self.windows[index + 1 :], "aborted")
                )
                self._persist(state, deferred)
                raise
            public, _record = self._record_success(state, index, window, outcome)
            self.window_results.append(public)
            if outcome["incomplete_reason"]:
                self._add_warning(
                    f"窗口 {window['start_date']}..{window['end_date']} 未取完"
                    f"（{outcome['incomplete_reason']}）；已保留为缺口，下次继续"
                )
            self._persist(state, deferred)
            if outcome["incomplete_reason"] == "budget":
                self._budget_exhausted = True
                deferred.extend(
                    self._defer_windows(self.windows[index + 1 :], "budget")
                )
                self._add_warning(
                    f"本次运行已达到 {self.max_requests} 个请求上限；"
                    f"剩余 {len(self.windows) - index - 1} 个窗口推迟到下次"
                )
                break
            index += 1

        self.elapsed_ms = int(round((self.clock() - started) * 1000))
        if self._state_dirty:
            self._persist(state, deferred)
        coverage = (
            coverage_summary(
                state, plan.get("target_start_date"), plan.get("target_end_date")
            )
            if self.incremental
            else coverage_summary({}, None, None)
        )

        return {
            "kind": "rail_12306",
            "source": RAIL_ORDER_ENDPOINT,
            "query": {
                "query_where": self.query_where,
                "query_type": self.query_type,
                "start_date": self.start_date,
                "end_date": self.end_date,
                "requested_start_date": self.requested_start_date,
                "requested_end_date": self.requested_end_date,
                "oldest_allowed": self.plan["oldest_allowed"],
                "newest_allowed": self.plan["newest_allowed"],
                "max_span_days": self.plan["max_span_days"],
                "retention_days": self.plan["retention_days"],
                "page_size": self.page_size,
                "come_from_flag": RAIL_COME_FROM_FLAG,
                "windows": len(self.windows),
                "queried": bool(self.windows),
                "incremental": self.incremental,
                "state_file": str(self.state_path) if self.incremental else None,
                "requests_sent": self.requests_sent,
                "windows_planned": len(self.windows),
                "windows_fetched": len(self.window_results),
                "windows_deferred": len(deferred),
                "coverage": coverage,
                "elapsed_ms": self.elapsed_ms,
                "budget_exhausted": self._budget_exhausted,
                "force_refresh": self.force_refresh,
                "deferred_windows": deferred,
                "window_status": list(self._window_status),
                "failure": self._failure,
            },
            "windows": self.window_results,
            "pages": self.pages,
        }

    def probe(self) -> Dict:
        """One read-only GET of the login-gated order page (status/keepalive)."""

        require_login(self.jar)
        response = self.client.get(
            RAIL_ORDER_PAGE_URL,
            cookie_header=self._cookie_header(),
            headers={
                "Referer": RAIL_TRAIN_ORDER_PAGE,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        )
        self._remember(response)
        check_rail12306_html(response.text, response.url, response.status)
        if response.status >= 400:
            raise ApiError(f"12306 订单页返回 HTTP {response.status}")
        return {
            "status": response.status,
            "url": response.url,
            "logged_in": True,
            "orders": 0,
        }

    def probe_session(self) -> Dict:
        """Non-raising keepalive probe; see module-level :func:`probe_session`."""

        return probe_session(self.client, self.jar, clock=self.clock)


# ---------------------------------------------------------------------------
# electronic invoice (电子发票): 180-day read-only lookback
# ---------------------------------------------------------------------------
#
# Why this exists: ``queryMyOrder`` only serves 30 days.  The e-invoice list
# page queries by *boarding date* over 今天-179 .. 今天+14 and returns
# ``sequence_no`` / ``local_start_time`` / ``sale_price`` / ``board_train_code``
# per row, which is the only public read-only source of "amount + date" beyond
# the 30-day order horizon.
#
# The endpoint is gated twice: a logged-in session (``login/conf`` ->
# ``is_login``) and an account-level "铁路12306 APP 授权查询" flag
# (``psr_qr_code_result`` / the server's own 授权 message).  This module never
# tries to obtain, refresh or bypass that authorization: it fails closed with a
# Chinese hint telling the owner to do it once, manually, in the APP.


def invoice_date_window(
    today,
    start=None,
    end=None,
    *,
    lookback_days: int = RAIL_INVOICE_LOOKBACK_DAYS,
    floor: datetime.date = RAIL_INVOICE_DATE_FLOOR,
) -> Dict:
    """Clamp a requested boarding-date range to the e-invoice 180-day window.

    The page's picker is ``今天-179 .. 今天+14`` (clamped by ``r()`` to the
    2024-11-01 policy start).  We only ever ask for trips that already
    happened, so the newest legal date here is **today**, never a future one.
    A range with no overlap sends no request at all (there is no value in
    probing the boundary; history shows out-of-range probes get throttled).
    """

    if lookback_days < 0:
        raise ValueError("lookback_days must be >= 0")
    today_day = _as_day(today)
    oldest = max(today_day - datetime.timedelta(days=lookback_days), floor)
    newest = today_day
    requested_start = _as_day(start) if start is not None else oldest
    requested_end = _as_day(end) if end is not None else newest
    if requested_start > requested_end:
        raise UsageError("--start-date 不能晚于 --end-date")

    warnings: List[str] = []
    clipped_start = max(requested_start, oldest)
    clipped_end = min(requested_end, newest)
    if requested_start < oldest:
        warnings.append(
            f"起始日期 {requested_start} 早于 12306 电子发票可查下限 {oldest}"
            f"（今天-{lookback_days}，且不早于 {floor}）；已截断为 {oldest}"
        )
    if requested_end > newest:
        warnings.append(
            f"结束日期 {requested_end} 晚于今天 {newest}；已截断为 {newest}"
            f"（未来乘车日期的待开票记录不在回溯范围）"
        )

    window: Dict = {
        "start_date": None,
        "end_date": None,
        "queried": False,
        "days": 0,
        "requested_start_date": requested_start.isoformat(),
        "requested_end_date": requested_end.isoformat(),
        "oldest_allowed": oldest.isoformat(),
        "newest_allowed": newest.isoformat(),
        "lookback_days": lookback_days,
        "warnings": warnings,
    }
    if clipped_start > clipped_end:
        warnings.append(
            f"请求区间 {requested_start}..{requested_end} 与 12306 电子发票可查"
            f"区间 {oldest}..{newest} 没有交集；未发送查询。"
        )
        return window
    window.update(
        {
            "start_date": clipped_start.isoformat(),
            "end_date": clipped_end.isoformat(),
            "queried": True,
            "days": (clipped_end - clipped_start).days + 1,
        }
    )
    return window


def build_invoice_form(
    start_date,
    end_date,
    page_index: int = RAIL_INVOICE_PAGE_INDEX_BASE,
    *,
    invoice_type: str = RAIL_INVOICE_TYPE_PENDING,
    order_num: str = "",
    ticket_type: str = "",
) -> Dict[str, str]:
    """The exact jQuery ``data`` object the invoice list page sends.

    ``from_date``/``end_date`` are ``yyyyMMdd``; ``pageIndex`` starts at 1 (the
    order page's is zero-based).  There is no ``pageSize`` and no signature.
    """

    return {
        "from_date": _as_day(start_date).strftime("%Y%m%d"),
        "end_date": _as_day(end_date).strftime("%Y%m%d"),
        "order_num": order_num or "",
        "pageIndex": str(int(page_index)),
        "ticket_type": ticket_type or "",
        "type": str(invoice_type),
    }


def _payload_invoice_records(payload) -> List[Dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    results = data.get("results") or []
    return [row for row in results if isinstance(row, dict)]


def _page_invoice_records(page) -> List[Dict]:
    if not isinstance(page, dict):
        return []
    records = page.get("records")
    if isinstance(records, list):
        return [row for row in records if isinstance(row, dict)]
    raw = page.get("raw")
    if isinstance(raw, str):
        try:
            return _payload_invoice_records(json.loads(raw))
        except ValueError:
            return []
    return _payload_invoice_records(page)


def _invoice_record_key(record: Dict) -> str:
    parts = [
        _text(record.get("sequence_no")),
        _text(record.get("local_start_time")),
        _text(record.get("board_train_code")),
        _text(record.get("sale_price")),
        _text(record.get("create_time")),
    ]
    key = "|".join(part or "" for part in parts)
    return key or json.dumps(record, ensure_ascii=False, sort_keys=True)


def parse_invoice_records(pages: Iterable) -> List[Dict]:
    """Unique invoice rows across pages (dedupe by order/day/train/price)."""

    output: List[Dict] = []
    seen = set()
    for page in pages:
        for record in _page_invoice_records(page):
            key = _invoice_record_key(record)
            if key in seen:
                continue
            seen.add(key)
            output.append(record)
    return output


def _invoice_datetime(value) -> Optional[str]:
    """Keep ``rows.normalize_occurred`` from reading ``20260430`` as an epoch."""

    text = _text(value)
    if not text:
        return None
    compact = text.strip()
    if compact.isdigit() and len(compact) == 8:
        return f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]}"
    if compact.isdigit() and len(compact) == 14:
        return (
            f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]} "
            f"{compact[8:10]}:{compact[10:12]}:{compact[12:14]}"
        )
    return text


def _invoice_occurred(record: Dict) -> Optional[str]:
    """Boarding day: ``local_start_time``, then ``start_time``, then ``create_time``.

    ``start_time`` is often a bare ``HH:MM`` clock time; such a value is kept
    only if the other two fields are missing (and then it simply yields
    ``occurred=None`` rather than a wrong date).
    """

    for key in ("local_start_time", "start_time", "create_time"):
        occurred = _invoice_datetime(record.get(key))
        if not occurred:
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}", occurred):
            return occurred
        if occurred.isdigit() and len(occurred) in (10, 13):
            return occurred  # epoch seconds/milliseconds
    return None


def _invoice_amount(record: Dict) -> Tuple[Optional[str], Optional[str]]:
    """Ticket amount plus which field it came from (annotated in ``extra``)."""

    for key in ("sale_price", "return_serial_price", "resign_serial_price"):
        text = _text(record.get(key))
        if text and re.search(r"\d", text):
            return text, key
    return None, None


def _invoice_title(record: Dict) -> Optional[str]:
    train = _text(record.get("board_train_code"))
    origin = _text(record.get("from_station_name"))
    destination = _text(record.get("to_station_name"))
    route = (
        f"{origin}→{destination}"
        if origin and destination
        else (origin or destination)
    )
    parts = [part for part in (train, route) if part]
    if parts:
        return " ".join(parts)
    sequence = _text(record.get("sequence_no"))
    return f"12306 行程 {sequence}" if sequence else "12306 行程"


def _invoice_identity_fields(record: Dict) -> Tuple:
    amount, _source = _invoice_amount(record)
    return (
        _text(record.get("sequence_no")),
        _invoice_occurred(record),
        _text(record.get("board_train_code")),
        _text(record.get("from_station_name")),
        _text(record.get("to_station_name")),
        _text(record.get("passenger_name")),
        amount,
    )


def invoice_rows(payload: Dict) -> List[Dict]:
    """Normalized rows for ``the ledger``.

    Shape matches ``connectors/kit/rows.py`` exactly (``source, record_type, id,
    occurred, amount, direction, merchant, title, status, extra``) so the
    ledger can ingest them without a special case: one ``trip`` row per e-invoice
    ticket, ``direction="out"`` (a bought ticket), ``amount`` from
    ``sale_price`` with ``return_serial_price``/``resign_serial_price`` as an
    annotated fallback.

    Ids: ``12306:invoice:<sequence_no>``; when the server omits the order
    number a content hash is used instead.  If one payload contains *several*
    ticks for the same ``sequence_no`` (a multi-passenger or multi-leg order —
    the server shape is not yet observable, see the evidence note), a stable
    content-hash suffix keeps every ticket as its own row instead of silently
    collapsing them.  ``extra["order_row_id"]`` carries the id the 30-day order
    reader would use (``12306:trip:<sequence_no>``) so a consumer can detect the
    same trip arriving from both readers.
    """

    # Imported lazily: rows.py imports this module at import time.
    from ..kit.rows import canonical, make_record, stable_id

    payload = payload if isinstance(payload, dict) else {}
    query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    invoice_type = _text(query.get("type"))
    records = parse_invoice_records(payload.get("pages") or [])
    sequence_counts: Dict[str, int] = {}
    for record in records:
        sequence = _text(record.get("sequence_no"))
        if sequence:
            sequence_counts[sequence] = sequence_counts.get(sequence, 0) + 1

    rows: List[Dict] = []
    seen = set()
    for record in records:
        amount, amount_source = _invoice_amount(record)
        occurred = _invoice_occurred(record)
        train = _text(record.get("board_train_code"))
        origin = _text(record.get("from_station_name"))
        destination = _text(record.get("to_station_name"))
        sequence = _text(record.get("sequence_no"))
        identity = _invoice_identity_fields(record)
        row_id = stable_id("12306", "invoice", sequence, *identity)
        if sequence and sequence_counts.get(sequence, 0) > 1:
            digest = hashlib.sha1(
                canonical(identity).encode("utf-8")
            ).hexdigest()[:8]
            row_id = f"12306:invoice:{sequence}:{digest}"
        if row_id in seen:
            continue
        seen.add(row_id)
        rows.append(
            make_record(
                source="12306",
                record_type="trip",
                id=row_id,
                occurred=occurred,
                amount=amount,
                direction="out",
                merchant="中国铁路 12306",
                title=_invoice_title(record),
                status=_text(record.get("einvoice_status"))
                or _text(record.get("invoice_state")),
                extra={
                    "invoice_type": invoice_type,
                    "sequence_no": sequence,
                    "order_row_id": f"12306:trip:{sequence}" if sequence else None,
                    "create_time": _text(record.get("create_time")),
                    "board_train_code": train,
                    "from_station_name": origin,
                    "to_station_name": destination,
                    "local_start_time": _text(record.get("local_start_time")),
                    "start_time": _text(record.get("start_time")),
                    "sale_price": _text(record.get("sale_price")),
                    "return_serial_price": _text(record.get("return_serial_price")),
                    "resign_serial_price": _text(record.get("resign_serial_price")),
                    "amount_source": amount_source or "none",
                    "einvoice_status": _text(record.get("einvoice_status")),
                    "invoice_state": _text(record.get("invoice_state")),
                    "passenger_name": _text(record.get("passenger_name")),
                },
            )
        )
    rows.sort(key=lambda row: (row.get("occurred") or "", row["id"]))
    return rows


class RailInvoiceFetcher:
    """Read-only reader for the 12306 e-invoice list (180-day lookback).

    Only two endpoints are ever touched, both called by the public invoice
    pages themselves:

    * ``POST /otn/login/conf`` (gate: ``is_login`` / ``psr_qr_code_result``)
    * ``POST /otn/eInvoice/queryPsr`` (list query, ``type=2`` 待开票 / ``1`` 已开票)

    Every issue / red-flush / download / e-mail endpoint of the same bundle is
    listed in :data:`RAIL_INVOICE_FORBIDDEN_ENDPOINTS` and is never called.
    There is no local state: the whole 180-day window is re-queried on each run
    (that is the natural ``--refresh`` default for a read-only list), bounded by
    ``max_requests`` and ``min_interval``.
    """

    def __init__(
        self,
        client,
        jar: CookieJar,
        *,
        invoice_type: str = RAIL_INVOICE_TYPE_PENDING,
        now: Optional[datetime.datetime] = None,
        min_interval: float = RAIL_INVOICE_MIN_INTERVAL,
        max_requests: int = RAIL_INVOICE_MAX_REQUESTS,
        page_size: int = RAIL_INVOICE_PAGE_SIZE,
        max_pages: Optional[int] = None,
        lookback_days: int = RAIL_INVOICE_LOOKBACK_DAYS,
        floor: datetime.date = RAIL_INVOICE_DATE_FLOOR,
        clock=None,
        sleep=None,
    ):
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        self.client = client
        self.jar = jar
        self.invoice_type = str(invoice_type)
        self.moment = now or datetime.datetime.now(BEIJING)
        self.min_interval = float(min_interval)
        self.max_requests = int(max_requests)
        self.page_size = int(page_size)
        self.max_pages = max_pages
        self.lookback_days = int(lookback_days)
        self.floor = floor if isinstance(floor, datetime.date) else _as_day(floor)
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep

        self.requests_sent = 0
        self.pages: List[Dict] = []
        self.warnings: List[str] = []
        self.gate: Optional[Dict] = None
        self.window: Optional[Dict] = None
        self._last_request_at: Optional[float] = None
        self._last_response: Optional[Response] = None

    # -- wiring ---------------------------------------------------------
    def _cookie_header(self) -> str:
        return self.jar.header_for(RAIL_HOST)

    def _add_warning(self, text: str) -> None:
        if text and text not in self.warnings:
            self.warnings.append(text)

    def _budget_left(self) -> int:
        return max(0, self.max_requests - self.requests_sent)

    def _pace(self) -> None:
        moment = self.clock()
        if self.min_interval > 0 and self._last_request_at is not None:
            remaining = self.min_interval - (moment - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
                moment = self.clock()
        self._last_request_at = moment

    def _post_json(
        self, url: str, form: Dict[str, str], *, referer: str
    ) -> Tuple[Response, Dict, str]:
        if self._budget_left() <= 0:
            raise _RequestBudgetExhausted()
        self._pace()
        self.requests_sent += 1
        response = self.client.post_form(
            url,
            form,
            cookie_header=self._cookie_header(),
            headers={
                "Referer": referer,
                "Origin": RAIL_ORIGIN,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        self._last_response = response
        refresh_cookies_from_response(self.jar, response)
        text = response.text
        payload = decode_rail_json(response, text)
        return response, payload, text

    # -- gate -----------------------------------------------------------
    def check_gate(self) -> Dict:
        """1 read-only ``POST /otn/login/conf``: logged in? APP-authorized?

        Returns ``{logged_in, psr_authorized, psr_qr_code_result, ei_email,
        http_status, elapsed_ms}``.  ``logged_in=False`` raises
        :class:`AuthRequired`.  ``psr_authorized=False`` is returned, not
        raised, so a caller can decide to stop (``fetch`` does, fail-closed).
        No cookie value is ever returned or printed.
        """

        started = self.clock()
        response, payload, _text_body = self._post_json(
            RAIL_LOGIN_CONF_URL, {}, referer=RAIL_INVOICE_REFERER
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        is_login = data.get("is_login")
        if is_login is None:
            is_login = payload.get("is_login")
        psr = data.get("psr_qr_code_result")
        if psr is None:
            psr = payload.get("psr_qr_code_result")
        info = {
            "logged_in": _flag(is_login),
            "psr_authorized": _flag(psr),
            "psr_qr_code_result": _text(psr) or (str(psr) if psr is not None else None),
            "ei_email": data.get("ei_email", payload.get("ei_email")),
            "http_status": response.status,
            "elapsed_ms": int(round((self.clock() - started) * 1000)),
        }
        self.gate = info
        if not info["logged_in"]:
            raise AuthRequired(
                "12306 登录态无效（login/conf 返回 is_login 不是 Y）",
                "请在浏览器中打开 https://kyfw.12306.cn/otn/resources/login.html "
                "扫码登录后重试；本工具不会尝试绕过登录",
            )
        return info

    @staticmethod
    def _authorization_failure(exc: Exception) -> bool:
        message = getattr(exc, "message", None) or str(exc)
        return any(marker in message for marker in ("授权", "核验"))

    def _translate_authorization_error(self, exc: ApiError) -> Exception:
        if not self._authorization_failure(exc):
            return exc
        return AuthRequired(
            f"{RAIL_INVOICE_AUTH_MESSAGE}（服务端原文: {_safe_error_text(exc)}）",
            RAIL_INVOICE_AUTH_HINT,
        )

    def _request_page(
        self, page_index: int, window: Dict
    ) -> Tuple[Response, Dict, str]:
        form = build_invoice_form(
            window["start_date"],
            window["end_date"],
            page_index,
            invoice_type=self.invoice_type,
        )
        try:
            return self._post_json(
                RAIL_INVOICE_QUERY_URL, form, referer=RAIL_INVOICE_REFERER
            )
        except ApiError as exc:
            translated = self._translate_authorization_error(exc)
            if translated is exc:
                raise
            raise translated from exc

    # -- fetch ----------------------------------------------------------
    def fetch(
        self,
        start=None,
        end=None,
        page_size_hint: Optional[int] = None,
        *,
        gate: Optional[Dict] = None,
    ) -> Dict:
        """Query the e-invoice list once, oldest..newest dates inside the window.

        ``start``/``end`` accept ``YYYY-MM-DD`` or ``YYYYMMDD`` and are clamped
        to ``[max(today-179, 2024-11-01), today]``; see
        :func:`invoice_date_window`.  ``gate`` may be a previous
        :meth:`check_gate` result to avoid a second ``login/conf`` request.

        Raises :class:`AuthRequired` when not logged in or when the account has
        not passed the 铁路12306 APP 授权 gate (fail-closed: no list request is
        sent in that case), and :class:`ApiError` on ``error.html`` / non-JSON /
        risk pages — no retry in any of those cases.
        """

        require_login(self.jar)
        started = self.clock()
        self.requests_sent = 0
        self.pages = []
        self.warnings = []
        self._last_request_at = None
        self.gate = None

        window = invoice_date_window(
            self.moment,
            start,
            end,
            lookback_days=self.lookback_days,
            floor=self.floor,
        )
        self.window = window
        for text in window["warnings"]:
            self._add_warning(text)

        page_size = int(page_size_hint or self.page_size)
        if page_size < 1:
            raise ValueError("page_size_hint must be >= 1")
        gate_info = gate if isinstance(gate, dict) else None
        total: Optional[int] = None
        collected = 0
        budget_exhausted = False

        if window["queried"]:
            if gate_info is None:
                gate_info = self.check_gate()
            if gate_info.get("logged_in") is False:
                raise AuthRequired(
                    "12306 登录态无效（gate 显示未登录）",
                    "请在浏览器中打开 https://kyfw.12306.cn/otn/resources/login.html "
                    "扫码登录后重试",
                )
            if not gate_info.get("psr_authorized"):
                raise AuthRequired(RAIL_INVOICE_AUTH_MESSAGE, RAIL_INVOICE_AUTH_HINT)

            page_index = RAIL_INVOICE_PAGE_INDEX_BASE
            while True:
                if self._budget_left() <= 0:
                    budget_exhausted = True
                    self._add_warning(
                        f"本次运行已达到 {self.max_requests} 个请求上限（含门禁）："
                        f"只取到第 {len(self.pages)} 页、{collected}/{total!r} 条；"
                        f"请调大 --max-requests 后重跑"
                        f"（本命令无增量状态，重跑会从第 1 页全量重查）"
                    )
                    break
                if self.max_pages is not None and len(self.pages) >= self.max_pages:
                    self._add_warning(
                        f"已达到单次最大页数 {self.max_pages}："
                        f"{collected}/{total!r} 条；请调大 --max-pages 后重跑"
                    )
                    break
                response, payload, text = self._request_page(page_index, window)
                data = (
                    payload.get("data")
                    if isinstance(payload.get("data"), dict)
                    else {}
                )
                results = data.get("results")
                if not isinstance(results, list):
                    results = []
                if total is None:
                    total = _int(data.get("total"))
                collected += len(results)
                self.pages.append(
                    {
                        "page": len(self.pages) + 1,
                        "window": 1,
                        "page_index": page_index,
                        "query_start_date": window["start_date"],
                        "query_end_date": window["end_date"],
                        "status": response.status,
                        "total": total,
                        "records": results,
                        "raw": text,
                    }
                )
                page_index += 1
                if not results:
                    if total is not None and collected < total:
                        self._add_warning(
                            f"服务端声明 total={total}，但第 {len(self.pages)} 页"
                            f"为空；已提前停止（不补页、不重试）"
                        )
                    break
                if total is not None and collected >= total:
                    break
                if total is None and len(results) < page_size:
                    break

        elapsed_ms = int(round((self.clock() - started) * 1000))
        return {
            "kind": "rail_12306_invoice",
            "source": RAIL_INVOICE_QUERY_URL,
            "query": {
                "type": self.invoice_type,
                "type_label": RAIL_INVOICE_TYPE_LABELS.get(
                    self.invoice_type, "unknown"
                ),
                "start_date": window["start_date"],
                "end_date": window["end_date"],
                "requested_start_date": window["requested_start_date"],
                "requested_end_date": window["requested_end_date"],
                "oldest_allowed": window["oldest_allowed"],
                "newest_allowed": window["newest_allowed"],
                "lookback_days": window["lookback_days"],
                "window": {
                    "start_date": window["start_date"],
                    "end_date": window["end_date"],
                    "days": window["days"],
                },
                "windows": 1 if window["queried"] else 0,
                "page_size": page_size,
                "pages": len(self.pages),
                "total": total,
                "collected": collected,
                "requests_sent": self.requests_sent,
                "queried": bool(window["queried"]),
                "elapsed_ms": elapsed_ms,
                "gate": gate_info,
                "budget_exhausted": budget_exhausted,
                "warnings": list(self.warnings),
            },
            "pages": list(self.pages),
        }


def invoice_summary(payload: Dict) -> Dict:
    """Compact human summary for the CLI (no personal data)."""

    payload = payload if isinstance(payload, dict) else {}
    query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    gate = query.get("gate") if isinstance(query.get("gate"), dict) else {}
    return {
        "kind": payload.get("kind"),
        "type": query.get("type"),
        "type_label": query.get("type_label"),
        "window": query.get("window"),
        "records": len(parse_invoice_records(payload.get("pages") or [])),
        "total": query.get("total"),
        "requests_sent": query.get("requests_sent"),
        "pages": query.get("pages"),
        "queried": query.get("queried"),
        "gate": {
            "logged_in": gate.get("logged_in"),
            "psr_authorized": gate.get("psr_authorized"),
            "ei_email": gate.get("ei_email"),
        }
        if gate
        else None,
    }


def summarize(payload: Dict) -> Dict:
    orders = parse_orders(payload.get("pages") or [])
    span = date_range(orders)
    query = payload.get("query") if isinstance(payload, dict) else {}
    query = query if isinstance(query, dict) else {}
    summary = {
        "pages": len(payload.get("pages") or []),
        "windows": len(payload.get("windows") or []),
        "queried": bool(query.get("queried", payload.get("pages"))),
        "orders": len(orders),
        "date_range": span,
    }
    if "incremental" in query:
        summary["incremental"] = query.get("incremental")
    if "requests_sent" in query:
        summary["requests_sent"] = query.get("requests_sent")
    if isinstance(query.get("coverage"), dict):
        coverage = query["coverage"]
        summary["coverage"] = {
            "covered_days": coverage.get("covered_days"),
            "target_days": coverage.get("target_days"),
            "gap_days": coverage.get("gap_days"),
        }
    return summary
