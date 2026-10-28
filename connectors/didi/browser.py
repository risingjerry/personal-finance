"""Browser-assisted, read-only Didi trip history reader.

Why a browser:

* Didi's web app signs *its own* axios requests with ``wsgsig``.  We do not
  know, compute, forge or replay that signature.  Instead we launch a real
  Chromium engine, put the owner's browser session cookies into an in-memory
  browser context, and let Didi's own JavaScript build and sign the requests.

Safety rules implemented here:

* cookies are passed only through memory (Playwright API or a child-process
  stdin pipe); they are never printed, logged, put in argv, or written to disk;
* only read-only GET traffic is observed; the only clicks are clicks on order
  cards to open their detail page;
* a captcha/slider/login/risk page stops the run immediately;
* saved JSON has token/ticket/sign values recursively redacted.

The module is import-safe on a Python without Playwright: every Playwright
import is lazy so the normal CLI and the offline unit tests keep working.
"""

from __future__ import annotations

import datetime
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .blocking import DIDI_LOGIN_MARKERS
from ..kit.browser import (  # noqa: F401  (re-exported: tests and actions import them from here)
    BrowserEngine,
    _node_env,
    _node_module_paths,
    _redact_text,
    detect_browser_engine,
    find_installed_chromium,
    redact_browser,
    redact_url,
)
from ..kit.cookies import CookieJar
from .session import merge_cookies
from .reader import (
    BEIJING,
    _find_fare,
    _iter_order_items,
    _order_id,
    _product_id,
    _timestamp,
    date_range,
)
from ..kit.errors import (
    ApiError,
    AuthRequired,
    BrowserUnavailable,
    NetworkError,
    ConnectorError,
    RiskBlocked,
)

DIDI_ORDER_LIST_URL = (
    "https://common.diditaxi.com.cn/general/webEntry?h=1#/order-list"
)
DIDI_ORDER_LIST_PATH = "/passenger/history"
DIDI_DETAIL_PATH_RE = re.compile(r"/pOrderDetail/?$", re.IGNORECASE)
DIDI_FARE_PATH_RE = re.compile(
    r"/(?:pGetRealTimeFeeDetail|pGetBillDetail)/?$", re.IGNORECASE
)

# Buttons/text that would turn this read-only run into a write action.  They
# are never clicked.  An order card may still contain such a label; we click a
# neutral child (route/time text) instead of the label.
FORBIDDEN_ACTION_WORDS = (
    "支付",
    "去支付",
    "立即支付",
    "发票",
    "开发票",
    "开票",
    "投诉",
    "取消订单",
    "取消行程",
    "退款",
    "申请退款",
    "评价",
    "修改订单",
    "删除订单",
    "确认上车",
    "确认到达",
)

_BLOCK_SELECTORS = (
    ".geetest_panel",
    ".geetest_widget",
    ".nc-container",
    ".nc_1_wrapper",
    "[class*='slide-verify']",
    "[class*='slider-verify']",
    "[class*='captcha']",
    "[id*='captcha']",
    "iframe[src*='captcha']",
    "iframe[src*='verify']",
    ".verify-wrap",
    ".verify-box",
)

def _is_local_host(host: str) -> bool:
    host = host.lstrip(".").lower()
    return host in ("localhost", "127.0.0.1", "::1")


def cookie_specs(jar: CookieJar) -> List[Dict[str, Any]]:
    """Convert the in-memory browser jar to Playwright ``add_cookies`` records.

    Values stay in memory.  This function never logs or returns a repr that
    includes a value.
    """

    specs: List[Dict[str, Any]] = []
    for cookie in jar.entries():
        if not cookie.name or not cookie.value:
            continue
        domain = (cookie.host_key or "").strip()
        if not domain:
            continue
        local = _is_local_host(domain)
        specs.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": domain,
                "path": cookie.path or "/",
                # Secure cookies are still sent over HTTPS; keeping this false
                # for localhost lets the offline synthetic test use HTTP.
                "secure": not local,
                "sameSite": "Lax",
            }
        )
    return specs


# ---------------------------------------------------------------------------
# Response capture
# ---------------------------------------------------------------------------


@dataclass
class Capture:
    kind: str
    url: str
    status: int
    response: Any
    order_id: str = ""
    page: Optional[int] = None
    timemode: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "status": self.status,
            "response": self.response,
            "order_id": self.order_id,
            "page": self.page,
            "timemode": self.timemode,
        }


def _capture_kind(path: str) -> str:
    path = (path or "").rstrip("/")
    if path.endswith(DIDI_ORDER_LIST_PATH):
        return "order_list"
    if DIDI_DETAIL_PATH_RE.search(path):
        return "order_detail"
    if DIDI_FARE_PATH_RE.search(path):
        return "fare_detail"
    return ""


def _query_dict(url: str) -> Dict[str, str]:
    try:
        pairs = urllib.parse.parse_qsl(
            urllib.parse.urlsplit(url).query, keep_blank_values=True
        )
    except ValueError:
        return {}
    output: Dict[str, str] = {}
    for key, value in pairs:
        output.setdefault(key, value)
    return output


