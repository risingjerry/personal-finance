"""京东 login / risk-page checks."""

from __future__ import annotations

from ..kit.blocking import _contains_any
from ..kit.errors import ApiError, AuthRequired, RiskBlocked


JD_LOGIN_MARKERS = (
    "passport.jd.com/new/login",
    "京东登录",
    "账户登录",
    "扫码登录",
    "请登录后",
    "请先登录",
)


JD_RISK_MARKERS = (
    "x5sec",
    "punish",
    "jdjrv",
    "验证码",
    "安全验证",
    "滑动验证",
    "请完成验证",
    "risk-control",
    "riskcontrol",
)


def check_jd_html(text: str, url: str = "", status: int = 200) -> None:
    lowered_url = url.lower()
    if any(marker in lowered_url for marker in ("x5sec", "punish", "captcha")):
        raise RiskBlocked(
            "京东请求被重定向到验证/风控地址",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    if "passport.jd.com" in lowered_url:
        raise AuthRequired(
            "京东跳转到了登录页",
            "请在浏览器中打开 https://www.jd.com/ 并扫码登录后重试",
        )
    if _contains_any(text, JD_RISK_MARKERS):
        raise RiskBlocked(
            "京东返回了验证/风控页面",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    marker = _contains_any(text, JD_LOGIN_MARKERS)
    if marker and "订单列表" not in text:
        raise AuthRequired(
            "京东返回了登录页",
            "请在浏览器中打开 https://www.jd.com/ 并扫码登录后重试",
        )
    if status in (401, 403):
        raise RiskBlocked(
            "京东拒绝了请求（HTTP %d）" % status,
            "可能是登录状态或风控问题；请在浏览器中确认后重试",
        )


def check_jd_api(payload, status: int = 200) -> None:
    if not isinstance(payload, dict):
        raise ApiError("京东接口返回了非 JSON 对象")
    code = payload.get("code")
    message = str(payload.get("message") or payload.get("msg") or "")
    lowered = message.lower()
    if code in (0, "0", None) and not any(
        token in lowered for token in ("error", "fail", "登录", "验证")
    ):
        return
    if code in (3, "3") or any(
        token in lowered
        for token in ("not login", "no login", "未登录", "请登录", "session")
    ):
        raise AuthRequired(
            "京东登录状态已失效",
            "请在浏览器中打开 https://www.jd.com/ 并扫码登录后重试",
        )
    if any(
        token in lowered
        for token in (
            "验证", "风控", "risk", "captcha", "x5sec", "punish", "频繁",
            "h5st", "签名", "非法",
        )
    ):
        raise RiskBlocked(
            "京东接口返回了验证/风控提示",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    if status in (401, 403):
        raise RiskBlocked(
            "京东接口拒绝了请求（HTTP %d）" % status,
            "可能是登录状态或风控问题；请在浏览器中确认后重试",
        )
    raise ApiError(f"京东接口返回失败 (code={code!r}): {message[:200]}")