"""JD Baitiao (京东白条) monthly bills and consumption details.

The legacy Baitiao bill centre at https://baitiao.jd.com/v3/ious/list is a
server-rendered page whose page bundle (/js/index_2.0.0.js) calls a small
set of read-only JSON endpoints.  Those endpoints accept the ordinary JD PC
session (``thor`` + ``pin`` on ``.jd.com``) and do not require h5st or any
other request signature.

Only the two list endpoints used by the page's 账单 / 消费明细 tabs are
called, plus the read-only "not yet billed" summary.  No repayment, staging,
cancellation, or other write endpoint is ever invoked.
"""

from __future__ import annotations

import datetime
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .blocking import check_jd_html
from ..kit.cookies import CookieJar
from ..kit.errors import ApiError, AuthRequired, NetworkError, RiskBlocked
from ..kit.http import HttpClient, Response
from .reader import BEIJING, apply_login_cookie_rotations, require_login

BAITIAO_HOST = "baitiao.jd.com"
BAITIAO_ORIGIN = f"https://{BAITIAO_HOST}"
BAITIAO_REFERER = f"{BAITIAO_ORIGIN}/v3/ious/list"
BAITIAO_BASE = f"{BAITIAO_ORIGIN}/v3/ious"
BILL_ENDPOINT = f"{BAITIAO_BASE}/getBillList"
TRANSACTION_ENDPOINT = f"{BAITIAO_BASE}/billConsumeList"
UNBILLED_ENDPOINT = f"{BAITIAO_BASE}/queryNotOutAccount"

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50

# The page's jQuery helper sends type:"post" with a form-encoded body.  The
# Referer/Origin pair is part of the server-side check on these endpoints.
REQUEST_HEADERS = {
    "Referer": BAITIAO_REFERER,
    "Origin": BAITIAO_ORIGIN,
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
TIMESTAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?"
)

LOGIN_HINT = (
    "请在浏览器中打开 https://www.jd.com/ 或 https://jr.jd.com/ 并扫码登录后重试"
)


def _as_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _amount(value) -> Optional[str]:
    """Normalize an API amount to a plain two-decimal string."""

    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if not number.is_finite():
        return text
    return format(number.quantize(Decimal("0.01")), "f")