class CaptureStore:
    """Thread-free capture store fed by Playwright response callbacks."""

    def __init__(self) -> None:
        self.captures: List[Capture] = []
        self.list_captures: List[Capture] = []
        self.details: Dict[str, Dict[str, Any]] = {}
        self.current_order_id = ""
        self.dom_orders: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self.http_errors: List[Tuple[str, int, str]] = []
        self._seen: set = set()

    def on_response(self, response: Any, page: Any = None) -> None:
        try:
            url = getattr(response, "url", "") or ""
            path = urllib.parse.urlsplit(url).path
            kind = _capture_kind(path)
            if not kind:
                return
            status = int(getattr(response, "status", 0) or 0)
            query = _query_dict(url)
            order_id = (
                query.get("order_id")
                or query.get("oid")
                or query.get("orderId")
                or self.current_order_id
            )
            if kind == "order_list":
                page_number = None
                try:
                    page_number = int(query.get("pagenum", "0") or 0)
                except (TypeError, ValueError):
                    page_number = 0
                timemode = query.get("timemode", "")
            else:
                page_number = None
                timemode = ""
            key = (kind, page_number, timemode, order_id)
            if key in self._seen:
                return
            self._seen.add(key)

            payload: Any = None
            if status >= 400:
                self.http_errors.append((kind, status, url))
                payload = {"_http_status": status}
            else:
                try:
                    payload = response.json()
                except Exception:
                    try:
                        text = response.text() or ""
                    except Exception:
                        text = ""
                    payload = {"_non_json": True, "text": text[:2000]}
            capture = Capture(
                kind=kind,
                url=url,
                status=status,
                response=payload,
                order_id=order_id if kind != "order_list" else "",
                page=page_number,
                timemode=timemode,
            )
            self.captures.append(capture)
            if kind == "order_list":
                self.list_captures.append(capture)
            else:
                bucket = self.details.setdefault(
                    order_id or "", {"detail": None, "fare": None}
                )
                target = "detail" if kind == "order_detail" else "fare"
                if bucket.get(target) is None:
                    bucket[target] = payload
        except Exception:
            # A capture callback must never take down the browser run.
            return

    def list_order_ids(self) -> List[str]:
        ids: List[str] = []
        seen = set()
        for capture in self.list_captures:
            for item in _iter_order_items(capture.response):
                oid = _order_id(item)
                if oid and oid not in seen:
                    seen.add(oid)
                    ids.append(oid)
        return ids

    def list_order_count(self) -> int:
        return len(self.list_order_ids())

    def last_list_response(self) -> Optional[Dict[str, Any]]:
        for capture in reversed(self.list_captures):
            if isinstance(capture.response, dict):
                return capture.response
        return None

    def has_detail(self, order_id: str) -> bool:
        bucket = self.details.get(order_id) or {}
        return bool(bucket.get("detail") or bucket.get("fare"))


# ---------------------------------------------------------------------------
# Risk / login classification
# ---------------------------------------------------------------------------


def _contains_any(text: str, markers: Iterable[str]) -> Optional[str]:
    lowered = (text or "").lower()
    for marker in markers:
        if marker.lower() in lowered:
            return marker
    return None


def classify_block(
    url: str,
    text: str = "",
    *,
    selector_hits: int = 0,
) -> Optional[Tuple[str, str]]:
    """Return ``(kind, message)`` for a login/captcha/risk page.

    kind is ``auth``, ``risk`` or ``slider``.  The caller stops immediately;
    nothing is ever solved or bypassed.
    """

    lowered_url = (url or "").lower()
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        path = (urllib.parse.urlsplit(url).path or "").lower()
    except ValueError:
        host, path = "", ""

    if host.startswith(("passport.", "login.", "sso.", "epassport.")):
        return ("auth", "滴滴跳转到了登录页")
    if any(
        marker in lowered_url
        for marker in ("x5sec", "punish", "/captcha", "/verify", "/risk")
    ) or any(marker in path for marker in ("captcha", "verify", "risk")):
        return ("risk", "滴滴请求被重定向到验证/风控地址")

    if selector_hits:
        return ("risk", "页面出现了验证码或滑块组件")

    risk_marker = _contains_any(
        text,
        (
            "验证码",
            "滑动验证",
            "请完成验证",
            "安全验证",
            "访问频繁",
            "请求频繁",
            "操作频繁",
            "账号异常",
            "风险拦截",
            "请完成安全验证",
        ),
    )
    if risk_marker:
        return ("risk", f"页面显示了验证/风控提示（{risk_marker}）")

    auth_marker = _contains_any(
        text,
        (
            "请登录",
            "请先登录",
            "登录已失效",
            "登录状态已失效",
            "未登录",
            "重新登录",
            "login_outdate",
        ),
    )
    if auth_marker:
        return ("auth", f"页面显示了登录提示（{auth_marker}）")

    # The public Didi markers are broader; use them only when the page looks
    # like a standalone login/risk page, not for ordinary app chrome text.
    if _contains_any(text, DIDI_LOGIN_MARKERS) and (
        "密码" in text or "手机号" in text or "验证码登录" in text
    ):
        return ("auth", "页面显示了滴滴登录表单")
    return None


def _visible_text(page: Any, limit: int = 8000) -> str:
    try:
        return (page.locator("body").inner_text(timeout=1500) or "")[:limit]
    except Exception:
        return ""


