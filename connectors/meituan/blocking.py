"""美团月付 login / risk-page checks."""

from __future__ import annotations

from ..kit.blocking import _contains_any
from ..kit.errors import ApiError, AuthRequired, NeedsRiskSignature, RiskBlocked


# --- Meituan Yuefu (npay.meituan.com pay-bill) ---------------------------
#
# Static analysis of the public pay-bill bundle shows a plain POST to
# /trade/bill/getBillList with no mtgsig in the public request.  These
# markers therefore only stop on an explicit server refusal; they never
# assume that an ordinary bill page is a captcha page.
MEITUAN_LOGIN_CODES = ("50103",)


MEITUAN_LOGIN_MARKERS = (
    "未登录",
    "请登录",
    "登录后",
    "not login",
    "no login",
    "session expired",
    "passport.meituan.com",
)


MEITUAN_SIGNATURE_MARKERS = (
    "mtgsig",
    "签名",
    "signature",
    "sign error",
    "sign_error",
    "h5st",
)


MEITUAN_RISK_MARKERS = (
    "验证码",
    "安全验证",
    "请完成验证",
    "身份核实",
    "risk",
    "riskcontrol",
    "captcha",
    "x5sec",
    "punish",
    "频繁",
    "拒绝",
)


def check_meituan_api(payload, status: int = 200) -> None:
    """Stop clearly on a Meituan bill API failure.

    ``50103`` is the public page's own "not logged in" code.  A message that
    explicitly mentions mtgsig/signature gets a distinct NeedsRiskSignature so
    the report can explain that the SDK, not this tool, must produce it.
    """

    if not isinstance(payload, dict):
        raise ApiError("美团月付接口返回了非 JSON 对象")
    if status == 401:
        raise AuthRequired(
            "美团月付登录状态已失效（HTTP 401）",
            "请在浏览器中打开 https://npay.meituan.com/pay-bill/index.html?payType=4 并登录后重试",
        )
    if status == 403:
        raise RiskBlocked(
            "美团月付拒绝了请求（HTTP 403）",
            "可能是登录状态或风控问题；请在浏览器中确认后重试",
        )

    code = ""
    message = ""
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or error.get("msg") or "")
    if not code:
        code = str(payload.get("code") or "")
    if not message:
        message = str(payload.get("message") or payload.get("msg") or "")
    status_text = str(payload.get("status") or "").lower()
    lowered = f"{code} {message}".lower()

    if status_text == "success":
        return
    if status_text != "fail" and code in ("0", "200"):
        return
    if code in MEITUAN_LOGIN_CODES or any(
        marker in lowered for marker in MEITUAN_LOGIN_MARKERS
    ):
        raise AuthRequired(
            "美团月付登录状态已失效",
            "请在浏览器中打开 https://npay.meituan.com/pay-bill/index.html?payType=4 "
            "并登录后重试；本工具不会尝试绕过登录",
        )
    if any(marker in lowered for marker in MEITUAN_SIGNATURE_MARKERS):
        raise NeedsRiskSignature(
            "美团月付接口要求风控签名（mtgsig），本工具不会伪造或绕过",
            "mtgsig 由美团页面/客户端内的反爬 SDK 生成，不在公开 bundle 中；"
            "可在浏览器中打开月付账单页人工查看，或改用浏览器页面驱动",
        )
    if any(marker in lowered for marker in MEITUAN_RISK_MARKERS):
        raise RiskBlocked(
            "美团月付接口返回了验证/风控提示",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    raise ApiError(f"美团月付接口返回失败 (code={code!r}): {message[:200]}")


def check_meituan_html(text: str, url: str = "", status: int = 200) -> None:
    """Recognise a login or verification page that is not the bill JSON."""

    lowered_url = url.lower()
    if any(marker in lowered_url for marker in ("x5sec", "punish", "captcha")):
        raise RiskBlocked(
            "美团月付请求被重定向到验证/风控地址",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    if "passport.meituan.com" in lowered_url or "login" in lowered_url:
        raise AuthRequired(
            "美团月付跳转到了登录页",
            "请在浏览器中打开 https://npay.meituan.com/pay-bill/index.html?payType=4 并登录后重试",
        )
    if status == 401:
        raise AuthRequired(
            "美团月付登录状态已失效（HTTP 401）",
            "请在浏览器中打开月付账单页并登录后重试",
        )
    if status == 403:
        raise RiskBlocked(
            "美团月付拒绝了请求（HTTP 403）",
            "可能是登录状态或风控问题；请在浏览器中确认后重试",
        )
    if _contains_any(text, MEITUAN_RISK_MARKERS):
        raise RiskBlocked(
            "美团月付返回了验证/风控页面",
            "请先在浏览器中人工完成验证后再重试；本工具不会尝试破解或绕过",
        )
    if _contains_any(text, MEITUAN_LOGIN_MARKERS):
        raise AuthRequired(
            "美团月付返回了登录页",
            "请在浏览器中打开 https://npay.meituan.com/pay-bill/index.html?payType=4 并登录后重试",
        )
