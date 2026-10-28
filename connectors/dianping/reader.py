"""Dianping (大众点评) order history reader.

The current "我的订单" entry exposes 新餐饮订单 at
``https://t.dianping.com/account/daocan_orders``.  It is a server-rendered
HTML list (15 rows per page) with plain ``?pageno=N`` pagination.  Its page
bundle (``page::order``) contains only write XHRs (delete/cancel/refund/pay),
so the read path is the HTML page itself, not a JSON list API.

The older ``ajax/json/order/search`` endpoint (kept below for compatibility)
is login-gated and may require Meituan's H5guard ``mtgsig`` signature.  This
module never computes, copies or forges ``mtgsig``; if a response asks for it
the fetch stops with ``RiskSignatureRequired`` (message starts with
``needs risk signature``).
"""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .blocking import (
    check_dianping_html,
    check_dianping_payload,
)
from ..kit.cookies import CookieJar
from ..kit.errors import ApiError, AuthRequired
from ..kit.http import HttpClient, Response

DIANPING_HOST = "t.dianping.com"
DIANPING_ORDER_PAGE = "https://t.dianping.com/account/coupons"
DIANPING_ORDER_API = "https://t.dianping.com/ajax/json/order/search"
DIANPING_REFERER = DIANPING_ORDER_PAGE
# 《我的订单 -> 新餐饮订单》是服务端渲染的 HTML 列表页（不是 JSON API）。
# 页面 bundle `page::order` 里只有删除/取消/退款/合并付款等写接口；列表本身
# 由服务端直接输出，翻页是普通链接 ?pageno=N。
DIANPING_DAOCAN_PAGE = "https://t.dianping.com/account/daocan_orders"
DIANPING_DAOCAN_SOURCE = "daocan"
# 15 rows per server-rendered page; 100 pages leaves ample headroom for a full
# historical pull while still stopping on empty/repeated/no-NextPage pages.
DIANPING_DEFAULT_MAX_PAGES = 100
DIANPING_MOBILE_HOME = "https://m.dianping.com/"
DIANPING_PC_HOME = "https://www.dianping.com/"
DIANPING_LOGIN_COOKIES = ("dper", "dpscope", "dpassport")
DIANPING_MTGSIG_HEADER = "mtgsig"
DIANPING_SOURCE = "groupbuy"
DIANPING_DAOCAN_ROW_RE = re.compile(
    r"""<tr\b([^>]*?\bdata-id\s*=\s*["']order-item["'][^>]*)>(.*?)</tr>""",
    re.IGNORECASE | re.DOTALL,
)
DIANPING_DAOCAN_NEXT_RE = re.compile(r"<a\b([^>]*)>", re.IGNORECASE)
DIANPING_DAOCAN_H4_RE = re.compile(r"<h4\b[^>]*>(.*?)</h4>", re.IGNORECASE | re.DOTALL)
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
SCRIPT_SRC_RE = re.compile(
    r"""<script\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
LINK_JS_RE = re.compile(
    r"""<link\b[^>]*?\bhref\s*=\s*["']([^"']+\.js(?:\?[^"']*)?)["']""",
    re.IGNORECASE,
)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
FORM_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*["']([^"']*)["']""")
ANCHOR_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]{0,4000}>")
ORDERISH_PATH_RE = re.compile(
    r"""["']((?:https?://[^"']+)?/[^"'\s<>]*(?:ajax|order|orderlist|coupon|api)[^"'\s<>]*)["']""",
    re.IGNORECASE,
)