def _selector_hits(page: Any) -> int:
    try:
        for frame in page.frames:
            frame_url = (getattr(frame, "url", "") or "").lower()
            if any(
                marker in frame_url
                for marker in ("captcha", "geetest", "verify", "risk")
            ):
                return 1
    except Exception:
        pass
    for selector in _BLOCK_SELECTORS:
        try:
            locator = page.locator(selector)
            if locator.count() <= 0:
                continue
            try:
                if locator.first.is_visible():
                    return 1
            except Exception:
                return 1
        except Exception:
            continue
    return 0


def _current_block(page: Any) -> Optional[Tuple[str, str]]:
    return classify_block(
        getattr(page, "url", "") or "",
        _visible_text(page),
        selector_hits=_selector_hits(page),
    )


# ---------------------------------------------------------------------------
# Python Playwright driver
# ---------------------------------------------------------------------------


def _has_forbidden_words(text: str) -> bool:
    return any(word in (text or "") for word in FORBIDDEN_ACTION_WORDS)


def _safe_click(page: Any, card: Any) -> bool:
    """Click a neutral part of an order card, never a write-action button."""

    try:
        card.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    for selector in (
        ".order-desc",
        ".order-sub-desc",
        ".from",
        ".to",
        ".order-time",
        ".time",
    ):
        try:
            target = card.locator(selector).first
            if target.count() <= 0:
                continue
            if not target.is_visible():
                continue
            label = target.inner_text(timeout=800) or ""
            if _has_forbidden_words(label):
                continue
            target.click(timeout=5000)
            return True
        except Exception:
            continue
    try:
        box = card.bounding_box()
        if box and box.get("width") and box.get("height"):
            page.mouse.click(
                box["x"] + min(18, box["width"] / 3),
                box["y"] + min(18, box["height"] / 3),
            )
            return True
    except Exception:
        pass
    try:
        if _has_forbidden_words(card.inner_text(timeout=800) or ""):
            return False
        card.click(timeout=5000)
        return True
    except Exception:
        return False


