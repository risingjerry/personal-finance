"""携程 login / risk-page checks."""

from __future__ import annotations

from ..kit.blocking import _contains_any
from ..kit.errors import ApiError, AuthRequired, NeedsRiskSignature, RiskBlocked


CTRIP_LOGIN_MARKERS = (
    "passport.ctrip.com",
    "login.ctrip.com",
    "请先登录",
    "请登录",
    "未登录",
    "登录已失效",
    "登录超时",
    "login required",
)


CTRIP_RISK_MARKERS = (
    "x5sec",
    "punish",
    "验证码",
    "安全验证",
    "滑动验证",
    "请完成验证",
    "captcha",
    "riskcontrol",
    "访问过于频繁",
    "请求过于频繁",
)


CTRIP_SIGNATURE_MARKERS = (
    "w-payload-source",
    "c_sign",
    "csign",
    "c-sign",
    "signature",
    "签名",
    "验签",
    "非法请求",
    "请求头",
    "anti-bot",
    "antibot",
)


def check_ctrip_html(text: str, url: str = "", status: int = 200) -> None:
    lowered_url = url.lower()
    if status in (430, 431, 432):
        raise NeedsRiskSignature(
            "携程要求风控签名 (needs risk signature)，HTTP %d" % status,
            "订单页由 c-sign.js 生成 w-payload-source 请求头；本工具不会伪造或绕过。"
            "请在浏览器中打开 https://my.ctrip.com/myinfo/all 手动查看/导出订单",
        )
    if "passport.ctrip.com" in lowered_url or "login.ctrip.com" in lowered_url:
        raise AuthRequired(
            "携程跳转到了登录页",
            "请在浏览器中打开 https://my.ctrip.com/myinfo/all 并登录后重试",
        )
    if status == 401:
        raise AuthRequired(
            "携程拒绝了请求（HTTP 401，未登录）",
            "请在浏览器中打开 https://my.ctrip.com/myinfo/all 并登录后重试",
        )
    if status in (403, 429):
        raise RiskBlocked(
            "携程拒绝了请求（HTTP %d）" % status,
            "可能是登录状态或风控问题；请在浏览器中确认后重试",
        )
    if _contains_any(text, CTRIP_RISK_MARKERS):
        raise RiskBlocked(
            "携程返回了验证/风控页面",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    marker = _contains_any(text, CTRIP_LOGIN_MARKERS)
    if marker:
        raise AuthRequired(
            "携程返回了登录页",
            "请在浏览器中打开 https://my.ctrip.com/myinfo/all 并登录后重试",
        )


def _ctrip_error_text(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    result = payload.get("Result") if isinstance(payload.get("Result"), dict) else {}
    response = (
        payload.get("ResponseStatus")
        if isinstance(payload.get("ResponseStatus"), dict)
        else {}
    )
    parts = [
        result.get("ResultMsg"),
        result.get("Message"),
        response.get("Message"),
        response.get("ErrorMsg"),
    ]
    errors = response.get("Errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                parts.append(item.get("Message"))
                parts.append(item.get("ErrorCode"))
    return " ".join(str(part) for part in parts if part).strip()


def _ctrip_error_codes(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    response = (
        payload.get("ResponseStatus")
        if isinstance(payload.get("ResponseStatus"), dict)
        else {}
    )
    codes = []
    errors = response.get("Errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict) and item.get("ErrorCode"):
                codes.append(str(item["ErrorCode"]))
    return " ".join(codes).lower()


def check_ctrip_api(payload, status: int = 200) -> None:
    """Validate a Ctrip SOA response without ever signing or retrying for it."""

    if status in (430, 431, 432):
        raise NeedsRiskSignature(
            "携程要求风控签名 (needs risk signature)，HTTP %d" % status,
            "订单接口由 c-sign.js 生成 w-payload-source 请求头；本工具不会伪造或绕过。"
            "请在浏览器中打开 https://my.ctrip.com/myinfo/all 手动查看/导出订单",
        )
    if status == 401:
        raise AuthRequired(
            "携程登录状态已失效（HTTP 401）",
            "请在浏览器中打开 https://my.ctrip.com/myinfo/all 并登录后重试",
        )
    if not isinstance(payload, dict):
        raise ApiError("携程接口返回了非 JSON 对象")

    result = payload.get("Result") if isinstance(payload.get("Result"), dict) else {}
    response = (
        payload.get("ResponseStatus")
        if isinstance(payload.get("ResponseStatus"), dict)
        else {}
    )
    code = result.get("ResultCode")
    ack = response.get("Ack")
    message = _ctrip_error_text(payload)
    lowered = message.lower()
    codes = _ctrip_error_codes(payload)

    success = (
        200 <= status < 300
        and code in (0, "0", None)
        and ack in (0, "0", None, "Success", "success")
        and not any(
            token in lowered
            for token in ("not login", "no login", "未登录", "请登录", "签名", "验签")
        )
    )
    if success:
        return

    if any(marker in lowered for marker in CTRIP_SIGNATURE_MARKERS) or any(
        marker in codes for marker in ("sign", "csign", "c_sign", "payload", "wclient")
    ):
        raise NeedsRiskSignature(
            "携程要求风控签名 (needs risk signature)",
            "订单页 bundle 会加载 c-sign.js 并计算 w-payload-source；"
            "本工具不会伪造或绕过该签名。请在浏览器中打开 "
            "https://my.ctrip.com/myinfo/all 手动查看/导出订单",
        )

    if any(
        token in lowered
        for token in (
            "not login",
            "no login",
            "login",
            "未登录",
            "请登录",
            "登录已失效",
            "登录超时",
        )
    ) or "mobilerequestfilterexception" in codes:
        raise AuthRequired(
            "携程登录状态已失效",
            "请在浏览器中打开 https://my.ctrip.com/myinfo/all 并登录后重试",
        )

    if status in (401, 403, 429) or any(
        marker in lowered for marker in CTRIP_RISK_MARKERS
    ):
        raise RiskBlocked(
            "携程接口返回了验证/风控提示",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )

    hint = None
    if code in (-2, "-2") or "parameter" in lowered:
        hint = (
            "真实 PC 订单中心要求 body 带 Channel=\"Online\"、"
            "ClientVersion=\"99.99\"、Locale=\"\"，并使用 PageIndex/PageSize；"
            "请检查是否遗漏这些字段"
        )
    raise ApiError(
        "携程接口返回失败 (ResultCode=%r, Ack=%r): %s"
        % (code, ack, message[:200] or "无错误信息"),
        hint,
    )