DATETIME_RE = re.compile(
    r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?"
)
DATE_ONLY_RE = re.compile(r"^(20\d{2})[-/](\d{1,2})[-/](\d{1,2})$")
ORDER_LIST_KEYS = (
    "list",
    "orderList",
    "orders",
    "records",
    "dataList",
    "result",
)
ORDER_ID_KEYS = (
    "orderId",
    "order_id",
    "orderNo",
    "order_no",
    "orderCode",
    "tradeId",
    "couponId",
    "dealId",
    "deal_id",
    "id",
)
ORDER_TIME_KEYS = (
    "occurred",
    "orderTime",
    "createTime",
    "createDate",
    "payTime",
    "useTime",
    "consumeTime",
    "orderDate",
    "submitTime",
    "addTime",
    "time",
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not follow redirects for Dianping requests.

    Following a redirect to verify.meituan.com or account.dianping.com with
    urllib would copy the caller-supplied Cookie header to the new origin.
    That would leak Dianping cookies, so Dianping uses a no-redirect opener
    and inspects the Location header itself.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def no_redirect_opener():
    return urllib.request.build_opener(_NoRedirectHandler())


def check_dianping_login(jar: CookieJar) -> None:
    if not jar.has_any(DIANPING_LOGIN_COOKIES, DIANPING_HOST):
        raise AuthRequired(
            "浏览器中没有找到大众点评登录 Cookie",
            "请先在浏览器中登录大众点评，再重新运行",
        )


def build_api_url(page: int, page_size: int = 20) -> str:
    query = urllib.parse.urlencode({"page": int(page), "pageSize": int(page_size)})
    return f"{DIANPING_ORDER_API}?{query}"


def _parse_datetime(value) -> Optional[str]:
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
    date_match = DATE_ONLY_RE.match(text)
    if date_match:
        year, month, day = date_match.groups()
        try:
            return datetime.datetime(int(year), int(month), int(day)).strftime(
                "%Y-%m-%d 00:00:00"
            )
        except ValueError:
            return None
    match = DATETIME_RE.search(text)
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        return datetime.datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0)
        ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _order_id(order: Dict) -> Optional[str]:
    for key in ORDER_ID_KEYS:
        value = order.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _order_time(order: Dict) -> Optional[str]:
    for key in ORDER_TIME_KEYS:
        if key in order:
            parsed = _parse_datetime(order.get(key))
            if parsed:
                return parsed
    return None


def extract_orders(payload) -> List[Dict]:
    """Best-effort extraction for summaries; the raw response is preserved."""

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    candidates = []
    for key in ORDER_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            candidates = value
            break
    if not candidates:
        data = payload.get("data")
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            if _order_id(data):
                return [data]
            for key in ORDER_LIST_KEYS:
                value = data.get(key)
                if isinstance(value, list):
                    candidates = value
                    break
            if not candidates and isinstance(data.get("data"), list):
                candidates = data["data"]
    return [item for item in candidates if isinstance(item, dict)]


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y"):
            return True
        if lowered in ("false", "0", "no", "n"):
            return False
    return None


def has_next_page(payload, page: int, page_size: int, item_count: int) -> bool:
    """Conservatively decide whether to request another page.

    The exact Dianping JSON shape is not public (the order page is behind
    login), so unknown shapes keep going until an empty/repeated page or the
    caller's max-pages limit.  Explicit pagination fields are honoured.
    """

    if item_count == 0:
        return False
    containers = [payload]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            containers.append(data)
            if isinstance(data.get("data"), dict):
                containers.append(data["data"])
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("hasNext", "hasMore", "has_next", "has_more"):
            value = _as_bool(container.get(key))
            if value is not None:
                return value
        for key in ("isEnd", "is_end", "lastPage", "last_page", "end"):
            value = _as_bool(container.get(key))
            if value is not None:
                return not value
        total_page = _as_int(
            container.get("totalPage")
            or container.get("totalPages")
            or container.get("pageCount")
            or container.get("pages")
        )
        if total_page is not None:
            return page < total_page
        total = _as_int(
            container.get("total")
            or container.get("totalCount")
            or container.get("count")
        )
        if total is not None:
            current = (
                _as_int(
                    container.get("page")
                    or container.get("pageNo")
                    or container.get("currentPage")
                )
                or page
            )
            size = (
                _as_int(container.get("pageSize") or container.get("size"))
                or page_size
            )
            return current * size < total
    return True


def date_range(orders: Iterable[Dict]) -> Optional[Tuple[str, str]]:
    dates = []
    for order in orders:
        value = order.get("occurred")
        if value:
            dates.append(value)
    if not dates:
        return None
    return min(dates), max(dates)


