"""Ctrip (携程 / Trip.com China) order history reader.

Only the read-only order-centre endpoints used by the real
``my.ctrip.com/myinfo/all`` page are called:

* ``10098/GetOrderWithBM.json`` -- the last-year / recent order list
  (``orderType`` = ``recent`` on the PC page).
* ``10098/GetHistoryOrderList.json`` -- older orders, searched by booking
  date (the PC page's ``history`` / "more than one year" path).

The old H5 bundle also exposes ``10098/getAllOrders.json``.  That endpoint is
legacy: every request body recovered from the H5 bundle is rejected by the
live service with ``ResultCode=-2 / request parameter error``.  The real PC
order centre does not call it; use the PC request shape below.

The request format is the one the real PC page sends: a ``head`` with an
empty ``auth`` (the session comes from the cookies), ``cver`` ``1.0``, a
numeric ``sequence`` extension, and a body carrying ``Channel=Online``,
``ClientVersion=99.99`` and ``Locale=""``.  The browser bundle injects those
three body fields from its runtime config; omitting ``Channel`` makes the
service return an empty order list with no error.

The page may add a ``w-payload-source`` header computed by the external
anti-bot SDK ``c-sign.js``.  This module never loads, computes, forges or
bypasses that signature.  If the server asks for it, the fetch stops with
:class:`NeedsRiskSignature` and tells the owner to use the logged-in browser
page instead.
"""

from __future__ import annotations

import datetime
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .blocking import check_ctrip_api, check_ctrip_html
from ..kit.cookies import CookieJar
from ..kit.errors import ApiError, AuthRequired, NetworkError
from ..kit.http import HttpClient

# The PC page is served from my.ctrip.com; its SOA gateway is m.ctrip.com.
# The page itself calls these endpoints cross-origin with credentials and a
# ``cookieOrigin: https://my.ctrip.com`` header.
CTRIP_PAGE_HOST = "my.ctrip.com"
CTRIP_API_HOST = "m.ctrip.com"
CTRIP_HOST = CTRIP_PAGE_HOST
CTRIP_SERVICE = "10098"
CTRIP_RECENT_OPERATION = "GetOrderWithBM"
CTRIP_HISTORY_OPERATION = "GetHistoryOrderList"
CTRIP_LEGACY_OPERATION = "getAllOrders"
CTRIP_RECENT_PATH = (
    f"/restapi/soa2/{CTRIP_SERVICE}/{CTRIP_RECENT_OPERATION}.json"
)
CTRIP_HISTORY_PATH = (
    f"/restapi/soa2/{CTRIP_SERVICE}/{CTRIP_HISTORY_OPERATION}.json"
)
CTRIP_LEGACY_PATH = (
    f"/restapi/soa2/{CTRIP_SERVICE}/{CTRIP_LEGACY_OPERATION}.json"
)
CTRIP_ENDPOINT = f"https://{CTRIP_API_HOST}{CTRIP_RECENT_PATH}"
CTRIP_HISTORY_ENDPOINT = f"https://{CTRIP_API_HOST}{CTRIP_HISTORY_PATH}"
CTRIP_LEGACY_ENDPOINT = f"https://{CTRIP_API_HOST}{CTRIP_LEGACY_PATH}"
CTRIP_REFERER = "https://my.ctrip.com/"
CTRIP_ORIGIN = "https://my.ctrip.com"
CTRIP_COOKIE_ORIGIN = "https://my.ctrip.com"
CTRIP_CSIGN_URL = (
    "https://static.tripcdn.com/packages/train/csign/^1.0.6/c-sign.js?expires=1d"
)
CTRIP_SIGNATURE_HEADER = "w-payload-source"

# Values observed in the live PC bundle runtime:
#   channel:"Online", clientVersion:"99.99", locale:"", userScrollTurnPage:false
CTRIP_CHANNEL = "Online"
CTRIP_CLIENT_VERSION = "99.99"
CTRIP_LOCALE = ""
CTRIP_HEAD_CVER = "1.0"
CTRIP_PAGE_SIZE = 15
CTRIP_HISTORY_PAGE_SIZE = 300
CTRIP_MAX_PAGE_SIZE = 300
CTRIP_DEFAULT_LANG = "01"
CTRIP_DEFAULT_SID = "8888"
CTRIP_SYSCODE = "09"
CTRIP_HISTORY_BEGIN = "2000-01-01 00:00:00"
BEIJING = datetime.timezone(datetime.timedelta(hours=8))

