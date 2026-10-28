"""Meituan Yuefu (美团月付) bill and transaction reader.

The public entry is the Meituan wallet bill page::

    https://npay.meituan.com/pay-bill/index.html?payType=4&entry=creditpay_bill_detail

``payType=4`` is the bundle's ``PayType.monthPay`` ("美团月付").  The page
POSTs a compact JSON body to ``/trade/bill/getBillList`` on the same origin;
it does not send an ``mtgsig`` header in the public bundle.  The same endpoint
with ``financialTransType=4`` (and without ``payAttr``) returns the month-pay repayment history, including the
``payDetailTag`` / ``payDetails`` fields for the funding bank card.  This
module mirrors those read-only requests, stores each page's raw response in
one 0600 JSON file, and never writes cookie values into the file or terminal.

The dedicated ``npay.meituan.com/creditpay/`` 月付 app is also public until
it redirects to its login page.  Its post-login index page is server-gated,
so its ``window.API_JOB`` controller/job name is not available without a
logged-in session.  The wallet bill page above is the public, verifiable
bill-list path and is what this module uses.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Dict, Iterable, List, Optional, Tuple

from .blocking import check_meituan_api, check_meituan_html
from ..kit.cookies import CookieJar
from ..kit.errors import ApiError, AuthRequired, NetworkError, UsageError
from ..kit.http import HttpClient, Response

MEITUAN_HOST = "npay.meituan.com"
MEITUAN_ENTRY = (
    "https://npay.meituan.com/pay-bill/index.html"
    "?payType=4&entry=creditpay_bill_detail"
)
MEITUAN_BILL_ENDPOINT = "https://npay.meituan.com/trade/bill/getBillList"
MEITUAN_USERINFO_ENDPOINT = "https://npay.meituan.com/trade/bill/userInfo"
MEITUAN_REFERER = MEITUAN_ENTRY
MEITUAN_ORIGIN = "https://npay.meituan.com"

# PayType.monthPay = 4 in the public bundle.  The page's ``payType`` query
# parameter is validated against the same enum and sent as ``payAttr``.
MEITUAN_PAY_TYPE = 4
MEITUAN_PAY_ATTR = 4
MEITUAN_PAGE_SIZE = 20

# ``FinancialTransNumber.Repayment`` in the public pay-bill bundle.  The
# wallet page sends this as ``financialTransType=4`` when the user taps the
# "还款" quick filter.  The repayment query must NOT send ``payAttr``: with
# ``payAttr=4`` the server returns an empty month list, while omitting it
# returns the month-pay repayment history.
MEITUAN_REPAYMENT_FILTER = 4
# ``MinDate`` in the public bundle (2019-06-01); used as the lower bound for
# the repayment window.
MEITUAN_REPAYMENT_START = "20190601"

# The page reads these with getParamFromUrlOrCookie('u','userid','userId',
# 'user_id'); the session credential itself is the ``token`` cookie.
MEITUAN_LOGIN_COOKIES = ("token", "userId", "u", "userid", "uuid", "ci")
MEITUAN_REQUIRED_LOGIN_COOKIES = ("token",)
MEITUAN_USER_ID_COOKIES = ("userId", "u", "userid", "user_id")

BEIJING = datetime.timezone(datetime.timedelta(hours=8))


def user_id_from_jar(jar: CookieJar, host: str = MEITUAN_HOST) -> str:
    """Return the account id cookie used by the bill page, or ''."""

    for name in MEITUAN_USER_ID_COOKIES:
        value = jar.get(name, host)
        if value:
            return value
    return ""


def missing_login_cookies(jar: CookieJar, host: str = MEITUAN_HOST) -> List[str]:
    """Names of required Meituan login cookies that are absent.

    ``token`` is the session credential.  The account-id cookie
    (``userId``/``u``/``userid``/``user_id``) is reported as a warning only:
    the page sends it when present, but the server authenticates with the
    token.
    """

    missing = [
        name
        for name in MEITUAN_REQUIRED_LOGIN_COOKIES
        if not jar.get(name, host)
    ]
    return missing


def login_cookie_presence(
    jar: CookieJar, host: str = MEITUAN_HOST
) -> Dict[str, bool]:
    presence = {name: bool(jar.get(name, host)) for name in MEITUAN_LOGIN_COOKIES}
    presence["account_id"] = bool(user_id_from_jar(jar, host))
    return presence


def require_login(jar: CookieJar, host: str = MEITUAN_HOST) -> None:
    missing = missing_login_cookies(jar, host)
    if missing:
        raise AuthRequired(
            "浏览器中没有找到美团月付登录 Cookie: " + ", ".join(missing),
            "请在浏览器中打开 https://npay.meituan.com/pay-bill/index.html?payType=4 "
            "并登录后重试",
        )


def build_page_payload(
    page: int,
    *,
    user_id: str = "",
    page_size: int = MEITUAN_PAGE_SIZE,
) -> Dict:
    """Match the public page's compact JSON body.

    Fields and order follow the captured unauthenticated page request:
    ``{"userid","nb_app","nb_platform","currentPage","payAttr","pageSize",
    "ctype"}``.
    """

    return {
        "userid": user_id or "",
        "nb_app": "web",
        "nb_platform": "touch",
        "currentPage": int(page),
        "payAttr": int(MEITUAN_PAY_ATTR),
        "pageSize": int(page_size),
        "ctype": "",
    }


def build_userinfo_payload(user_id: str = "") -> Dict:
    return {
        "userid": user_id or "",
        "nb_app": "web",
        "nb_platform": "touch",
        "ctype": "",
    }


def repayment_window(now: Optional[datetime.datetime] = None) -> Tuple[str, str]:
    """Return inclusive ``YYYYMMDD`` bounds for the repayment query.

    The public page passes ``startTime``/``endTime`` when a month is selected;
    a wide window lets one paginated pass cover the whole repayment history.
    """

    current = now or datetime.datetime.now(BEIJING)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING)
    else:
        current = current.astimezone(BEIJING)
    return MEITUAN_REPAYMENT_START, current.strftime("%Y%m%d")


def build_repayment_payload(
    page: int,
    *,
    user_id: str = "",
    page_size: int = MEITUAN_PAGE_SIZE,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> Dict:
    """Match the page's "还款" quick-filter request.

    The page sends ``financialTransType=4`` and deliberately omits ``payAttr``
    (the month-pay attribute filters to transactions *paid with* month-pay,
    which excludes repayments).  ``startTime``/``endTime`` are ``YYYYMMDD``.
    """

    payload = build_page_payload(page, user_id=user_id, page_size=page_size)
    payload.pop("payAttr", None)
    payload["financialTransType"] = int(MEITUAN_REPAYMENT_FILTER)
    start, end = start_time, end_time
    if start is None or end is None:
        default_start, default_end = repayment_window()
        start = start or default_start
        end = end or default_end
    payload["startTime"] = str(start)
    payload["endTime"] = str(end)
    return payload


def masked_payload(payload: Dict) -> Dict:
    """Copy the request body without writing the account-id cookie value."""

    masked = dict(payload)
    if masked.get("userid"):
        masked["userid"] = "<from cookie>"
    return masked


def _coerce_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value) -> Optional[datetime.datetime]:
    """Parse the bill item's ``transModUnixTime`` (seconds) or a datetime."""

    number = _coerce_int(value)
    if number is not None and number > 0:
        if number < 10_000_000_000:  # seconds
            number *= 1000
        try:
            return datetime.datetime.fromtimestamp(number / 1000, BEIJING)
        except (OverflowError, OSError, ValueError):
            return None
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=BEIJING)
        except ValueError:
            continue
    return None