def _unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _resolve(base: str, value: str) -> str:
    value = value.strip()
    if value.startswith("//"):
        return "https:" + value
    if value.startswith(("http://", "https://")):
        return value
    return urllib.parse.urljoin(base, value)


def _url_host(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.hostname or DIANPING_HOST


def probe_order_page(
    client: HttpClient,
    jar: CookieJar,
    url: str = DIANPING_ORDER_PAGE,
    *,
    save_html: Optional[Path] = None,
) -> Dict:
    """Fetch the logged-in order page once and report the bundles it loads.

    Only the page HTML and asset URLs are inspected.  The returned structure
    contains no Cookie header, no token value and no raw HTML, so it is safe to
    write under ``out/`` for diagnostics.
    """

    response = client.get(
        url,
        cookie_header=jar.header_for(_url_host(url)),
        headers={
            "Referer": DIANPING_PC_HOME,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )

    location = response.header("location")
    if response.status in (301, 302, 303, 307, 308):
        if location:
            check_dianping_html("", location, response.status)
            raise ApiError(
                "大众点评订单页跳转到了未知地址"
                f"（HTTP {response.status}）"
            )
        raise ApiError(f"大众点评订单页返回了空跳转（HTTP {response.status}）")

    text = response.text
    check_dianping_html(text, response.url, response.status)
    if save_html is not None:
        save_html = Path(save_html)
        save_html.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(save_html, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(response.body)
        os.chmod(save_html, 0o600)

    title_match = TITLE_RE.search(text)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    scripts = _unique(
        _resolve(response.url, value)
        for value in SCRIPT_SRC_RE.findall(text) + LINK_JS_RE.findall(text)
    )
    orderish = _unique(
        _resolve(response.url, value) for value in ORDERISH_PATH_RE.findall(text)
    )
    anchors = _unique(
        _resolve(response.url, value)
        for value in ANCHOR_RE.findall(text)
        if re.search(
            r"(account|order|coupon|mylist|member|my|user|ajax|api)", value, re.I
        )
    )
    forms = []
    for tag in FORM_RE.findall(text):
        attrs = dict((key.lower(), value) for key, value in ATTR_RE.findall(tag))
        forms.append(
            {
                "method": attrs.get("method", "get").lower(),
                "action": _resolve(response.url, attrs.get("action", "")),
            }
        )
    data_urls = _unique(
        _resolve(response.url, value)
        for tag in TAG_RE.findall(text)
        for key, value in ATTR_RE.findall(tag)
        if re.search(r"(url|api|order|coupon|page)", key, re.I)
        and re.match(r"^(?:https?://|//|/)", value)
    )
    return {
        "kind": "dianping_probe",
        "requested_url": url,
        "final_url": response.url,
        "status": response.status,
        "content_type": response.header("content-type"),
        "title": title,
        "html_bytes": len(response.body),
        "scripts": scripts,
        "inline_order_paths": orderish[:150],
        "links": anchors[:150],
        "forms": forms[:20],
        "data_urls": data_urls[:100],
    }



# ---------------------------------------------------------------------------
# 《我的订单 -> 新餐饮订单》 (server-rendered HTML)
# ---------------------------------------------------------------------------

def build_daocan_url(page: int) -> str:
    """Build the read-only HTML order-list URL for 新餐饮订单.

    The page itself paginates with ``?pageno=N`` (see the ``NextPage`` anchor
    in the server response).  Page 1 has no query string.
    """

    page = int(page)
    if page < 1:
        raise ValueError("page must be >= 1")
    if page == 1:
        return DIANPING_DAOCAN_PAGE
    return f"{DIANPING_DAOCAN_PAGE}?pageno={page}"


def _clean_text(fragment: str) -> str:
    text = html.unescape(TAG_RE.sub(" ", fragment or ""))
    return re.sub(r"\s+", " ", text).strip()


def _attr(attrs: str, name: str) -> str:
    match = re.search(
        rf"""\b{re.escape(name)}\s*=\s*["']([^"']*)["']""", attrs or "", re.I
    )
    return html.unescape(match.group(1)).strip() if match else ""


def _td_text(inner: str, class_name: str) -> str:
    match = re.search(
        rf"""<td\b[^>]*class\s*=\s*["'][^"']*\b{re.escape(class_name)}\b[^"']*["'][^>]*>(.*?)</td>""",
        inner or "",
        re.I | re.S,
    )
    return _clean_text(match.group(1)) if match else ""


def extract_daocan_orders(html_text: str) -> List[Dict]:
    """Parse the server-rendered 新餐饮订单 rows.

    Only the public presentation fields are extracted.  The raw HTML is kept
    separately by the fetcher so the caller can re-parse or archive it.
    """

    orders: List[Dict] = []
    for match in DIANPING_DAOCAN_ROW_RE.finditer(html_text or ""):
        attrs, inner = match.group(1), match.group(2)
        order_id = _attr(attrs, "data-orderid")
        title_match = DIANPING_DAOCAN_H4_RE.search(inner)
        title = _clean_text(title_match.group(1)) if title_match else ""
        text = _clean_text(inner)
        if not order_id:
            no_match = re.search(r"订单号[:：]\s*([0-9A-Za-z_-]+)", text)
            order_id = no_match.group(1) if no_match else ""
        if not order_id and not title:
            continue
        status_text = _td_text(inner, "t-status")
        status_match = re.match(r"([^\s券]+)", status_text)
        coupon_codes = re.findall(r"券码[:：]\s*([0-9A-Za-z_-]+)", text)
        expire_match = re.search(r"过期时间[:：]\s*([0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2})", text)
        occurred = _clean_text(_td_text(inner, "t-time"))
        orders.append(
            {
                "order_id": order_id or None,
                "title": title or None,
                "occurred": occurred or None,
                "expire_at": expire_match.group(1) if expire_match else None,
                "quantity": _clean_text(_td_text(inner, "t-amount")) or None,
                "amount": _clean_text(_td_text(inner, "t-price")) or None,
                "status": status_match.group(1) if status_match else (status_text or None),
                "coupon_codes": coupon_codes,
            }
        )
    return orders


def has_next_daocan_page(html_text: str) -> bool:
    """True when the page contains a ``NextPage`` pagination link."""

    for match in DIANPING_DAOCAN_NEXT_RE.finditer(html_text or ""):
        attrs = match.group(1)
        if re.search(r"""\bclass\s*=\s*["'][^"']*\bNextPage\b""", attrs, re.I):
            if re.search(r"""\bhref\s*=\s*["'][^"']+["']""", attrs, re.I):
                return True
    return False


class DianpingDaocanFetcher:
    """Fetch the read-only 新餐饮订单 HTML pages.

    The list is server-rendered: the page bundle's only XHR endpoints are
    write actions (delete/cancel/refund/pay), so this fetcher never calls
    them.  It downloads the same HTML the browser shows and keeps each raw
    response for auditability.
    """

    def __init__(
        self,
        client: HttpClient,
        jar: CookieJar,
        *,
        max_pages: int = DIANPING_DEFAULT_MAX_PAGES,
    ):
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        self.client = client
        self.jar = jar
        self.max_pages = max_pages
        self.pages: List[Dict] = []
        self.warnings: List[str] = []

    def _cookie_header(self) -> str:
        return self.jar.header_for(DIANPING_HOST)

    def _request(self, page: int) -> Tuple[Response, str, List[Dict]]:
        url = build_daocan_url(page)
        response = self.client.get(
            url,
            cookie_header=self._cookie_header(),
            headers={
                "Referer": DIANPING_DAOCAN_PAGE,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        location = response.header("location")
        if response.status in (301, 302, 303, 307, 308):
            if location:
                check_dianping_html("", location, response.status)
                raise ApiError(
                    "大众点评新餐饮订单页跳转到了未知地址"
                    f"（HTTP {response.status}）"
                )
            raise ApiError(f"大众点评新餐饮订单页返回了空跳转（HTTP {response.status}）")

        text = response.text
        check_dianping_html(text, response.url, response.status)
        orders = extract_daocan_orders(text)
        return response, text, orders

    def fetch(self) -> Dict:
        previous_digest: Optional[str] = None
        for page in range(1, self.max_pages + 1):
            response, text, orders = self._request(page)
            digest = hashlib.sha256(response.body).hexdigest()
            self.pages.append(
                {
                    "source": DIANPING_DAOCAN_SOURCE,
                    "page": page,
                    "url": response.url,
                    "status": response.status,
                    "orders": orders,
                    "html": text,
                }
            )
            if not orders:
                break
            if previous_digest is not None and digest == previous_digest:
                self.warnings.append(
                    f"第 {page} 页与上一页完全相同，已停止（服务端可能忽略了 pageno 参数）"
                )
                break
            previous_digest = digest
            if not has_next_daocan_page(text):
                break
        else:
            self.warnings.append(
                f"已达到单次最大页数 {self.max_pages}，新餐饮订单可能尚未取完"
            )
        return {"kind": "dianping_orders", "pages": self.pages}


class DianpingFetcher:
    """Legacy JSON order-search fetcher (kept for compatibility/tests).

    The CLI now uses :class:`DianpingDaocanFetcher`; this class documents the
    older ``ajax/json/order/search`` path and still fails closed on mtgsig.
    """

    def __init__(
        self,
        client: HttpClient,
        jar: CookieJar,
        *,
        max_pages: int = 50,
        page_size: int = 20,
    ):
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if page_size < 1:
            raise ValueError("page_size must be >= 1")
        self.client = client
        self.jar = jar
        self.max_pages = max_pages
        self.page_size = page_size
        self.pages: List[Dict] = []
        self.warnings: List[str] = []

    def _cookie_header(self) -> str:
        return self.jar.header_for(DIANPING_HOST)

    def _request(self, page: int) -> Tuple[Response, object]:
        response = self.client.get(
            build_api_url(page, self.page_size),
            cookie_header=self._cookie_header(),
            headers={
                "Referer": DIANPING_REFERER,
                "Origin": "https://t.dianping.com",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
            },
        )

        location = response.header("location")
        if response.status in (301, 302, 303, 307, 308):
            if location:
                check_dianping_html("", location, response.status)
                raise ApiError(
                    "大众点评订单接口跳转到了未知地址"
                    f"（HTTP {response.status}）"
                )
            raise ApiError(f"大众点评订单接口返回了空跳转（HTTP {response.status}）")

        try:
            payload = json.loads(response.text)
        except ValueError:
            check_dianping_html(response.text, response.url, response.status)
            raise ApiError("大众点评订单接口返回了非 JSON 内容（页面结构可能已变化）")
        check_dianping_payload(payload, response.status)
        return response, payload

    def fetch(self) -> Dict:
        previous_digest: Optional[str] = None
        for page in range(1, self.max_pages + 1):
            response, payload = self._request(page)
            digest = hashlib.sha256(response.text.encode("utf-8")).hexdigest()
            self.pages.append(
                {
                    "source": DIANPING_SOURCE,
                    "page": page,
                    "url": response.url,
                    "status": response.status,
                    "body": payload,
                }
            )
            orders = extract_orders(payload)
            if not orders:
                break
            if previous_digest is not None and digest == previous_digest:
                self.warnings.append(
                    f"第 {page} 页与上一页完全相同，已停止（服务端可能忽略了 page 参数）"
                )
                break
            previous_digest = digest
            if not has_next_page(payload, page, self.page_size, len(orders)):
                break
        else:
            self.warnings.append(
                f"已达到单次最大页数 {self.max_pages}，大众点评订单可能尚未取完"
            )
        return {"kind": "dianping_orders", "pages": self.pages}


def summarize(payload: Dict) -> Dict:
    orders: List[Dict] = []
    seen = set()
    for page in payload.get("pages") or []:
        page_orders = page.get("orders")
        if page_orders is None:
            page_orders = extract_orders(page.get("body"))
        for index, order in enumerate(page_orders):
            order_id = order.get("order_id") or _order_id(order)
            key = order_id or f"{page.get('page')}:{index}"
            if key in seen:
                continue
            seen.add(key)
            orders.append(
                {
                    "order_id": order_id,
                    "occurred": order.get("occurred") or _order_time(order),
                }
            )
    return {
        "pages": len(payload.get("pages") or []),
        "orders": len(orders),
        "date_range": date_range(orders),
    }
