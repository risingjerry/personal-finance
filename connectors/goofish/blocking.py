"""闲鱼 login / risk-page checks."""

from __future__ import annotations

from ..kit.blocking import _contains_any
from ..kit.errors import ApiError, AuthRequired, NeedsRiskSignature, RiskBlocked


GOOFISH_LOGIN_MARKERS = (
    "login.goofish.com",
    "passport.goofish.com",
    "请登录后",
    "请先登录",
    "亲，请登录",
)


GOOFISH_RISK_MARKERS = (
    "x5sec",
    "_____tmd_____",
    "punish",
    "rgv587",
    "验证码",
    "安全验证",
    "滑动验证",
    "请完成验证",
    "riskcontrol",
    "risk-control",
    "captcha",
)


# These markers mean the request reached an anti-bot challenge that expects a
# token produced by the page's AWSC / fyOBJ SDK (``ua``, ``umidtoken``, ``et``
# or a baxia ``x5sec`` payload).  We never compute, replay or fabricate one.
GOOFISH_SIGNATURE_MARKERS = (
    "needs_risk_signature",
    "need_risk_signature",
    "risk_signature",
    "x-sign",
    "bx-ua",
    "umidtoken",
    "asac",
)


def check_goofish_ret(payload) -> None:
    """Raise a typed error for a non-success Goofish mtop response.

    The public Goofish bundle only adds ``ua``/``umidtoken`` from
    ``window.fyOBJ`` when the caller sets ``data.asac``.  This tool never sets
    it, so if the server asks for those values we stop with an explicit
    "needs risk signature" error rather than forging them.
    """

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
            "闲鱼登录状态已失效",
            "请在浏览器中打开 https://www.goofish.com/ 并扫码登录后重试；"
            "本工具不会尝试绕过登录",
        )
    if any(marker.upper() in upper for marker in GOOFISH_SIGNATURE_MARKERS):
        raise NeedsRiskSignature(
            "闲鱼接口要求风控签名（needs risk signature）",
            "该签名来自页面加载的 AWSC/fyOBJ SDK（window.fyOBJ.getFYToken()/"
            "getUidToken()，仅在 data.asac 为真时由前端加入 ua/umidtoken）。"
            "本工具不会伪造或绕过它；可用已登录的浏览器打开 www.goofish.com "
            "手动查看，或等待后续的浏览器驱动支持",
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
            "闲鱼返回了验证/风控页面",
            "如果页面要求的是 AWSC/fyOBJ 风控签名（needs risk signature），"
            "本工具不会伪造或绕过；请先在浏览器中人工完成验证后再重试",
        )
    raise ApiError(f"闲鱼接口返回失败: {ret_text[:200]}")


def check_goofish_html(text: str, url: str = "", status: int = 200) -> None:
    lowered_url = url.lower()
    if any(marker in lowered_url for marker in ("x5sec", "punish", "captcha")):
        raise RiskBlocked(
            "闲鱼请求被重定向到验证/风控地址",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    if status in (401, 403):
        raise AuthRequired(
            "闲鱼拒绝了请求（HTTP %d）" % status,
            "请确认浏览器中仍处于登录状态",
        )
    if _contains_any(text, GOOFISH_RISK_MARKERS):
        raise RiskBlocked(
            "闲鱼返回了验证/风控页面",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    if "login.goofish.com" in lowered_url or "passport.goofish.com" in lowered_url:
        raise AuthRequired(
            "闲鱼跳转到了登录页",
            "请在浏览器中打开 https://www.goofish.com/ 并扫码登录后重试",
        )
    marker = _contains_any(text, GOOFISH_LOGIN_MARKERS)
    if marker:
        raise AuthRequired(
            "闲鱼返回了登录页",
            "请在浏览器中打开 https://www.goofish.com/ 并扫码登录后重试",
        )