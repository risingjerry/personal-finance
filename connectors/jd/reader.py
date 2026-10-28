"""JD order history reader.

The JD order centre is a Vite single-page app.  Static analysis of
the public bundle shows that it calls ``pcorder_center_list`` and
attempts ``window.PSign.sign`` to attach an ``h5st`` parameter; when that sign
step throws, the bundle only reports a metric and still sends the request
without ``h5st``/``x-api-eid-token`` — the site's own no-sign path.  This tool
uses that path first (one POST per page) and only parses the order-page HTML
when the API request itself fails.  The HTML page is a Vite shell with no
order cards, so reading it first used to cost one wasted GET per page.

The list API body is ``{page, d, s, recycleType?}`` — there is no ``pageSize``
parameter, the page size is the server's (20 in the live 2026-10 bundle) — and
the response carries ``data.orderList`` plus ``data.totalNum``, which lets the
reader stop after exactly as many pages as the account has orders instead of
probing a guaranteed-empty tail page.

This module never computes, fetches or bypasses h5st, ``x-api-eid-token`` or
jsToken, and it stops (fail-closed) on any risk-control response.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .blocking import check_jd_api, check_jd_html
from ..kit.cookies import CookieJar
from ..kit.errors import ApiError, AuthRequired, NetworkError, UsageError
from ..kit.http import HttpClient, Response
from ..kit.spec import LoginSet

JD_HOST = "order.jd.com"
JD_API_HOST = "api.m.jd.com"
JD_HTML_ENDPOINT = "https://order.jd.com/center/list.action"
JD_API_ENDPOINT = f"https://{JD_API_HOST}/api"
JD_API_APP_ID = "order-jd-com"
JD_API_FUNCTION_ID = "pcorder_center_list"
JD_API_SOURCE = "pc_order_center"
JD_STATUS_ALL = "4096"

# The bundle's request wrapper (``Qr``) always appends these query parameters,
# in this order, then the per-call ones (``functionId``, ``source``).  Kept as
# one tuple so a test can assert the exact wire shape: no h5st, no
# x-api-eid-token, no jsToken is ever added here.
JD_API_QUERY_KEYS = (
    "appid",
    "client",
    "clientVersion",
    "uuid",
    "loginType",
    "t",
    "functionId",
    "source",
)
JD_API_CLIENT = "pc"
JD_API_CLIENT_VERSION = "1.0.0"
JD_API_LOGIN_TYPE = "3"

# A logged-in PC session stores thor + pin on .jd.com/.jd.hk.  The mobile site
# (m.jd.com) instead stores pt_key + pt_pin.  Either complete pair is enough;
# requiring both would wrongly report a real PC session as logged out.
JD_PC_LOGIN_COOKIES = ("thor", "pin")
JD_MOBILE_LOGIN_COOKIES = ("pt_key", "pt_pin")


JD_PC_LOGIN_SET = LoginSet(
    "pc", "PC（thor + pin）", JD_PC_LOGIN_COOKIES
)
JD_MOBILE_LOGIN_SET = LoginSet(
    "mobile", "移动端（pt_key + pt_pin）", JD_MOBILE_LOGIN_COOKIES
)
JD_LOGIN_SETS = (JD_PC_LOGIN_SET, JD_MOBILE_LOGIN_SET)

# Everything status reports.  pinId/unick/TrackID are part of the PC session but
# do not gate login, so they are informational only.
JD_LOGIN_COOKIES = (
    "thor", "pin", "pinId", "unick", "TrackID", "pt_key", "pt_pin",
)
JD_REQUIRED_LOGIN_COOKIES = JD_PC_LOGIN_COOKIES + JD_MOBILE_LOGIN_COOKIES
BEIJING = datetime.timezone(datetime.timedelta(hours=8))

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

DATETIME_RE = re.compile(
    r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?"
)
ORDER_ID_RE = re.compile(r"\b(\d{8,24})\b")
CURRENCY_RE = re.compile(r"¥\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: Optional[Dict[str, str]] = None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: List[object] = []
        self.parent = parent

    def classes(self) -> List[str]:
        return (self.attrs.get("class") or "").split()

    def has_class_prefix(self, prefix: str) -> bool:
        return any(name.startswith(prefix) for name in self.classes())

    def text(self, separator: str = "") -> str:
        parts: List[str] = []
        stack = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
                continue
            for child in reversed(item.children):
                stack.append(child)
        return separator.join(part for part in parts if part).strip()

    def descendants(self) -> Iterable["Node"]:
        stack = list(reversed(self.children))
        while stack:
            item = stack.pop()
            if isinstance(item, Node):
                yield item
                stack.extend(reversed(item.children))

    def find_all(self, predicate) -> List["Node"]:
        return [node for node in self.descendants() if predicate(node)]

    def first(self, predicate) -> Optional["Node"]:
        for node in self.descendants():
            if predicate(node):
                return node
        return None

    def first_class(self, prefix: str) -> Optional["Node"]:
        return self.first(lambda node: node.has_class_prefix(prefix))

    def find_class(self, prefix: str) -> List["Node"]:
        return self.find_all(lambda node: node.has_class_prefix(prefix))


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: List[Node] = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    builder = _TreeBuilder()
    try:
        builder.feed(html)
        builder.close()
    except Exception:
        # A malformed tail should not discard the order cards we already have.
        pass
    return builder.root


def _first_text(root: Node, prefix: str) -> Optional[str]:
    node = root.first_class(prefix)
    if node is None:
        return None
    text = node.text(" ").strip()
    return text or None


def _first_attr(root: Node, prefix: str, attr: str) -> Optional[str]:
    node = root.first_class(prefix)
    if node is None:
        return None
    value = node.attrs.get(attr)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_datetime(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().isdigit()
    ):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        if number > 0:
            if number < 10_000_000_000:
                number *= 1000
            try:
                return datetime.datetime.fromtimestamp(
                    number / 1000, BEIJING
                ).strftime("%Y-%m-%d %H:%M:%S")
            except (OverflowError, OSError, ValueError):
                return None
    text = str(value).strip()
    match = DATETIME_RE.search(text)
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


def _normalize_amount(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    text = str(value).replace("¥", "").replace(",", "").strip()
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d{1,2})?", text)
    return match.group(0) if match else None


def _year_of(occurred: Optional[str]) -> Optional[int]:
    if not occurred:
        return None
    match = re.match(r"(\d{4})", occurred)
    return int(match.group(1)) if match else None


def _parse_card(card: Node) -> Optional[Dict]:
    time_node = card.first_class("orderTime-")
    occurred = None
    if time_node is not None:
        value_node = time_node.first_class("metaValue-")
        time_text = (
            (value_node.text(" ") if value_node is not None else "")
            or (value_node.attrs.get("title") if value_node is not None else "")
            or time_node.text(" ")
        )
        occurred = _parse_datetime(time_text)
    if occurred is None:
        match = DATETIME_RE.search(card.text(" "))
        if match:
            occurred = _parse_datetime(match.group(0))

    order_id = None
    meta = card.first_class("orderMetas-")
    if meta is not None:
        for value_node in meta.find_class("metaValue-"):
            candidate = value_node.attrs.get("title") or value_node.text(" ")
            match = ORDER_ID_RE.search(candidate or "")
            if match:
                order_id = match.group(1)
                break
    if order_id is None:
        match = re.search(r"订单号\s*[:：]?\s*(\d{8,24})", card.text(" "))
        if match:
            order_id = match.group(1)
    if not order_id:
        return None

    shop = _first_attr(card, "shopNames-", "title") or _first_text(card, "shopNames-")
    status = _first_text(card, "statusText-")

    paid = None
    price = card.first_class("priceColumn-")
    if price is not None:
        symbol = _first_text(price, "priceSymbol-") or ""
        integer = _first_text(price, "priceInteger-") or ""
        decimal = _first_text(price, "priceDecimal-") or ""
        if integer:
            paid = _normalize_amount(symbol + integer + decimal)
    if paid is None:
        match = CURRENCY_RE.search(card.text(" "))
        if match:
            paid = _normalize_amount(match.group(1))

    items = []
    for product in card.find_class("productItem-"):
        title = _first_attr(product, "goodImg-", "title")
        if not title:
            title = _first_attr(product, "productTitle-", "title")
        if not title:
            title = _first_text(product, "productTitle-")
        quantity_text = _first_text(product, "countNum-")
        try:
            quantity = int(quantity_text) if quantity_text else None
        except ValueError:
            quantity = None
        item_id = None
        link = product.first_class("goodImg-")
        if link is not None:
            match = re.search(r"/(\d+)\.html", link.attrs.get("href") or "")
            if match:
                item_id = match.group(1)
        if title or item_id:
            items.append(
                {"title": title, "quantity": quantity, "item_id": item_id}
            )

    return {
        "order_id": order_id,
        "occurred": occurred,
        "shop": shop,
        "status": status,
        "paid": paid,
        "items": items,
        "source": "html",
    }


def parse_html_orders(html: str) -> List[Dict]:
    """Parse order cards from a rendered or server-rendered JD order page."""

    root = parse_html(html)
    cards = root.find_class("orderCard-")
    orders: List[Dict] = []
    seen = set()
    for card in cards:
        order = _parse_card(card)
        if order is None:
            continue
        if order["order_id"] in seen:
            continue
        seen.add(order["order_id"])
        orders.append(order)
    return orders


def _year_matches(
    orders: Sequence[Dict], year: int, *, check_fallback: bool = True
) -> Tuple[List[Dict], bool]:
    """Return (matching, fallback).  JD may show recent orders for empty years."""

    matched = [order for order in orders if _year_of(order.get("occurred")) == year]
    fallback = bool(orders) and not matched and check_fallback
    return matched, fallback


def parse_years(spec: Optional[str], current_year: Optional[int] = None) -> List[int]:
    current = current_year or datetime.datetime.now().year
    if not spec or not spec.strip():
        return [current]
    years = set()
    for token in re.split(r"[,\s]+", spec.strip()):
        if not token:
            continue
        if "-" in token:
            start_text, _, end_text = token.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise UsageError(f"无法解析年份范围: {token}") from exc
            if start > end:
                raise UsageError(f"年份范围起点大于终点: {token}")
            years.update(range(start, end + 1))
        else:
            try:
                years.add(int(token))
            except ValueError as exc:
                raise UsageError(f"无法解析年份: {token}") from exc
    if not years:
        raise UsageError("--years 不能为空")
    for year in years:
        if year < 2000 or year > current:
            raise UsageError(f"年份超出支持范围（2000-{current}）: {year}")
    return sorted(years)


def d_for_year(year: int, current_year: Optional[int] = None) -> str:
    current = current_year or datetime.datetime.now().year
    if year == current:
        return "2"
    if year >= 2014:
        return str(year)
    # d=3 is JD's "earlier" bucket; the caller filters it by exact year.
    return "3"


def html_url(d_value: str, page: int) -> str:
    query = urllib.parse.urlencode({"d": d_value, "s": JD_STATUS_ALL, "page": page})
    return f"{JD_HTML_ENDPOINT}?{query}"


def _api_items(item: Dict) -> List[Dict]:
    candidates = []
    for key in ("wareList", "productList", "skuList", "itemList", "products"):
        value = item.get(key)
        if isinstance(value, list):
            candidates = value
            break
    items = []
    for product in candidates:
        if not isinstance(product, dict):
            continue
        title = (
            product.get("name")
            or product.get("title")
            or product.get("skuName")
            or product.get("productName")
        )
        quantity = (
            product.get("num")
            or product.get("quantity")
            or product.get("count")
        )
        if quantity is None and isinstance(product.get("amountDes"), dict):
            quantity = product["amountDes"].get("value")
        try:
            quantity = int(quantity) if quantity is not None else None
        except (TypeError, ValueError):
            quantity = None
        item_id = (
            product.get("productId")
            or product.get("skuId")
            or product.get("wareId")
            or product.get("itemId")
        )
        if title or item_id:
            items.append(
                {
                    "title": title,
                    "quantity": quantity,
                    "item_id": str(item_id) if item_id is not None else None,
                }
            )
    return items


def _api_order(item: Dict) -> Optional[Dict]:
    if not isinstance(item, dict):
        return None
    order_id = (
        item.get("orderId")
        or item.get("order_id")
        or item.get("id")
        or item.get("orderNo")
    )
    if order_id is None:
        return None
    order_id = str(order_id)
    occurred = None
    for key in (
        "dateSubmit", "orderTime", "orderDate", "createTime", "creationTime",
        "submitTime",
    ):
        if item.get(key) is not None:
            occurred = _parse_datetime(item.get(key))
            if occurred:
                break
    shop_info = item.get("shopInfo") if isinstance(item.get("shopInfo"), dict) else {}
    shop = (
        shop_info.get("shopName")
        or shop_info.get("shopTitle")
        or item.get("shopName")
        or item.get("venderName")
    )
    status_info = (
        item.get("statusInfo") if isinstance(item.get("statusInfo"), dict) else {}
    )
    status = (
        status_info.get("orderStateName")
        or status_info.get("statusName")
        or item.get("statusName")
        or item.get("orderStatusName")
    )
    pay_info = (
        item.get("itemPayInfo") if isinstance(item.get("itemPayInfo"), dict) else {}
    )
    paid = None
    for key in (
        "fistPrice", "firstPrice", "totalPrice", "orderTotalPrice",
        "payPrice", "actualPay", "orderPrice",
    ):
        if pay_info.get(key) is not None:
            paid = _normalize_amount(pay_info.get(key))
            if paid:
                break
        if item.get(key) is not None:
            paid = _normalize_amount(item.get(key))
            if paid:
                break
    return {
        "order_id": order_id,
        "occurred": occurred,
        "shop": shop,
        "status": status,
        "paid": paid,
        "items": _api_items(item),
        "source": "api",
    }


def parse_api_orders(payload) -> List[Dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    candidates = []
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        for key in ("orderList", "orders", "list", "orderInfoList"):
            if isinstance(data.get(key), list):
                candidates = data[key]
                break
    if not candidates:
        for key in ("orderList", "orders", "list"):
            if isinstance(payload.get(key), list):
                candidates = payload[key]
                break
    orders = []
    seen = set()
    for item in candidates:
        order = _api_order(item)
        if order is None or order["order_id"] in seen:
            continue
        seen.add(order["order_id"])
        orders.append(order)
    return orders


def date_range(orders: Iterable[Dict]) -> Optional[Tuple[str, str]]:
    dates = [order.get("occurred") for order in orders if order.get("occurred")]
    if not dates:
        return None
    return min(dates), max(dates)


def apply_login_cookie_rotations(
    jar: CookieJar, response: Response, *, default_host: str = JD_HOST
) -> int:
    """Apply response cookies once; report login-cookie rotations. Never write back to browser."""

    return sum(name in JD_LOGIN_COOKIES for name in jar.apply_response(response))


def detect_login_set(
    jar: CookieJar, host: str = JD_HOST
) -> Optional[LoginSet]:
    """Return the first complete PC/mobile login set present in the jar."""

    for login_set in JD_LOGIN_SETS:
        if all(jar.get(name, host) for name in login_set.cookies):
            return login_set
    return None


def missing_login_cookies(jar: CookieJar, host: str = JD_HOST) -> List[str]:
    """Names that are absent when neither complete login set is available."""

    if detect_login_set(jar, host) is not None:
        return []
    missing: List[str] = []
    for login_set in JD_LOGIN_SETS:
        for name in login_set.cookies:
            if not jar.get(name, host) and name not in missing:
                missing.append(name)
    return missing


def require_login(jar: CookieJar, host: str = JD_HOST) -> None:
    missing = missing_login_cookies(jar, host)
    if missing:
        raise AuthRequired(
            "浏览器中没有找到完整的京东登录 Cookie 组合"
            "（PC 需要 thor + pin，移动端需要 pt_key + pt_pin）: "
            + ", ".join(missing),
            "请在浏览器中打开 https://www.jd.com/ 并扫码登录后重试",
        )


def _api_total_num(payload) -> Optional[int]:
    """Read ``data.totalNum`` without trusting its type.

    Returns ``None`` when the server omitted it or sent something that is not
    a number, so the caller can fall back to empty-page pagination.
    """

    if not isinstance(payload, dict):
        return None
    candidates = []
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("totalNum"))
        candidates.append(data.get("total_num"))
    candidates.append(payload.get("totalNum"))
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


class JdFetcher:
    def __init__(
        self,
        client: HttpClient,
        jar: CookieJar,
        years: Sequence[int],
        *,
        max_pages: int = 50,
        api_fallback: bool = True,
        html_fallback: bool = True,
        concurrency: int = 1,
    ):
        """``api_fallback`` keeps its old meaning: False is ``--html-only``.

        With ``api_fallback=True`` the reader is API-first; ``html_fallback``
        only decides whether a failed *first* API request may be retried
        through the order-page HTML.  Risk-control and login errors are never
        retried on either path — they fail closed.
        """

        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.client = client
        self.jar = jar
        self.client.cookie_jar = jar
        self.years = list(years)
        self.max_pages = max_pages
        self.api_fallback = api_fallback
        self.html_fallback = html_fallback
        self.concurrency = concurrency
        self._effective_concurrency = 1
        self.pages: List[Dict] = []
        self.warnings: List[str] = []
        self.cookie_rotations = 0
        self.api_requests = 0
        self.html_requests = 0
        self.year_queries: List[Dict] = []
        self._current_year = datetime.datetime.now().year

    def _cookie_header(self, host: str = JD_HOST) -> str:
        # Use the request host so host-only cookies are sent to the right
        # endpoint too; .jd.com cookies match both order.jd.com and
        # api.m.jd.com.
        return self.jar.header_for(host)

    def _fetch_html(self, d_value: str, page: int) -> Response:
        url = html_url(d_value, page)
        self.html_requests += 1
        response = self.client.get(
            url,
            cookie_header=self._cookie_header(JD_HOST),
            headers={
                "Referer": "https://order.jd.com/center/list.action",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        self.cookie_rotations += apply_login_cookie_rotations(self.jar, response)
        return response

    def _fetch_api(
        self, d_value: str, page: int
    ) -> Tuple[List[Dict], str, Optional[int]]:
        """One read-only ``pcorder_center_list`` call, exactly like the bundle.

        Query: appid/client/clientVersion/uuid(__jda)/loginType/t + functionId +
        source.  Body: ``{"body": JSON({page, d, s})}`` — the bundle never sends
        a pageSize and only adds ``recycleType`` when it is non-zero, so the
        recycle bin stays out of this read.
        """

        data = {"page": page, "d": d_value, "s": JD_STATUS_ALL}
        params = {
            "appid": JD_API_APP_ID,
            "client": JD_API_CLIENT,
            "clientVersion": JD_API_CLIENT_VERSION,
            "uuid": self.jar.get("__jda", JD_HOST) or "",
            "loginType": JD_API_LOGIN_TYPE,
            "t": str(
                int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
            ),
            "functionId": JD_API_FUNCTION_ID,
            "source": JD_API_SOURCE,
        }
        url = JD_API_ENDPOINT + "?" + urllib.parse.urlencode(params)
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        self.api_requests += 1
        response = self.client.post_form(
            url,
            {"body": body},
            cookie_header=self._cookie_header(JD_API_HOST),
            headers={
                "Referer": html_url(d_value, page),
                "Origin": "https://order.jd.com",
            },
        )
        self.cookie_rotations += apply_login_cookie_rotations(self.jar, response)
        if response.status >= 400:
            # 401/403 become RiskBlocked inside check_jd_html: fail closed.
            check_jd_html(response.text, response.url, response.status)
            raise NetworkError(f"京东接口请求失败 (HTTP {response.status})")
        try:
            payload = json.loads(response.text)
        except ValueError as exc:
            # A risk-control page served instead of JSON still fails closed.
            check_jd_html(response.text, response.url, response.status)
            raise ApiError("京东接口返回了非 JSON 内容") from exc
        check_jd_api(payload, response.status)
        return parse_api_orders(payload), response.text, _api_total_num(payload)

    def _api_year(self, year: int, d_value: str, seen_order_ids: set) -> bool:
        """Read one year through the API.  False means "fall back to HTML".

        Fail-closed rule: only a plain :class:`ApiError`/``NetworkError`` on
        the *first* page can trigger the HTML fallback.  ``AuthRequired`` and
        ``RiskBlocked`` propagate immediately with no second request.
        """

        check_fallback = d_value != "3"
        first_page_index = len(self.pages)
        previous_ids: Optional[List[str]] = None
        total_num: Optional[int] = None
        expected_total: Optional[int] = None
        fetched = 0
        page = 1
        while page <= self.max_pages:
            try:
                orders, raw, page_total = self._fetch_api(d_value, page)
            except (NetworkError, ApiError) as exc:
                if page > 1 or not self.html_fallback:
                    raise
                self.warnings.append(
                    f"{year}: 只读 API 首请求失败（{exc.message}），回退 HTML 解析"
                )
                return False
            if page_total is not None:
                if total_num is None:
                    total_num = page_total
                else:
                    # Defend against a non-monotonic totalNum: never let a
                    # later page shrink the target.
                    total_num = max(total_num, page_total)
            matched, fallback = _year_matches(
                orders, year, check_fallback=check_fallback
            )
            if fallback:
                self.warnings.append(
                    f"{year}: 京东第 {page} 页返回了非 {year} 年的订单，"
                    "判定为“无订单年份回退”，已忽略"
                )
                self.pages.append(
                    {"d": d_value, "page": page, "orders": [], "html": raw}
                )
                break
            if not orders:
                self.pages.append(
                    {"d": d_value, "page": page, "orders": [], "html": raw}
                )
                break
            if page == 1:
                # Only trust totalNum when the first page is consistent with
                # it (a smaller total than the rows we already hold is stale
                # data, not a stop signal).
                if page_total and page_total >= len(orders):
                    expected_total = page_total
            if matched and len(matched) < len(orders):
                self.warnings.append(
                    f"{year}: 第 {page} 页有 {len(orders) - len(matched)} "
                    "条订单不在目标年份，已过滤"
                )
            page_orders = []
            for order in matched:
                if order["order_id"] in seen_order_ids:
                    continue
                seen_order_ids.add(order["order_id"])
                page_orders.append(order)
            self.pages.append(
                {"d": d_value, "page": page, "orders": page_orders, "html": raw}
            )
            fetched += len(orders)
            ids = [order["order_id"] for order in orders]
            if previous_ids is not None and set(ids) <= set(previous_ids):
                break
            previous_ids = ids
            # Precise stop: totalNum is the number of orders in this filter,
            # so once we hold that many there is nothing left to probe.
            if expected_total is not None and fetched >= expected_total:
                break
            page += 1
        else:
            self.warnings.append(
                f"{year}: 已达到单次最大页数 {self.max_pages}，"
                "该年份可能尚未取完"
            )
        self.year_queries.append(
            {
                "year": year,
                "d": d_value,
                "transport": "api",
                "total_num": total_num,
                "pages": len(self.pages) - first_page_index,
            }
        )
        return True

    def _html_year(self, year: int, d_value: str, seen_order_ids: set) -> None:
        """Legacy path: parse order cards out of the rendered HTML page."""

        check_fallback = d_value != "3"
        first_page_index = len(self.pages)
        previous_ids: Optional[List[str]] = None
        page = 1
        while page <= self.max_pages:
            response = self._fetch_html(d_value, page)
            check_jd_html(response.text, response.url, response.status)
            if response.status >= 400:
                raise NetworkError(f"京东订单页请求失败 (HTTP {response.status})")
            html_orders = parse_html_orders(response.text)
            matched, fallback = _year_matches(
                html_orders, year, check_fallback=check_fallback
            )
            raw = response.text
            if fallback:
                self.warnings.append(
                    f"{year}: 京东第 {page} 页返回了非 {year} 年的订单，"
                    "判定为“无订单年份回退”，已忽略"
                )
                self.pages.append(
                    {"d": d_value, "page": page, "orders": [], "html": raw}
                )
                break
            if not html_orders:
                if page == 1 and not self.api_fallback:
                    self.warnings.append(
                        f"{year}: 京东 HTML 未包含订单，且已用 --html-only "
                        "禁用只读 API"
                    )
                self.pages.append(
                    {"d": d_value, "page": page, "orders": [], "html": raw}
                )
                break
            if matched and len(matched) < len(html_orders):
                self.warnings.append(
                    f"{year}: 第 {page} 页有 {len(html_orders) - len(matched)} "
                    "条订单不在目标年份，已过滤"
                )
            page_orders = []
            for order in matched:
                if order["order_id"] in seen_order_ids:
                    continue
                seen_order_ids.add(order["order_id"])
                page_orders.append(order)
            self.pages.append(
                {"d": d_value, "page": page, "orders": page_orders, "html": raw}
            )
            ids = [order["order_id"] for order in html_orders]
            if previous_ids is not None and set(ids) <= set(previous_ids):
                break
            previous_ids = ids
            # d=3 is a combined "earlier" bucket.  A page can legitimately
            # contain no order from the requested year; keep going until
            # the bucket ends instead of treating it as a recent-order
            # fallback.
            page += 1
        else:
            self.warnings.append(
                f"{year}: 已达到单次最大页数 {self.max_pages}，"
                "该年份可能尚未取完"
            )
        self.year_queries.append(
            {
                "year": year,
                "d": d_value,
                "transport": "html",
                "total_num": None,
                "pages": len(self.pages) - first_page_index,
            }
        )

    def fetch(self) -> Dict:
        require_login(self.jar)
        started = time.monotonic()
        self._effective_concurrency = (
            min(self.concurrency, len(self.years)) if self.concurrency > 1 and len(self.years) > 1 else 1
        )
        if self._effective_concurrency > 1:
            self._fetch_years_concurrent()
        else:
            seen_order_ids: set = set()
            for year in self.years:
                self._fetch_one_year(year, seen_order_ids)
        payload: Dict = {"kind": "jd_pages", "pages": self.pages}
        payload["query"] = self.query_info(
            elapsed_ms=int((time.monotonic() - started) * 1000)
        )
        payload["query"]["concurrency"] = self._effective_concurrency
        return payload

    def _fetch_one_year(self, year: int, seen_order_ids: set) -> None:
        d_value = d_for_year(year, self._current_year)
        if d_value == "3" and year < 2014:
            self.warnings.append(
                f"{year}: 京东使用 d=3（更早订单）抓取，并按年份过滤"
            )
        handled = False
        if self.api_fallback:
            handled = self._api_year(year, d_value, seen_order_ids)
        if not handled:
            self._html_year(year, d_value, seen_order_ids)

    def _fetch_years_concurrent(self) -> None:
        """Read years in parallel with a fixed worker count.

        Each worker is its own reader instance: ``pages``/``warnings``/
        ``year_queries`` stay private, while the shared ``HttpClient`` (one
        connection pool) and ``CookieJar`` (locked) are handed over.  The
        request *rate* is still capped by the shared :class:`Pacer`, which
        hands out start slots rather than serialising whole requests, so
        concurrency overlaps latency without increasing request frequency.

        Results are merged in the caller's year order, so output is stable no
        matter which worker finishes first; ``order_id`` de-duplication happens
        per worker and again at merge time.
        """

        workers = self._effective_concurrency
        results: Dict[int, Dict] = {}
        errors: List[BaseException] = []

        def run(year: int) -> None:
            worker = JdFetcher(
                self.client,
                self.jar,
                [year],
                max_pages=self.max_pages,
                api_fallback=self.api_fallback,
                html_fallback=self.html_fallback,
                concurrency=1,
            )
            worker._current_year = self._current_year
            try:
                worker.fetch()
            except BaseException as exc:  # collected, re-raised in year order
                errors.append(exc)
                return
            results[year] = {
                "pages": worker.pages,
                "warnings": worker.warnings,
                "year_queries": worker.year_queries,
                "api_requests": worker.api_requests,
                "html_requests": worker.html_requests,
                "cookie_rotations": worker.cookie_rotations,
            }

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run, self.years))

        if errors:
            # One failing year must not silently drop the others: keep what
            # succeeded, then fail closed with the first error.
            self._merge_concurrent(results)
            raise errors[0]

        self._merge_concurrent(results)

    def _merge_concurrent(self, results: Dict[int, Dict]) -> None:
        seen: set = set()
        for year in self.years:
            found = results.get(year)
            if not found:
                continue
            self.warnings.extend(found["warnings"])
            self.api_requests += found["api_requests"]
            self.html_requests += found["html_requests"]
            self.cookie_rotations += found["cookie_rotations"]
            self.year_queries.extend(found["year_queries"])
            for entry in found["pages"]:
                kept = []
                for order in entry.get("orders", []):
                    if order["order_id"] in seen:
                        continue
                    seen.add(order["order_id"])
                    kept.append(order)
                merged_entry = dict(entry)
                merged_entry["orders"] = kept
                self.pages.append(merged_entry)

    def query_info(self, *, elapsed_ms: int = 0) -> Dict:
        """Observability for benchmarks/CLI summaries; no ids or cookies."""

        totals = [
            entry["total_num"]
            for entry in self.year_queries
            if entry.get("total_num") is not None
        ]
        if self.api_requests and self.html_requests:
            transport = "api+html"
        elif self.api_requests:
            transport = "api"
        elif self.html_requests:
            transport = "html"
        else:
            transport = "none"
        return {
            "transport": transport,
            "api_requests": self.api_requests,
            "html_requests": self.html_requests,
            "total_num": sum(totals) if totals else None,
            "elapsed_ms": elapsed_ms,
            "years": [dict(entry) for entry in self.year_queries],
        }

    def probe_api(self) -> Dict:
        """One read-only API request: proves the *actual* list path works.

        ``probe()`` reads the HTML page, which is a Vite shell today, so it can
        only ever report "0 order cards" and says nothing about whether the
        order data is reachable.  The API probe costs the same one request and
        reports the server's ``totalNum``.
        """

        require_login(self.jar)
        started = time.monotonic()
        d_value = d_for_year(self._current_year, self._current_year)
        orders, _raw, total_num = self._fetch_api(d_value, 1)
        return {
            "status": 200,
            "transport": "api",
            "page": 1,
            "orders": len(orders),
            "total_num": total_num,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "cookie_rotations": self.cookie_rotations,
        }

    def probe(self) -> Dict:
        """One lightweight read-only request for status/keepalive."""

        require_login(self.jar)
        d_value = d_for_year(self._current_year, self._current_year)
        response = self._fetch_html(d_value, 1)
        check_jd_html(response.text, response.url, response.status)
        if response.status >= 400:
            raise NetworkError(f"京东订单页请求失败 (HTTP {response.status})")
        orders = parse_html_orders(response.text)
        return {
            "status": response.status,
            "url": html_url(d_value, 1),
            "page": 1,
            "orders": len(orders),
            "cookie_rotations": self.cookie_rotations,
        }


def summarize(payload: Dict) -> Dict:
    orders = []
    seen = set()
    for page in payload.get("pages") or []:
        for order in page.get("orders") or []:
            if order.get("order_id") in seen:
                continue
            seen.add(order.get("order_id"))
            orders.append(order)
    span = date_range(orders)
    summary = {
        "pages": len(payload.get("pages") or []),
        "orders": len(orders),
        "date_range": span,
    }
    query = payload.get("query")
    if isinstance(query, dict):
        for key in ("transport", "api_requests", "html_requests", "total_num",
                    "elapsed_ms"):
            if key in query:
                summary[key] = query[key]
    return summary
