"""Didi (滴滴出行) trip history reader.

Only three read-only endpoints are used, all recovered from the public web
bundle:

* ``GET https://common.diditaxi.com.cn/passenger/history`` -- order list
* ``GET https://api.udache.com/gulfstream/passenger/v2/core/pOrderDetail``
* ``GET https://api.udache.com/gulfstream/passenger/v2/other/pGetRealTimeFeeDetail``

The list endpoint authenticates with a ``token`` query parameter.  The token
can come from ``PFIN_DIDI_TOKEN`` or, without owner pasting, from the
browser session cookie ``ticket`` (written by the web app's js-cookie code on
``common.diditaxi.com.cn``).  The current bundle signs *every* axios request
with ``wsgsig`` (the sign whitelist contains ``"*"``), so an unsigned list
request is rejected with HTTP 501.  This module never computes, forges or
replays ``wsgsig``; if the server demands it, :class:`NeedsRiskSignature` is
raised and the CLI says so.
"""

from __future__ import annotations

import datetime
import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from .blocking import check_didi_html, check_didi_payload
from ..kit.cookies import CookieJar
from ..kit.errors import (
    ApiError,
    AuthRequired,
    NeedsRiskSignature,
    ConnectorError,
    RiskBlocked,
)
from ..kit.env import getenv
from ..kit.http import HttpClient, Response

DIDI_WEB_ORIGIN = "https://common.diditaxi.com.cn"
DIDI_WEB_ENTRY = DIDI_WEB_ORIGIN + "/general/webEntry"
DIDI_HISTORY_ENDPOINT = DIDI_WEB_ORIGIN + "/passenger/history"
DIDI_API_ORIGIN = "https://api.udache.com"
DIDI_ORDER_DETAIL_ENDPOINT = (
    DIDI_API_ORIGIN + "/gulfstream/passenger/v2/core/pOrderDetail"
)
DIDI_FARE_DETAIL_ENDPOINT = (
    DIDI_API_ORIGIN
    + "/gulfstream/passenger/v2/other/pGetRealTimeFeeDetail"
)
DIDI_BILL_DETAIL_ENDPOINT = (
    DIDI_API_ORIGIN + "/gulfstream/post-sale/v1/other/pGetBillDetail"
)

DIDI_TOKEN_ENV = "PFIN_DIDI_TOKEN"
DIDI_PHONE_ENV = "PFIN_DIDI_PHONE"
DIDI_OPENID_ENV = "PFIN_DIDI_OPENID"
DIDI_CHANNEL_ENV = "PFIN_DIDI_CHANNEL"
DIDI_APPVERSION_ENV = "PFIN_DIDI_APPVERSION"

DIDI_APPVERSION = "6.0.19"
DIDI_CLIENT_TYPE = "201"
DIDI_ACCESS_KEY_ID = "37"
DIDI_DEFAULT_BUSINESS_ID = "260"
DIDI_REFERER = DIDI_WEB_ENTRY
BEIJING = datetime.timezone(datetime.timedelta(hours=8))

# Cookie domains inspected on the owner's browser profile.  The public web bundle
# writes the session ticket with js-cookie (``ticket`` on the webEntry host)
# and the trinity-login SDK keeps its own ``didih5_trinity_login_ticket_*``
# cookie.  Both are candidates for the query token used by /passenger/history.
DIDI_COOKIE_HOST_SUFFIXES = ("diditaxi.com.cn", "xiaojukeji.com", "udache.com")
DIDI_COOKIE_HOSTS = (
    "common.diditaxi.com.cn",
    "api.udache.com",
    "api.xiaojukeji.com",
)
DIDI_TOKEN_COOKIE_NAMES = (
    "ticket",
    "didih5_trinity_login_ticket_role_30004",
    "didih5_trinity_login_ticket",
    "token",
    "didi_token",
    "passenger_token",
    "access_token",
    "authorization",
)