def _date(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    match = DATE_RE.match(text)
    if match:
        return match.group(0)
    return text or None


def _timestamp(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    match = TIMESTAMP_RE.match(text)
    if not match:
        return _date(text)
    year, month, day, hour, minute, second = match.groups()
    return f"{year}-{month}-{day} {hour}:{minute}:{second or '00'}"


def _month(value) -> Optional[str]:
    date = _date(value)
    return date[:7] if date and DATE_RE.match(date) else None


def _string(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_bill(row: Dict) -> Dict:
    """Turn one getBillList row into a stable, documented shape."""

    return {
        "bill_id": _string(row.get("billId")),
        "bill_month": _month(row.get("billDate")),
        "bill_date": _date(row.get("billDate")),
        "due_date": _date(row.get("billLimitDate")),
        "period_start": _date(row.get("billDateStart")),
        "period_end": _date(row.get("billDateEnd")),
        "is_current": _truthy(row.get("isCurBill")),
        "status": _as_int(row.get("status")),
        "amount": _amount(row.get("billAmt")),
        "paid": _amount(row.get("payedAmt")),
        "refund": _amount(row.get("refundAmt")),
        "should_pay": _amount(row.get("sdpAmt")),
        "min_payment": _amount(row.get("minPayAmt")),
        "installment_count": _as_int(row.get("planedNum")),
        "installment_amount": _amount(row.get("planedAmt")),
        "installment_remaining": _amount(row.get("restPlanAmt")),
        "overdue_amount": _amount(row.get("sdpOverSum")),
        "overdue_days": _as_int(row.get("realOverDays")),
        "overdue": _truthy(row.get("isOverdueDay")),
        "raw": row,
    }


def normalize_transaction(row: Dict) -> Dict:
    """Turn one billConsumeList row into a stable, documented shape."""

    return {
        "order_id": _string(row.get("orderId")),
        "occurred": _timestamp(row.get("consumerDate")),
        "product": _string(row.get("productName")),
        "amount": _amount(row.get("orderAmount")),
        "discount": _amount(row.get("loanAmountDiscount")),
        "sys_code": _as_int(row.get("sysCode")),
        "biz_code": _as_int(row.get("bizCode")),
        "order_url": _string(row.get("orderUrl")),
        "raw": row,
    }


def normalize_unbilled(payload: Dict) -> Optional[Dict]:
    """Normalize the queryNotOutAccount envelope, or None when empty."""

    bill = payload.get("billNOTOut")
    if not isinstance(bill, dict):
        return None
    return {
        "bill_id": _string(bill.get("billId")),
        "bill_date": _date(bill.get("billDate")),
        "amount": _amount(bill.get("billAmt")),
        "should_pay": _amount(bill.get("sdpAmt")),
        "min_payment": _amount(bill.get("minPayAmt")),
        "paid": _amount(bill.get("payedAmt")),
        "refund": _amount(bill.get("refundAmt")),
        "is_current": _truthy(bill.get("isCurBill")),
        "can_repay": _truthy(bill.get("canRepay")),
        "overdue_days": _as_int(bill.get("realOverDays")),
        "late_status": _as_int(bill.get("lateStatus")),
        "raw": payload,
    }


def _bill_key(row: Dict) -> Tuple:
    return ("bill", _string(row.get("billId")))


def _transaction_key(row: Dict) -> Tuple:
    return (
        "transaction",
        _string(row.get("orderId")),
        _string(row.get("consumerDate")),
        _string(row.get("orderAmount")),
        _string(row.get("productName")),
    )


def _auth_required(detail: str) -> AuthRequired:
    message = "京东白条登录状态已失效" + (f"（{detail}）" if detail else "")
    return AuthRequired(message, LOGIN_HINT)


def _risk_blocked(detail: str) -> RiskBlocked:
    return RiskBlocked(
        "京东白条返回了验证/风控提示" + (f"（{detail}）" if detail else ""),
        "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
    )


def decode_payload(response: Response, endpoint: str) -> Dict:
    """Validate one Baitiao JSON response and map failures to typed errors."""

    endpoint_name = endpoint.rsplit("/", 1)[-1]
    text = response.text
    if response.status >= 400:
        check_jd_html(text, response.url, response.status)
        raise NetworkError(
            f"京东白条接口 {endpoint_name} 请求失败 (HTTP {response.status})"
        )
    try:
        payload = json.loads(text)
    except ValueError:
        # A login redirect ends on a passport HTML page; risk pages are HTML
        # too.  Let the shared JD detector produce the precise error.
        check_jd_html(text, response.url, response.status)
        raise ApiError(f"京东白条接口 {endpoint_name} 返回了非 JSON 内容")
    if not isinstance(payload, dict):
        raise ApiError(f"京东白条接口 {endpoint_name} 返回了非 JSON 对象")

    error = _string(payload.get("error"))
    if error and error.lower() in ("notlogin", "not_login", "login_required"):
        raise _auth_required(error)

    top_code = _string(payload.get("code")) or ""
    top_message = " ".join(
        _string(payload.get(key)) or "" for key in ("message", "msg", "errorMsg")
    ).strip()
    top_combined = f"{top_code} {top_message}".lower()
    if any(
        token in top_combined
        for token in (
            "未登录",
            "请先登录",
            "not login",
            "notlogin",
            "login_required",
            "login required",
        )
    ) or top_code in ("3", "401"):
        raise _auth_required(top_message or top_code)
    if any(
        token in top_combined
        for token in ("验证", "风控", "captcha", "x5sec", "punish", "risk", "h5st")
    ):
        raise _risk_blocked(top_message or top_code)
    if error:
        raise ApiError(f"京东白条接口返回失败: {error}")

    result = payload.get("result")
    if isinstance(result, dict):
        code = _string(result.get("code")) or ""
        message = " ".join(
            _string(result.get(key)) or ""
            for key in ("info", "message", "msg")
        ).strip()
        combined = f"{code} {message}".lower()
        if any(
            token in combined
            for token in (
                "未登录",
                "请先登录",
                "not login",
                "notlogin",
                "login_required",
                "login required",
            )
        ):
            raise _auth_required(message or code)
        if any(
            token in combined
            for token in (
                "验证",
                "风控",
                "captcha",
                "x5sec",
                "punish",
                "risk",
                "h5st",
            )
        ):
            raise _risk_blocked(message or code)
        if result.get("isSuccess") is False:
            detail = message or (f"code={code}" if code else "未知原因")
            raise ApiError(f"京东白条接口返回失败: {detail}")

    return payload


class BaitiaoFetcher:
    """Read-only pagination over the Baitiao bill and consumption endpoints."""

    def __init__(
        self,
        client: HttpClient,
        jar: CookieJar,
        *,
        max_pages: int = 50,
        page_size: int = DEFAULT_PAGE_SIZE,
        include_unbilled: bool = True,
    ):
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        self.client = client
        self.jar = jar
        self.max_pages = max_pages
        self.page_size = page_size
        self.include_unbilled = include_unbilled
        self.bills: List[Dict] = []
        self.transactions: List[Dict] = []
        self.unbilled: Optional[Dict] = None
        self.warnings: List[str] = []
        self.cookie_rotations = 0
        self.pages = {"bills": 0, "transactions": 0}
        self._seen: Dict[str, Set[Tuple]] = {"bills": set(), "transactions": set()}

    def _post(self, endpoint: str, form: Dict[str, str]) -> Dict:
        response = self.client.post_form(
            endpoint,
            form,
            cookie_header=self.jar.header_for(BAITIAO_HOST),
            headers=REQUEST_HEADERS,
        )
        self.cookie_rotations += apply_login_cookie_rotations(
            self.jar, response, default_host=BAITIAO_HOST
        )
        return decode_payload(response, endpoint)

    def _collect(
        self,
        *,
        endpoint: str,
        list_key: str,
        label: str,
        key_func: Callable[[Dict], Tuple],
        normalize: Callable[[Dict], Dict],
        target: List[Dict],
        seen: Set[Tuple],
    ) -> int:
        page = 1
        fetched = 0
        while page <= self.max_pages:
            payload = self._post(
                endpoint, {"pageNum": str(page), "pageSize": str(self.page_size)}
            )
            rows = payload.get(list_key)
            if rows is None:
                total = _as_int(payload.get("totalCount"))
                # The API omits the list entirely on an empty page (or when
                # the account has no bills at all) and still reports success.
                if total in (None, 0):
                    rows = []
                else:
                    raise ApiError(f"京东白条{label}接口缺少 {list_key} 字段")
            if not isinstance(rows, list):
                raise ApiError(f"京东白条{label}接口的 {list_key} 不是数组")

            new_rows = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = key_func(row)
                if key in seen:
                    continue
                seen.add(key)
                target.append(normalize(row))
                new_rows += 1

            fetched += 1
            page_count = _as_int(payload.get("pageCount"))
            if not rows or new_rows == 0:
                break
            if page_count is not None:
                if page >= page_count:
                    break
            elif len(rows) < self.page_size:
                break
            page += 1
        else:
            self.warnings.append(
                f"{label}: 已达到单次最大页数 {self.max_pages}，可能尚未取完"
            )
        return fetched

    def _fetch_unbilled(self) -> None:
        try:
            payload = self._post(UNBILLED_ENDPOINT, {})
        except (ApiError, NetworkError) as exc:
            # The unbilled summary is a bonus; the bill/transaction export is
            # still useful if this one endpoint is unavailable.
            self.warnings.append(f"未出账单汇总获取失败，已跳过: {exc.message}")
            return
        unbilled = normalize_unbilled(payload)
        if unbilled is None:
            self.warnings.append("未出账单汇总为空，已跳过")
            return
        self.unbilled = unbilled

    def probe(self) -> Dict:
        """Read only the first bill page, never transactions or repayment."""
        require_login(self.jar, BAITIAO_HOST)
        payload = self._post(BILL_ENDPOINT, {"pageNum": "1", "pageSize": "1"})
        rows = payload.get("billList") or []
        if not isinstance(rows, list):
            raise ApiError("京东白条账单接口的 billList 不是数组")
        return {"orders": len(rows), "cookie_rotations": self.cookie_rotations}

    def fetch(self) -> Dict:
        require_login(self.jar, BAITIAO_HOST)
        self.pages["bills"] = self._collect(
            endpoint=BILL_ENDPOINT,
            list_key="billList",
            label="账单",
            key_func=_bill_key,
            normalize=normalize_bill,
            target=self.bills,
            seen=self._seen["bills"],
        )
        self.pages["transactions"] = self._collect(
            endpoint=TRANSACTION_ENDPOINT,
            list_key="detailsInfo",
            label="消费明细",
            key_func=_transaction_key,
            normalize=normalize_transaction,
            target=self.transactions,
            seen=self._seen["transactions"],
        )
        if self.include_unbilled:
            self._fetch_unbilled()
        return {
            "kind": "jd_baitiao",
            "source": BAITIAO_BASE,
            "fetched_at": datetime.datetime.now(BEIJING).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "bills": self.bills,
            "transactions": self.transactions,
            "unbilled": self.unbilled,
            "pages": dict(self.pages),
        }


def _range(values: Iterable[Optional[str]]) -> Optional[Tuple[str, str]]:
    present = [value for value in values if value]
    if not present:
        return None
    return min(present), max(present)


def summarize(payload: Dict) -> Dict:
    bills = payload.get("bills") or []
    transactions = payload.get("transactions") or []
    return {
        "bills": len(bills),
        "transactions": len(transactions),
        "bill_range": _range(bill.get("bill_date") for bill in bills),
        "transaction_range": _range(
            transaction.get("occurred") for transaction in transactions
        ),
        "pages": payload.get("pages") or {},
        "has_unbilled": payload.get("unbilled") is not None,
    }