# ``cticket`` is the login ticket used by the session.  The PC page sends an
# empty ``head.auth`` and relies on the cookie, but the cookie itself is still
# required before any request is made.  GUID is the public client id.
CTRIP_LOGIN_COOKIES = ("cticket", "GUID", "_RF1", "DUID")
CTRIP_REQUIRED_LOGIN_COOKIES = ("cticket",)

# Ctrip serialises dates as ``/Date(1735689600000+0800)/``; the older H5 code
# also used ``/Date(1735689600000)/``.  Both forms are accepted.
EPOCH_RE = re.compile(r"/Date\((-?\d{10,13})(?:[+-]\d{4})?\)/")
DATETIME_RE = re.compile(
    r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?"
)
DATE_ONLY_RE = re.compile(r"^(20\d{2})[-/](\d{1,2})[-/](\d{1,2})$")

# The SOA response echoes the login ticket in ``Auth``.  That value is the
# same secret as the ``cticket`` cookie, so it must never be written to the
# output file (or printed).  Only the parsed order data is kept.
SENSITIVE_RESPONSE_KEYS = frozenset(
    ("auth", "cticket", "cookie", "set-cookie", "authorization")
)


@dataclass
class CtripRequest:
    url: str
    body: Dict
    headers: Dict[str, str] = field(default_factory=dict)
    kind: str = "recent"
    page_index: int = 1
    page_size: int = CTRIP_PAGE_SIZE

    @property
    def auth(self) -> str:
        head = self.body.get("head") if isinstance(self.body, dict) else {}
        return str((head or {}).get("auth") or "")

    def __repr__(self) -> str:  # never include the ticket
        return (
            f"CtripRequest(url={self.url!r}, kind={self.kind!r}, "
            f"page={self.page_index!r}, auth=<redacted>)"
        )


def new_sequence_id() -> str:
    """The page's ``genSequenceId()``: epoch milliseconds + random suffix."""

    return f"{int(time.time() * 1000)}{random.randint(0, 9999)}"


def build_head(
    cid: str,
    *,
    auth: str = "",
    sequence: Optional[str] = None,
    cver: str = CTRIP_HEAD_CVER,
    lang: str = CTRIP_DEFAULT_LANG,
    sid: str = CTRIP_DEFAULT_SID,
) -> Dict:
    """Build the PC page's ``head`` object.

    The live page sends ``auth:""`` even for a logged-in session; the cookie
    is the credential.  ``xsid`` is *not* present in the captured request.
    """

    head = {
        "cid": cid or "",
        "ctok": "",
        "cver": cver,
        "lang": lang,
        "sid": sid,
        "syscode": CTRIP_SYSCODE,
        "auth": auth or "",
        "extension": [{"name": "protocal", "value": "https"}],
    }
    if sequence:
        head["extension"].append({"name": "sequence", "value": str(sequence)})
    return head


def _validate_page(page_index: int, page_size: int) -> None:
    if page_index < 1:
        raise ValueError("page_index must be >= 1")
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    if page_size > CTRIP_MAX_PAGE_SIZE:
        raise ValueError("page_size must be <= %d" % CTRIP_MAX_PAGE_SIZE)


def build_recent_body(
    page_index: int,
    page_size: int,
    *,
    cid: str = "",
    auth: str = "",
    sequence: Optional[str] = None,
) -> Dict:
    """Body for ``GetOrderWithBM.json`` (the page's recent list)."""

    _validate_page(page_index, page_size)
    return {
        "head": build_head(cid, auth=auth, sequence=sequence),
        "PageIndex": int(page_index),
        "PageSize": int(page_size),
        "BizTypes": [],
        "FilterValidOrder": False,
        "BookingDateTime": "",
        "IsMergeTrainItem": True,
        "OrderStatusClassify": "All",
        "OrderIDs": "",
        "BeginBookingDateTime": "",
        "EndBookingDateTime": "",
        "FilterRequiredOrderIDArray": [],
        "NeedOrderAmountDetail": True,
        # Injected by the PC bundle's request wrapper from runtime config.
        "ClientVersion": CTRIP_CLIENT_VERSION,
        "Channel": CTRIP_CHANNEL,
        "Locale": CTRIP_LOCALE,
    }