_REDACT_KEYS = {
    "token",
    "access_token",
    "accesstoken",
    "ticket",
    "authorization",
    "cookie",
}
_TOKEN_QUERY_RE = re.compile(
    r"([?&](?:token|access_token|ticket)=)[^&#]*", re.IGNORECASE
)


def token_from_env(environ: Optional[Dict[str, str]] = None) -> str:
    """Read ``PFIN_DIDI_TOKEN`` without ever echoing it."""

    token = (getenv(DIDI_TOKEN_ENV, environ) or "").strip()
    if not token:
        raise AuthRequired(
            f"缺少滴滴登录令牌（环境变量 {DIDI_TOKEN_ENV}）",
            "在浏览器中登录 https://common.diditaxi.com.cn/general/webEntry，"
            "然后直接运行 didi fetch（默认读取 ticket Cookie）；"
            f"或把页面 token 放进环境变量 {DIDI_TOKEN_ENV}。"
            "本工具不会打印或写入 token/cookie 值",
        )
    return token


def _env(environ: Optional[Dict[str, str]], name: str) -> str:
    return (getenv(name, environ) or "").strip()


@dataclass
class TokenCookie:
    """A cookie that may carry the Didi session ticket.

    Its repr never contains the value, so it is safe to print in status
    output and in test failures.
    """

    name: str
    host_key: str
    value: str

    def __repr__(self) -> str:  # never include the value
        return (
            f"TokenCookie(name={self.name!r}, host_key={self.host_key!r})"
        )


def decode_cookie_token(value: str) -> str:
    """Undo js-cookie's URI encoding without changing ordinary tokens."""

    text = (value or "").strip()
    if not text:
        return ""
    if re.search(r"%[0-9A-Fa-f]{2}", text):
        decoded = urllib.parse.unquote(text)
        if decoded:
            return decoded.strip()
    return text


def _token_candidate_names(jar: CookieJar) -> List[str]:
    names = list(DIDI_TOKEN_COOKIE_NAMES)
    for cookie in jar.entries():
        lowered = cookie.name.lower()
        if (
            ("ticket" in lowered or "token" in lowered)
            and cookie.name not in names
        ):
            names.append(cookie.name)
    return names


def find_token_cookie(jar: CookieJar) -> Optional[TokenCookie]:
    """Find the most likely Didi session cookie, preferring known names.

    The value is returned to the caller only; this function never logs it.
    """

    for name in _token_candidate_names(jar):
        for host in DIDI_COOKIE_HOSTS:
            cookie = jar.get_cookie(name, host)
            if cookie is not None and cookie.value:
                return TokenCookie(
                    cookie.name, cookie.host_key, decode_cookie_token(cookie.value)
                )
        cookie = jar.get_cookie(name)
        if cookie is not None and cookie.value:
            return TokenCookie(
                cookie.name, cookie.host_key, decode_cookie_token(cookie.value)
            )
    return None


