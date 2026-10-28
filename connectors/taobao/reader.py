"""Taobao 已买到的宝贝 (mtop bought list) reader.

Only the read-only ``mtop.taobao.order.queryboughtlistv2`` endpoint is used.
The request format and signing scheme were recovered from the public web
bundle and lib-mtop.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .blocking import check_taobao_html, check_taobao_ret
from ..kit.cookies import CookieJar
from ..kit.errors import ApiError, AuthRequired
from ..kit.http import HttpClient, Response

TAOBAO_API = "mtop.taobao.order.queryboughtlistv2"
TAOBAO_VERSION = "1.0"
TAOBAO_APP_KEY = "12574478"
TAOBAO_TTID = "201200@taobao_iphone_99.99.99"
TAOBAO_HOST = "h5api.m.taobao.com"
TAOBAO_ENDPOINT = (
    f"https://{TAOBAO_HOST}/h5/{TAOBAO_API}/{TAOBAO_VERSION}/"
)
TAOBAO_REFERER = (
    "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm"
)
TAOBAO_JSV = "2.7.2"
BEIJING = datetime.timezone(datetime.timedelta(hours=8))

TOKEN_COOKIE = "_m_h5_tk"
TOKEN_ENC_COOKIE = "_m_h5_tk_enc"
TOKEN_ERROR_MARKERS = (
    "TOKEN_EMPTY",
    "TOKEN_EXOIRED",  # the typo used by mtop itself
    "TOKEN_EXPIRED",
    "TOKEN_ILLEGAL",
    # lib-mtop's __processToken also retries ILLEGAL_ACCESS when using
    # synchronised cookies; treat it as a token-refresh trigger here too.
    "ILLEGAL_ACCESS",
)

# Login cookies live in browser.  The two required ones are enough to tell a
# logged-in session apart from a missing/stale one; the others are reported
# by `status` but are not prerequisites for the read-only mtop call.
TAOBAO_LOGIN_COOKIES = ("cookie2", "unb", "_tb_token_", "sgcookie")
TAOBAO_REQUIRED_LOGIN_COOKIES = ("cookie2", "unb")

# One request plus at most one retry, as requested.  lib-mtop's own default
# is five attempts; this tool deliberately caps it lower.
MAX_PAGE_ATTEMPTS = 2


@dataclass
class TaobaoRequest:
    url: str
    form: Dict[str, str]
    data_json: str
    sign: str
    timestamp_ms: int
    app_key: str = TAOBAO_APP_KEY
    token: str = field(default="", repr=False)


def build_page_data(page: int, tab_code: str = "all") -> Dict:
    """The exact data object used by the bundle for one page."""

    return {
        "tabCode": tab_code or "all",
        "appVersion": "3.0",
        "OrderType": "OrderList",
        "condition": {
            "inner": 1,
            "deviceLevel": "high",
            "version": "1.0.0",
            "reqFrom": "tbpc",
        },
        "appName": "tborder",
        "templateConfigVersion": 0,
        "forceBoughtlist4": 4,
        "page": int(page),
    }


def canonical_json(value) -> str:
    """Match JSON.stringify: compact separators, real UTF-8 characters."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def mtop_sign(token: str, timestamp_ms: int, app_key: str, data_json: str) -> str:
    """md5(token + '&' + t + '&' + appKey + '&' + data).

    Confirmed in lib-mtop 2.7.2 ``__processRequestUrl``.  The token is the
    first ``_``-separated part of the ``_m_h5_tk`` cookie.
    """

    payload = f"{token}&{timestamp_ms}&{app_key}&{data_json}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def build_request(
    page: int,
    token: str,
    *,
    timestamp_ms: Optional[int] = None,
    app_key: str = TAOBAO_APP_KEY,
    user_agent: str,
    tab_code: str = "all",
) -> TaobaoRequest:
    if timestamp_ms is None:
        timestamp_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    data_json = canonical_json(build_page_data(page, tab_code))
    sign = mtop_sign(token, timestamp_ms, app_key, data_json)
    query = [
        ("jsv", TAOBAO_JSV),
        ("appKey", app_key),
        ("t", str(timestamp_ms)),
        ("sign", sign),
        ("api", TAOBAO_API),
        ("v", TAOBAO_VERSION),
        ("type", "originaljson"),
        ("ttid", TAOBAO_TTID),
    ]
    url = TAOBAO_ENDPOINT + "?" + urllib.parse.urlencode(query)
    return TaobaoRequest(
        url=url,
        form={"data": data_json, "ua": user_agent},
        data_json=data_json,
        sign=sign,
        timestamp_ms=timestamp_ms,
        app_key=app_key,
        token=token,
    )


