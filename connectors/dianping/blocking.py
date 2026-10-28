"""大众点评 login / risk-page checks."""

from __future__ import annotations

from ..kit.blocking import _contains_any_lower
from ..kit.errors import ApiError, AuthRequired, NeedsRiskSignature


DIANPING_LOGIN_MARKERS = (
    "account.dianping.com/login",
    "maccount.dianping.com/login",
    "mlogin.dianping.com",
    "www.dianping.com/login",
    "请登录",
    "请登陆",
    "请登陆或注册",
    "登录后",
    "扫码登录",
)


DIANPING_RISK_MARKERS = (
    "verify.meituan.com",
    "spiderindefence",
    "x5sec",
    "punish",
    "验证码",
    "安全验证",
    "滑动验证",
    "请完成验证",
    "mtgsig",
    "风控",
    "captcha",
    "riskcontrol",
    "risk-control",
    "x-forbid-code",
    "x-forbid-reason",
)


DIANPING_SIGNATURE_MARKERS = (
    "mtgsig",
    "verify.meituan.com",
    "spiderindefence",
    "needs risk signature",
    "签名",
    "风控",
    "验证码",
    "安全验证",
    "滑动验证",
    "请完成验证",
    "captcha",
    "x-forbid-code",
    "x-forbid-reason",
)


def _dianping_risk_error(detail: str = "") -> NeedsRiskSignature:
    message = "needs risk signature: 大众点评订单接口要求 H5guard 生成的 mtgsig"
    if detail:
        message += f"（{detail}）"
    return NeedsRiskSignature(
        message,
        "本工具不会伪造或绕过 mtgsig；请在已登录的大众点评页面中人工查看/导出，"
        "或等待后续支持在浏览器中驱动页面",
    )


def _dianping_auth_error(detail: str = "大众点评返回了登录页") -> AuthRequired:
    return AuthRequired(
        detail,
        "请在浏览器中重新登录大众点评后重试",
    )


def check_dianping_html(text: str, url: str = "", status: int = 200) -> None:
    """Stop on Dianping login/risk pages without trying to solve anything."""

    lowered_url = (url or "").lower()
    if _contains_any_lower(lowered_url, ("verify.meituan.com", "spiderindefence")):
        raise _dianping_risk_error("请求被重定向到 verify.meituan.com")
    if _contains_any_lower(
        lowered_url,
        (
            "account.dianping.com/login",
            "maccount.dianping.com/login",
            "mlogin.dianping.com",
            "www.dianping.com/login",
        ),
    ):
        raise _dianping_auth_error("大众点评跳转到了登录页")

    if _contains_any_lower(text, DIANPING_RISK_MARKERS):
        raise _dianping_risk_error("页面含验证/风控标记")
    if _contains_any_lower(text, DIANPING_LOGIN_MARKERS):
        raise _dianping_auth_error("大众点评返回了登录页")

    if status in (401, 403, 406, 412):
        # A bare 401/403/406/412 without a login body is treated as risk
        # control: the owner should first check the login state in a normal
        # browser.  Dianping uses 406 for its verify.meituan.com envelope.
        raise _dianping_risk_error(f"HTTP {status}")


def check_dianping_payload(payload, status: int = 200) -> None:
    """Validate a read-only Dianping JSON response.

    The public no-cookie probe returns HTTP 200 with
    ``{"code":403,"msg":{"message":"请登陆或注册"}}``; that is an auth
    failure, not the browser risk page.
    """

    if isinstance(payload, list):
        return
    if not isinstance(payload, dict):
        raise ApiError("大众点评接口返回了非 JSON 对象")

    code = payload.get("code", payload.get("status"))
    message = payload.get("message") or payload.get("msg") or payload.get("errorMsg") or ""
    if isinstance(message, dict):
        message = message.get("message") or message.get("msg") or ""
    message_text = str(message)

    # A real unsigned probe of t.dianping.com/deal/1 returned HTTP 200 with
    # this envelope: {"code":406,"msg":"您的网络好像不太给力...",
    # "customData":{"verifyPageUrl":"https://verify.meituan.com/...",
    # "riskLevel":"199", ...}}.  That is the risk-control path, not a plain
    # API error, and it is exactly where mtgsig is required.
    custom = payload.get("customData") or payload.get("custom_data")
    top_level_verify = any(
        key in payload for key in ("verifyPageUrl", "verifyUrl", "riskLevel")
    )
    if isinstance(custom, dict):
        custom_risk = any(
            key in custom
            for key in ("verifyPageUrl", "verifyUrl", "riskLevel", "imageUrl")
        )
        custom_values = " ".join(
            str(value)
            for value in custom.values()
            if isinstance(value, (str, int, float))
        )
        custom_risk = custom_risk or _contains_any_lower(
            custom_values, ("verify.meituan.com", "spiderindefence", "captcha")
        )
        if custom_risk:
            raise _dianping_risk_error("服务端返回了 verify.meituan.com 风控页面")
    if top_level_verify:
        raise _dianping_risk_error("服务端返回了 verify.meituan.com 风控页面")
    if code in (406, 412, "406", "412"):
        raise _dianping_risk_error(f"服务端返回 code={code}")

    if _contains_any_lower(message_text, DIANPING_SIGNATURE_MARKERS):
        raise _dianping_risk_error(message_text[:120])

    if code in (0, 200, "0", "200", None) and not _contains_any_lower(
        message_text, ("error", "fail", "失败", "登录", "登陆", "验证", "风控")
    ):
        return

    if code in (401, 403, "401", "403") or _contains_any_lower(
        message_text, ("请登录", "请登陆", "not login", "no login", "未登录", "session")
    ):
        raise _dianping_auth_error(
            "大众点评登录状态已失效"
            if message_text
            else "大众点评接口返回了未登录状态"
        )

    if status in (401, 403):
        raise _dianping_risk_error(f"HTTP {status}")

    raise ApiError(f"大众点评接口返回失败 (code={code!r}): {message_text[:200]}")