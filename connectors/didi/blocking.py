"""滴滴 login / signature / risk-page checks."""

from __future__ import annotations

from ..kit.blocking import _texts
from ..kit.errors import ApiError, AuthRequired, NeedsRiskSignature, RiskBlocked


DIDI_LOGIN_MARKERS = (
    "请登录",
    "请先登录",
    "登录后",
    "登录已失效",
    "登录状态",
    "未登录",
    "login_outdate",
    "token invalid",
    "invalid token",
    "token expired",
    "token error",
    "token失效",
    "token 失效",
    "passport.diditaxi",
    "login.diditaxi",
    "sso.diditaxi",
)


DIDI_SIGNATURE_MARKERS = (
    "wsgsig",
    "验签",
    "签名错误",
    "签名无效",
    "签名校验",
    "sign error",
    "invalid sign",
    "signature",
    "missing sign",
)


DIDI_RISK_MARKERS = (
    "验证码",
    "安全验证",
    "滑动验证",
    "请完成验证",
    "风控",
    "risk-control",
    "riskcontrol",
    "captcha",
    "x5sec",
    "punish",
    "访问频繁",
    "请求频繁",
    "限流",
    "forbidden",
    "illegal",
    "非法请求",
)


def _raise_didi_signature() -> None:
    raise NeedsRiskSignature(
        "didi API needs risk signature (wsgsig); "
        "this tool does not forge or bypass it",
        "当前滴滴 bundle 给列表/详情/费用请求都加 wsgsig；本工具只做无签名只读请求，"
        "不会伪造或重放签名。请在已登录的浏览器页面中人工查看，"
        "或等待支持签名的方案。",
    )


def check_didi_payload(payload, status: int = 200) -> None:
    """Raise a typed error for a non-success Didi JSON response.

    ``errno == 0`` is success.  Signature-related messages become
    :class:`NeedsRiskSignature`; login messages become :class:`AuthRequired`;
    other risk controls become :class:`RiskBlocked`.
    """

    if not isinstance(payload, dict):
        raise ApiError("滴滴接口返回了非 JSON 对象")

    errno = payload.get("errno")
    text = _texts(payload)
    lowered = text.lower()

    if errno in (0, "0", None) and not any(
        token in lowered
        for token in ("error", "fail", "失败", "登录", "验证", "签名")
    ):
        return

    if str(errno) in ("101", "401") or any(
        marker.lower() in lowered for marker in DIDI_LOGIN_MARKERS
    ):
        raise AuthRequired(
            "滴滴登录状态已失效或 URL token 无效",
            "请从已登录的滴滴页面 URL 重新获取 token，"
            "放进环境变量 PFIN_DIDI_TOKEN 后重试",
        )

    if any(marker.lower() in lowered for marker in DIDI_SIGNATURE_MARKERS):
        _raise_didi_signature()

    if str(errno) == "403" or status in (401, 403) or any(
        marker.lower() in lowered for marker in DIDI_RISK_MARKERS
    ):
        raise RiskBlocked(
            "滴滴接口返回了验证/风控提示",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )

    raise ApiError(f"滴滴接口返回失败 (errno={errno!r}): {text[:200]}")


def check_didi_html(text: str, url: str = "", status: int = 200) -> None:
    lowered_url = url.lower()
    lowered_text = text.lower()
    # Live check (2026-10): /passenger/history with any query/body but no
    # wsgsig returns HTTP 501 from "router/2.43.2"; a bare request returns
    # JSON.  The current bundle signs every axios request because the sign
    # whitelist contains "*".  This is the unsigned-request rejection, not a
    # login/captcha page, so stop with the signature-specific error.
    if status == 501 and (
        "router/" in lowered_text or "not implemented" in lowered_text
    ):
        _raise_didi_signature()
    if any(
        marker in lowered_url
        for marker in ("x5sec", "punish", "captcha", "verify", "risk")
    ):
        raise RiskBlocked(
            "滴滴请求被重定向到验证/风控地址",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    if any(marker in lowered_url for marker in ("passport", "login", "sso")):
        raise AuthRequired(
            "滴滴跳转到了登录页",
            "请从已登录的滴滴页面 URL 重新获取 token 后重试",
        )
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in DIDI_SIGNATURE_MARKERS):
        _raise_didi_signature()
    if any(marker.lower() in lowered for marker in DIDI_RISK_MARKERS):
        raise RiskBlocked(
            "滴滴返回了验证/风控页面",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    if any(marker.lower() in lowered for marker in DIDI_LOGIN_MARKERS):
        raise AuthRequired(
            "滴滴返回了登录页",
            "请从已登录的滴滴页面 URL 重新获取 token 后重试",
        )
    if status in (401, 403):
        raise AuthRequired(
            "滴滴拒绝了请求（HTTP %d）" % status,
            "请确认 PFIN_DIDI_TOKEN 仍有效",
        )