def build_history_body(
    page_index: int,
    page_size: int,
    *,
    cid: str = "",
    auth: str = "",
    sequence: Optional[str] = None,
    begin_booking: str = CTRIP_HISTORY_BEGIN,
    end_booking: Optional[str] = None,
    previous_last_booking: str = "",
) -> Dict:
    """Body for ``GetHistoryOrderList.json`` (older-than-one-year search)."""

    _validate_page(page_index, page_size)
    if not end_booking:
        end_booking = datetime.datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "head": build_head(cid, auth=auth, sequence=sequence),
        "PageIndex": int(page_index),
        "PageSize": int(page_size),
        "BizTypes": [],
        "FilterValidOrder": False,
        "IsMergeTrainItem": True,
        "OrderStatusClassify": "All",
        "BeginBookingDateTime": begin_booking,
        "EndBookingDateTime": end_booking,
        "PreviousPageLastBookingDateTime": previous_last_booking,
        # The PC bundle injects these from its runtime config for every body.
        "ClientVersion": CTRIP_CLIENT_VERSION,
        "Channel": CTRIP_CHANNEL,
        "Locale": CTRIP_LOCALE,
        "OrderIDs": "",
    }


def build_body(
    page_index: int,
    page_size: int,
    *,
    cid: str = "",
    auth: str = "",
    kind: str = "recent",
    **kwargs,
) -> Dict:
    """Build a recent or history body; kept as one testable entry point."""

    if kind == "history":
        return build_history_body(
            page_index, page_size, cid=cid, auth=auth, **kwargs
        )
    if kind != "recent":
        raise ValueError("kind must be 'recent' or 'history'")
    return build_recent_body(page_index, page_size, cid=cid, auth=auth)


def build_headers() -> Dict[str, str]:
    """Headers sent by the real PC page (no signature header is generated)."""

    return {
        "Referer": CTRIP_REFERER,
        "Origin": CTRIP_ORIGIN,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        # Custom cross-origin header the PC bundle sets for cookie sessions.
        "cookieOrigin": CTRIP_COOKIE_ORIGIN,
    }


def build_request(
    page_index: int,
    page_size: int,
    *,
    cid: str = "",
    auth: str = "",
    kind: str = "recent",
    sequence: Optional[str] = None,
    **kwargs,
) -> CtripRequest:
    """Build one read-only POST request.  No signature header is generated."""

    if sequence is None:
        sequence = new_sequence_id()
    if kind == "history":
        url = CTRIP_HISTORY_ENDPOINT
        body = build_history_body(
            page_index,
            page_size,
            cid=cid,
            auth=auth,
            sequence=sequence,
            **kwargs,
        )
    elif kind == "recent":
        url = CTRIP_ENDPOINT
        body = build_recent_body(
            page_index, page_size, cid=cid, auth=auth, sequence=sequence
        )
    else:
        raise ValueError("kind must be 'recent' or 'history'")
    return CtripRequest(
        url=url,
        body=body,
        headers=build_headers(),
        kind=kind,
        page_index=int(page_index),
        page_size=int(page_size),
    )


def redact_response(value):
    """Remove the echoed login ticket before storing a raw response body."""

    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_RESPONSE_KEYS:
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = redact_response(item)
        return cleaned
    if isinstance(value, list):
        return [redact_response(item) for item in value]
    return value


def sanitized_raw(payload) -> str:
    """Serialise a response without cookie-equivalent values."""

    return json.dumps(redact_response(payload), ensure_ascii=False, separators=(",", ":"))


def _first_value(mapping: Dict, keys: Sequence[str]):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _parse_datetime(value) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        if number <= 0:
            return None
        if number < 10_000_000_000:
            number *= 1000
        try:
            return datetime.datetime.fromtimestamp(number / 1000, BEIJING).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    match = EPOCH_RE.search(text)
    if match:
        return _parse_datetime(int(match.group(1)))
    if text.isdigit() and len(text) in (10, 13):
        return _parse_datetime(int(text))
    match = DATETIME_RE.search(text)
    if not match:
        match = DATE_ONLY_RE.search(text)
        if not match:
            return None
        year, month, day = match.groups()
        try:
            return datetime.datetime(
                int(year), int(month), int(day), 0, 0, 0
            ).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    year, month, day, hour, minute, second = match.groups()
    try:
        return datetime.datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0)
        ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _amount(value) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        value = _first_value(value, ("Value", "value", "Amount", "amount"))
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    text = str(value).replace("¥", "").replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d{1,2})?", text)
    return match.group(0) if match else None


