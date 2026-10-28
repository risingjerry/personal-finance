"""12306 login / risk-page checks."""

from __future__ import annotations

from ..kit.blocking import _contains_any, _safe_preview
from ..kit.errors import ApiError, AuthRequired, NeedsRiskSignature


# 12306 login and risk markers.  The order page itself contains a hidden
# "登录" header link, so generic "登录" is deliberately not a marker here.
RAIL_LOGIN_MARKERS = (
    "passport.12306.cn",
    "/otn/passport",
    "请登录",
    "用户登录",
    "扫码登录",
    "登录页",
    "登录已过期",
    "重新登录",
    "登录超时",
    'id="J-userName"',
    "J-password",
)


RAIL_RISK_MARKERS = (
    "验证码",
    "安全验证",
    "滑动验证",
    "请完成验证",
    "身份验证",
    "风控",
    "risk-control",
    "riskcontrol",
    "x5sec",
    "punish",
    "h5st",
    "captcha",
    "slide-passcode",
    "checkLoginVerify",
    "签名",
    "signature",
)


RAIL_EMPTY_MARKERS = (
    "没有查询到",
    "未查询到",
    "没有符合",
    "无符合",
    "暂无订单",
    "没有订单",
)


def rail_message_text(payload) -> str:
    """Collect the server's own message fields without dumping the payload."""

    if not isinstance(payload, dict):
        return ""
    parts = []
    for key in ("message", "msg", "errMsg", "errmsg"):
        value = payload.get(key)
        if value:
            parts.append(str(value))
    messages = payload.get("messages")
    if isinstance(messages, list):
        parts.extend(str(item) for item in messages if item)
    elif messages:
        parts.append(str(messages))
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("errMsg", "errmsg", "message", "msg"):
            value = data.get(key)
            if value:
                parts.append(str(value))
    return " ".join(parts)


def is_rail_empty_result(payload) -> bool:
    text = rail_message_text(payload)
    return any(marker in text for marker in RAIL_EMPTY_MARKERS)


def _rail_risk_error() -> NeedsRiskSignature:
    return NeedsRiskSignature(
        "12306 要求风险签名/验证（needs risk signature）",
        "本工具不会伪造或绕过签名/验证；请在浏览器中打开 "
        "https://kyfw.12306.cn/otn/view/train_order.html 人工完成后再重试。"
        "真实浏览器的登录页会执行站点自身的前端脚本，通常可以继续操作。",
    )


def check_rail12306_html(text: str, url: str = "", status: int = 200) -> None:
    lowered_url = url.lower()
    if "passport" in lowered_url or "login.html" in lowered_url:
        raise AuthRequired(
            "12306 跳转到了登录页",
            "请在浏览器中打开 https://kyfw.12306.cn/otn/resources/login.html "
            "扫码登录后重试；本工具不会尝试绕过登录",
        )
    if "error.html" in lowered_url:
        preview = _safe_preview(text)
        detail = f"；响应摘要: {preview}" if preview else ""
        raise ApiError(
            "12306 拒绝了请求（跳转到错误页，可能是查询过于频繁、"
            f"日期越界或登录态异常）{detail}",
            "请先在浏览器中打开订单页确认；本工具不会重试或绕过风控，"
            "也不会发送越界日期查询",
        )
    if _contains_any(text, RAIL_RISK_MARKERS):
        raise _rail_risk_error()
    marker = _contains_any(text, RAIL_LOGIN_MARKERS)
    if marker:
        raise AuthRequired(
            "12306 返回了登录页",
            "请在浏览器中打开 https://kyfw.12306.cn/otn/resources/login.html "
            "扫码登录后重试",
        )
    if status in (401, 403):
        raise AuthRequired(
            "12306 拒绝了请求（HTTP %d）" % status,
            "请确认浏览器中仍处于登录状态",
        )
    if status >= 500:
        raise ApiError(f"12306 服务端错误（HTTP {status}）")


def check_rail12306_json(payload, status: int = 200) -> None:
    if not isinstance(payload, dict):
        raise ApiError("12306 接口返回了非 JSON 对象")
    if status in (401, 403):
        raise AuthRequired(
            "12306 拒绝了请求（HTTP %d）" % status,
            "请确认浏览器中仍处于登录状态",
        )

    text = rail_message_text(payload)
    lowered = text.lower()
    if any(
        marker.lower() in lowered
        for marker in (
            "未登录",
            "请先登录",
            "登录已失效",
            "登录已过期",
            "重新登录",
            "登录超时",
            "session expired",
        )
    ):
        raise AuthRequired(
            "12306 登录状态已失效",
            "请在浏览器中打开 https://kyfw.12306.cn/otn/resources/login.html "
            "扫码登录后重试",
        )
    if any(marker.lower() in lowered for marker in RAIL_RISK_MARKERS):
        raise _rail_risk_error()

    status_value = payload.get("status")
    failed = status_value is False or str(status_value).lower() == "false"
    if failed and not is_rail_empty_result(payload):
        detail = text[:200] or "无错误信息"
        raise ApiError(f"12306 接口返回失败: {detail}")