def _pay_time(value, month: str = "") -> Optional[str]:
    """Turn the display ``payTime`` (e.g. ``10-24 19:59``) into a date."""

    if not value:
        return None
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y%m%d%H%M%S",
    ):
        try:
            return datetime.datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    # Python 3.13+ rejects ``%m-%d`` without a year (leap-day ambiguity), so
    # parse the page's "10-24 19:59" shape explicitly and take the year from
    # the enclosing month bucket.
    match = re.match(
        r"^(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$",
        text,
    )
    if not match:
        return None
    month_num, day_num, hour, minute, second = match.groups()
    year = (
        int(month[:4])
        if len(month) >= 4 and month[:4].isdigit()
        else datetime.datetime.now().year
    )
    try:
        return datetime.datetime(
            year,
            int(month_num),
            int(day_num),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
        ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _item_occurred(item: Dict, month: str = "") -> Optional[str]:
    when = _timestamp(item.get("transModUnixTime"))
    if when is not None:
        return when.strftime("%Y-%m-%d %H:%M:%S")
    return _pay_time(item.get("payTime"), month)


def _item_amount_fen(item: Dict) -> Optional[int]:
    for key in ("orderAmount", "amount", "payAmount", "tradeAmount"):
        value = _coerce_int(item.get(key))
        if value is not None:
            return value
    return None


def _item_record(item: Dict, month: str) -> Dict:
    trade_no = item.get("tradeNo")
    pay_id = item.get("payId")
    amount_fen = _item_amount_fen(item)
    return {
        "trade_no": str(trade_no) if trade_no is not None else None,
        "pay_id": str(pay_id) if pay_id is not None else None,
        "month": month or None,
        "occurred": _item_occurred(item, month),
        "title": item.get("orderTitle"),
        "business": item.get("businessName"),
        "amount_fen": amount_fen,
        "amount_yuan": f"{amount_fen / 100:.2f}" if amount_fen is not None else None,
        "promo_fen": _coerce_int(item.get("orderPromoAmount")),
        "money_flow": _coerce_int(item.get("moneyFlow")),
        "trade_type": _coerce_int(item.get("tradeType")),
        "order_type": item.get("orderType"),
    }


def _parse_payload_transactions(payload: Dict) -> List[Dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    months = data.get("monthData")
    if not isinstance(months, list):
        return []

    transactions: List[Dict] = []
    for month_item in months:
        if not isinstance(month_item, dict):
            continue
        month = str(month_item.get("month") or "")
        items = month_item.get("billItemList")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if not item.get("tradeNo") and not item.get("payId"):
                continue
            transactions.append(_item_record(item, month))
    return transactions


def parse_pay_details(value) -> Tuple[Optional[List[Dict]], Optional[str]]:
    """Parse the repayment item's ``payDetails`` JSON string.

    Returns ``(parsed_list, raw_on_error)``.  The parsed list is kept as-is
    (``fundType``, ``bankName``, ``cardNoSuffix``, ``moneyCent``, ...); if the
    value is not JSON, the original string is returned so the row can preserve
    it verbatim.
    """

    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)], None
    if isinstance(value, dict):
        return [value], None
    if not isinstance(value, str) or not value.strip():
        return None, None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None, value
    if isinstance(parsed, list):
        return [entry for entry in parsed if isinstance(entry, dict)], None
    if isinstance(parsed, dict):
        return [parsed], None
    return None, value