def classify_biz(biz_type: Optional[str]) -> str:
    lowered = str(biz_type or "").lower()
    if "flight" in lowered:
        return "flight"
    if "hotel" in lowered or "accommodation" in lowered:
        return "hotel"
    if "train" in lowered or "rail" in lowered:
        return "train"
    return "other"


def _nested_items(entity: Dict) -> List[Dict]:
    items: List[Dict] = []
    for key, value in entity.items():
        if key not in ("OrderItems", "Items") and not str(key).endswith("OrderItems"):
            continue
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            title = _first_value(
                item,
                (
                    "ProductName",
                    "ProductNameCN",
                    "HotelName",
                    "TrainNo",
                    "FlightNo",
                    "Name",
                    "Title",
                    "ItemName",
                ),
            )
            if title is None:
                continue
            quantity = _first_value(item, ("Quantity", "Count", "Num", "Amount"))
            try:
                quantity = int(quantity) if quantity is not None else None
            except (TypeError, ValueError):
                quantity = None
            items.append(
                {
                    "title": str(title),
                    "quantity": quantity,
                    "status": _first_value(item, ("OrderStatusName", "StatusName")),
                }
            )
    return items


def parse_order(entity) -> Optional[Dict]:
    if not isinstance(entity, dict):
        return None
    order_id = _first_value(
        entity, ("OrderID", "OrderId", "orderId", "MasterOrderId", "OrderNo", "ID")
    )
    if order_id is None:
        return None
    order_id = str(order_id)
    view = entity.get("ViewInfo") if isinstance(entity.get("ViewInfo"), dict) else {}
    occurred = _parse_datetime(
        _first_value(
            entity,
            (
                "BookingDate",
                "OrderDate",
                "CreateDate",
                "OrderTime",
                "BookingTime",
                "SubmitTime",
                "OrderCreateTime",
            ),
        )
    )
    if occurred is None:
        occurred = _parse_datetime(
            _first_value(view, ("BookingDate", "OrderDate", "CreateDate"))
        )
    biz_type = _first_value(entity, ("BizType", "BizTypeOrigin", "BusinessType")) or ""
    order_type = _first_value(entity, ("OrderType", "OrderTypeName"))
    category = classify_biz(f"{biz_type} {order_type or ''}")
    title = _first_value(
        entity, ("OrderName", "ProductName", "Title", "OrderTitle")
    ) or ""
    paid = _amount(
        _first_value(
            entity, ("OrderTotalPrice", "TotalPrice", "OrderAmount", "PayAmount")
        )
    )
    if paid is None:
        paid = _amount(_first_value(view, ("OrderTotalPrice", "TotalPrice")))
    status = _first_value(
        entity, ("OrderStatusName", "OrderStatus", "StatusName", "OrderStatusCode")
    )
    if status is None:
        status = _first_value(view, ("OrderStatusName", "OrderStatus"))
    currency = _first_value(view, ("CurrencyCode", "Currency")) or _first_value(
        entity, ("CurrencyCode", "Currency")
    )
    items = _nested_items(entity)
    if not title and items:
        title = items[0].get("title") or ""
    return {
        "order_id": order_id,
        "category": category,
        "biz_type": str(biz_type),
        "order_type": order_type,
        "title": str(title) if title else None,
        "occurred": occurred,
        "status": str(status) if status else None,
        "paid": paid,
        "currency": currency or "CNY",
        "items": items,
        "source": "ctrip",
    }