def request_context(environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    return {
        "phone": _env(environ, DIDI_PHONE_ENV),
        "openid": _env(environ, DIDI_OPENID_ENV),
        "channel": _env(environ, DIDI_CHANNEL_ENV),
        "appversion": _env(environ, DIDI_APPVERSION_ENV) or DIDI_APPVERSION,
    }


def build_history_params(
    page: int,
    timemode: str,
    token: str,
    *,
    phone: str = "",
    openid: str = "",
    channel: str = "",
    appversion: str = DIDI_APPVERSION,
    now_ms: Optional[int] = None,
) -> Dict[str, str]:
    """Exactly the query object built by the order-list component.

    The bundle sends ``pagenum`` (0-based), ``timemode`` (cursor), the fixed
    ``datatype=webapp``/``apiver=2.0.0`` pair, a millisecond timestamp, and
    the session fields.  ``token`` is the credential; the browser also sends
    its same-host ``ticket`` cookie, which the fetcher mirrors when available.
    """

    timestamp = now_ms
    if timestamp is None:
        timestamp = int(
            datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000
        )
    return {
        "pagenum": str(int(page)),
        "timemode": timemode or "",
        "datatype": "webapp",
        "apiver": "2.0.0",
        "timestamp": str(int(timestamp)),
        "token": token,
        "phone": phone,
        "appversion": appversion,
        "channel": channel,
        "openid": openid,
        "daijia_token": "",
        "daijia_pid": "",
    }


def _order_id(order: Dict) -> str:
    for key in ("orderId", "order_id", "oid", "order"):
        value = order.get(key)
        if isinstance(value, dict):
            value = value.get("order_id") or value.get("oid") or value.get("orderId")
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _product_id(order: Dict) -> str:
    value = order.get("productId") or order.get("product_id") or order.get("business_id")
    if value is None:
        return DIDI_DEFAULT_BUSINESS_ID
    text = str(value).strip()
    return text or DIDI_DEFAULT_BUSINESS_ID


def build_detail_params(
    order: Dict,
    token: str,
    *,
    appversion: str = DIDI_APPVERSION,
    channel: str = "",
    access_key_id: str = DIDI_ACCESS_KEY_ID,
    city_id: str = "",
) -> Dict[str, str]:
    """Common params from the page's store ``request`` action plus oid."""

    oid = _order_id(order)
    business_id = _product_id(order)
    return {
        "business_id": business_id,
        "origin_id": "1",
        "token": token,
        "order_id": oid,
        "app_version": appversion,
        "client_type": DIDI_CLIENT_TYPE,
        "map_type": "soso",
        "channel": channel,
        "lang": "zh-CN",
        "city_id": city_id,
        "from": "",
        "oid": oid,
        "appversion": appversion,
        "access_key_id": access_key_id,
        "booking_assign_timeout": "1",
        "nginx_cors": "false",
    }


def build_fare_params(
    order: Dict,
    token: str,
    *,
    access_key_id: str = DIDI_ACCESS_KEY_ID,
) -> Dict[str, str]:
    """Params used by view-details-v2 for pGetRealTimeFeeDetail."""

    return {
        "order_id": _order_id(order),
        "token": token,
        "nginx_cors": "false",
        "lang": "zh-CN",
        "from": "",
        "business_id": _product_id(order),
        "access_key_id": access_key_id,
    }


def _timestamp(value) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().isdigit()
    ):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
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
    match = re.search(
        r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?",
        text,
    )
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        return datetime.datetime(
            int(year), int(month), int(day), int(hour), int(minute),
            int(second or 0),
        ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


_AMOUNT_RE = re.compile(r"-?\d+(?:\.\d{1,2})?")


def _normalize_amount(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("value", "text", "amount", "fee", "price"):
            if key in value:
                amount = _normalize_amount(value.get(key))
                if amount:
                    return amount
        return None
    text = re.sub(r"<[^>]+>", "", str(value))
    text = text.replace("¥", "").replace("￥", "").replace("元", "")
    text = text.replace(",", "").strip()
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    amount = match.group(0)
    if amount in ("-0", "-0.0", "-0.00"):
        return "0"
    return amount


def _find_fare(value) -> Optional[str]:
    """Find the most likely paid amount in an order detail or fare payload.

    ``real_pay.total_fee`` (actual paid) wins over ``need_pay.total_fee``
    (original amount), then a direct ``total_fee``/``should_pay_fee``.  The
    recursive search skips discount and payment-method children so a small
    discount line is never mistaken for the trip fare.
    """

    if isinstance(value, dict):
        for key in ("real_pay", "need_pay"):
            child = value.get(key)
            if isinstance(child, dict):
                amount = _normalize_amount(child.get("total_fee"))
                if amount:
                    return amount
        for key in (
            "total_fee",
            "totalFee",
            "should_pay_fee",
            "actual_fee",
            "pay_price",
            "fee",
        ):
            amount = _normalize_amount(value.get(key))
            if amount:
                return amount
        bill = value.get("bill_basic")
        if isinstance(bill, dict):
            amount = _normalize_amount(
                bill.get("should_pay_fee") or bill.get("total_fee")
            )
            if amount:
                return amount
        for key, child in value.items():
            if key in (
                "discount",
                "discounts",
                "intros",
                "payment_info",
                "pay_details",
                "sub_items",
            ):
                continue
            amount = _find_fare(child)
            if amount:
                return amount
        return None
    if isinstance(value, list):
        for child in value:
            amount = _find_fare(child)
            if amount:
                return amount
    return None


def parse_order_item(item: Dict) -> Optional[Dict]:
    if not isinstance(item, dict):
        return None
    oid = _order_id(item)
    if not oid:
        return None
    scene = item.get("scene")
    if isinstance(scene, str):
        scenes = [scene]
    elif isinstance(scene, list):
        scenes = [str(part) for part in scene if part]
    else:
        scenes = []
    occurred = _timestamp(
        item.get("setuptimestamp")
        or item.get("setupTimestamp")
        or item.get("order_time")
        or item.get("create_time")
    )
    fare = _find_fare(item)
    return {
        "order_id": oid,
        "occurred": occurred,
        "product_id": _product_id(item),
        "product_name": item.get("product_name") or item.get("productName"),
        "status": item.get("hisstatus") or item.get("status"),
        "from": item.get("fromAddress") or item.get("from_address"),
        "to": item.get("toAddress") or item.get("to_address"),
        "scene": scenes,
        "class_type": item.get("classtype"),
        "fare": fare,
        "source": "history",
    }


def _iter_order_items(payload: Dict) -> Iterable[Dict]:
    if not isinstance(payload, dict):
        return []
    containers = [payload]
    if isinstance(payload.get("data"), dict):
        containers.append(payload["data"])
    items: List[Dict] = []
    for container in containers:
        for key in ("order_waiting", "order_done", "orders", "list"):
            value = container.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
    return items


def parse_order_list(payload: Dict) -> List[Dict]:
    orders: List[Dict] = []
    seen = set()
    for item in _iter_order_items(payload):
        order = parse_order_item(item)
        if order is None or order["order_id"] in seen:
            continue
        seen.add(order["order_id"])
        orders.append(order)
    return orders


def _truthy(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text not in ("0", "false", "no", "n", "")


def redact_secret(value, secret: str):
    """Recursively remove the session token before it can be printed/saved.

    Raw server payloads and order URLs are untrusted; this is a second line of
    defence on top of never logging request URLs.
    """

    if not secret:
        return value
    quoted = urllib.parse.quote(secret, safe="")
    if isinstance(value, str):
        text = value.replace(secret, "[REDACTED]")
        if quoted and quoted != secret:
            text = text.replace(quoted, "[REDACTED]")
        return _TOKEN_QUERY_RE.sub(r"\1[REDACTED]", text)
    if isinstance(value, list):
        return [redact_secret(item, secret) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secret(item, secret) for item in value)
    if isinstance(value, dict):
        output = {}
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in _REDACT_KEYS:
                output[key] = "[REDACTED]"
            else:
                output[key] = redact_secret(child, secret)
        return output
    return value


def date_range(orders: Iterable[Dict]) -> Optional[Tuple[str, str]]:
    dates = [order.get("occurred") for order in orders if order.get("occurred")]
    if not dates:
        return None
    return min(dates), max(dates)


class DidiFetcher:
    """Fetch the read-only trip list, then optional per-order detail/fare."""

    def __init__(
        self,
        client: HttpClient,
        token: str,
        *,
        jar: Optional[CookieJar] = None,
        token_source: str = "env",
        phone: str = "",
        openid: str = "",
        channel: str = "",
        appversion: str = DIDI_APPVERSION,
        max_pages: int = 50,
        with_detail: bool = True,
        with_fare: bool = True,
    ):
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if not token:
            raise AuthRequired(
                f"缺少滴滴登录令牌（环境变量 {DIDI_TOKEN_ENV} 或浏览器 Cookie）"
            )
        self.client = client
        self.token = token
        self.jar = jar
        self.token_source = token_source
        self.phone = phone
        self.openid = openid
        self.channel = channel
        self.appversion = appversion
        self.max_pages = max_pages
        self.with_detail = with_detail
        self.with_fare = with_fare
        self.pages: List[Dict] = []
        self.orders: List[Dict] = []
        self.warnings: List[str] = []
        self.needs_risk_signature = False
        self._seen_orders = set()

    # -- transport ---------------------------------------------------------

    def _scrub_error(self, exc: ConnectorError) -> None:
        quoted = urllib.parse.quote(self.token, safe="")

        def clean(value: str) -> str:
            value = value.replace(self.token, "[REDACTED]")
            if quoted and quoted != self.token:
                value = value.replace(quoted, "[REDACTED]")
            return value

        for attr in ("message", "hint"):
            value = getattr(exc, attr, None)
            if isinstance(value, str):
                setattr(exc, attr, clean(value))
        # Exception.__str__ reads args, not self.message; scrub both so a
        # traceback can never echo the session token.
        if exc.args:
            exc.args = tuple(
                clean(value) if isinstance(value, str) else value
                for value in exc.args
            )

    def _cookie_header(self, endpoint: str) -> str:
        if self.jar is None:
            return ""
        host = urllib.parse.urlsplit(endpoint).hostname or ""
        return self.jar.header_for(host) if host else ""

    def _get(self, endpoint: str, params: Dict[str, str]) -> Response:
        query = urllib.parse.urlencode(params)
        url = endpoint + ("&" if "?" in endpoint else "?") + query
        try:
            return self.client.get(
                url,
                # The browser sends its Cookie header automatically; sending
                # the matching browser cookies keeps both auth paths available.
                cookie_header=self._cookie_header(endpoint),
                headers={
                    "Referer": DIDI_REFERER,
                    "Origin": DIDI_WEB_ORIGIN,
                    "Accept": "application/json, text/plain, */*",
                },
            )
        except ConnectorError as exc:
            self._scrub_error(exc)
            raise

    def _decode(self, response: Response) -> Dict:
        text = response.text
        if not text or not text.strip():
            raise ApiError("滴滴接口返回了空响应")
        if text.lstrip().startswith("<"):
            check_didi_html(text, response.url, response.status)
            raise ApiError("滴滴接口返回了 HTML 而不是 JSON")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            check_didi_html(text, response.url, response.status)
            raise ApiError("滴滴接口返回了非 JSON 内容") from exc
        if not isinstance(payload, dict):
            raise ApiError("滴滴接口返回了非 JSON 对象")
        try:
            check_didi_payload(payload, response.status)
        except ConnectorError as exc:
            self._scrub_error(exc)
            raise
        return payload

    # -- list --------------------------------------------------------------

    def _fetch_list(self) -> None:
        page = 0
        timemode = ""
        seen_positions = set()
        while page < self.max_pages:
            position = (page, timemode)
            if position in seen_positions:
                break
            seen_positions.add(position)
            request_timemode = timemode
            params = build_history_params(
                page,
                request_timemode,
                self.token,
                phone=self.phone,
                openid=self.openid,
                channel=self.channel,
                appversion=self.appversion,
            )
            response = self._get(DIDI_HISTORY_ENDPOINT, params)
            payload = self._decode(response)
            items = list(_iter_order_items(payload))
            page_orders: List[Dict] = []
            for item in items:
                order = parse_order_item(item)
                if order is None:
                    continue
                if order["order_id"] in self._seen_orders:
                    continue
                self._seen_orders.add(order["order_id"])
                self.orders.append(order)
                page_orders.append(order)
            record = {
                "pagenum": page,
                "timemode": request_timemode,
                "orders": page_orders,
                "response": payload,
            }
            self.pages.append(record)

            # New "timemode" protocol: the server hands out a cursor and a
            # havenext flag.  Older responses have no timemode and simply
            # increment pagenum until order_waiting/order_done are both empty.
            next_timemode = payload.get("timemode")
            if isinstance(next_timemode, str) and next_timemode:
                record["next_timemode"] = next_timemode
                record["havenext"] = payload.get("havenext")
                record["mhavenext"] = payload.get("mhavenext")
                timemode = next_timemode
                if not _truthy(payload.get("havenext")):
                    break
                if not _truthy(payload.get("mhavenext")):
                    page = 0
                else:
                    page += 1
                continue

            if not items:
                break
            if not page_orders:
                # Legacy protocol repeated the same page; stop instead of
                # walking forever.
                break
            page += 1
        else:
            self.warnings.append(
                f"已达到单次最大页数 {self.max_pages}，滴滴行程可能尚未取完"
            )

    # -- per-order detail / fare ------------------------------------------

    def _enrich(self) -> None:
        for order in self.orders:
            if self.needs_risk_signature:
                break
            if self.with_detail:
                try:
                    detail = self._request_detail(order)
                except NeedsRiskSignature as exc:
                    self.needs_risk_signature = True
                    self.warnings.append(
                        "滴滴详情接口要求 wsgsig 风险签名，已停止请求详情/费用；"
                        f"行程列表仍已保存（{exc.message}）"
                    )
                    break
                except (AuthRequired, RiskBlocked):
                    raise
                except ApiError as exc:
                    self.warnings.append(
                        f"订单 {order['order_id']} 详情获取失败: {exc.message}"
                    )
                else:
                    order["detail"] = detail
                    fare = _find_fare(detail)
                    if fare:
                        order["fare"] = fare

            if not self.with_fare or order.get("fare"):
                continue
            try:
                fare_payload = self._request_fare(order)
            except NeedsRiskSignature as exc:
                self.needs_risk_signature = True
                self.warnings.append(
                    "滴滴费用接口要求 wsgsig 风险签名，已停止请求费用；"
                    f"行程列表仍已保存（{exc.message}）"
                )
                break
            except (AuthRequired, RiskBlocked):
                raise
            except ApiError as exc:
                self.warnings.append(
                    f"订单 {order['order_id']} 费用获取失败: {exc.message}"
                )
                continue
            order["fare_detail"] = fare_payload
            fare = _find_fare(fare_payload)
            if fare:
                order["fare"] = fare

    def _request_detail(self, order: Dict) -> Dict:
        params = build_detail_params(
            order,
            self.token,
            appversion=self.appversion,
            channel=self.channel,
        )
        return self._decode(self._get(DIDI_ORDER_DETAIL_ENDPOINT, params))

    def _request_fare(self, order: Dict) -> Dict:
        params = build_fare_params(order, self.token)
        return self._decode(self._get(DIDI_FARE_DETAIL_ENDPOINT, params))

    # -- public ------------------------------------------------------------

    def fetch(self) -> Dict:
        self._fetch_list()
        if self.with_detail or self.with_fare:
            self._enrich()
        payload: Dict = {
            "kind": "didi_trips",
            "token_source": self.token_source,
            "pages": self.pages,
        }
        if self.needs_risk_signature:
            payload["risk_signature_required"] = True
        return redact_secret(payload, self.token)


def summarize(payload: Dict) -> Dict:
    orders: List[Dict] = []
    seen = set()
    # Browser mode emits a normalized top-level ``trips`` list; HTTP mode
    # keeps the legacy ``pages[].orders`` shape.
    source = payload.get("trips")
    if isinstance(source, list):
        for order in source:
            if not isinstance(order, dict):
                continue
            oid = order.get("order_id")
            if oid in seen:
                continue
            seen.add(oid)
            orders.append(order)
    else:
        for page in payload.get("pages") or []:
            for order in page.get("orders") or []:
                oid = order.get("order_id")
                if oid in seen:
                    continue
                seen.add(oid)
                orders.append(order)
    span = date_range(orders)
    with_fare = sum(1 for order in orders if order.get("fare"))
    return {
        "pages": len(payload.get("pages") or []),
        "orders": len(orders),
        "with_fare": with_fare,
        "date_range": span,
    }
