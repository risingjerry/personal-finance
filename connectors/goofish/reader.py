"""Goofish (闲鱼) bought and sold orders, read-only.

The request format is mtop, and the site loads the same ``lib-mtop`` used by
Taobao.  The public bundles show:

* bought orders: ``mtop.idle.web.trade.bought.list`` v1.0 on
  ``https://www.goofish.com/bought``;
* sold orders: ``mtop.taobao.idle.trade.merchant.sold.get`` v1.0 in the public
  seller-workbench bundle.

Both use appKey ``34839810``, mainDomain ``goofish.com`` and the standard
``md5(token + "&" + t + "&" + appKey + "&" + data)`` mtop signature.  This
module reuses the Taobao signing / token-refresh helpers and never sends a
write, cancel, refund, repay, transfer, invoice or payment request.
"""

from __future__ import annotations

import datetime
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .blocking import check_goofish_html, check_goofish_ret
from ..kit.cookies import CookieJar
from ..kit.errors import ApiError, AuthRequired
from ..kit.http import HttpClient, Response
from ..taobao.reader import (
    canonical_json,
    is_token_error,
    mtop_sign,
    refresh_tokens_from_response as _taobao_refresh_tokens,
    ret_text,
)

GOOFISH_HOST = "www.goofish.com"
GOOFISH_API_HOST = "h5api.m.goofish.com"
GOOFISH_APP_KEY = "34839810"
GOOFISH_JSV = "2.7.2"
GOOFISH_BOUGHT_API = "mtop.idle.web.trade.bought.list"
GOOFISH_BOUGHT_VERSION = "1.0"
GOOFISH_SOLD_API = "mtop.taobao.idle.trade.merchant.sold.get"
GOOFISH_SOLD_VERSION = "1.0"
GOOFISH_NAV_API = "mtop.idle.web.user.page.nav"
GOOFISH_NAV_VERSION = "1.0"
GOOFISH_BOUGHT_ENDPOINT = (
    f"https://{GOOFISH_API_HOST}/h5/{GOOFISH_BOUGHT_API}/{GOOFISH_BOUGHT_VERSION}/"
)
GOOFISH_SOLD_ENDPOINT = (
    f"https://{GOOFISH_API_HOST}/h5/{GOOFISH_SOLD_API}/{GOOFISH_SOLD_VERSION}/"
)
GOOFISH_NAV_ENDPOINT = (
    f"https://{GOOFISH_API_HOST}/h5/{GOOFISH_NAV_API}/{GOOFISH_NAV_VERSION}/"
)
GOOFISH_BOUGHT_REFERER = "https://www.goofish.com/bought"
GOOFISH_SELLER_REFERER = "https://seller.goofish.com/"
BEIJING = datetime.timezone(datetime.timedelta(hours=8))

TOKEN_COOKIE = "_m_h5_tk"
TOKEN_ENC_COOKIE = "_m_h5_tk_enc"

# The Goofish web login (lib-login, site="xianyu") and the auto-login bundle
# read `unb`; `cookie2` is the session cookie passed to mtop.  `_m_h5_tk` is
# the optional mtop signing token (auto-bootstrapped), not a login prerequisite.
GOOFISH_LOGIN_COOKIES = ("cookie2", "unb", "_m_h5_tk")
GOOFISH_REQUIRED_LOGIN_COOKIES = ("cookie2", "unb")

# One request plus at most one token-refresh retry, matching the Taobao reader.
MAX_PAGE_ATTEMPTS = 2
DEFAULT_ROWS_PER_PAGE = 20

BOUGHT_STATUSES = ("ALL", "NOT_PAY", "NOT_SHIP", "SHIPPED", "NOT_RATE", "REFUND")


@dataclass
class GoofishRequest:
    api: str
    version: str
    url: str
    form: Dict[str, str]
    data_json: str
    sign: str
    timestamp_ms: int
    app_key: str = GOOFISH_APP_KEY
    token: str = field(default="", repr=False)


def build_bought_data(
    page: int, order_status: str = "ALL", offset_row=None
) -> Dict:
    """The exact data object used by the public /bought bundle."""

    data: Dict = {
        "pageNumber": int(page),
        "orderStatus": order_status or "ALL",
    }
    # JSON.stringify omits undefined; the first page has no offsetRow.
    if offset_row is not None and offset_row != "":
        data["offsetRow"] = offset_row
    return data