def extract_entities(payload) -> List[Dict]:
    if not isinstance(payload, dict):
        return []
    for key in ("OrderEnities", "OrderEntities", "Orders", "OrderList"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    data = payload.get("Data")
    if isinstance(data, dict):
        return extract_entities(data)
    return []


def total_count(payload) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for key in ("TotalCount", "Total", "TotalNum"):
        value = payload.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    result = payload.get("Result")
    if isinstance(result, dict):
        return total_count(result)
    return None


def result_summary(payload) -> Dict:
    result = payload.get("Result") if isinstance(payload.get("Result"), dict) else {}
    response = (
        payload.get("ResponseStatus")
        if isinstance(payload.get("ResponseStatus"), dict)
        else {}
    )
    return {
        "result_code": result.get("ResultCode"),
        "ack": response.get("Ack"),
    }


def missing_login_cookies(jar: CookieJar, host: str = CTRIP_HOST) -> List[str]:
    return [
        name
        for name in CTRIP_REQUIRED_LOGIN_COOKIES
        if not jar.get(name, host)
    ]


def login_cookie_presence(jar: CookieJar, host: str = CTRIP_HOST) -> Dict[str, bool]:
    return {name: bool(jar.get(name, host)) for name in CTRIP_LOGIN_COOKIES}


def require_login(jar: CookieJar, host: str = CTRIP_HOST) -> None:
    missing = missing_login_cookies(jar, host)
    if missing:
        raise AuthRequired(
            "浏览器中没有找到携程登录 Cookie: " + ", ".join(missing),
            "请在浏览器中打开 https://my.ctrip.com/myinfo/all 登录后重试；"
            "本工具不会代替你登录",
        )


class CtripFetcher:
    def __init__(
        self,
        client: HttpClient,
        jar: CookieJar,
        *,
        page_size: int = CTRIP_PAGE_SIZE,
        history_page_size: int = CTRIP_HISTORY_PAGE_SIZE,
        max_pages: int = 50,
        history_begin: str = CTRIP_HISTORY_BEGIN,
        history_end: Optional[str] = None,
    ):
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        if page_size > CTRIP_MAX_PAGE_SIZE:
            raise ValueError("page_size must be <= %d" % CTRIP_MAX_PAGE_SIZE)
        if history_page_size < 1:
            raise ValueError("history_page_size must be >= 1")
        if history_page_size > CTRIP_MAX_PAGE_SIZE:
            raise ValueError(
                "history_page_size must be <= %d" % CTRIP_MAX_PAGE_SIZE
            )
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        self.client = client
        self.jar = jar
        self.page_size = page_size
        self.history_page_size = history_page_size
        self.max_pages = max_pages
        self.history_begin = history_begin
        self.history_end = history_end
        self.pages: List[Dict] = []
        self.warnings: List[str] = []
        self.recent_total: Optional[int] = None
        self.history_total: Optional[int] = None

    def _cookie_header(self) -> str:
        # Browser sends only cookies whose domain/path match the API host.
        return self.jar.header_for(CTRIP_API_HOST)

    def _request_page(self, request: CtripRequest) -> Tuple[List[Dict], Optional[int], str, Dict]:
        response = self.client.post_json(
            request.url,
            request.body,
            cookie_header=self._cookie_header(),
            headers=request.headers,
        )
        raw = response.text
        if response.status in (430, 431, 432):
            check_ctrip_api(None, response.status)
        if "passport.ctrip.com" in response.url.lower():
            check_ctrip_html(raw, response.url, response.status)
        if response.status >= 400 and not raw.lstrip().startswith("{"):
            check_ctrip_html(raw, response.url, response.status)
            raise NetworkError(f"携程订单接口请求失败 (HTTP {response.status})")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            check_ctrip_html(raw, response.url, response.status)
            raise ApiError("携程订单接口返回了非 JSON 内容（页面结构可能已变化）") from exc
        if not isinstance(payload, dict):
            raise ApiError("携程订单接口返回了意外的 JSON 类型")
        check_ctrip_api(payload, response.status)
        return (
            extract_entities(payload),
            total_count(payload),
            # Never persist the echoed Auth/cticket value.
            sanitized_raw(payload),
            result_summary(payload),
        )

    def _page_record(
        self, request: CtripRequest, orders: List[Dict], total: Optional[int], raw: str, info: Dict
    ) -> Dict:
        return {
            "kind": request.kind,
            "endpoint": request.url,
            "page_index": request.page_index,
            "page_size": request.page_size,
            "offset": (request.page_index - 1) * request.page_size,
            "total": total,
            "orders": orders,
            "result": info,
            "raw": raw,
        }

    def _fetch_recent(self, seen: set) -> int:
        page_index = 1
        collected = 0
        while page_index <= self.max_pages:
            request = build_request(
                page_index,
                self.page_size,
                cid=self.jar.get("GUID", CTRIP_HOST) or "",
                kind="recent",
            )
            entities, page_total, raw, info = self._request_page(request)
            if page_total is not None:
                self.recent_total = page_total
            orders: List[Dict] = []
            for entity in entities:
                order = parse_order(entity)
                if order is None or order["order_id"] in seen:
                    continue
                seen.add(order["order_id"])
                orders.append(order)
            self.pages.append(
                self._page_record(request, orders, page_total or self.recent_total, raw, info)
            )
            if not entities or not orders:
                break
            collected += len(orders)
            if self.recent_total is not None and collected >= self.recent_total:
                break
            # A short page is a server-side cap only when TotalCount does not
            # prove that more rows exist.
            if len(entities) < self.page_size and (
                self.recent_total is None or collected >= self.recent_total
            ):
                break
            page_index += 1
        else:
            self.warnings.append(
                f"最近订单已达到单次最大页数 {self.max_pages}，可能尚未取完"
            )
        if self.recent_total is not None and collected < self.recent_total:
            self.warnings.append(
                f"携程最近订单接口报告共 {self.recent_total} 条，本次取到 {collected} 条"
            )
        return collected

    def _fetch_history(self, seen: set) -> int:
        page_index = 1
        collected = 0
        while page_index <= self.max_pages:
            request = build_request(
                page_index,
                self.history_page_size,
                cid=self.jar.get("GUID", CTRIP_HOST) or "",
                kind="history",
                begin_booking=self.history_begin,
                end_booking=self.history_end,
            )
            entities, _, raw, info = self._request_page(request)
            orders: List[Dict] = []
            for entity in entities:
                order = parse_order(entity)
                if order is None or order["order_id"] in seen:
                    continue
                seen.add(order["order_id"])
                orders.append(order)
            # History has no TotalCount; do not pretend the page length is a total.
            self.pages.append(
                self._page_record(request, orders, None, raw, info)
            )
            if not entities or not orders:
                break
            collected += len(orders)
            if len(entities) < self.history_page_size:
                break
            page_index += 1
        else:
            self.warnings.append(
                f"历史订单已达到单次最大页数 {self.max_pages}，可能尚未取完"
            )
        self.history_total = collected
        return collected

    def fetch(self) -> Dict:
        require_login(self.jar)
        seen = set()
        self._fetch_recent(seen)
        self._fetch_history(seen)
        return {
            "kind": "ctrip_orders",
            "source": CTRIP_ENDPOINT,
            "endpoints": [CTRIP_ENDPOINT, CTRIP_HISTORY_ENDPOINT],
            "channel": CTRIP_CHANNEL,
            "pages": self.pages,
        }

    def probe(self) -> Dict:
        """One lightweight read-only request for status/keepalive."""

        require_login(self.jar)
        request = build_request(
            1,
            1,
            cid=self.jar.get("GUID", CTRIP_HOST) or "",
            kind="recent",
        )
        entities, total, _, info = self._request_page(request)
        return {
            "orders": len(entities),
            "total": total,
            "result_code": info.get("result_code"),
            "ack": info.get("ack"),
        }


def date_range(orders: Iterable[Dict]) -> Optional[Tuple[str, str]]:
    dates = [order.get("occurred") for order in orders if order.get("occurred")]
    if not dates:
        return None
    return min(dates), max(dates)


def summarize(payload: Dict) -> Dict:
    orders: List[Dict] = []
    seen = set()
    by_source = {"recent": 0, "history": 0}
    for page in payload.get("pages") or []:
        kind = page.get("kind") or "recent"
        for order in page.get("orders") or []:
            if order.get("order_id") in seen:
                continue
            seen.add(order.get("order_id"))
            orders.append(order)
            by_source[kind] = by_source.get(kind, 0) + 1
    by_type = {"flight": 0, "hotel": 0, "train": 0, "other": 0}
    for order in orders:
        category = order.get("category") or "other"
        by_type[category] = by_type.get(category, 0) + 1
    return {
        "pages": len(payload.get("pages") or []),
        "orders": len(orders),
        "date_range": date_range(orders),
        "by_type": by_type,
        "by_source": by_source,
    }
