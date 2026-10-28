"""Didi browser-mode commands: login, fetch --browser, fastfill, merge, report, keepalive.

The Didi list/detail APIs require wsgsig, which only the page's own SDK can
produce.  These commands therefore drive a real Chromium (Playwright) with a
CLI-owned persistent login profile and let the page sign its own requests.
Nothing here computes, forges or replays wsgsig; captcha/login/risk pages stop
the run.
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..kit.cookies import DEFAULT_KEYCHAIN_SERVICE, CookieJar
from .reader import DIDI_COOKIE_HOST_SUFFIXES, find_token_cookie
from .browser import (
    DIDI_ORDER_LIST_URL,
    DidiBrowserFetcher,
    _node_env,
    build_browser_payload,
    detect_browser_engine,
    summarize_browser,
)
from .session import (
    entry_url_path,
    load_fare_cache,
    load_session,
    mark_profile_ready,
    profile_dir,
    profile_ready,
    resolve_entry_url,
    save_entry_url,
    save_fare_cache,
    save_session,
    session_path,
    session_summary,
    session_usable,
    state_dir,
)
from ..kit.errors import (
    AuthRequired,
    BrowserUnavailable,
    NetworkError,
    RiskBlocked,
    UsageError,
)
from ..kit.output import ensure_out_dir, write_json

DIDI_FILE_PREFIX = "didi-trips"
BROWSER_ACTIONS = ("login", "fastfill", "merge", "report", "keepalive")  # login/keepalive are shared verbs


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def add_extra_actions(actions) -> None:
    """Register fastfill/merge/report next to the shared verbs, and the didi-only flags of
    ``status`` / ``login`` / ``keepalive`` (whose parsers the runner already created)."""

    choices = getattr(actions, "choices", {})
    status = choices.get("status")
    if status is not None:
        status.add_argument("--probe", action="store_true", help="只读请求列表第 0 页并打印脱敏后的响应体（不保存，不打印凭据）")
        status.add_argument("--probe-cookie-only", action="store_true", help="probe 时只带 Cookie、不带任何 query 参数")
        status.add_argument("--cookie-db", type=Path, default=None, help="浏览器 Cookies SQLite 路径（默认浏览器的 Default 配置）")
        status.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE, help="Keychain 服务名")
    login = choices.get("login")
    if login is not None:
        login.add_argument(
            "--entry-url-file", type=Path, default=None,
            help="可选：起始 URL 文件（0600）；默认使用已保存的订单页 URL",
        )
    didi_fast = actions.add_parser(
        "fastfill",
        help="快速全量：用页面签名接口翻完所有月份，再逐单直开详情页读金额（只读）",
    )
    didi_fast.add_argument("--out", type=Path, default=Path("out"), help="输出目录，默认 out")
    didi_fast.add_argument("--max-pages", type=int, default=80, help="最多翻多少个月，默认 80")
    didi_fast.add_argument("--timeout", type=float, default=60.0, help="单步超时秒数")
    didi_merge = actions.add_parser(
        "merge",
        help="把 out 目录下多次抓取的账单按订单号合并（金额优先），不联网",
    )
    didi_merge.add_argument("--out", type=Path, default=Path("out"), help="输出目录，默认 out")
    didi_report = actions.add_parser(
        "report",
        help="把最近一次抓取的账单打印成表格（日期/金额/起点/终点），不联网",
    )
    didi_report.add_argument("--out", type=Path, default=Path("out"), help="输出目录，默认 out")
    didi_report.add_argument("--file", type=Path, default=None, help="指定 didi-trips-*.json")
    didi_report.add_argument("--limit", type=int, default=0, help="只显示最近 N 单，0 表示全部")
    didi_report.add_argument("--csv", action="store_true", help="输出 CSV（含表头）")


def add_fetch_browser_args(didi_fetch: argparse.ArgumentParser) -> None:
    """Extra flags for ``didi fetch --browser``."""

    didi_fetch.add_argument(
        "--browser",
        action="store_true",
        help=(
            "用本机已安装的 Playwright Chromium 打开滴滴页面，由页面自身签名；"
            "不实现/伪造 wsgsig。未安装浏览器时只报告缺什么，不下载"
        ),
    )
    didi_fetch.add_argument(
        "--browser-engine",
        choices=("auto", "python", "node"),
        default="auto",
        help="Playwright 引擎偏好：auto（默认）、python 或 node",
    )
    didi_fetch.add_argument(
        "--browser-executable", default=None, help="可选，指定 Chromium 可执行文件路径（默认自动发现）"
    )
    didi_fetch.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认 headless）")
    didi_fetch.add_argument(
        "--max-details", type=int, default=0, help="最多点击多少单打开详情，0 表示全部（默认 0）"
    )
    didi_fetch.add_argument(
        "--skip-months",
        type=int,
        default=0,
        help="先快速跳过最近 N 个月（不点详情），从第 N 个月开始取金额；用于分段回填",
    )
    didi_fetch.add_argument(
        "--entry-url-file",
        type=Path,
        default=None,
        help=(
            "可选：存放滴滴订单页 URL 的文件（0600，含 token）。默认依次使用 "
            "本地会话里保存的 URL、$PFIN_DIDI_ENTRY_FILE、"
            f"{entry_url_path()}，"
            "最后从浏览器历史记录里取最近一次打开的订单页"
        ),
    )


# ---------------------------------------------------------------------------
# fetch --browser
# ---------------------------------------------------------------------------


def fetch_didi_browser(args, *, provider_factory) -> Tuple[Dict, List[str], Optional[Exception]]:
    """Read trips by letting Didi's own signed browser page do the requests.

    Returns ``(payload, warnings, blocked_error)``; the caller saves the
    payload first and raises ``blocked_error`` afterwards so partial read-only
    data is never lost.
    """

    profile = str(profile_dir()) if profile_ready() else None
    session = None if profile else load_session()
    use_stored = session_usable(session)
    extra = list((session or {}).get("cookies") or []) if use_stored else []
    if profile or use_stored:
        # The CLI owns the login state: no Keychain prompt or external browser-profile dependency.
        jar = CookieJar()
    else:
        jar = provider_factory(args, DIDI_COOKIE_HOST_SUFFIXES).load()
    found = find_token_cookie(jar)
    if found is None and not extra and not profile:
        raise AuthRequired(
            "没有找到滴滴登录态（本地会话和浏览器 Cookie 都不可用）",
            "请先运行 `python3 -m connectors.didi login`，或在浏览器中登录 "
            "https://common.diditaxi.com.cn/general/webEntry 再重新运行 --browser；"
            "本工具不会打印或写入 token/cookie 值",
        )
    url, url_source = resolve_entry_url(
        getattr(args, "entry_url_file", None), fallback=DIDI_ORDER_LIST_URL
    )
    if url_source == "browser-history":
        try:
            save_entry_url(url)
        except OSError:
            pass
    fare_cache = load_fare_cache()
    fetcher = DidiBrowserFetcher(
        jar,
        engine_prefer=getattr(args, "browser_engine", "auto"),
        executable=getattr(args, "browser_executable", None),
        headless=not getattr(args, "headed", False),
        timeout=args.timeout,
        max_pages=args.max_pages,
        with_detail=not getattr(args, "no_detail", False),
        with_fare=not getattr(args, "no_fare", False),
        max_details=getattr(args, "max_details", 0),
        url=url,
        extra_cookies=extra,
        profile_dir=profile,
        skip_detail_ids=sorted(fare_cache),
        skip_months=max(0, int(getattr(args, "skip_months", 0) or 0)),
    )
    payload = fetcher.fetch()
    # Fill amounts already known from previous runs, then remember new ones so
    # recurring queries only touch orders that are new (or were cancelled).
    trips = payload.get("trips") or []
    for trip in trips:
        oid = str(trip.get("order_id") or "")
        if not trip.get("fare") and fare_cache.get(oid):
            trip["fare"] = fare_cache[oid]
    updated = dict(fare_cache)
    closed = 0
    for trip in trips:
        oid = str(trip.get("order_id") or "")
        if not oid:
            continue
        if trip.get("fare"):
            updated[oid] = str(trip["fare"])
        elif any(word in str(trip.get("status") or "") for word in ("关闭", "取消", "未支付")):
            updated[oid] = ""
            closed += 1
    try:
        save_fare_cache(updated)
    except OSError:
        fetcher.warnings.append("无法写费用缓存（不影响本次结果）")
    payload["fares"] = {
        "total": len(trips),
        "with_fare": sum(1 for trip in trips if trip.get("fare")),
        "cached": len(fare_cache),
        "closed": closed,
    }
    if fetcher.cookies_after:
        try:
            save_session(fetcher.cookies_after, entry_url=url)
        except OSError:
            fetcher.warnings.append("无法写本地登录态（本次结果不受影响）")
    payload["url_source"] = url_source
    payload["session_cookies"] = len(extra)
    payload["login_state"] = "profile" if profile else ("cookies" if extra else "browser")
    return payload, fetcher.warnings, blocked_error(payload)


def blocked_error(payload: Dict) -> Optional[Exception]:
    blocked = payload.get("blocked")
    if not blocked:
        return None
    kind = str(blocked.get("kind") or "")
    message = str(blocked.get("message") or "滴滴页面出现风控拦截")
    if kind == "auth":
        return AuthRequired(
            message,
            "已保存拦截前抓到的只读数据；请运行 `python3 -m connectors.didi login` 重新登录。"
            "本工具不会绕过登录或风控",
        )
    return RiskBlocked(
        message,
        "已保存拦截前抓到的只读数据；本工具会立即停止，"
        "不会尝试破解、完成或绕过验证码/滑块/风控",
    )


def format_browser_summary(payload: Dict) -> str:
    summary = summarize_browser(payload)
    total = int(summary.get("orders") or 0)
    with_fare = int(summary.get("with_fare") or 0)
    percent = int(round(with_fare * 100 / total)) if total else 0
    span = summary.get("date_range")
    text_span = f"{span[0]} .. {span[1]}" if span else "无"
    return (
        f"didi (browser): trips={total} with_fare={with_fare} ({percent}%) "
        f"date_range={text_span} login={payload.get('login_state', '-')}"
    )


def browser_plan(args) -> str:
    lines = [
        "plan: didi fetch --browser (dry-run; no network, no Keychain, "
        "no Cookie DB, no browser launch)",
        f"page: {DIDI_ORDER_LIST_URL}",
        "engine: " + getattr(args, "browser_engine", "auto")
        + " (Python Playwright first, then Node Playwright; 不会自动下载)",
        "login: CLI-owned persistent profile from `python3 -m connectors.didi login`; "
        "falls back to browser cookies injected in memory only",
        "signing: the page's own JavaScript builds and signs requests with "
        "wsgsig; this tool never computes, forges or replays wsgsig",
        "list capture: JSON responses of /passenger/history, month by month",
        "detail capture: click each order card, record pOrderDetail / "
        "pGetRealTimeFeeDetail responses; known fares come from the local cache",
        "read-only: never clicks pay, invoice, complaint, cancel, refund or "
        "any other write action",
        "stop: captcha, slider, login page or any risk block -> report and stop",
        f"click limit: {getattr(args, 'max_details', 0) or 'all orders'}",
        "headless: " + ("no (--headed)" if getattr(args, "headed", False) else "yes"),
        f"output: {args.out or 'OUT'}/{DIDI_FILE_PREFIX}-YYYYmmdd-HHMMSS.json (0600) "
        "+ didi-rows-YYYYmmdd-HHMMSS.jsonl (0600)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# login / fastfill / merge / report / keepalive
# ---------------------------------------------------------------------------


def run_action(args, *, write_outputs) -> Optional[int]:
    """Dispatch a browser-only ``didi`` action (None when ``args.action`` is not one of them).

    ``write_outputs(args, payload)`` is the main CLI's writer (raw JSON +
    normalized rows + ledger) so fastfill output matches every other fetch.
    """

    action = args.action
    if action == "fastfill":
        return _run_fastfill(args, write_outputs=write_outputs)
    if action == "merge":
        return _run_merge(args)
    if action == "report":
        return _run_report(args)
    return None


def didi_login(args) -> int:
    """Open a visible browser once and pin the Didi login state locally."""

    node = shutil.which("node")
    if not node:
        raise BrowserUnavailable(
            "找不到 node，无法打开登录窗口",
            "请安装 Node.js（本工具不会自动下载）",
        )
    script = Path(__file__).with_name("didi_login.cjs")
    if not script.is_file():
        raise BrowserUnavailable("缺少内置登录脚本 didi_login.cjs", "请检查 connectors/didi 是否完整")
    engine = detect_browser_engine("node")
    url, source = resolve_entry_url(
        getattr(args, "entry_url_file", None), fallback=DIDI_ORDER_LIST_URL
    )
    timeout = max(60.0, float(getattr(args, "login_timeout", 600.0)))
    payload = {
        "profile_dir": str(profile_dir()),
        "url": url,
        "executable_path": engine.executable or "",
        "timeout_ms": int(timeout * 1000),
        "state_path": str(session_path()),
        "entry_url_path": str(entry_url_path()),
    }
    print("即将打开浏览器窗口，请在窗口里完成滴滴登录（扫码或短信验证码）。")
    print(f"起始地址来源：{source}")
    if not url or url == DIDI_ORDER_LIST_URL:
        print(
            "如果窗口里没有登录入口，请在地址栏打开 "
            "https://common.diditaxi.com.cn/general/webEntry?h=1 再登录。"
        )
    proc = subprocess.run(
        [node, str(script)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout + 180.0,
        check=False,
        env=_node_env(),
    )
    if proc.returncode != 0 or not session_path().is_file():
        print(
            "登录没有完成：窗口里没有出现成功的订单列表请求。可以重跑 `python3 -m connectors.didi login`。",
            file=sys.stderr,
        )
        return 4
    mark_profile_ready()
    sess = session_summary()
    print(
        "登录态已固定在本机："
        f"{profile_dir()}（0700），cookie {sess.get('cookies', 0)} 个，"
        f"保存于 {sess.get('saved_at')}。"
    )
    print("以后直接运行：python3 -m connectors.didi fastfill --out out")
    return 0


def _run_fastfill(args, *, write_outputs) -> int:
    if not profile_ready():
        raise AuthRequired("还没有固定登录态", "请先运行 `python3 -m connectors.didi login` 完成一次登录")
    node = shutil.which("node")
    if not node:
        raise BrowserUnavailable("找不到 node", "请安装 Node.js（本工具不会自动下载）")
    script = Path(__file__).with_name("didi_fastfill.cjs")
    engine = detect_browser_engine("node")
    url, _source = resolve_entry_url(fallback=DIDI_ORDER_LIST_URL)
    out_dir = ensure_out_dir(args.out)
    result_path = out_dir / ".fastfill-result.json"
    request = {
        "profile_dir": str(profile_dir()),
        "executable_path": engine.executable or "",
        "url": url,
        "max_months": int(args.max_pages),
        "out": str(result_path),
    }
    proc = subprocess.run(
        [node, str(script)],
        input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
        timeout=max(1800.0, args.timeout * 60),
        check=False,
        env=_node_env(),
    )
    if proc.returncode != 0 or not result_path.is_file():
        raise NetworkError("快速抓取没有完成", "可重试；若反复失败请用 didi fetch --browser")
    data = json.loads(result_path.read_text(encoding="utf-8"))
    try:
        result_path.unlink()
    except OSError:
        pass
    captures = [
        {
            "kind": "order_list",
            "url": f"{DIDI_ORDER_LIST_URL.split('?')[0]}/passenger/history",
            "status": 200,
            "response": page_data,
            "page": 0,
            "timemode": str(page_data.get("timemode") or ""),
        }
        for page_data in (data.get("pages") or [])
    ]
    built = build_browser_payload(
        {
            "captures": captures,
            "details": {},
            "dom_fares": data.get("fares") or {},
            "dom_orders": [],
            "warnings": [],
        },
        start_url=url,
    )
    notes = data.get("notes") or {}
    for trip in built.get("trips") or []:
        note = notes.get(trip.get("order_id"))
        if note:
            trip["fare_note"] = note
    built["source"] = "fastfill"
    cache = load_fare_cache()
    for oid, fare in (data.get("fares") or {}).items():
        if fare:
            cache[str(oid)] = str(fare)
    try:
        save_fare_cache(cache)
    except OSError:
        pass
    for warning in built.get("warnings") or []:
        print(f"警告: {warning}", file=sys.stderr)
    print(format_browser_summary(built))
    write_outputs(args, built)
    return 0


def _run_merge(args) -> int:
    out_dir = Path(args.out)
    files = sorted(out_dir.glob(f"{DIDI_FILE_PREFIX}-*.json"))
    files = [f for f in files if not f.name.startswith(f"{DIDI_FILE_PREFIX}-merged-")]
    if not files:
        raise UsageError(f"{out_dir} 里没有 {DIDI_FILE_PREFIX}-*.json")
    merged: Dict[str, Dict] = {}
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for trip in data.get("trips") or []:
            if not isinstance(trip, dict):
                continue
            oid = str(trip.get("order_id") or "")
            if not oid:
                continue
            current = merged.get(oid)
            if current is None:
                merged[oid] = dict(trip)
                continue
            for key, value in trip.items():
                if value in (None, "", [], {}):
                    continue
                if key == "fare_detail" and current.get(key):
                    continue
                current[key] = value
    trips = sorted(merged.values(), key=lambda t: str(t.get("occurred") or ""), reverse=True)
    payload = {
        "kind": "didi_trips",
        "source": "merged",
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trips": trips,
        "pages": [],
        "warnings": [],
        "blocked": None,
    }
    path = write_json(out_dir, f"{DIDI_FILE_PREFIX}-merged", payload)
    with_fare = sum(1 for t in trips if t.get("fare"))
    print(f"合并 {len(files)} 个文件 -> {len(trips)} 单，含金额 {with_fare} 单")
    print(f"written: {path}")
    print(f"查看: python3 -m connectors.didi report --file {path}")
    return 0


def _latest_trip_file(out_dir: Path) -> Path:
    candidates = sorted(
        Path(out_dir).glob(f"{DIDI_FILE_PREFIX}-*.json"), key=lambda path: path.stat().st_mtime
    )
    if not candidates:
        raise UsageError(f"{out_dir} 里没有 {DIDI_FILE_PREFIX}-*.json，请先跑 didi fastfill")
    return candidates[-1]


def _run_report(args) -> int:
    path = Path(args.file) if args.file else _latest_trip_file(Path(args.out))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"读不了账单文件: {path}") from exc
    trips = [t for t in (data.get("trips") or []) if isinstance(t, dict)]
    if args.limit and args.limit > 0:
        trips = trips[: args.limit]
    with_fare = sum(1 for trip in trips if trip.get("fare"))
    if args.csv:
        import csv

        writer = csv.writer(sys.stdout)
        writer.writerow(["occurred", "fare", "from", "to", "status", "order_id"])
        for trip in trips:
            writer.writerow(
                [trip.get(k) or "" for k in ("occurred", "fare", "from", "to", "status", "order_id")]
            )
        return 0
    print(f"账单文件: {path}")
    print(f"{'时间':<17}{'金额':>8}  {'起点':<20}→ {'终点':<20}{'状态'}")
    for trip in trips:
        occurred = str(trip.get("occurred") or "")[:16]
        fare = str(trip.get("fare") or "-")
        origin = str(trip.get("from") or "")[:20]
        dest = str(trip.get("to") or "")[:20]
        status = str(trip.get("status") or "")
        print(f"{occurred:<17}{fare:>8}  {origin:<20}→ {dest:<20}{status}")
    print(f"共 {len(trips)} 单，含金额 {with_fare} 单")
    return 0


def _run_keepalive(args) -> int:
    if not profile_ready():
        print("还没有固定登录态，请先运行：python3 -m connectors.didi login", file=sys.stderr)
        return 4
    url, _source = resolve_entry_url(fallback=DIDI_ORDER_LIST_URL)
    fetcher = DidiBrowserFetcher(
        CookieJar(),
        engine_prefer="node",
        headless=True,
        timeout=args.timeout,
        max_pages=1,
        with_detail=False,
        with_fare=False,
        url=url,
        profile_dir=str(profile_dir()),
    )
    payload = fetcher.fetch()
    blocked = payload.get("blocked")
    trips = payload.get("trips") or []
    if blocked:
        kind = str(blocked.get("kind") or "")
        message = str(blocked.get("message") or "滴滴页面出现风控拦截")
        print(f"保活失败（{kind}）：{message}", file=sys.stderr)
        try:
            (state_dir() / "stale").write_text(
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S\n")
            )
        except OSError:
            pass
        return 4
    try:
        (state_dir() / "stale").unlink()
    except OSError:
        pass
    print(
        f"保活成功：订单列表可读（{len(trips)} 单），"
        f"登录态已续期 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    return 0


def status_lines() -> List[str]:
    """Local browser-mode state for ``didi status`` (no network, no secrets)."""

    sess = session_summary()
    lines = [
        "browser profile: " + ("ready" if profile_ready() else "missing (run `python3 -m connectors.didi login`)"),
    ]
    if sess.get("stored"):
        lines.append(f"local session: stored (saved {sess.get('saved_at')}, cookies={sess.get('cookies')})")
    entry_url, entry_source = resolve_entry_url(fallback="")
    lines.append("entry url: " + ("present" if entry_url else "missing") + f" (source={entry_source})")
    lines.append(f"fare cache: {len(load_fare_cache())} orders")
    stale = state_dir() / "stale"
    if stale.is_file():
        try:
            when = stale.read_text(encoding="utf-8").strip()
        except OSError:
            when = ""
        lines.append(f"stale marker: {when or 'present'}（最近一次保活/抓取失败）")
    return lines