def ret_text(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    ret = payload.get("ret")
    if isinstance(ret, str):
        return ret
    if isinstance(ret, list):
        return " ".join(str(item) for item in ret)
    return ""


def is_token_error(payload) -> bool:
    upper = ret_text(payload).upper()
    return any(marker in upper for marker in TOKEN_ERROR_MARKERS)


def missing_login_cookies(jar: CookieJar, host: str = TAOBAO_HOST) -> List[str]:
    """Names of required Taobao login cookies absent from the jar.

    Only these cookies decide whether the user is logged in.  A missing
    ``_m_h5_tk`` is deliberately *not* an error: lib-mtop bootstraps it with
    an empty-token request, and so do we.
    """

    return [
        name
        for name in TAOBAO_REQUIRED_LOGIN_COOKIES
        if not jar.get(name, host)
    ]


def require_login(jar: CookieJar, host: str = TAOBAO_HOST) -> None:
    missing = missing_login_cookies(jar, host)
    if missing:
        raise AuthRequired(
            "浏览器中没有找到淘宝登录 Cookie: " + ", ".join(missing),
            "请在浏览器中打开 https://www.taobao.com/ 并扫码登录后重试；"
            "_m_h5_tk 会由工具自动刷新，不需要手动处理",
        )


def refresh_tokens_from_response(
    jar: CookieJar, response: Response, *, default_host: str = TAOBAO_HOST
) -> bool:
    """Apply response cookies once; report whether a usable token was refreshed. Never write back to browser."""

    names = jar.apply_response(response)
    return TOKEN_COOKIE in names and bool(jar.get(TOKEN_COOKIE, default_host))


def extract_components(page) -> Dict:
    """Return the component map no matter whether page is raw or wrapped."""

    if not isinstance(page, dict):
        return {}
    data = page.get("data")
    if isinstance(data, dict):
        if any(key.startswith("MainGroup_") for key in data):
            return data
        nested = data.get("data")
        if isinstance(nested, dict):
            return nested
    if any(key.startswith("MainGroup_") for key in page):
        return page
    return {}


def page_order_ids(page) -> List[str]:
    components = extract_components(page)
    ids = []
    for key, value in components.items():
        if not key.startswith("MainGroup_"):
            continue
        fields = (value or {}).get("fields") or {}
        order_id = fields.get("orderId")
        if order_id:
            ids.append(str(order_id))
    return ids


def has_main_group(page) -> bool:
    return bool(page_order_ids(page)) or any(
        key.startswith("MainGroup_") for key in extract_components(page)
    )


def _yuan(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("¥", "").replace(",", "").strip()
    return text or None


def _timestamp(value) -> Optional[datetime.datetime]:
    if value is None or value == "":
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number < 10_000_000_000:  # seconds, not milliseconds
        number *= 1000
    try:
        return datetime.datetime.fromtimestamp(number / 1000, BEIJING)
    except (OverflowError, OSError, ValueError):
        return None


def parse_orders(pages: Iterable) -> List[Dict]:
    """One row per unique order, compatible with the existing parser."""

    output: Dict[str, Dict] = {}
    for page in pages:
        data = extract_components(page)
        for key, value in data.items():
            if not key.startswith("MainGroup_"):
                continue
            fields = (value or {}).get("fields") or {}
            order_id = fields.get("orderId")
            if not order_id or str(order_id) in output:
                continue
            order_id = str(order_id)
            seller = (data.get(f"sellerInfo_{order_id}") or {}).get("fields") or {}
            item_keys = sorted(
                item_key
                for item_key in data
                if item_key.startswith(
                    (f"item_{order_id}_", f"itemFliggy_{order_id}_")
                )
            )
            items = []
            for item_key in item_keys:
                item = (data.get(item_key) or {}).get("fields") or {}
                inner = item.get("item") or {}
                items.append(
                    {
                        "title": inner.get("title"),
                        "sku": inner.get("skuText"),
                        "quantity": inner.get("quantity"),
                        "paid": _yuan(
                            (inner.get("priceInfo") or {}).get("actualTotalFee")
                        ),
                    }
                )
            pay = {}
            for pay_key, pay_value in data.items():
                if pay_key == f"pay_{order_id}" or pay_key.startswith(
                    f"pay_{order_id}/"
                ):
                    pay = (pay_value or {}).get("fields") or {}
                    break
            fees = [(pay.get("actualFee") or {}).get("value")]
            fees.extend(
                ((item.get("itemPay") or {}).get("actualFee") or {}).get("value")
                for item in (
                    ((data.get(item_key) or {}).get("fields") or {})
                    for item_key in item_keys
                )
            )
            paid = next((fee for fee in fees if fee), None)
            shop = seller.get("seller") or {}
            when = _timestamp(
                fields.get("createOrderTime") or seller.get("orderCreateTime")
            )
            main = (data.get(f"Main_{order_id}") or {}).get("fields") or {}
            output[order_id] = {
                "order_id": order_id,
                "occurred": when.strftime("%Y-%m-%d %H:%M:%S") if when else None,
                "shop": " ".join(
                    part
                    for part in (shop.get("shopName"), shop.get("shopSubName"))
                    if part
                )
                or None,
                "items": items,
                "paid": _yuan(paid),
                "status": (seller.get("status") or {}).get("text"),
                "order_status_code": fields.get("orderStatus"),
                "order_type": main.get("orderType"),
                "archived": str(fields.get("archive")).lower() == "true",
            }
    return list(output.values())


def date_range(orders: Iterable[Dict]) -> Optional[Tuple[str, str]]:
    dates = [order.get("occurred") for order in orders if order.get("occurred")]
    if not dates:
        return None
    return min(dates), max(dates)


class TaobaoFetcher:
    """Fetch pages, stopping exactly as the page itself does."""

    def __init__(
        self,
        client: HttpClient,
        jar: CookieJar,
        *,
        app_key: str = TAOBAO_APP_KEY,
        max_pages: int = 100,
        tab_code: str = "all",
    ):
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        self.client = client
        self.jar = jar
        self.app_key = app_key
        self.max_pages = max_pages
        self.tab_code = tab_code
        self.pages: List[Dict] = []
        self.warnings: List[str] = []
        self.token_rotations = 0
        self.token_retries = 0

    def _request_once(self, page: int) -> Response:
        raw_token = self.jar.get(TOKEN_COOKIE, TAOBAO_HOST) or ""
        # lib-mtop uses only the part before the first "_" (value is token_ts).
        # With no cookie at all this is an empty token, which is exactly the
        # bootstrap request lib-mtop sends before it has a token.
        token = raw_token.split("_", 1)[0]
        plan = build_request(
            page,
            token,
            app_key=self.app_key,
            user_agent=self.client.user_agent,
            tab_code=self.tab_code,
        )
        return self.client.post_form(
            plan.url,
            plan.form,
            cookie_header=self.jar.header_for(TAOBAO_HOST),
            headers={
                "Referer": TAOBAO_REFERER,
                "Origin": "https://buyertrade.taobao.com",
            },
        )

    def _decode(self, response: Response) -> Dict:
        text = response.text
        try:
            payload = json.loads(text)
        except ValueError:
            check_taobao_html(text, response.url, response.status)
            raise ApiError("淘宝返回了非 JSON 内容（页面结构可能已变化）")
        if not isinstance(payload, dict):
            check_taobao_html(text, response.url, response.status)
            raise ApiError("淘宝返回了意外的 JSON 类型")
        return payload

    def _raise_token_error(self, rotated: bool) -> None:
        missing = missing_login_cookies(self.jar)
        if missing:
            raise AuthRequired(
                "浏览器中没有找到淘宝登录 Cookie: " + ", ".join(missing),
                "请在浏览器中打开 https://www.taobao.com/ 并扫码登录后重试；"
                "_m_h5_tk 会由工具自动刷新，不需要手动处理",
            )
        if rotated:
            message = "淘宝 _m_h5_tk 令牌刷新后仍被拒绝（每页最多重试 1 次）"
        else:
            message = "淘宝 _m_h5_tk 令牌无效，且服务器响应没有提供新的令牌"
        raise AuthRequired(
            message,
            "请先在浏览器中打开一次淘宝“已买到的宝贝”页面；"
            "如果仍然失败，请在浏览器中重新扫码登录淘宝",
        )

    def _request_page(self, page: int) -> Tuple[Response, Dict]:
        """Request one page, applying lib-mtop's token bootstrap once.

        A token error may be retried exactly once, and only when the response
        supplied a new ``_m_h5_tk`` via ``Set-Cookie``.  The page counter never
        moves on a retry, so no page is ever requested more than twice.
        """

        for attempt in range(1, MAX_PAGE_ATTEMPTS + 1):
            response = self._request_once(page)
            rotated = refresh_tokens_from_response(self.jar, response)
            if rotated:
                self.token_rotations += 1
            payload = self._decode(response)
            if not is_token_error(payload):
                check_taobao_ret(payload)
                return response, payload
            if attempt < MAX_PAGE_ATTEMPTS and rotated:
                self.token_retries += 1
                continue
            self._raise_token_error(rotated)
        raise AssertionError("unreachable")

    def fetch(self) -> Dict:
        require_login(self.jar)
        previous_ids: Optional[List[str]] = None
        page = 1
        while page <= self.max_pages:
            response, payload = self._request_page(page)
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ApiError("淘宝接口没有返回 data 组件")
            page_object = data
            if isinstance(data.get("data"), dict) and not any(
                key.startswith("MainGroup_") for key in data
            ):
                page_object = data["data"]
            self.pages.append(page_object)
            ids = page_order_ids(page_object)
            if not ids:
                break
            if previous_ids is not None and set(ids) == set(previous_ids):
                break
            previous_ids = ids
            page += 1
        else:
            self.warnings.append(
                f"已达到单次最大页数 {self.max_pages}，淘宝列表可能尚未取完"
            )

        return {
            "kind": "boughtlist_mtop",
            "source": TAOBAO_ENDPOINT,
            "pages": self.pages,
        }

    def probe(self) -> Dict:
        """One lightweight read-only request for status/keepalive."""

        require_login(self.jar)
        response, payload = self._request_page(1)
        return {
            "status": response.status,
            "ret": ret_text(payload)[:120],
            "token_rotations": self.token_rotations,
            "token_retries": self.token_retries,
            "has_data": isinstance(payload.get("data"), dict),
        }


def summarize(payload: Dict) -> Dict:
    orders = parse_orders(payload.get("pages") or [])
    span = date_range(orders)
    return {
        "pages": len(payload.get("pages") or []),
        "orders": len(orders),
        "date_range": span,
    }
