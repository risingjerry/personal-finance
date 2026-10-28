"""A connector's login description and its offline / online login checks.

SiteSpec says which cookies prove a login; inspect_site reads their presence and expiry without network
(browser cookie metadata merged with the connector's Keychain session); probe() runs the connector's one
light read-only request. Cookie values are never printed.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from .cookies import CookieProvider, StoredCookie, domain_matches
from .env import getenv
from .errors import (
    ApiError,
    AuthRequired,
    CookieError,
    NeedsRiskSignature,
    NetworkError,
    ConnectorError,
    RiskBlocked,
    internal_error,
)
from .http import HttpClient


@dataclass(frozen=True)
class LoginSet:
    """One alternative cookie combination that can authenticate a session."""

    key: str
    label: str
    cookies: Tuple[str, ...]


@dataclass(frozen=True)
class SiteSpec:
    key: str
    label: str
    host_suffixes: Tuple[str, ...]
    cookie_host: str
    login_cookies: Tuple[str, ...]
    required_cookies: Tuple[str, ...]
    login_url: str
    token_cookies: Tuple[str, ...] = ()
    login_sets: Tuple[LoginSet, ...] = ()
    required_mode: str = "all"  # "all" or "any"
    env_token: Optional[str] = None
    # Documented blockers: still report cookies, but never pretend the site is
    # fully usable without the missing signature/token.
    blocked_reason: Optional[str] = None
    experimental: bool = False
    # False for documented status/probe-only sites: status and keepalive may
    # report them, but fetch-all must never try to export orders.
    fetchable: bool = True
    # Fetched inside another site's run (same login), e.g. 白条 with jd: fetch-all skips it.
    merged_into: Optional[str] = None

    @property
    def cookie_sets(self) -> Tuple[LoginSet, ...]:
        """Alternative login sets; a single required set by default."""

        if self.login_sets:
            return self.login_sets
        return (LoginSet(self.key, "", self.required_cookies),)


@dataclass
class CookieCheck:
    name: str
    required: bool
    present: bool
    expired: bool
    session: bool
    expires_at: Optional[datetime.datetime]
    storage: str
    host_key: str = ""
    token: bool = False

    @property
    def readable(self) -> bool:
        return self.storage in ("plain", "v10", "env")

    @property
    def usable(self) -> bool:
        return self.present and not self.expired and self.readable


@dataclass
class Inspection:
    spec: SiteSpec
    cookies: List[CookieCheck] = field(default_factory=list)
    error: Optional[str] = None
    env_token_present: bool = False

    def _check(self, name: str) -> Optional[CookieCheck]:
        for check in self.cookies:
            if check.name == name:
                return check
        return None

    def _set_usable(self, login_set: LoginSet) -> bool:
        checks = [self._check(name) for name in login_set.cookies]
        return all(check is not None and check.usable for check in checks)

    @property
    def usable_login_sets(self) -> List[LoginSet]:
        return [
            login_set
            for login_set in self.spec.cookie_sets
            if self._set_usable(login_set)
        ]

    @property
    def login_set(self) -> Optional[LoginSet]:
        usable = self.usable_login_sets
        return usable[0] if usable else None

    @property
    def login_method(self) -> str:
        return " + ".join(
            login_set.label
            for login_set in self.usable_login_sets
            if login_set.label
        )

    @property
    def missing_required(self) -> List[str]:
        if not self.needs_login:
            return []
        missing: List[str] = []
        for login_set in self.spec.cookie_sets:
            for name in login_set.cookies:
                check = self._check(name)
                if (check is None or not check.present) and name not in missing:
                    missing.append(name)
        return missing

    @property
    def unusable_required(self) -> List[str]:
        if not self.needs_login:
            return []
        unusable: List[str] = []
        for login_set in self.spec.cookie_sets:
            for name in login_set.cookies:
                check = self._check(name)
                if (
                    check is not None
                    and check.present
                    and not check.usable
                    and name not in unusable
                ):
                    unusable.append(name)
        return unusable

    @property
    def has_any_login_cookie(self) -> bool:
        """True when at least one required cookie (or the env token) exists.

        ``status`` still lists every site, but a site that was never logged in
        should not make the whole run exit 3; only a detected problem with an
        existing login does.
        """

        if self.env_token_present:
            return True
        return any(c.present for c in self.cookies if c.required)

    @property
    def needs_login(self) -> bool:
        if self.env_token_present:
            return False
        if self.spec.required_mode == "any":
            required = [c for c in self.cookies if c.required]
            return not any(c.usable for c in required)
        return not self.usable_login_sets


@dataclass
class ProbeResult:
    spec: SiteSpec
    status: str  # ok | logged_out | blocked | needs_signature | network_error | cookie_error | error
    detail: str = ""
    token_rotations: int = 0
    token_retries: int = 0
    cookie_rotations: int = 0


def _best_cookie(
    cookies: Sequence[StoredCookie],
    name: str,
    now: datetime.datetime,
    host: str,
) -> Optional[StoredCookie]:
    matches = [
        c
        for c in cookies
        if c.name == name
        and c.has_value
        and domain_matches(c.host_key, host)
    ]
    if not matches:
        return None
    # Prefer a live, host-specific cookie over an expired or broader one.
    matches.sort(
        key=lambda c: (
            c.is_expired(now),
            0 if not c.host_key.startswith(".") else 1,
            -len(c.host_key),
            -len(c.path),
        )
    )
    return matches[0]


def inspect_site(
    spec: SiteSpec,
    provider: CookieProvider,
    now: Optional[datetime.datetime] = None,
) -> Inspection:
    """Read merged browser/session presence and expiry without network access."""

    moment = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        stored = provider.inspect()
    except CookieError as exc:
        return Inspection(spec=spec, error=exc.render())

    env_present = bool(spec.env_token and getenv(spec.env_token))
    checks: List[CookieCheck] = []
    required_names = {
        name for login_set in spec.cookie_sets for name in login_set.cookies
    }
    for name in spec.login_cookies + spec.token_cookies:
        cookie = _best_cookie(
            stored,
            name,
            moment,
            spec.cookie_host,
        )
        if cookie is None:
            checks.append(
                CookieCheck(
                    name=name,
                    required=name in required_names,
                    present=False,
                    expired=False,
                    session=False,
                    expires_at=None,
                    storage="empty",
                    token=name in spec.token_cookies,
                )
            )
            continue
        checks.append(
            CookieCheck(
                name=name,
                required=name in required_names,
                present=True,
                expired=cookie.is_expired(moment),
                session=cookie.is_session(),
                expires_at=cookie.expires_at(),
                storage=cookie.storage,
                host_key=cookie.host_key,
                token=name in spec.token_cookies,
            )
        )
    return Inspection(
        spec=spec, cookies=checks, env_token_present=env_present
    )


def _storage_text(check: CookieCheck) -> str:
    if not check.present:
        return "缺失"
    if check.storage == "v10" or check.storage == "plain":
        return "存在"
    return "存在（不支持的加密格式，本工具无法解密）"


def format_expiry(check: CookieCheck) -> str:
    if not check.present:
        return "—"
    if check.session:
        return "会话 Cookie（无绝对过期时间）"
    if check.expires_at is None:
        return "未知（expires_utc 无法解析）"
    local = check.expires_at.astimezone()
    stamp = local.strftime("%Y-%m-%d %H:%M:%S %Z")
    if check.expired:
        return f"已过期（{stamp}）"
    return stamp


def login_needed_line(spec: SiteSpec) -> str:
    """One-line, greppable form of "this site needs a QR login".

    Shared by ``status`` / ``keepalive`` / ``maintenance`` so the three commands
    never disagree about whether 12306 needs the user to scan a code.
    """

    return f"{spec.label}: {LOGIN_SCAN_LABEL} → {spec.login_url}"


def login_hint(spec: SiteSpec) -> str:
    return (
        f"{LOGIN_SCAN_LABEL}：请在浏览器中打开 {spec.login_url} 并扫码登录，"
        "然后运行该连接器的 `status --online` 确认。"
    )


def _spec_flags(spec: SiteSpec) -> List[str]:
    lines: List[str] = []
    if spec.experimental:
        lines.append("  标注: 实验性站点（登录验证未通过，取数可能失败）")
    if spec.blocked_reason:
        lines.append(f"  已知阻塞: {spec.blocked_reason}")
    return lines


def render_inspection(inspection: Inspection) -> List[str]:
    spec = inspection.spec
    lines = [f"{spec.label}（{' / '.join(spec.host_suffixes)}）"]
    if inspection.error:
        lines.append(f"  Cookie 数据库: 读取失败 — {inspection.error}")
        lines.append("  结论: 无法检查登录 Cookie，请确认浏览器的 Cookie 数据库路径和权限。")
        lines.extend(_spec_flags(spec))
        return lines

    lines.append("  Cookie 存在性与过期时间（不显示任何值）:")
    multi_set = len(spec.cookie_sets) > 1
    for check in inspection.cookies:
        if check.required:
            if spec.required_mode == "any":
                role = "候选"
            else:
                role = "组合" if multi_set else "必需"
        elif check.token:
            role = "令牌"
        else:
            role = "可选"
        lines.append(
            f"    {check.name:<28} {role}  {_storage_text(check):<28} "
            f"过期: {format_expiry(check)}"
        )

    if spec.env_token:
        lines.append(
            f"  环境变量 {spec.env_token}: "
            + ("已设置（值不显示）" if inspection.env_token_present else "未设置")
        )

    if spec.login_sets:
        if inspection.usable_login_sets:
            lines.append(
                "  登录方式: "
                + " / ".join(
                    login_set.label
                    for login_set in inspection.usable_login_sets
                )
            )
        else:
            lines.append(
                "  登录方式: 未检测到完整组合；需要 "
                + " 或 ".join(
                    login_set.label for login_set in spec.login_sets
                )
                + " 任一组"
            )

    if inspection.needs_login:
        if spec.required_mode == "any":
            required = [c.name for c in inspection.cookies if c.required]
            lines.append(
                "  结论: 没有可用的登录 Cookie（任一项即可："
                + ", ".join(required)
                + "）。"
            )
        else:
            if inspection.missing_required:
                lines.append(
                    "  结论: 登录 Cookie 组合缺失（"
                    + ", ".join(inspection.missing_required)
                    + "）。"
                )
            if inspection.unusable_required:
                lines.append(
                    "  结论: 登录 Cookie 组合已过期或不可读（"
                    + ", ".join(inspection.unusable_required)
                    + "）。"
                )
        lines.append("  " + login_hint(spec))
    else:
        suffix = (
            f"（登录方式: {inspection.login_method}）"
            if inspection.login_method and not spec.login_sets
            else ""
        )
        if spec.env_token and inspection.env_token_present:
            suffix = f"（令牌来源: 环境变量 {spec.env_token}）"
        lines.append(f"  结论: 登录 Cookie 存在且未过期{suffix}。")

    token_checks = [c for c in inspection.cookies if c.token]
    if any(not c.usable for c in token_checks):
        lines.append(
            "  令牌: _m_h5_tk 缺失或已过期，工具会在下一次请求时自动刷新；"
            "这不等同于需要重新登录。"
        )
    lines.extend(_spec_flags(spec))
    return lines


def _probe_error(spec: SiteSpec, exc: ConnectorError) -> ProbeResult:
    if isinstance(exc, AuthRequired):
        return ProbeResult(spec, "logged_out", exc.render())
    if isinstance(exc, NeedsRiskSignature):
        return ProbeResult(spec, "needs_signature", exc.render())
    if isinstance(exc, RiskBlocked):
        return ProbeResult(spec, "blocked", exc.render())
    if isinstance(exc, NetworkError):
        return ProbeResult(spec, "network_error", exc.message)
    if isinstance(exc, CookieError):
        return ProbeResult(spec, "cookie_error", exc.render())
    if isinstance(exc, ApiError):
        return ProbeResult(spec, "error", exc.render())
    return ProbeResult(spec, "error", exc.render())


def _select_login_set(
    spec: SiteSpec, jar
) -> Tuple[Optional[LoginSet], List[Tuple[LoginSet, List[str]]]]:
    """Return the first complete login set, or per-set missing names."""

    failures: List[Tuple[LoginSet, List[str]]] = []
    for login_set in spec.cookie_sets:
        missing = [
            name
            for name in login_set.cookies
            if not jar.get(name, spec.cookie_host)
        ]
        if not missing:
            return login_set, []
        failures.append((login_set, missing))
    return None, failures


def render_probe(result: ProbeResult) -> str:
    if result.status == "ok":
        extra = ""
        if result.token_rotations or result.cookie_rotations:
            extra = "；服务端轮换了 Cookie（CLI 会话已接收，未写回浏览器）"
        return f"  在线检查: 已登录（{result.detail}{extra}）"
    if result.status == "logged_out":
        return (
            "  在线检查: 未登录或登录已失效 — "
            + result.detail
            + "\n  "
            + login_hint(result.spec)
        )
    if result.status == "needs_signature":
        return (
            "  在线检查: 需要风控签名（无法在不伪造签名的前提下确认） — "
            + result.detail
            + "\n  本工具不会计算、伪造或重放签名；请在浏览器中人工查看。"
        )
    if result.status == "blocked":
        return (
            "  在线检查: 无法确认（服务器要求验证/风控拦截） — "
            + result.detail
            + "\n  请先在浏览器中人工完成验证后再试；本工具不会尝试绕过。"
        )
    if result.status == "network_error":
        return f"  在线检查: 网络请求失败 — {result.detail}"
    return f"  在线检查: 失败 — {result.detail}"


def render_keepalive(result: ProbeResult) -> str:
    if result.status == "ok":
        extra = ""
        if result.token_rotations or result.cookie_rotations:
            extra = "；服务端轮换了 Cookie（CLI 会话已接收，未写回浏览器）"
        return f"{result.spec.label}: 已只读访问 {result.detail}{extra}"
    if result.status == "logged_out":
        return (
            f"{result.spec.label}: {LOGIN_SCAN_LABEL}（会话已失效）— {result.detail}\n"
            f"  {login_hint(result.spec)}"
        )
    if result.status == "needs_signature":
        return (
            f"{result.spec.label}: 未完成只读访问（需要风控签名） — "
            f"{result.detail}\n  本工具不会伪造或重放签名。"
        )
    if result.status == "blocked":
        return (
            f"{result.spec.label}: 未完成只读访问（验证/风控拦截） — "
            f"{result.detail}\n  请先在浏览器中人工完成验证后再试。"
        )
    if result.status == "network_error":
        return f"{result.spec.label}: 只读访问失败（网络） — {result.detail}"
    return f"{result.spec.label}: 只读访问失败 — {result.detail}"


def result_exit_code(result: ProbeResult) -> int:
    return {
        "ok": 0,
        "logged_out": 3,
        "cookie_error": 3,
        "blocked": 4,
        "needs_signature": 4,
        "network_error": 5,
        "error": 6,
        "internal_error": 70,
    }.get(result.status, 6)


LOGIN_SCAN_LABEL = "需要登录扫码"


KEEPALIVE_NOTE = (
    "说明: keepalive 对每个站点只做一次只读访问（淘宝令牌缺失时可能多一次刷新请求），\n"
    "      用于让服务端把空闲会话继续视为活跃；已标注阻塞/实验性的站点也会如实报告。\n"
    "      Cookie 的绝对过期时间无法被任何客户端延长，到期后仍需要在浏览器中扫码重新登录。\n"
    "      服务端通过 Set-Cookie 轮换的 Cookie（淘宝 _m_h5_tk/_m_h5_tk_enc、京东登录 Cookie）\n"
    "      保存到 CLI 独立登录钥匙串；不会写回浏览器。"
)


def probe(
    spec: SiteSpec,
    provider: CookieProvider,
    client: HttpClient,
    check: Callable,
) -> ProbeResult:
    """Cookie sanity first (no request without a login), then the connector's own read-only ``check``.

    ``check(spec, client, jar)`` returns a ProbeResult; connectors with their own browser session pass a
    ``check`` that ignores the jar.
    """

    try:
        jar = provider.load()
        client.cookie_jar = jar
    except CookieError as exc:
        if "没有可用的登录 Cookie" in exc.message:
            return ProbeResult(spec, "logged_out", exc.render())
        return ProbeResult(spec, "cookie_error", exc.render())
    if spec.required_mode == "any":
        if not any(jar.get(name, spec.cookie_host) for name in spec.required_cookies):
            return ProbeResult(spec, "logged_out", "缺少登录 Cookie: " + ", ".join(spec.required_cookies))
    else:
        login_set, failures = _select_login_set(spec, jar)
        if login_set is None:
            if len(failures) == 1:
                detail = "缺少登录 Cookie: " + ", ".join(failures[0][1])
            else:
                detail = "缺少完整的登录 Cookie 组合: " + "；".join(
                    f"{item.label} 缺 {', '.join(missing)}" for item, missing in failures
                )
            return ProbeResult(spec, "logged_out", detail)
    try:
        return check(spec, client, jar)
    except ConnectorError as exc:
        return _probe_error(spec, exc)
    except Exception as exc:
        return ProbeResult(spec, "internal_error", internal_error(exc))