def repayment_mode(value) -> str:
    """Return the repayment-type text as shown by the source.

    The real bill item carries it inside ``orderTitle`` (for example
    ``【美团月付】主动还款2026年9月账单`` or ``...代扣还款...``).  Synthetic
    or future responses may expose an explicit ``repayType`` field; that wins.
    """

    if isinstance(value, dict):
        for key in (
            "repayType",
            "repay_type",
            "repaymentType",
            "repayment_type",
            "repayMode",
            "repay_mode",
        ):
            explicit = value.get(key)
            if explicit:
                return str(explicit).strip()
        text = str(value.get("orderTitle") or value.get("title") or "").strip()
    else:
        text = str(value or "").strip()
    for token in ("主动还款", "自动还款", "代扣还款"):
        if token in text:
            return token
    return text or "还款"


def _parse_payload_repayments(payload: Dict) -> List[Dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    months = data.get("monthData")
    if not isinstance(months, list):
        return []

    repayments: List[Dict] = []
    for month_item in months:
        if not isinstance(month_item, dict):
            continue
        month = str(month_item.get("month") or "")
        items = month_item.get("billItemList")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if not item.get("tradeNo") and not item.get("payId"):
                continue
            record = _item_record(item, month)
            details, raw_details = parse_pay_details(item.get("payDetails"))
            record.update(
                {
                    "order_title": item.get("orderTitle"),
                    "pay_detail_tag": item.get("payDetailTag"),
                    "pay_details": details,
                    "pay_details_raw": raw_details,
                    "repay_mode": repayment_mode(item),
                }
            )
            repayments.append(record)
    return repayments


def _responses_from_pages(pages) -> List[Dict]:
    responses: List[Dict] = []
    if not isinstance(pages, list):
        return responses
    for page in pages:
        if not isinstance(page, dict):
            continue
        response = page.get("response")
        if isinstance(response, dict):
            responses.append(response)
        elif isinstance(page.get("data"), dict):
            # Allow a bare list of API responses.
            responses.append(page)
    return responses


def parse_repayments(payload: Dict) -> List[Dict]:
    """Flatten the repayment pass from a raw output file or API response.

    Accepts the tool payload (``{"repayments": {"pages": [...]}}``), a single
    API response, or a list of API responses.  It never mutates the input.
    """

    if isinstance(payload, list):
        responses = _responses_from_pages(payload)
    elif isinstance(payload, dict):
        responses = []
        repayment = payload.get("repayments")
        if isinstance(repayment, dict):
            pages = repayment.get("pages")
            if isinstance(pages, list):
                responses = _responses_from_pages(pages)
            elif isinstance(repayment.get("data"), dict):
                responses = [repayment]
        if not responses:
            pages = payload.get("repayment_pages") or payload.get("repaymentPages")
            responses = _responses_from_pages(pages)
        if not responses and isinstance(payload.get("data"), dict):
            responses = [payload]
    else:
        return []
    repayments: List[Dict] = []
    for response in responses:
        repayments.extend(_parse_payload_repayments(response))
    return repayments


def parse_transactions(payload: Dict) -> List[Dict]:
    """Flatten ``monthData[].billItemList`` from one response or an output file.

    Accepts either a single API response (``{"data": ...}``) or the tool's
    payload (``{"pages": [{"response": ...}]}``) so tests and summaries share
    one parser.  It never mutates the raw response.
    """

    if not isinstance(payload, dict):
        return []
    pages = payload.get("pages")
    if isinstance(pages, list):
        transactions: List[Dict] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            response = page.get("response")
            if isinstance(response, dict):
                transactions.extend(_parse_payload_transactions(response))
        return transactions
    return _parse_payload_transactions(payload)


def date_range(orders: Iterable[Dict]) -> Optional[Tuple[str, str]]:
    dates = [order.get("occurred") for order in orders if order.get("occurred")]
    if not dates:
        return None
    return min(dates), max(dates)


def _unique_items(items: Iterable[Dict]) -> List[Dict]:
    seen = set()
    unique = []
    for item in items:
        key = (item.get("trade_no"), item.get("pay_id"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def summarize(payload: Dict) -> Dict:
    transactions = _unique_items(parse_transactions(payload))
    repayments = _unique_items(parse_repayments(payload))
    months = set()
    for page in payload.get("pages") or []:
        if not isinstance(page, dict):
            continue
        response = page.get("response")
        data = response.get("data") if isinstance(response, dict) else None
        month_list = data.get("monthData") if isinstance(data, dict) else None
        if not isinstance(month_list, list):
            continue
        for month in month_list:
            if isinstance(month, dict) and month.get("month"):
                months.add(str(month["month"]))
    repayment = payload.get("repayments")
    repayment_pages = (
        len(repayment.get("pages") or [])
        if isinstance(repayment, dict)
        else 0
    )
    return {
        "pages": len(payload.get("pages") or []),
        "orders": len(transactions),
        "transactions": len(transactions),
        "months": len(months),
        "date_range": date_range(transactions),
        "repayment_pages": repayment_pages,
        "repayments": len(repayments),
        "repayment_date_range": date_range(repayments),
    }


def _decode(response: Response) -> Dict:
    if response.status >= 400:
        check_meituan_html(response.text, response.url, response.status)
        raise NetworkError(f"美团月付接口请求失败 (HTTP {response.status})")
    try:
        payload = json.loads(response.text)
    except ValueError as exc:
        check_meituan_html(response.text, response.url, response.status)
        raise ApiError("美团月付接口返回了非 JSON 内容（页面结构可能已变化）") from exc
    if not isinstance(payload, dict):
        check_meituan_html(response.text, response.url, response.status)
        raise ApiError("美团月付接口返回了意外的 JSON 类型")
    check_meituan_api(payload, response.status)
    return payload


class MeituanYuefuFetcher:
    """Read the public Meituan wallet bill list and month-pay repayments."""

    def __init__(
        self,
        client: HttpClient,
        jar: CookieJar,
        *,
        max_pages: int = 50,
        page_size: int = MEITUAN_PAGE_SIZE,
        include_repayments: bool = True,
        repayment_start: Optional[str] = None,
        repayment_end: Optional[str] = None,
    ):
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        self.client = client
        self.jar = jar
        self.max_pages = max_pages
        self.page_size = page_size
        self.include_repayments = include_repayments
        self.repayment_start = repayment_start
        self.repayment_end = repayment_end
        self.pages: List[Dict] = []
        self.repayment_pages: List[Dict] = []
        self.warnings: List[str] = []

    def _cookie_header(self) -> str:
        return self.jar.header_for(MEITUAN_HOST)

    def _post(self, url: str, payload: Dict) -> Response:
        return self.client.post_json(
            url,
            payload,
            cookie_header=self._cookie_header(),
            headers={
                "Referer": MEITUAN_REFERER,
                "Origin": MEITUAN_ORIGIN,
            },
        )

    def _fetch_bill_page(self, page: int) -> Tuple[Dict, str, Dict]:
        payload = build_page_payload(
            page,
            user_id=user_id_from_jar(self.jar),
            page_size=self.page_size,
        )
        response = self._post(MEITUAN_BILL_ENDPOINT, payload)
        decoded = _decode(response)
        return decoded, response.text, payload

    def _fetch_repayment_page(
        self, page: int, start_time: str, end_time: str
    ) -> Tuple[Dict, str, Dict]:
        payload = build_repayment_payload(
            page,
            user_id=user_id_from_jar(self.jar),
            page_size=self.page_size,
            start_time=start_time,
            end_time=end_time,
        )
        response = self._post(MEITUAN_BILL_ENDPOINT, payload)
        decoded = _decode(response)
        return decoded, response.text, payload

    def _fetch_bill_pages(self) -> None:
        page = 0
        while page < self.max_pages:
            payload, raw, request = self._fetch_bill_page(page)
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ApiError("美团月付接口没有返回 data 对象")
            month_data = data.get("monthData")
            if month_data is None:
                month_data = []
            if not isinstance(month_data, list):
                raise ApiError("美团月付接口返回的 monthData 不是列表")

            self.pages.append(
                {
                    "page": page,
                    "request": masked_payload(request),
                    "response": payload,
                    "raw": raw,
                }
            )

            next_page = _coerce_int(data.get("nextPage"))
            if next_page is None or next_page <= page:
                break
            if next_page > page + self.max_pages:
                self.warnings.append(
                    f"服务端 nextPage={next_page} 超出单次最大页数，已停止"
                )
                break
            page = next_page
        else:
            self.warnings.append(
                f"已达到单次最大页数 {self.max_pages}，账单可能尚未取完"
            )

    def _fetch_repayment_pages(self) -> Dict:
        default_start, default_end = repayment_window()
        start_time = self.repayment_start or default_start
        end_time = self.repayment_end or default_end
        page = 0
        while page < self.max_pages:
            payload, raw, request = self._fetch_repayment_page(
                page, start_time, end_time
            )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ApiError("美团月付还款接口没有返回 data 对象")
            month_data = data.get("monthData")
            if month_data is None:
                month_data = []
            if not isinstance(month_data, list):
                raise ApiError("美团月付还款接口返回的 monthData 不是列表")

            self.repayment_pages.append(
                {
                    "page": page,
                    "request": masked_payload(request),
                    "response": payload,
                    "raw": raw,
                }
            )

            next_page = _coerce_int(data.get("nextPage"))
            if next_page is None or next_page <= page:
                break
            if next_page > page + self.max_pages:
                self.warnings.append(
                    f"服务端 nextPage={next_page} 超出单次最大页数，还款记录可能尚未取完"
                )
                break
            page = next_page
        else:
            self.warnings.append(
                f"已达到单次最大页数 {self.max_pages}，还款记录可能尚未取完"
            )

        return {
            "source": MEITUAN_BILL_ENDPOINT,
            "filter": {
                "financialTransType": MEITUAN_REPAYMENT_FILTER,
                "startTime": start_time,
                "endTime": end_time,
            },
            "pages": self.repayment_pages,
        }

    def fetch(self) -> Dict:
        require_login(self.jar)
        if not user_id_from_jar(self.jar):
            self.warnings.append(
                "未找到 userId/u 账号 Cookie；仍使用 token 读取账单"
            )

        self._fetch_bill_pages()
        repayments = (
            self._fetch_repayment_pages() if self.include_repayments else None
        )

        return {
            "kind": "meituan_yuefu",
            "source": MEITUAN_BILL_ENDPOINT,
            "entry": MEITUAN_ENTRY,
            "pay_type": MEITUAN_PAY_TYPE,
            "pay_attr": MEITUAN_PAY_ATTR,
            "pages": self.pages,
            "repayments": repayments,
        }


    def probe(self) -> Dict:
        """One lightweight read-only request for status/keepalive."""

        require_login(self.jar)
        payload = build_userinfo_payload(user_id_from_jar(self.jar))
        response = self._post(MEITUAN_USERINFO_ENDPOINT, payload)
        decoded = _decode(response)
        data = decoded.get("data")
        return {
            "status": response.status,
            "url": MEITUAN_USERINFO_ENDPOINT,
            "has_data": isinstance(data, dict),
        }


def parse_page_size(spec: Optional[str]) -> int:
    if spec is None or str(spec).strip() == "":
        return MEITUAN_PAGE_SIZE
    try:
        value = int(str(spec).strip())
    except ValueError as exc:
        raise UsageError(f"无法解析 --page-size: {spec}") from exc
    if value < 1 or value > 100:
        raise UsageError("--page-size 必须在 1-100 之间")
    return value
