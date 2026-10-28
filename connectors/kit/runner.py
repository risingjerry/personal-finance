"""The one command-line shape every connector shares.

    python3 -m connectors.<name> manifest                  what this connector is (JSON)
    python3 -m connectors.<name> status [--online] [--json] is the login there (and still accepted)?
    python3 -m connectors.<name> login [--wait N] [--clear] log in once (a person scans / types codes)
    python3 -m connectors.<name> fetch --out DIR [...]     raw JSON + <name>-rows-<ts>.jsonl, read-only
    python3 -m connectors.<name> keepalive [--json]        one light read-only request so the session stays warm

A connector module only declares a Connector: how to fetch, summarise and turn its payload into rows, and
(optionally) its online check, its own login and extra actions. Everything here is read-only: no command
can buy, pay, cancel, confirm or change anything on the platform.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from .cookies import BROWSER_APP, DEFAULT_COOKIE_DB, DEFAULT_KEYCHAIN_SERVICE, CookieProvider, browser_label
from .errors import ConnectorError, UsageError, internal_error
from .http import HttpClient, Pacer
from .output import write_json
from .rows import stamp_from_raw, write_rows
from .session import SessionStore
from .spec import (
    ProbeResult,
    SiteSpec,
    inspect_site,
    probe,
    render_inspection,
    render_keepalive,
    render_probe,
    result_exit_code,
)


@dataclass
class Connector:
    key: str  # command / folder / rows-file name, e.g. "taobao"
    label: str  # human name, e.g. "淘宝"
    prefix: str  # raw file prefix, e.g. "taobao-boughtlist"
    fetch: Callable  # (args, jar) -> (payload, warnings); jar is None when spec is None
    summary: Callable  # (payload) -> one-paragraph text
    rows: Callable  # (payload) -> list of rows (connectors.kit.rows.make_record)
    spec: Optional[SiteSpec] = None  # browser login cookies; None for API-key / own-browser connectors
    describe: str = ""  # one line for --help and the manifest
    add_fetch_args: Callable = lambda parser: None
    validate: Callable = lambda args: None
    plan: Optional[Callable] = None  # (args) -> dry-run text
    check: Optional[Callable] = None  # (spec, client, jar) -> ProbeResult: the online check
    login: Optional[Callable] = None  # (args) -> exit code; default opens spec.login_url in browser and waits
    status: Optional[Callable] = None  # (args) -> (lines, exit code); default inspects spec's cookies
    keepalive: Optional[Callable] = None  # (args) -> exit code; default runs `check`
    extra: Optional[Callable] = None  # (subparsers) -> None: connector-specific actions
    run_extra: Optional[Callable] = None  # (args) -> exit code or None
    login_kind: str = "browser"  # browser | qr | api-key
    keepalive_hours: float = 24.0
    min_delay: float = 1.0  # floor for --delay (seconds between request starts)
    max_pages: int = 50
    record_types: Tuple[str, ...] = ("order",)
    secrets: Tuple[str, ...] = ()  # secret names an API-key connector needs (kit/secrets.py)

    def manifest(self) -> Dict:
        return {
            "schema": "pfin.connector/1",
            "key": self.key,
            "label": self.label,
            "description": self.describe,
            "verbs": ["manifest", "status", "login", "fetch", "keepalive"],
            "login": self.login_kind,
            "login_url": self.spec.login_url if self.spec else None,
            "keepalive_hours": self.keepalive_hours,
            "outputs": {"raw": f"{self.prefix}-<ts>.json", "rows": f"{self.key}-rows-<ts>.jsonl"},
            "record_types": list(self.record_types),
            "secrets": list(self.secrets),
            "read_only": True,
        }


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def provider(connector: Connector, args) -> CookieProvider:
    spec = connector.spec
    return CookieProvider(
        getattr(args, "cookie_db", None) or DEFAULT_COOKIE_DB,
        keychain_service=getattr(args, "keychain_service", None) or DEFAULT_KEYCHAIN_SERVICE,
        host_suffixes=spec.host_suffixes,
        session_store=SessionStore(spec.key, spec.host_suffixes),
    )


def client(connector: Connector, args) -> HttpClient:
    return HttpClient(pacer=Pacer(max(connector.min_delay, getattr(args, "delay", 1.2))), timeout=getattr(args, "timeout", 30.0))


def browser_args(parser) -> None:
    parser.add_argument("--cookie-db", type=Path, default=None, help="浏览器 Cookies 数据库（默认浏览器的 Default 配置）")
    parser.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE, help="Keychain 服务名")


def net_args(parser, connector: Connector) -> None:
    parser.add_argument("--timeout", type=float, default=30.0, help="单次请求超时秒数")
    parser.add_argument("--delay", type=float, default=max(1.2, connector.min_delay), help=f"请求间隔秒数（不低于 {connector.min_delay}）")


def build_parser(connector: Connector) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"pfin-{connector.key}", description=f"{connector.label}：{connector.describe}（只读）")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("manifest", help="输出这个连接器的说明（JSON）")
    status = actions.add_parser("status", help="登录状态（不发请求）；--online 再发一次只读请求确认")
    status.add_argument("--online", action="store_true")
    status.add_argument("--json", action="store_true")
    login = actions.add_parser("login", help="登录一次（扫码、验证码由你完成）")
    login.add_argument("--wait", type=int, default=300, help="等待登录完成的秒数，默认 300")
    login.add_argument("--clear", action="store_true", help="只清掉本工具保存的会话（不改浏览器），不登录")
    fetch = actions.add_parser("fetch", help="拉取并保存原始 JSON 和统一格式的 rows")
    fetch.add_argument("--out", type=Path, help="输出目录（非 --dry-run 必填）")
    fetch.add_argument("--dry-run", action="store_true", help="只打印请求计划，不读登录信息、不联网")
    fetch.add_argument("--max-pages", type=int, default=connector.max_pages, help=f"最多翻页数，默认 {connector.max_pages}")
    keepalive = actions.add_parser("keepalive", help="发一次轻量只读请求，让登录会话保持活跃")
    keepalive.add_argument("--json", action="store_true")
    for sub in (status, login, fetch, keepalive):
        if connector.spec is not None:
            browser_args(sub)
        net_args(sub, connector)
    connector.add_fetch_args(fetch)
    if connector.extra:
        connector.extra(actions)
    return parser


# ---------------------------------------------------------------------------
# the five verbs
# ---------------------------------------------------------------------------


def do_status(connector: Connector, args) -> int:
    if connector.status:
        lines, code = connector.status(args)
    else:
        if connector.spec is None:
            raise UsageError(f"{connector.label} 没有浏览器登录状态可查")
        inspection = inspect_site(connector.spec, provider(connector, args))
        lines = render_inspection(inspection)
        code = 3 if inspection.error or (inspection.needs_login and inspection.has_any_login_cookie) else 0
        if args.online and connector.check and not inspection.error:
            result = probe(connector.spec, provider(connector, args), client(connector, args), connector.check)
            lines.append(render_probe(result))
            code = code or result_exit_code(result)
    if getattr(args, "json", False):
        print(json.dumps({"connector": connector.key, "lines": lines, "exit": code}, ensure_ascii=False))
    else:
        print("\n".join(lines))
    return code


def do_login(connector: Connector, args) -> int:
    if args.clear:
        if connector.spec is None:
            raise UsageError(f"{connector.label} 没有本工具保存的浏览器会话")
        SessionStore(connector.spec.key, connector.spec.host_suffixes).clear()
        print(f"{connector.label}：本工具保存的会话已清除（浏览器未改动）")
        return 0
    if connector.login:
        return connector.login(args)
    spec = connector.spec
    if spec is None:
        raise UsageError(f"{connector.label} 不需要浏览器登录")
    subprocess.run(["open", "-a", BROWSER_APP, spec.login_url], check=False)
    print(f"已在 {browser_label()} 打开 {connector.label} 登录页：{spec.login_url}")
    print(f"请在浏览器里登录（扫码、验证码由你完成），最多等 {args.wait} 秒……")
    deadline = time.monotonic() + max(10, args.wait)
    while time.monotonic() < deadline:
        inspection = inspect_site(spec, provider(connector, args))
        if not inspection.error and not inspection.needs_login:
            print(f"{connector.label}：已登录")
            return 0
        time.sleep(10)
    print(f"{connector.label}：{args.wait} 秒内没有检测到登录", file=sys.stderr)
    return 3


def do_fetch(connector: Connector, args) -> int:
    if args.dry_run:
        print(connector.plan(args) if connector.plan else f"plan: {connector.key} fetch（dry-run：不读登录信息、不联网）")
        return 0
    if args.max_pages < 1:
        raise UsageError("--max-pages 必须 >= 1")
    if args.out is None:
        raise UsageError("缺少 --out", "例如：--out ./out")
    connector.validate(args)
    jar = provider(connector, args).load() if connector.spec is not None else None
    payload, warnings = connector.fetch(args, jar)
    for warning in warnings:
        print(f"警告: {warning}", file=sys.stderr)
    # A connector with two feeds (e.g. Alipay personal bills / merchant funds) names each output itself.
    # A fetch that hit a wall part way (e.g. a risk signature) still saves what it got, then fails.
    blocked = payload.pop("_blocked", None) if isinstance(payload, dict) else None
    prefix = payload.pop("_prefix", connector.prefix) if isinstance(payload, dict) else connector.prefix
    rows_name = payload.pop("_rows", connector.key) if isinstance(payload, dict) else connector.key
    records = connector.rows(payload)
    raw_path = write_json(args.out, prefix, payload)
    rows_path = write_rows(args.out, rows_name, records, stamp=stamp_from_raw(raw_path, prefix))
    print(connector.summary(payload))
    print(f"written: {raw_path}")
    print(f"rows: {len(records)} -> {rows_path}")
    if blocked is not None:
        raise blocked
    return 0


def do_keepalive(connector: Connector, args) -> int:
    if connector.keepalive:
        return connector.keepalive(args)
    if connector.spec is None or connector.check is None:
        print(json.dumps({"connector": connector.key, "status": "skip", "detail": "不需要保活"}) if args.json else f"{connector.label}：不需要保活")
        return 0
    result: ProbeResult = probe(connector.spec, provider(connector, args), client(connector, args), connector.check)
    if args.json:
        print(json.dumps({"connector": connector.key, "status": result.status, "detail": result.detail}, ensure_ascii=False))
    else:
        print(render_keepalive(result))
    return result_exit_code(result)


def main(connector: Connector, argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser(connector).parse_args(argv)
    try:
        if args.action == "manifest":
            print(json.dumps(connector.manifest(), ensure_ascii=False, indent=1))
            return 0
        if args.action == "status":
            return do_status(connector, args)
        if args.action == "login":
            return do_login(connector, args)
        if args.action == "fetch":
            return do_fetch(connector, args)
        if args.action == "keepalive":
            return do_keepalive(connector, args)
        if connector.run_extra:
            code = connector.run_extra(args)
            if code is not None:
                return code
        raise UsageError(f"未知动作: {args.action}")
    except ConnectorError as exc:
        print(f"错误: {exc.render()}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # never dump internals that might hold sensitive values
        print(internal_error(exc), file=sys.stderr)
        return 70


def run(connector: Connector) -> None:
    raise SystemExit(main(connector))
