"""滴滴连接器：滴滴出行行程（列表、详情、费用），只读。

Two ways in:

* HTTP mode (``DidiFetcher`` in reader.py) -- the order list is signed with wsgsig, so this mode is normally
  rejected (HTTP 501) and ends with "needs risk signature". Kept as the documented fallback.
* Browser mode (``fetch --browser`` / ``fastfill`` / ``keepalive``) -- a real Chromium with its own persistent
  profile (``login`` opens it once, a person logs in) loads the order pages and the page signs its own
  requests. Nothing here computes, forges or replays wsgsig; captcha / login / risk pages stop the run.

State (profile, fare cache, stale marker, session file) lives in the shared application-support
directory (override ``PFIN_DIDI_STATE_DIR``); see session.state_dir().
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..kit.cookies import DEFAULT_COOKIE_DB, DEFAULT_KEYCHAIN_SERVICE, CookieProvider
from ..kit.env import getenv
from ..kit.errors import AuthRequired, CookieError, NeedsRiskSignature, ConnectorError, UsageError, internal_error
from ..kit.http import HttpClient, Pacer
from ..kit.output import format_summary
from ..kit.rows import make_record, stable_id, stamp_from_raw, write_rows
from ..kit.output import write_json
from ..kit.runner import Connector
from ..kit.session import SessionStore
from ..kit.spec import ProbeResult, SiteSpec
from . import actions as didi_actions
from .actions import (
    DIDI_FILE_PREFIX,
    add_extra_actions,
    add_fetch_browser_args,
    browser_plan,
    fetch_didi_browser,
    format_browser_summary,
    profile_ready,
    status_lines,
)
from .reader import (
    DIDI_COOKIE_HOST_SUFFIXES,
    DIDI_FARE_DETAIL_ENDPOINT,
    DIDI_HISTORY_ENDPOINT,
    DIDI_ORDER_DETAIL_ENDPOINT,
    DIDI_TOKEN_COOKIE_NAMES,
    DIDI_TOKEN_ENV,
    DIDI_WEB_ENTRY,
    DIDI_WEB_ORIGIN,
    DidiFetcher,
    build_history_params,
    find_token_cookie,
    redact_secret,
    request_context,
    summarize,
    token_from_env,
)

PREFIX = DIDI_FILE_PREFIX  # didi-trips

# The key stays "didi": Keychain sessions are stored per SiteSpec.key.
SPEC = SiteSpec(
    key="didi",
    label="滴滴",
    host_suffixes=DIDI_COOKIE_HOST_SUFFIXES,
    cookie_host=DIDI_WEB_ORIGIN.split("://", 1)[1],
    login_cookies=DIDI_TOKEN_COOKIE_NAMES,
    required_cookies=DIDI_TOKEN_COOKIE_NAMES,
    login_url=DIDI_WEB_ENTRY,
    required_mode="any",
    env_token=DIDI_TOKEN_ENV,
    blocked_reason=(
        "纯 HTTP 模式会被 wsgsig 拦截；请用 `fetch --browser` / `fastfill`"
        "（由滴滴页面自己签名，本工具不伪造或重放签名）"
    ),
)


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------


def _provider(args, suffixes=DIDI_COOKIE_HOST_SUFFIXES) -> CookieProvider:
    """browser cookies (only HTTP mode, ``status`` and the no-profile fallback read them)."""

    return CookieProvider(
        getattr(args, "cookie_db", None) or DEFAULT_COOKIE_DB,
        keychain_service=getattr(args, "keychain_service", None) or DEFAULT_KEYCHAIN_SERVICE,
        host_suffixes=suffixes,
        session_store=SessionStore(SPEC.key, suffixes),
    )


def _didi_jar(args, *, provider=None):
    return (provider or _provider(args)).load()


class DidiConnector(Connector):
    def manifest(self) -> Dict:
        data = super().manifest()
        data["login_url"] = SPEC.login_url
        return data


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def add_fetch_args(parser) -> None:
    parser.add_argument("--cookie-db", type=Path, default=None, help="浏览器 Cookies SQLite 路径（HTTP 模式 / 没有本机登录态时用）")
    parser.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE, help="Keychain 服务名")
    parser.add_argument("--no-cookie", action="store_true", help="只使用环境变量 token，不读取浏览器 Cookie 数据库")
    parser.add_argument("--no-detail", action="store_true", help="只抓行程列表，不请求每单详情（pOrderDetail）")
    parser.add_argument("--no-fare", action="store_true", help="不为缺少金额的行程请求费用明细（pGetRealTimeFeeDetail）")
    parser.add_argument("--phone", default=None, help=f"可选，默认读环境变量 {DIDI_TOKEN_ENV.replace('TOKEN', 'PHONE')}")
    parser.add_argument("--openid", default=None, help="可选，默认读环境变量 PFIN_DIDI_OPENID")
    parser.add_argument("--channel", default=None, help="可选，默认读环境变量 PFIN_DIDI_CHANNEL")
    add_fetch_browser_args(parser)


def validate(args) -> None:
    if getattr(args, "browser", False) and getattr(args, "no_cookie", False):
        raise UsageError("--browser 使用本机登录态或浏览器 Cookie，不能与 --no-cookie 同用")


def fetch_didi(args, *, client=None, environ: Optional[Dict[str, str]] = None, provider=None) -> Tuple[Dict, List[str], bool]:
    """HTTP mode: fetch trips using the env token or the browser session cookie."""

    token = (getenv(DIDI_TOKEN_ENV, environ) or "").strip()
    token_source = "env"
    jar = None
    if not token:
        if getattr(args, "no_cookie", False):
            token_from_env(environ)  # raises the established AuthRequired hint
        jar = _didi_jar(args, provider=provider)
        found = find_token_cookie(jar)
        if found is None:
            raise AuthRequired(
                "浏览器中没有找到滴滴登录 Cookie",
                "请先在浏览器中登录 https://common.diditaxi.com.cn/general/webEntry，"
                "再重新运行；或设置环境变量 "
                f"{DIDI_TOKEN_ENV}。本工具不会打印或写入 token/cookie 值",
            )
        token = found.value
        token_source = f"cookie:{found.name}"
    context = request_context(environ)
    for field_name in ("phone", "openid", "channel"):
        value = getattr(args, field_name, None)
        if value is not None:
            context[field_name] = value
    client = client or HttpClient(cookie_jar=jar, pacer=Pacer(max(1.0, args.delay)), timeout=args.timeout)
    fetcher = DidiFetcher(
        client,
        token,
        jar=jar,
        token_source=token_source,
        phone=context["phone"],
        openid=context["openid"],
        channel=context["channel"],
        appversion=context["appversion"],
        max_pages=args.max_pages,
        with_detail=not getattr(args, "no_detail", False),
        with_fare=not getattr(args, "no_fare", False),
    )
    payload = fetcher.fetch()
    return payload, fetcher.warnings, fetcher.needs_risk_signature


def fetch(args, jar):
    """``jar`` is always None: this connector reads its own login (local browser profile, or browser for HTTP mode)."""

    if getattr(args, "browser", False):
        payload, warnings, blocked = fetch_didi_browser(args, provider_factory=_provider)
        return ({**payload, "_blocked": blocked} if blocked else payload), warnings
    payload, warnings, needs = fetch_didi(args)
    blocked = None
    if needs:
        blocked = NeedsRiskSignature(
            "didi API needs risk signature (wsgsig); trip list was saved, but per-order fare/detail is blocked",
            "只想跳过详情/费用可加 --no-detail --no-fare；如果列表本身也被 501 "
            "签名拦截，则没有无签名方案。请改用 `fetch --browser` 或 `fastfill`（由页面自己签名）。"
            "本工具不会伪造或重放 wsgsig",
        )
    return ({**payload, "_blocked": blocked} if blocked else payload), warnings


def summary(payload: Dict) -> str:
    if payload.get("login_state") or payload.get("source") in ("browser", "fastfill", "merged"):
        return format_browser_summary(payload)
    data = summarize(payload)
    lines = [format_summary("didi", data)]
    if payload.get("token_source"):
        lines.append(f"auth: {payload['token_source']} (value never printed)")
    if data.get("with_fare"):
        lines.append(f"fares: {data['with_fare']}/{data['orders']}")
    return "\n".join(lines)


def rows(payload: Dict) -> List[Dict]:
    out = []
    seen = set()
    # Browser/fastfill mode writes a top-level ``trips`` list; HTTP mode keeps the legacy ``pages[].orders`` shape.
    trips = payload.get("trips")
    if isinstance(trips, list):
        orders = [t for t in trips if isinstance(t, dict)]
    else:
        orders = [order for page in payload.get("pages") or [] for order in page.get("orders") or []]
    for order in orders:
        oid = order.get("order_id")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        origin = order.get("from")
        destination = order.get("to")
        product = order.get("product_name") or order.get("product")
        title = f"{origin}→{destination}" if origin and destination else product
        out.append(
            make_record(
                source="didi",
                record_type="trip",
                id=stable_id("didi", "trip", oid),
                occurred=order.get("occurred"),
                amount=order.get("fare"),
                direction="out",
                merchant=product,
                title=title,
                status=order.get("status"),
                extra={
                    "product_id": order.get("product_id"),
                    "scene": order.get("scene"),
                    "class_type": order.get("class_type"),
                    "from": origin,
                    "to": destination,
                    "fare_note": order.get("fare_note"),
                    "has_detail": isinstance(order.get("detail"), dict),
                    "has_fare_detail": isinstance(order.get("fare_detail"), dict),
                    "source": order.get("source"),
                },
            )
        )
    return out


def write_fastfill_outputs(args, payload: Dict) -> None:
    """Raw JSON + rows for ``fastfill``, named exactly like a ``fetch`` run."""

    raw_path = write_json(args.out, PREFIX, payload)
    records = rows(payload)
    rows_path = write_rows(args.out, SPEC.key, records, stamp=stamp_from_raw(raw_path, PREFIX))
    print(f"written: {raw_path}")
    print(f"rows: {len(records)} -> {rows_path}")


def plan(args) -> str:
    if getattr(args, "browser", False):
        return browser_plan(args)
    lines = [
        "plan: didi fetch (dry-run, no network, no Keychain, no Cookie DB)",
        f"list: GET {DIDI_HISTORY_ENDPOINT}",
        "params: pagenum (0-based), timemode, datatype=webapp, "
        "apiver=2.0.0, timestamp (ms), token, phone, appversion, channel, "
        "openid, daijia_token, daijia_pid",
        f"auth: ${DIDI_TOKEN_ENV} if set, otherwise the Didi session cookie "
        "from the browser (ticket / trinity-login); values never printed or written",
        "paging: legacy pagenum++ until order_waiting/order_done are both empty; "
        "new timemode cursor until havenext=false",
        f"detail: GET {DIDI_ORDER_DETAIL_ENDPOINT}",
        f"fare: GET {DIDI_FARE_DETAIL_ENDPOINT}",
        "signature: the page signs all axios requests (including this list) with "
        "wsgsig (bundled obfuscated fSvY module)",
        "           this tool never computes, forges or replays wsgsig; it stops "
        'with a "needs risk signature" error',
        "pacing: >= 1.0s between requests",
        f"output: {args.out or 'OUT'}/{PREFIX}-YYYYmmdd-HHMMSS.json (0600)",
        "rows: didi-rows-YYYYmmdd-HHMMSS.jsonl (0600)",
    ]
    if getattr(args, "no_detail", False):
        lines.append("mode: --no-detail (list only, no per-order detail)")
    elif getattr(args, "no_fare", False):
        lines.append("mode: --no-fare (detail only, no extra fare request)")
    else:
        lines.append("mode: list + per-order detail + fare (detail/fare need wsgsig)")
    lines.append("browser mode: add --browser (page signs its own requests) or run `fastfill`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# status / online check / keepalive / login
# ---------------------------------------------------------------------------


def _didi_token(jar) -> Optional[str]:
    env_token = (getenv(DIDI_TOKEN_ENV) or "").strip()
    if env_token:
        return env_token
    if jar is None:
        return None
    found = find_token_cookie(jar)
    return found.value if found is not None else None


def check(spec, client, jar) -> ProbeResult:
    """The HTTP-mode online check: page 0 of the list, no detail, no fare. Normally ends in "needs_signature"
    (wsgsig), which is reported as such. ``jar`` may be None when only the env token is set."""

    token = _didi_token(jar)
    if not token:
        return ProbeResult(spec, "logged_out", f"缺少滴滴令牌（浏览器 Cookie 或环境变量 {DIDI_TOKEN_ENV}）")
    fetcher = DidiFetcher(
        client,
        token,
        jar=jar,
        token_source="env" if getenv(DIDI_TOKEN_ENV) else "cookie",
        max_pages=1,
        with_detail=False,
        with_fare=False,
    )
    payload = fetcher.fetch()
    info = summarize(payload)
    return ProbeResult(spec, "ok", f"行程列表第 1 页可读（行程 {info.get('orders', 0)} 条）")


def probe_http(spec: SiteSpec, provider: CookieProvider, client: HttpClient) -> ProbeResult:
    """Load the browser login (or the env token alone) and run :func:`check`."""

    jar = None
    client.cookie_jar = None
    try:
        jar = provider.load()
        client.cookie_jar = jar
    except CookieError as exc:
        if not (getenv(DIDI_TOKEN_ENV) or "").strip():
            return ProbeResult(spec, "cookie_error", exc.render())
    try:
        return check(spec, client, jar)
    except ConnectorError as exc:
        from ..kit.spec import _probe_error

        return _probe_error(spec, exc)
    except Exception as exc:
        return ProbeResult(spec, "internal_error", internal_error(exc))


def _didi_probe_lines(args, jar, token: str, source: str) -> List[str]:
    """One read-only page-0 list request, with the response body redacted."""

    if bool(getattr(args, "probe_cookie_only", False)):
        url = DIDI_HISTORY_ENDPOINT
        label = "probe: GET /passenger/history with Cookie only, no query"
    else:
        params = build_history_params(0, "", token)
        url = DIDI_HISTORY_ENDPOINT + "?" + urllib.parse.urlencode(params)
        label = "probe: GET /passenger/history pagenum=0 (read-only, response only)"
    client = HttpClient(
        cookie_jar=jar,
        pacer=Pacer(max(1.0, getattr(args, "delay", 1.2))),
        timeout=getattr(args, "timeout", 30.0),
    )
    response = client.get(
        url,
        cookie_header=jar.header_for("common.diditaxi.com.cn"),
        headers={
            "Referer": "https://common.diditaxi.com.cn/general/webEntry",
            "Origin": "https://common.diditaxi.com.cn",
            "Accept": "application/json, text/plain, */*",
        },
    )
    lines = [
        label,
        f"probe auth source: {source} (value never printed)",
        f"probe http: {response.status}",
        f"probe content-type: {response.header('content-type') or '(none)'}",
        f"probe body length: {len(response.text)}",
    ]
    try:
        payload = json.loads(response.text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        errno = payload.get("errno")
        message = str(payload.get("errmsg") or payload.get("errmsg_str") or payload.get("message") or payload.get("error") or "")
        lines.append(f"probe json: errno={errno!r} message={redact_secret(message, token)[:300]!r}")
        keys = ", ".join(sorted(str(key) for key in payload)[:20])
        lines.append(f"probe json keys: {keys}")
    # The body itself is never printed: it can carry rotated tokens, cookies and trip details.
    return lines


def didi_status(args, *, provider=None) -> Tuple[List[str], bool]:
    """Describe the local browser login and the browser cookies without printing a single value.

    Returns ``(lines, logged_in)``. A browser cookie database that cannot be read does not hide the local state.
    """

    probe = bool(getattr(args, "probe", False) or getattr(args, "online", False))
    lines = [
        "didi status (read-only; one probe request; cookie values are never printed)"
        if probe
        else "didi status (read-only; no network; cookie values are never printed)",
        f"domains: {', '.join(DIDI_COOKIE_HOST_SUFFIXES)}",
    ]
    token = ""
    source = ""
    jar = None
    try:
        jar = _didi_jar(args, provider=provider)
    except CookieError as exc:
        lines.append(f"浏览器 Cookie: 读取失败 — {exc.message}")
    if jar is not None:
        entries = jar.listing()
        lines.append(f"cookies: {len(entries)}")
        by_host: Dict[str, List[str]] = {}
        for host, _path, name in entries:
            by_host.setdefault(host or "(unknown)", []).append(name)
        for host in sorted(by_host):
            lines.append(f"  {host}: {', '.join(sorted(set(by_host[host])))}")
    env_token = (getenv(DIDI_TOKEN_ENV) or "").strip()
    lines.append(f"token env: {'set' if env_token else 'unset'}")
    found = find_token_cookie(jar) if jar is not None else None
    if found is None:
        lines.append("token cookie: not found")
    else:
        lines.append(f"token cookie: found name={found.name} host={found.host_key}")
    token = env_token or (found.value if found is not None else "")
    source = "env" if env_token else (f"cookie:{found.name}" if found is not None else "")
    if token:
        lines.append(f"ready: yes ({source})")
        if probe and jar is not None:
            lines.extend(_didi_probe_lines(args, jar, token, source))
    else:
        lines.append("ready: no (run `python3 -m connectors.didi login`, or log in to Didi in the browser, or set the env token)")
    lines.extend(status_lines())
    return lines, bool(token) or profile_ready()


def status(args):
    lines, ready = didi_status(args)
    return lines, 0 if ready else 3


def login(args) -> int:
    """A visible window; a person logs in (QR / SMS code) once and the profile is pinned locally."""

    ns = argparse.Namespace(
        login_timeout=float(getattr(args, "login_timeout", None) or args.wait),
        entry_url_file=getattr(args, "entry_url_file", None),
    )
    return didi_actions.didi_login(ns)


def keepalive(args) -> int:
    """Browser-mode keepalive: a headless visit of the order page lets the page renew its own login.
    Scheduling is pfin sync's job (config.json "sync"), not this connector's."""

    ns = argparse.Namespace(
        timeout=max(60.0, float(getattr(args, "timeout", 60.0))),
    )
    code = didi_actions._run_keepalive(ns)
    if getattr(args, "json", False):
        print(json.dumps({"connector": SPEC.key, "status": "ok" if code == 0 else "failed", "exit": code}, ensure_ascii=False))
    return code


def run_extra(args) -> Optional[int]:
    return didi_actions.run_action(args, write_outputs=write_fastfill_outputs)


CONNECTOR = DidiConnector(
    key="didi",
    label="滴滴",
    prefix=PREFIX,
    spec=None,  # the runner would load browser cookies before every fetch; browser mode must not need them
    describe="滴滴出行行程（列表、详情、费用）；页面自己签名，由独立浏览器登录态读取",
    fetch=fetch,
    summary=summary,
    rows=rows,
    add_fetch_args=add_fetch_args,
    validate=validate,
    plan=plan,
    check=check,
    login=login,
    status=status,
    keepalive=keepalive,
    extra=add_extra_actions,
    run_extra=run_extra,
    login_kind="browser",
    keepalive_hours=24.0,
    max_pages=50,
    min_delay=1.0,
    record_types=("trip",),
)