class PythonPlaywrightDriver:
    """Drive an installed Python Playwright Chromium."""

    def __init__(
        self,
        engine: BrowserEngine,
        *,
        headless: bool = True,
        timeout: float = 30.0,
        max_pages: int = 50,
        with_detail: bool = True,
        with_fare: bool = True,
        max_details: int = 0,
        url: str = DIDI_ORDER_LIST_URL,
    ):
        self.engine = engine
        self.headless = headless
        self.timeout = timeout
        self.max_pages = max_pages
        self.with_detail = with_detail
        self.with_fare = with_fare
        self.max_details = max_details
        self.url = url

    def _add_cookies(self, context: Any, specs: List[Dict[str, Any]]) -> int:
        added = 0
        for spec in specs:
            try:
                context.add_cookies([spec])
                added += 1
            except Exception:
                continue
        return added

    def _on_new_page(self, new_page: Any, store: CaptureStore) -> None:
        try:
            new_page.on("response", lambda response: store.on_response(response, new_page))
        except Exception:
            pass

    def _dismiss_safe_modals(self, page: Any) -> None:
        for text in ("我知道了", "知道了", "关闭"):
            if _has_forbidden_words(text):
                continue
            try:
                locator = page.get_by_text(text, exact=True)
                if locator.count() <= 0:
                    continue
                target = locator.last
                if target.is_visible():
                    target.click(timeout=2000)
            except Exception:
                continue

    def _wait_first_list(self, page: Any, store: CaptureStore) -> None:
        deadline = time.monotonic() + max(15.0, self.timeout)
        while time.monotonic() < deadline:
            block = _current_block(page)
            if block is not None:
                raise _block_error(block)
            if store.list_captures:
                if store.http_errors:
                    self._raise_http_error(store)
                self._raise_list_payload_error(store)
                return
            if store.http_errors:
                self._raise_http_error(store)
            text = _visible_text(page)
            if "加载失败" in text or "暂时无法获取您的订单" in text:
                # Give the page a moment: the app first shows this while the
                # login/SDK bootstrap is still running.
                if time.monotonic() + 2.0 < deadline:
                    page.wait_for_timeout(2000)
                if not store.list_captures:
                    raise AuthRequired(
                        "滴滴订单页没有进入订单列表（登录态可能已失效）",
                        "页面显示“加载失败/暂时无法获取您的订单”。请先在浏览器中"
                        "重新登录滴滴，确认能人工打开订单列表后再运行；"
                        "本工具不会尝试绕过登录或风控。",
                    )
            page.wait_for_timeout(500)
        raise NetworkError(
            "等待滴滴订单列表超时",
            "页面可能加载很慢或网络受限；可增大 --timeout 后重试",
        )

    def _raise_http_error(self, store: CaptureStore) -> None:
        for kind, status, _url in store.http_errors:
            if kind == "order_list":
                if status in (401, 403):
                    raise RiskBlocked(
                        f"滴滴订单列表返回 HTTP {status}",
                        "可能是登录失效或风控；请先在浏览器中人工确认",
                    )
                if status == 501:
                    raise RiskBlocked(
                        "滴滴订单列表返回 HTTP 501（请求可能缺少 wsgsig 签名）",
                        "页面自身签名仍未通过；本工具不会重算、伪造或重放 "
                        "wsgsig。请先在浏览器中人工打开订单列表确认可用",
                    )
                raise NetworkError(f"滴滴订单列表返回 HTTP {status}")
            if kind in ("order_detail", "fare_detail") and status in (401, 403):
                raise RiskBlocked(
                    f"滴滴详情/费用接口返回 HTTP {status}",
                    "可能是登录失效或风控；本工具会立即停止",
                )

    def _raise_list_payload_error(self, store: CaptureStore) -> None:
        for capture in store.list_captures:
            payload = capture.response
            if not isinstance(payload, dict):
                continue
            if payload.get("_non_json"):
                text = str(payload.get("text") or "")
                block = classify_block(capture.url, text)
                if block is not None:
                    raise _block_error(block)
                if "登录" in text:
                    raise AuthRequired(
                        "滴滴列表接口返回了登录 HTML",
                        "请先在浏览器中人工重新登录滴滴后重试",
                    )
                if any(
                    marker in text
                    for marker in ("验证码", "验证", "风控", "频繁", "captcha")
                ):
                    raise RiskBlocked(
                        "滴滴列表接口返回了验证/风控 HTML",
                        "本工具会立即停止，不会尝试破解或绕过",
                    )
                raise ApiError("滴滴列表接口返回了非 JSON 内容")
            errno = payload.get("errno")
            if errno in (0, "0", None):
                continue
            text = " ".join(
                str(payload.get(key) or "")
                for key in ("errmsg", "error", "message", "msg", "ret")
            )
            lowered = text.lower()
            if any(
                marker in lowered
                for marker in (
                    "wsgsig", "签名", "验签", "sign error", "invalid sign",
                    "missing sign",
                )
            ):
                raise RiskBlocked(
                    "滴滴列表接口拒绝了浏览器请求（wsgsig 签名未被接受）",
                    "页面自身签名仍未通过；本工具不会重算、伪造或重放 wsgsig。"
                    "请先在浏览器中人工打开订单列表确认可用",
                )
            if str(errno) in ("101", "401", "41005") or any(
                marker in text
                for marker in ("请登录", "未登录", "登录失效", "login")
            ):
                raise AuthRequired(
                    "滴滴列表接口返回未登录",
                    "请先在浏览器中人工重新登录滴滴后重试；本工具不会绕过登录",
                )
            if str(errno) == "403" or any(
                marker in text for marker in ("风控", "验证", "频繁", "限制")
            ):
                raise RiskBlocked(
                    "滴滴列表接口返回验证/风控提示",
                    "本工具会立即停止，不会尝试破解或绕过",
                )
            raise ApiError(f"滴滴列表接口返回失败 (errno={errno!r})")

    def _scroll_to_oldest(self, page: Any, store: CaptureStore) -> None:
        last_count = store.list_order_count()
        stable = 0
        rounds = max(3, self.max_pages * 3)
        for _ in range(rounds):
            if self._list_finished(store):
                return
            try:
                page.evaluate(
                    """() => {
                        const els = [document.scrollingElement,
                                     ...document.querySelectorAll('*')];
                        for (const el of els) {
                            if (!el) continue;
                            if (el.scrollHeight > el.clientHeight + 40) {
                                el.scrollTop = el.scrollHeight;
                            }
                        }
                        window.scrollTo(0, document.body.scrollHeight);
                    }"""
                )
                page.mouse.move(620, 520)
                page.mouse.wheel(0, 24000)
            except Exception:
                pass
            page.wait_for_timeout(1400)
            block = _current_block(page)
            if block is not None:
                raise _block_error(block)
            if store.list_captures:
                self._raise_list_payload_error(store)
            count = store.list_order_count()
            if count > last_count:
                last_count = count
                stable = 0
            else:
                stable += 1
            text = _visible_text(page)
            if "没有更多" in text or "已加载全部" in text or "到底了" in text:
                return
            if stable >= 4:
                if not self._list_finished(store):
                    store.warnings.append(
                        "下拉加载在连续多轮后没有新订单，可能未到最旧一单"
                    )
                return
        store.warnings.append(
            f"已达到最大滚动轮数（{rounds}），滴滴行程可能尚未取完"
        )

    def _list_finished(self, store: CaptureStore) -> bool:
        response = store.last_list_response()
        if not isinstance(response, dict):
            return False
        if "havenext" in response:
            value = response.get("havenext")
            if value is False or value in (0, "0", "false", "False"):
                return True
        if not response.get("timemode"):
            items = list(_iter_order_items(response))
            if not items and store.list_captures:
                return True
        return False

    def _order_cards(self, page: Any) -> List[Dict[str, Any]]:
        try:
            return page.evaluate(
                """() => [...document.querySelectorAll('.order-item')].map((el, i) => {
                    const vm = el.__vue__;
                    const item = (vm && vm.item) || {};
                    const oid = item.orderId || item.oid || item.order_id || '';
                    return {
                        index: i,
                        order_id: String(oid || ''),
                        text: (el.innerText || '').slice(0, 300),
                    };
                })"""
            )
        except Exception:
            return []

    def _wait_for_detail(self, page: Any, store: CaptureStore, oid: str) -> None:
        deadline = time.monotonic() + max(10.0, self.timeout)
        while time.monotonic() < deadline:
            if store.http_errors:
                self._raise_http_error(store)
            block = _current_block(page)
            if block is not None:
                raise _block_error(block)
            if store.has_detail(oid):
                # Give a second, separate fare XHR a short window to arrive.
                if self.with_fare and not (store.details.get(oid) or {}).get("fare"):
                    page.wait_for_timeout(800)
                return
            page.wait_for_timeout(300)
        store.warnings.append(f"订单 {oid} 未捕获到详情/费用响应")

    def _restore_order_list(self, page: Any, store: CaptureStore) -> None:
        current = (getattr(page, "url", "") or "").split("?")[0].rstrip("/")
        if "#/order-list" in (getattr(page, "url", "") or ""):
            return
        if current.endswith("/order-list"):
            return
        try:
            page.go_back(wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
        except Exception:
            try:
                page.goto(
                    self.url,
                    wait_until="domcontentloaded",
                    timeout=int(self.timeout * 1000),
                )
            except Exception:
                return
        page.wait_for_timeout(1200)
        if not store.list_captures:
            self._wait_first_list(page, store)
        try:
            if page.locator(".order-item").count() == 0:
                self._scroll_to_oldest(page, store)
        except (AuthRequired, RiskBlocked):
            raise
        except Exception:
            pass

    def _click_details(self, page: Any, context: Any, store: CaptureStore) -> None:
        if not self.with_detail:
            return
        order_ids = store.list_order_ids()
        if not order_ids:
            return
        cards = self._order_cards(page)
        if not cards:
            store.warnings.append(
                "订单列表响应已捕获，但页面没有可点击的订单卡片；跳过了详情点击"
            )
            return
        limit = len(order_ids)
        if self.max_details and self.max_details > 0:
            limit = min(limit, self.max_details)
        if limit < len(order_ids):
            store.warnings.append(
                f"按 --max-details={self.max_details} 只点击了前 {limit} 单"
            )

        for index, oid in enumerate(order_ids[:limit]):
            block = _current_block(page)
            if block is not None:
                raise _block_error(block)
            try:
                self._restore_order_list(page, store)
            except ConnectorError:
                raise
            except Exception:
                pass
            cards = self._order_cards(page)
            by_id = {
                str(card.get("order_id")): card
                for card in cards
                if card.get("order_id")
            }
            card_info = by_id.get(oid)
            card_index = card_info.get("index") if card_info else None
            if card_index is None or card_index >= len(cards):
                card_index = index if index < len(cards) else None
            if card_index is None:
                store.warnings.append(f"找不到订单 {oid} 的卡片，跳过详情")
                continue

            new_pages: List[Any] = []

            def on_page(new_page: Any) -> None:
                new_pages.append(new_page)
                self._on_new_page(new_page, store)

            context.on("page", on_page)
            store.current_order_id = oid
            try:
                card = page.locator(".order-item").nth(card_index)
                clicked = _safe_click(page, card)
                if not clicked:
                    store.warnings.append(f"订单 {oid} 的卡片点击失败，跳过详情")
                    continue
                detail_page = page
                route_seen = False
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    block = _current_block(page)
                    if block is not None:
                        raise _block_error(block)
                    if new_pages:
                        detail_page = new_pages[-1]
                        try:
                            detail_page.wait_for_load_state(
                                "domcontentloaded", timeout=3000
                            )
                        except Exception:
                            pass
                        route_seen = True
                        break
                    if "#/gulfstream-lite" in (page.url or ""):
                        route_seen = True
                        break
                    page.wait_for_timeout(250)
                if not route_seen:
                    store.warnings.append(
                        f"订单 {oid} 未观察到详情页路由（#/gulfstream-lite）"
                    )
                self._wait_for_detail(detail_page, store, oid)
                if new_pages:
                    try:
                        detail_page.close()
                    except Exception:
                        pass
                else:
                    self._restore_order_list(page, store)
            finally:
                try:
                    context.remove_listener("page", on_page)
                except Exception:
                    pass
            page.wait_for_timeout(900)

    def run(self, cookies: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - detection handles this
            raise BrowserUnavailable(
                "Python Playwright 不可用",
                "请先安装 playwright 及其 Chromium；本工具不会下载",
            ) from exc

        pw = None
        browser = None
        store = CaptureStore()
        page = None
        try:
            pw = sync_playwright().start()
            launch_kwargs: Dict[str, Any] = {"headless": self.headless}
            if self.engine.executable:
                launch_kwargs["executable_path"] = self.engine.executable
            try:
                browser = pw.chromium.launch(**launch_kwargs)
            except Exception as exc:
                raise BrowserUnavailable(
                    "Playwright Chromium 启动失败",
                    "Chromium 可能损坏或缺少依赖；本工具不会自动下载。"
                    f"错误类型: {type(exc).__name__}",
                ) from exc
            context = browser.new_context(
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1280, "height": 1000},
            )
            added = self._add_cookies(context, cookies)
            if added == 0:
                raise AuthRequired(
                    "没有把滴滴 Cookie 注入浏览器（Cookie 可能格式不兼容）",
                    "请确认浏览器中仍登录滴滴；本工具不会打印 Cookie 值",
                )
            page = context.new_page()
            page.on("response", lambda response: store.on_response(response, page))
            context.on("page", lambda new_page: self._on_new_page(new_page, store))

            try:
                page.goto(
                    self.url,
                    wait_until="domcontentloaded",
                    timeout=int(max(20.0, self.timeout) * 1000),
                )
            except Exception as exc:
                raise NetworkError(
                    f"打开滴滴订单页失败: {type(exc).__name__}"
                ) from exc
            block = _current_block(page)
            if block is not None:
                raise _block_error(block)
            self._dismiss_safe_modals(page)
            self._wait_first_list(page, store)
            blocked_info: Optional[Dict[str, Any]] = None
            try:
                self._scroll_to_oldest(page, store)
            except (AuthRequired, RiskBlocked) as exc:
                blocked_info = {
                    "kind": "auth" if isinstance(exc, AuthRequired) else "risk",
                    "message": exc.message,
                }
            if blocked_info is None:
                block = _current_block(page)
                if block is not None:
                    blocked_info = {"kind": block[0], "message": block[1]}
            if blocked_info is None:
                try:
                    self._click_details(page, context, store)
                except (AuthRequired, RiskBlocked) as exc:
                    blocked_info = {
                        "kind": (
                            "auth" if isinstance(exc, AuthRequired) else "risk"
                        ),
                        "message": exc.message,
                    }
            return {
                "ok": True,
                "engine": "python-playwright",
                "final_url": page.url if page else "",
                "cookies_after": context.cookies() if page else [],
                "captures": [capture.as_dict() for capture in store.captures],
                "details": store.details,
                "dom_orders": store.dom_orders,
                "warnings": store.warnings,
                "blocked": blocked_info,
            }
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass


def _block_error(block: Tuple[str, str]) -> ConnectorError:
    kind, message = block
    if kind == "auth":
        return AuthRequired(
            message,
            "请先在浏览器中人工重新登录滴滴，再重试；本工具不会绕过登录",
        )
    if kind == "slider":
        return RiskBlocked(
            message,
            "页面出现了滑块验证；本工具不会尝试完成或绕过验证",
        )
    return RiskBlocked(
        message,
        "页面出现了验证码/风控；本工具会立即停止，不会尝试破解或绕过",
    )


# ---------------------------------------------------------------------------
# Node Playwright fallback
# ---------------------------------------------------------------------------


class NodePlaywrightDriver:
    """Run the bundled Node driver with the installed Node Playwright."""

    def __init__(
        self,
        engine: BrowserEngine,
        *,
        headless: bool = True,
        timeout: float = 30.0,
        max_pages: int = 50,
        with_detail: bool = True,
        with_fare: bool = True,
        max_details: int = 0,
        url: str = DIDI_ORDER_LIST_URL,
        profile_dir: Optional[str] = None,
        skip_detail_ids: Optional[Sequence[str]] = None,
        skip_months: int = 0,
    ):
        self.engine = engine
        self.headless = headless
        self.timeout = timeout
        self.max_pages = max_pages
        self.with_detail = with_detail
        self.with_fare = with_fare
        self.max_details = max_details
        self.url = url
        self.profile_dir = profile_dir
        self.skip_detail_ids = list(skip_detail_ids or [])
        self.skip_months = max(0, int(skip_months or 0))

    def _driver_path(self) -> Path:
        path = Path(__file__).with_name("browser_driver.cjs")
        if not path.exists():
            raise BrowserUnavailable(
                "缺少内置 Node 浏览器驱动 browser_driver.cjs",
                "请检查 connectors/didi 是否完整",
            )
        return path

    def run(self, cookies: List[Dict[str, Any]]) -> Dict[str, Any]:
        node = shutil.which("node")
        if not node:
            raise BrowserUnavailable(
                "找不到 node，无法使用 Node Playwright",
                "请安装 Node.js 或改用 Python Playwright",
            )
        payload = {
            "cookies": cookies,
            "url": self.url,
            "profile_dir": self.profile_dir or "",
            "skip_detail_ids": self.skip_detail_ids,
            "skip_months": self.skip_months,
            "headless": self.headless,
            "timeout_ms": int(max(20.0, self.timeout) * 1000),
            "max_pages": self.max_pages,
            "with_detail": self.with_detail,
            "with_fare": self.with_fare,
            "max_details": self.max_details,
            "executable_path": self.engine.executable or "",
        }
        try:
            proc = subprocess.run(
                [node, str(self._driver_path())],
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(600.0, self.timeout * 10, (self.max_pages + self.skip_months) * 180.0),
                check=False,
                env=_node_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise NetworkError(
                "Node 浏览器驱动运行超时",
                "可增大 --timeout 后重试",
            ) from exc
        except OSError as exc:
            raise BrowserUnavailable(
                "无法启动 Node 浏览器驱动",
                f"错误: {type(exc).__name__}",
            ) from exc

        secrets = [
            str(cookie.get("value"))
            for cookie in cookies
            if isinstance(cookie, dict)
            and cookie.get("value")
            and len(str(cookie.get("value"))) >= 4
        ]
        stdout = proc.stdout.decode("utf-8", "replace").strip()
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        if not stdout:
            detail = _redact_text(stderr[-600:], secrets) if stderr else "无输出"
            raise NetworkError(
                "Node 浏览器驱动没有返回结果",
                detail,
            )
        try:
            result = json.loads(stdout)
        except ValueError as exc:
            raise NetworkError(
                "Node 浏览器驱动返回了非 JSON 结果",
                _redact_text(stdout[-300:], secrets),
            ) from exc
        if not isinstance(result, dict):
            raise NetworkError("Node 浏览器驱动返回了非 JSON 对象")
        if result.get("ok") is False:
            error = result.get("error") or {}
            kind = str(error.get("type") or "")
            message = str(error.get("message") or "浏览器驱动失败")
            hint = str(error.get("hint") or "")
            if kind in ("auth", "login"):
                raise AuthRequired(message, hint)
            if kind in ("risk", "captcha", "slider"):
                raise RiskBlocked(message, hint)
            if kind == "timeout":
                raise NetworkError(message, hint)
            if kind == "browser":
                raise BrowserUnavailable(message, hint)
            raise NetworkError(message, hint)
        result.setdefault("engine", "node-playwright")
        return result


# ---------------------------------------------------------------------------
# Payload building
# ---------------------------------------------------------------------------


def _place(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("displayname", "display_name", "address", "name", "text"):
            if value.get(key):
                return value.get(key)
        return None
    return value


def _browser_trip(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
    product = (
        item.get("product_name")
        or item.get("productName")
        or (scenes[0] if scenes else "")
        or _product_id(item)
    )
    return {
        "order_id": oid,
        "occurred": _timestamp(
            item.get("setuptimestamp")
            or item.get("setupTimestamp")
            or item.get("order_time")
            or item.get("create_time")
        ),
        "product": product,
        "from": _place(
            item.get("fromAddress")
            or item.get("from_address")
            or item.get("start_name")
            or item.get("from_name")
        ),
        "to": _place(
            item.get("toAddress")
            or item.get("to_address")
            or item.get("end_name")
            or item.get("to_name")
        ),
        "status": item.get("hisstatus")
        or item.get("status")
        or item.get("order_status"),
        "fare": _find_fare(item),
        "fare_detail": None,
    }


def build_browser_payload(
    result: Dict[str, Any],
    *,
    secrets: Sequence[str] = (),
    start_url: str = DIDI_ORDER_LIST_URL,
) -> Dict[str, Any]:
    """Turn captured browser responses into the documented output shape."""

    captures = [
        capture for capture in (result.get("captures") or [])
        if isinstance(capture, dict)
    ]
    trips: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for capture in captures:
        if capture.get("kind") != "order_list":
            continue
        response = capture.get("response")
        if not isinstance(response, dict):
            continue
        for item in _iter_order_items(response):
            trip = _browser_trip(item)
            if trip is None:
                continue
            oid = trip["order_id"]
            if oid not in trips:
                trips[oid] = trip
                order.append(oid)
            else:
                # A later page may have a fresher status/fare.
                for key, value in trip.items():
                    if value not in (None, "", []):
                        trips[oid][key] = value

    details = result.get("details") or {}
    if isinstance(details, dict):
        for oid, bucket in details.items():
            if not isinstance(bucket, dict):
                continue
            detail = bucket.get("detail")
            fare_payload = bucket.get("fare")
            if not (detail or fare_payload):
                continue
            trip = trips.get(oid)
            if trip is None:
                trip = {
                    "order_id": oid,
                    "occurred": None,
                    "product": None,
                    "from": None,
                    "to": None,
                    "status": None,
                    "fare": None,
                    "fare_detail": None,
                }
                trips[oid] = trip
                order.append(oid)
            fare_value = _find_fare(fare_payload) or _find_fare(detail)
            if fare_value:
                trip["fare"] = fare_value
            trip["fare_detail"] = {
                "detail": detail,
                "fare": fare_payload,
            }

    dom_fares = result.get("dom_fares") or {}
    if isinstance(dom_fares, dict):
        for oid, amount in dom_fares.items():
            trip = trips.get(str(oid))
            if trip is not None and not trip.get("fare") and amount:
                trip["fare"] = str(amount)

    for dom_trip in result.get("dom_orders") or []:
        if not isinstance(dom_trip, dict):
            continue
        oid = str(dom_trip.get("order_id") or "").strip()
        if not oid:
            continue
        trip = trips.get(oid)
        if trip is None:
            trip = {
                "order_id": oid,
                "occurred": dom_trip.get("occurred"),
                "product": dom_trip.get("product"),
                "from": dom_trip.get("from"),
                "to": dom_trip.get("to"),
                "status": dom_trip.get("status"),
                "fare": dom_trip.get("fare"),
                "fare_detail": None,
            }
            trips[oid] = trip
            order.append(oid)
        else:
            for key in ("occurred", "product", "from", "to", "status", "fare"):
                if not trip.get(key) and dom_trip.get(key):
                    trip[key] = dom_trip[key]

    pages: List[Dict[str, Any]] = []
    for capture in captures:
        entry = {
            "kind": capture.get("kind"),
            "url": redact_url(str(capture.get("url") or ""), secrets),
            "status": capture.get("status"),
            "response": redact_browser(capture.get("response"), secrets),
        }
        if capture.get("page") is not None:
            entry["page"] = capture.get("page")
        if capture.get("timemode"):
            entry["timemode"] = capture.get("timemode")
        if capture.get("order_id"):
            entry["order_id"] = capture.get("order_id")
        pages.append(entry)

    warnings = [
        str(item) for item in (result.get("warnings") or []) if item
    ]
    blocked = result.get("blocked")
    payload: Dict[str, Any] = {
        "kind": "didi_trips",
        "source": "browser",
        "engine": result.get("engine") or "",
        "url": redact_url(start_url, secrets),
        "generated_at": datetime.datetime.now(BEIJING).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "trips": [trips[oid] for oid in order],
        "pages": pages,
        "warnings": warnings,
        "blocked": redact_browser(blocked, secrets) if blocked else None,
    }
    return redact_browser(payload, secrets)


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------


class DidiBrowserFetcher:
    """Fetch trips by letting Didi's own page sign its requests."""

    def __init__(
        self,
        jar: CookieJar,
        *,
        engine_prefer: str = "auto",
        executable: Optional[str] = None,
        headless: bool = True,
        timeout: float = 30.0,
        max_pages: int = 50,
        with_detail: bool = True,
        with_fare: bool = True,
        max_details: int = 0,
        url: str = DIDI_ORDER_LIST_URL,
        extra_cookies: Optional[Sequence[Dict[str, Any]]] = None,
        profile_dir: Optional[str] = None,
        skip_detail_ids: Optional[Sequence[str]] = None,
        skip_months: int = 0,
        driver: Optional[Any] = None,
    ):
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        self.jar = jar
        self.engine_prefer = engine_prefer
        self.executable = executable
        self.headless = headless
        self.timeout = timeout
        self.max_pages = max_pages
        self.with_detail = with_detail
        self.with_fare = with_fare
        self.max_details = max_details
        self.url = url
        self.extra_cookies = list(extra_cookies or [])
        self.profile_dir = profile_dir
        self.skip_detail_ids = list(skip_detail_ids or [])
        self.skip_months = max(0, int(skip_months or 0))
        self.driver = driver
        self.warnings: List[str] = []
        self.engine: Optional[BrowserEngine] = None
        self.cookies_after: List[Dict[str, Any]] = []

    def _make_driver(self, engine: BrowserEngine) -> Any:
        kwargs = dict(
            headless=self.headless,
            timeout=self.timeout,
            max_pages=self.max_pages,
            with_detail=self.with_detail,
            with_fare=self.with_fare,
            max_details=self.max_details,
            url=self.url,
            profile_dir=self.profile_dir,
            skip_detail_ids=self.skip_detail_ids,
            skip_months=self.skip_months,
        )
        if engine.kind == "python":
            return PythonPlaywrightDriver(engine, **kwargs)
        return NodePlaywrightDriver(engine, **kwargs)

    def fetch(self) -> Dict[str, Any]:
        # The owner's stored session (freshest cookies from the last run) wins;
        # browser's cookie database only fills the gaps.
        specs = merge_cookies(self.extra_cookies or [], cookie_specs(self.jar))
        if not specs and not self.profile_dir:
            raise AuthRequired(
                "没有可注入浏览器的滴滴 Cookie（浏览器与本地会话都为空）",
                "请先运行 `python3 -m connectors.didi login` 完成一次登录；"
                "本工具不会打印 Cookie 值",
            )
        secret_values = [
            str(cookie.value)
            for cookie in self.jar.entries()
            if cookie.value and len(cookie.value) >= 4
        ]
        secret_values.extend(
            str(cookie.get("value"))
            for cookie in specs
            if cookie.get("value") and len(str(cookie.get("value"))) >= 4
        )
        secrets = list(dict.fromkeys(secret_values))
        engine = None
        if self.driver is None:
            # A persistent profile is only implemented by the Node driver.
            prefer = "node" if self.profile_dir else self.engine_prefer
            engine = detect_browser_engine(prefer)
            if self.executable:
                engine.executable = self.executable
            if (
                not self.headless
                and engine.executable
                and "headless" in engine.executable.lower()
            ):
                raise BrowserUnavailable(
                    "只找到了 Playwright headless shell，无法显示浏览器窗口",
                    "请安装完整 Chromium，或去掉 --headed 使用 headless 模式；"
                    "本工具不会自动下载",
                )
            self.engine = engine
            driver = self._make_driver(engine)
        else:
            driver = self.driver
            self.engine = BrowserEngine("fake", None, "injected test driver")
        try:
            result = driver.run(specs)
        except ConnectorError as exc:
            _scrub_error(exc, secrets)
            raise
        except Exception as exc:
            raise NetworkError(
                f"浏览器抓取失败: {type(exc).__name__}"
            ) from exc
        payload = build_browser_payload(
            result, secrets=secrets, start_url=self.url
        )
        self.cookies_after = list(result.get("cookies_after") or [])
        self.details_after = result.get("details") or {}
        self.warnings = list(payload.get("warnings") or [])
        if result.get("blocked"):
            payload["blocked"] = redact_browser(result["blocked"], secrets)
        return payload


def _scrub_error(exc: ConnectorError, secrets: Sequence[str]) -> None:
    for attr in ("message", "hint"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            setattr(exc, attr, _redact_text(value, secrets))
    if exc.args:
        exc.args = tuple(
            _redact_text(value, secrets) if isinstance(value, str) else value
            for value in exc.args
        )


def summarize_browser(payload: Dict[str, Any]) -> Dict[str, Any]:
    trips = [
        trip for trip in (payload.get("trips") or [])
        if isinstance(trip, dict)
    ]
    span = date_range(trips)
    with_fare = sum(1 for trip in trips if trip.get("fare"))
    return {
        "pages": len(payload.get("pages") or []),
        "orders": len(trips),
        "with_fare": with_fare,
        "date_range": span,
    }