def build_sold_data(
    page: int,
    query_code: str = "ALL",
    *,
    rows_per_page: int = DEFAULT_ROWS_PER_PAGE,
    order_ids: Optional[str] = None,
) -> Dict:
    """The list-page data object used by the seller-workbench bundle.

    The public bundle's IM card passes ``sessionId``/``sessionType`` too; the
    seller order page call is not in the IM component.  They are session
    routing hints, not authentication, and are omitted here.  If the backend
    requires them the request fails with a clear API error rather than a
    guessed value.
    """

    data: Dict = {
        "queryCode": query_code or "ALL",
        "pageNumber": int(page),
        "rowsPerPage": int(rows_per_page),
    }
    if order_ids:
        data["orderIds"] = str(order_ids)
    return data


def build_request(
    api: str,
    version: str,
    data: Dict,
    token: str,
    *,
    user_agent: str,
    timestamp_ms: Optional[int] = None,
    app_key: str = GOOFISH_APP_KEY,
    value_type: Optional[str] = "string",
) -> GoofishRequest:
    """Build an mtop request with the Taobao/lib-mtop signature.

    ``value_type="string"`` makes lib-mtop send ``type=json`` and add
    ``valueType=string``; the order endpoints use it.  The user-nav call does
    not, so it keeps lib-mtop's default ``type=originaljson``.
    """

    if timestamp_ms is None:
        timestamp_ms = int(
            datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000
        )
    data_json = canonical_json(data)
    sign = mtop_sign(token, timestamp_ms, app_key, data_json)
    query = [
        ("jsv", GOOFISH_JSV),
        ("appKey", app_key),
        ("t", str(timestamp_ms)),
        ("sign", sign),
        ("api", api),
        ("v", version),
        ("type", "json" if value_type == "string" else "originaljson"),
        ("accountSite", "xianyu"),
        ("dataType", "json"),
        ("timeout", "20000"),
        ("sessionOption", "AutoLoginOnly"),
    ]
    if value_type:
        query.append(("valueType", value_type))
    endpoint = f"https://{GOOFISH_API_HOST}/h5/{api.lower()}/{version}/"
    return GoofishRequest(
        api=api,
        version=version,
        url=endpoint + "?" + urllib.parse.urlencode(query),
        form={"data": data_json, "ua": user_agent},
        data_json=data_json,
        sign=sign,
        timestamp_ms=timestamp_ms,
        app_key=app_key,
        token=token,
    )


def missing_login_cookies(jar: CookieJar, host: str = GOOFISH_HOST) -> List[str]:
    """Names of required Goofish login cookies absent from the jar.

    ``_m_h5_tk`` is not required: lib-mtop bootstraps it with an empty-token
    request, and this reader does the same.
    """

    return [
        name
        for name in GOOFISH_REQUIRED_LOGIN_COOKIES
        if not jar.get(name, host)
    ]


def login_cookie_presence(
    jar: CookieJar, host: str = GOOFISH_HOST
) -> Dict[str, bool]:
    return {name: bool(jar.get(name, host)) for name in GOOFISH_LOGIN_COOKIES}


def require_login(jar: CookieJar, host: str = GOOFISH_HOST) -> None:
    missing = missing_login_cookies(jar, host)
    if missing:
        raise AuthRequired(
            "浏览器中没有找到闲鱼登录 Cookie: " + ", ".join(missing),
            "请在浏览器中打开 https://www.goofish.com/ 并扫码登录后重试；"
            "_m_h5_tk 会由工具自动刷新，不需要手动处理",
        )


def refresh_tokens_from_response(jar: CookieJar, response: Response) -> bool:
    """Apply rotated Goofish tokens through the shared cookie/session layer."""

    return _taobao_refresh_tokens(jar, response, default_host=GOOFISH_HOST)


def _text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _count(value) -> Optional[int]:
    """Coerce the nav API's numeric counts, which may arrive as strings."""

    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _amount(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value") or value.get("price")
    text = str(value).replace("¥", "").replace(",", "").replace("￥", "").strip()
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d{1,2})?", text)
    return match.group(0) if match else None


_DATETIME_RE = re.compile(
    r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?"
)


