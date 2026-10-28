"""淘宝 login / risk-page checks."""

from __future__ import annotations

from ..kit.blocking import _contains_any
from ..kit.errors import ApiError, AuthRequired, RiskBlocked


TAOBAO_LOGIN_MARKERS = (
    "login.taobao.com",
    "login.m.taobao.com",
    "亲，请登录",
    "请登录后",
    "passport.taobao.com",
    "havanaone",
)


TAOBAO_RISK_MARKERS = (
    "x5sec",
    "_____tmd_____",
    "punish",
    "验证码",
    "安全验证",
    "滑动验证",
    "请完成验证",
    "rgv587",
    "riskcontrol",
    "captcha",
)


def check_taobao_ret(payload) -> None:
    """Raise a typed error for a non-success mtop response."""

    ret = payload.get("ret") if isinstance(payload, dict) else None
    if isinstance(ret, str):
        ret_text = ret
    elif isinstance(ret, list):
        ret_text = " ".join(str(x) for x in ret)
    else:
        ret_text = ""
    upper = ret_text.upper()
    if "SUCCESS" in upper:
        return
    if not ret_text:
        return
    if any(
        token in upper
        for token in (
            "SESSION_EXPIRED",
            "SESSION_EXCEED",
            "NOT_LOGIN",
            "NO_LOGIN",
            "LOGIN_REQUIRED",
            "USER_NOT_LOGIN",
            "FAIL_SYS_LOGIN",
        )
    ):
        raise AuthRequired(
            "淘宝登录状态已失效",
            "请在浏览器中打开 https://www.taobao.com/ 并扫码登录后重试；本工具不会尝试绕过登录",
        )
    if any(
        token in upper
        for token in (
            "USER_VALIDATE",
            "VALIDATE",
            "RGV587",
            "PUNISH",
            "X5SEC",
            "CAPTCHA",
            "SLIDER",
            "RISK",
            "TRAFFIC_LIMIT",
            "ACCESS_DENIED",
            "ILLEGAL_ACCESS",
            "UNIT_PROTECT",
        )
    ):
        raise RiskBlocked(
            "淘宝返回了验证/风控页面",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    raise ApiError(f"淘宝接口返回失败: {ret_text[:200]}")


def check_taobao_html(text: str, url: str = "", status: int = 200) -> None:
    lowered_url = url.lower()
    if any(marker in lowered_url for marker in ("x5sec", "punish", "captcha")):
        raise RiskBlocked(
            "淘宝请求被重定向到验证/风控地址",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    if status in (401, 403):
        raise AuthRequired(
            "淘宝拒绝了请求（HTTP %d）" % status,
            "请确认浏览器中仍处于登录状态",
        )
    if _contains_any(text, TAOBAO_RISK_MARKERS):
        raise RiskBlocked(
            "淘宝返回了验证/风控页面",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    if "login.taobao.com" in lowered_url or "login.m.taobao.com" in lowered_url:
        raise AuthRequired(
            "淘宝跳转到了登录页",
            "请在浏览器中打开 https://www.taobao.com/ 并扫码登录后重试",
        )
    marker = _contains_any(text, TAOBAO_LOGIN_MARKERS)
    if marker:
        raise AuthRequired(
            "淘宝返回了登录页",
            "请在浏览器中打开 https://www.taobao.com/ 并扫码登录后重试",
        )