def _datetime_text(value) -> Optional[str]:
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
            return datetime.datetime.fromtimestamp(
                number / 1000, BEIJING
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return None
    match = _DATETIME_RE.search(str(value))
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


def _items(payload, side: str) -> List[Dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    container = data
    if side == "sold":
        module = data.get("module")
        if isinstance(module, dict):
            container = module
        elif isinstance(data.get("data"), dict):
            container = data["data"]
    items = container.get("items")
    if isinstance(items, list):
        return items
    nested = data.get("data")
    if side == "sold" and isinstance(nested, dict):
        nested_items = nested.get("items")
        if isinstance(nested_items, list):
            return nested_items
    return []


def _next_page(payload, side: str) -> bool:
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    container = data
    if side == "sold" and isinstance(data.get("module"), dict):
        container = data["module"]
    if "nextPage" not in container and side == "sold" and isinstance(data.get("data"), dict):
        container = data["data"]
    return str(container.get("nextPage", "")).lower() == "true"


def _last_end_row(payload) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    value = data.get("lastEndRow")
    if value is None:
        return None
    text = str(value)
    return text or None


def page_order_ids(payload, side: str = "bought") -> List[str]:
    ids: List[str] = []
    for item in _items(payload, side):
        if not isinstance(item, dict):
            continue
        common = item.get("commonData") or {}
        order_id = common.get("orderId") or common.get("orderIdStr")
        if order_id:
            ids.append(str(order_id))
    return ids


def parse_bought_order(item: Dict) -> Optional[Dict]:
    if not isinstance(item, dict):
        return None
    common = item.get("commonData") if isinstance(item.get("commonData"), dict) else {}
    head = (item.get("head") or {}).get("data") if isinstance(item.get("head"), dict) else {}
    head = head if isinstance(head, dict) else {}
    content = (
        (item.get("content") or {}).get("data")
        if isinstance(item.get("content"), dict)
        else {}
    )
    content = content if isinstance(content, dict) else {}
    detail = content.get("detailInfo") if isinstance(content.get("detailInfo"), dict) else {}
    price = content.get("priceInfo") if isinstance(content.get("priceInfo"), dict) else {}
    order_id = _text(common.get("orderId") or common.get("orderIdStr"))
    if not order_id:
        return None
    user = head.get("userInfo") if isinstance(head.get("userInfo"), dict) else {}
    quantity = price.get("buyAmount")
    try:
        quantity = int(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        quantity = None
    return {
        "side": "bought",
        "order_id": order_id,
        "occurred": _datetime_text(head.get("createTime")),
        "counterparty": _text(user.get("userNick") or user.get("userAliasNick")),
        "items": [
            {
                "title": _text(detail.get("auctionTitle")),
                "sku": _text(detail.get("skuInfo")),
                "quantity": quantity,
                "paid": _amount(price.get("price")),
            }
        ],
        "paid": _amount(price.get("price")),
        "status": _text(head.get("statusViewMsg")),
        "status_code": _text(common.get("tradeStatusEnum")),
        "item_id": _text(common.get("itemId")),
        "order_detail_url": _text(common.get("orderDetailUrl")),
        "in_refund": False,
    }


def parse_sold_order(item: Dict) -> Optional[Dict]:
    if not isinstance(item, dict):
        return None
    common = item.get("commonData") if isinstance(item.get("commonData"), dict) else {}
    buyer = item.get("buyerInfoVO") if isinstance(item.get("buyerInfoVO"), dict) else {}
    item_vo = item.get("itemVO") if isinstance(item.get("itemVO"), dict) else {}
    price = item.get("priceVO") if isinstance(item.get("priceVO"), dict) else {}
    order_id = _text(common.get("orderId") or common.get("orderIdStr"))
    if not order_id:
        return None
    quantity = price.get("buyNum")
    try:
        quantity = int(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        quantity = None
    paid = _amount(price.get("totalPrice") or price.get("auctionPrice"))
    return {
        "side": "sold",
        "order_id": order_id,
        "occurred": _datetime_text(common.get("createTime")),
        "counterparty": _text(
            buyer.get("name") or buyer.get("userNick") or buyer.get("phone")
        ),
        "items": [
            {
                "title": _text(item_vo.get("title")),
                "sku": None,
                "quantity": quantity,
                "paid": _amount(price.get("auctionPrice")),
            }
        ],
        "paid": paid,
        "status": _text(common.get("orderStatus")),
        "status_code": _text(common.get("orderStatus")),
        "item_id": _text(common.get("itemId")),
        "order_detail_url": None,
        "in_refund": str(common.get("inRefund")).lower() == "true",
    }


def parse_orders(pages: Iterable[Dict]) -> List[Dict]:
    """One normalized row per unique (side, order_id)."""

    output: Dict[Tuple[str, str], Dict] = {}
    for page in pages:
        if not isinstance(page, dict):
            continue
        side = page.get("side", "bought")
        payload = page.get("response")
        for item in _items(payload, side):
            order = (
                parse_bought_order(item)
                if side == "bought"
                else parse_sold_order(item)
            )
            if order is None:
                continue
            key = (side, order["order_id"])
            if key not in output:
                output[key] = order
    return list(output.values())


def date_range(orders: Iterable[Dict]) -> Optional[Tuple[str, str]]:
    dates = [order.get("occurred") for order in orders if order.get("occurred")]
    if not dates:
        return None
    return min(dates), max(dates)


class GoofishFetcher:
    """Fetch bought and/or sold Goofish orders with mtop token bootstrap."""

    def __init__(
        self,
        client: HttpClient,
        jar: CookieJar,
        *,
        sides: Sequence[str] = ("bought", "sold"),
        max_pages: int = 50,
        bought_status: str = "ALL",
        sold_status: str = "ALL",
        rows_per_page: int = DEFAULT_ROWS_PER_PAGE,
    ):
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        unknown = [side for side in sides if side not in ("bought", "sold")]
        if unknown:
            raise ValueError("unknown side: " + ", ".join(unknown))
        if rows_per_page < 1:
            raise ValueError("rows_per_page must be >= 1")
        self.client = client
        self.jar = jar
        self.sides = tuple(dict.fromkeys(sides))
        self.max_pages = max_pages
        self.bought_status = bought_status or "ALL"
        self.sold_status = sold_status or "ALL"
        self.rows_per_page = rows_per_page
        self.pages: List[Dict] = []
        self.orders: List[Dict] = []
        self.warnings: List[str] = []
        self.token_rotations = 0
        self.token_retries = 0

    def _token(self) -> str:
        raw_token = self.jar.get(TOKEN_COOKIE, GOOFISH_HOST) or ""
        return raw_token.split("_", 1)[0]

    def _request_once(self, api: str, version: str, data: Dict) -> Response:
        value_type = None if api == GOOFISH_NAV_API else "string"
        plan = build_request(
            api,
            version,
            data,
            self._token(),
            user_agent=self.client.user_agent,
            value_type=value_type,
        )
        if api == GOOFISH_SOLD_API:
            referer = GOOFISH_SELLER_REFERER
            origin = "https://seller.goofish.com"
        else:
            referer = GOOFISH_BOUGHT_REFERER
            origin = "https://www.goofish.com"
        return self.client.post_form(
            plan.url,
            plan.form,
            cookie_header=self.jar.header_for(GOOFISH_API_HOST),
            headers={"Referer": referer, "Origin": origin},
        )

    def _decode(self, response: Response) -> Dict:
        text = response.text
        try:
            payload = json.loads(text)
        except ValueError:
            check_goofish_html(text, response.url, response.status)
            raise ApiError("闲鱼返回了非 JSON 内容（页面结构可能已变化）")
        if not isinstance(payload, dict):
            check_goofish_html(text, response.url, response.status)
            raise ApiError("闲鱼返回了意外的 JSON 类型")
        return payload

    def _raise_token_error(self, rotated: bool) -> None:
        if missing_login_cookies(self.jar):
            raise AuthRequired(
                "浏览器中没有找到闲鱼登录 Cookie: "
                + ", ".join(missing_login_cookies(self.jar)),
                "请在浏览器中打开 https://www.goofish.com/ 并扫码登录后重试；"
                "_m_h5_tk 会由工具自动刷新，不需要手动处理",
            )
        if rotated:
            message = "闲鱼 _m_h5_tk 令牌刷新后仍被拒绝（每页最多重试 1 次）"
        else:
            message = "闲鱼 _m_h5_tk 令牌无效，且服务器响应没有提供新的令牌"
        raise AuthRequired(
            message,
            "请先在浏览器中打开一次闲鱼页面；如果仍然失败，请重新扫码登录闲鱼",
        )

    def _request_page(
        self, api: str, version: str, data: Dict
    ) -> Tuple[Response, Dict]:
        for attempt in range(1, MAX_PAGE_ATTEMPTS + 1):
            response = self._request_once(api, version, data)
            rotated = refresh_tokens_from_response(self.jar, response)
            if rotated:
                self.token_rotations += 1
            payload = self._decode(response)
            if not is_token_error(payload):
                check_goofish_ret(payload)
                return response, payload
            if attempt < MAX_PAGE_ATTEMPTS and rotated:
                self.token_retries += 1
                continue
            self._raise_token_error(rotated)
        raise AssertionError("unreachable")

    def _record_page(
        self,
        side: str,
        page: int,
        status: str,
        data: Dict,
        payload: Dict,
        raw_text: str,
    ) -> List[Dict]:
        page_orders: List[Dict] = []
        for item in _items(payload, side):
            order = (
                parse_bought_order(item)
                if side == "bought"
                else parse_sold_order(item)
            )
            if order is not None:
                page_orders.append(order)
        self.pages.append(
            {
                "side": side,
                "page": page,
                "status": status,
                "request_data": data,
                "response": payload,
                "raw": raw_text,
                "orders": page_orders,
            }
        )
        self.orders.extend(page_orders)
        return page_orders

    def _fetch_bought(self) -> None:
        previous_ids: Optional[List[str]] = None
        page = 1
        offset_row = None
        while page <= self.max_pages:
            data = build_bought_data(page, self.bought_status, offset_row)
            response, payload = self._request_page(
                GOOFISH_BOUGHT_API, GOOFISH_BOUGHT_VERSION, data
            )
            ids = page_order_ids(payload, "bought")
            self._record_page(
                "bought", page, self.bought_status, data, payload, response.text
            )
            if not ids:
                break
            if previous_ids is not None and set(ids) == set(previous_ids):
                break
            previous_ids = ids
            if not _next_page(payload, "bought"):
                break
            offset_row = _last_end_row(payload)
            page += 1
        else:
            self.warnings.append(
                f"已达到单次最大页数 {self.max_pages}，闲鱼买到的订单可能尚未取完"
            )

    def _fetch_sold(self) -> None:
        previous_ids: Optional[List[str]] = None
        page = 1
        while page <= self.max_pages:
            data = build_sold_data(
                page,
                self.sold_status,
                rows_per_page=self.rows_per_page,
            )
            response, payload = self._request_page(
                GOOFISH_SOLD_API, GOOFISH_SOLD_VERSION, data
            )
            ids = page_order_ids(payload, "sold")
            self._record_page(
                "sold", page, self.sold_status, data, payload, response.text
            )
            if not ids:
                break
            if previous_ids is not None and set(ids) == set(previous_ids):
                break
            previous_ids = ids
            if not _next_page(payload, "sold"):
                break
            page += 1
        else:
            self.warnings.append(
                f"已达到单次最大页数 {self.max_pages}，闲鱼卖出的订单可能尚未取完"
            )

    def fetch(self) -> Dict:
        require_login(self.jar)
        if "bought" in self.sides:
            self._fetch_bought()
        if "sold" in self.sides:
            self._fetch_sold()
        return {
            "kind": "goofish_orders",
            "source": {
                "bought": GOOFISH_BOUGHT_ENDPOINT,
                "sold": GOOFISH_SOLD_ENDPOINT,
            },
            "sides": list(self.sides),
            "pages": self.pages,
            "orders": {
                "bought": [
                    order for order in self.orders if order.get("side") == "bought"
                ],
                "sold": [
                    order for order in self.orders if order.get("side") == "sold"
                ],
            },
            "warnings": list(self.warnings),
        }

    def probe(self) -> Dict:
        """One lightweight read-only request for status/keepalive."""

        require_login(self.jar)
        data = {}
        response, payload = self._request_page(
            GOOFISH_NAV_API, GOOFISH_NAV_VERSION, data
        )
        payload_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        module = (
            payload_data.get("module")
            if isinstance(payload_data.get("module"), dict)
            else {}
        )
        base = module.get("base") if isinstance(module.get("base"), dict) else {}
        display_name = base.get("displayName")
        if not display_name:
            raise AuthRequired(
                "闲鱼导航接口没有返回登录用户",
                "请在浏览器中打开 https://www.goofish.com/ 并扫码登录后重试",
            )
        return {
            "status": response.status,
            "ret": ret_text(payload)[:120],
            "has_user": True,
            # Read-only corroboration for an empty sold list: the nav API
            # reports the same seller-side totals as the workbench header.
            "purchase_count": _count(base.get("purchaseCount")),
            "sold_count": _count(base.get("soldCount")),
            "token_rotations": self.token_rotations,
            "token_retries": self.token_retries,
        }


def summarize(payload: Dict) -> Dict:
    pages = payload.get("pages") or []
    orders = parse_orders(pages)
    span = date_range(orders)
    bought = [order for order in orders if order.get("side") == "bought"]
    sold = [order for order in orders if order.get("side") == "sold"]
    return {
        "pages": len(pages),
        "orders": len(orders),
        "date_range": span,
        "bought_orders": len(bought),
        "sold_orders": len(sold),
    }
