import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from connectors.dianping import CONNECTOR, fetch as fetch_dianping
from connectors.dianping.blocking import (
    check_dianping_html,
    check_dianping_payload,
)
from connectors.kit.cookies import Cookie, CookieJar
from connectors.dianping.reader import (
    DIANPING_DAOCAN_PAGE,
    DIANPING_MTGSIG_HEADER,
    DianpingDaocanFetcher,
    DianpingFetcher,
    _NoRedirectHandler,
    build_api_url,
    build_daocan_url,
    check_dianping_login,
    extract_daocan_orders,
    extract_orders,
    has_next_daocan_page,
    has_next_page,
    summarize,
)
from connectors.kit.errors import (
    ApiError,
    AuthRequired,
    NeedsRiskSignature,
)
from connectors.kit.http import Response

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def json_response(payload, *, status=200, url=None, headers=None):
    return Response(
        status=status,
        url=url or "https://t.dianping.com/ajax/json/order/search?page=1",
        headers=headers or {"content-type": "application/json;charset=UTF-8"},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def html_response(
    text, *, status=200, url=DIANPING_DAOCAN_PAGE, headers=None
):
    return Response(
        status=status,
        url=url,
        headers=headers or {"content-type": "text/html;charset=utf-8"},
        body=text.encode("utf-8"),
    )


class FakeClient:
    user_agent = "pfin-test-agent"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, cookie_header="", headers=None):
        self.calls.append(
            {
                "method": "get",
                "url": url,
                "cookie_header": cookie_header,
                "headers": dict(headers or {}),
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)

    def post_form(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("Dianping fetch must not use POST")


class DianpingRequestTests(unittest.TestCase):
    def test_api_url(self):
        self.assertEqual(
            build_api_url(3, 50),
            "https://t.dianping.com/ajax/json/order/search?page=3&pageSize=50",
        )

    def test_no_redirect_handler_does_not_follow(self):
        handler = _NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                None, None, 302, "Found", {}, "https://verify.meituan.com/x"
            )
        )

    def test_login_cookie_check(self):
        check_dianping_login(CookieJar([Cookie(".dianping.com", "dper", "x")]))
        with self.assertRaises(AuthRequired):
            check_dianping_login(CookieJar([Cookie(".dianping.com", "cy", "9")]))


class DianpingBlockingTests(unittest.TestCase):
    def test_normal_json_is_ok(self):
        check_dianping_payload(load_json("dianping_orders_page1.json"))

    def test_login_json(self):
        with self.assertRaises(AuthRequired):
            check_dianping_payload(load_json("dianping_login.json"))

    def test_mtgsig_json_needs_risk_signature(self):
        with self.assertRaises(NeedsRiskSignature) as caught:
            check_dianping_payload(load_json("dianping_risk.json"))
        self.assertIn("needs risk signature", str(caught.exception))
        self.assertIn(DIANPING_MTGSIG_HEADER, str(caught.exception))

    def test_verify_406_json_needs_risk_signature(self):
        with self.assertRaises(NeedsRiskSignature) as caught:
            check_dianping_payload(load_json("dianping_verify_406.json"))
        self.assertIn("needs risk signature", str(caught.exception))

    def test_captcha_json_needs_risk_signature(self):
        with self.assertRaises(NeedsRiskSignature):
            check_dianping_payload({"code": 403, "message": "请完成安全验证"})

    def test_signature_message_with_200_needs_risk_signature(self):
        with self.assertRaises(NeedsRiskSignature):
            check_dianping_payload({"code": 200, "message": "mtgsig required"})

    def test_generic_api_error(self):
        with self.assertRaises(ApiError):
            check_dianping_payload({"code": 500, "message": "服务器开小差了"})

    def test_login_html(self):
        with self.assertRaises(AuthRequired):
            check_dianping_html(
                "请登陆或注册",
                "https://account.dianping.com/login?redir=t.dianping.com",
            )

    def test_risk_html(self):
        with self.assertRaises(NeedsRiskSignature):
            check_dianping_html("安全验证")

    def test_verify_redirect_url(self):
        with self.assertRaises(NeedsRiskSignature) as caught:
            check_dianping_html(
                "",
                "https://verify.meituan.com/v2/app/general_page?action=spiderindefence",
            )
        self.assertIn("needs risk signature", str(caught.exception))

    def test_normal_html(self):
        check_dianping_html("<html><title>我的订单</title></html>")

    def test_http_406_is_risk(self):
        with self.assertRaises(NeedsRiskSignature) as caught:
            check_dianping_html("<html>请稍后再试</html>", status=406)
        self.assertIn("HTTP 406", str(caught.exception))


class DianpingParserTests(unittest.TestCase):
    def test_extract_orders(self):
        orders = extract_orders(load_json("dianping_orders_page1.json"))
        self.assertEqual([order["orderId"] for order in orders], ["DP-TEST-1001", "DP-TEST-1002"])

    def test_extract_orders_accepts_direct_list(self):
        orders = extract_orders([{"orderId": "1"}, "ignored", {"orderId": "2"}])
        self.assertEqual([order["orderId"] for order in orders], ["1", "2"])

    def test_extract_orders_accepts_data_list(self):
        orders = extract_orders({"code": 200, "data": [{"order_id": "a"}]})
        self.assertEqual(orders[0]["order_id"], "a")

    def test_extract_orders_accepts_nested_data_list(self):
        orders = extract_orders(
            {"code": 200, "data": {"data": [{"order_id": "nested"}]}}
        )
        self.assertEqual(orders[0]["order_id"], "nested")

    def test_has_next_honours_flags(self):
        self.assertTrue(has_next_page({"hasNext": True}, 1, 20, 2))
        self.assertFalse(has_next_page({"hasNext": False}, 1, 20, 2))
        self.assertFalse(has_next_page({"data": {"hasMore": False}}, 1, 20, 2))
        self.assertFalse(has_next_page({"data": {"totalPage": 1}}, 1, 20, 2))
        self.assertTrue(has_next_page({"data": {"totalPage": 2}}, 1, 20, 2))

    def test_has_next_stops_on_empty(self):
        self.assertFalse(has_next_page({}, 1, 20, 0))

    def test_summary(self):
        payload = {
            "kind": "dianping_orders",
            "pages": [
                {
                    "source": "groupbuy",
                    "page": 1,
                    "body": load_json("dianping_orders_page1.json"),
                },
                {
                    "source": "groupbuy",
                    "page": 2,
                    "body": load_json("dianping_orders_page2.json"),
                },
            ],
        }
        summary = summarize(payload)
        self.assertEqual(summary["pages"], 2)
        self.assertEqual(summary["orders"], 3)
        self.assertEqual(
            summary["date_range"],
            ("2025-10-03 18:00:00", "2025-12-01 12:30:00"),
        )


class DianpingFetcherTests(unittest.TestCase):
    def setUp(self):
        self.jar = CookieJar([Cookie(".dianping.com", "dper", "secret-value")])

    def test_two_pages_and_raw_body(self):
        page1 = load_json("dianping_orders_page1.json")
        page2 = load_json("dianping_orders_page2.json")
        client = FakeClient([json_response(page1), json_response(page2)])
        fetcher = DianpingFetcher(client, self.jar, max_pages=5, page_size=2)
        payload = fetcher.fetch()

        self.assertEqual(payload["kind"], "dianping_orders")
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(payload["pages"][0]["body"], page1)
        self.assertEqual(payload["pages"][1]["body"], page2)
        self.assertEqual(payload["pages"][0]["source"], "groupbuy")
        self.assertEqual(summarize(payload)["orders"], 3)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call["method"] == "get" for call in client.calls))
        self.assertIn("page=1", client.calls[0]["url"])
        self.assertIn("pageSize=2", client.calls[0]["url"])
        self.assertIn("dper=secret-value", client.calls[0]["cookie_header"])
        self.assertEqual(
            client.calls[0]["headers"]["Referer"],
            "https://t.dianping.com/account/coupons",
        )
        self.assertNotIn(DIANPING_MTGSIG_HEADER, client.calls[0]["headers"])

    def test_empty_page_stops(self):
        client = FakeClient(
            [json_response(load_json("dianping_orders_empty.json"))]
        )
        payload = DianpingFetcher(client, self.jar).fetch()
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(summarize(payload)["orders"], 0)
        self.assertEqual(len(client.calls), 1)

    def test_repeated_page_stops(self):
        page1 = load_json("dianping_orders_page1.json")
        client = FakeClient([json_response(page1), json_response(page1)])
        fetcher = DianpingFetcher(client, self.jar, max_pages=5, page_size=2)
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(summarize(payload)["orders"], 2)
        self.assertTrue(any("完全相同" in warning for warning in fetcher.warnings))

    def test_login_response_stops(self):
        client = FakeClient([json_response(load_json("dianping_login.json"))])
        with self.assertRaises(AuthRequired):
            DianpingFetcher(client, self.jar).fetch()

    def test_signature_response_stops_with_clear_error(self):
        client = FakeClient([json_response(load_json("dianping_risk.json"))])
        with self.assertRaises(NeedsRiskSignature) as caught:
            DianpingFetcher(client, self.jar).fetch()
        self.assertIn("needs risk signature", str(caught.exception))

    def test_verify_406_response_stops_with_clear_error(self):
        client = FakeClient(
            [json_response(load_json("dianping_verify_406.json"))]
        )
        with self.assertRaises(NeedsRiskSignature) as caught:
            DianpingFetcher(client, self.jar).fetch()
        self.assertIn("needs risk signature", str(caught.exception))

    def test_verify_redirect_stops_with_clear_error(self):
        client = FakeClient(
            [
                Response(
                    status=302,
                    url="https://t.dianping.com/ajax/json/order/search",
                    headers={
                        "location": (
                            "https://verify.meituan.com/v2/app/general_page"
                            "?action=spiderindefence&theme=dianping"
                        )
                    },
                    body=b"",
                )
            ]
        )
        with self.assertRaises(NeedsRiskSignature) as caught:
            DianpingFetcher(client, self.jar).fetch()
        self.assertIn("needs risk signature", str(caught.exception))

    def test_login_redirect_stops(self):
        client = FakeClient(
            [
                Response(
                    status=302,
                    url="https://t.dianping.com/ajax/json/order/search",
                    headers={
                        "location": (
                            "https://account.dianping.com/login"
                            "?redir=t.dianping.com/account/coupons"
                        )
                    },
                    body=b"",
                )
            ]
        )
        with self.assertRaises(AuthRequired):
            DianpingFetcher(client, self.jar).fetch()

    def test_non_json_html_is_reported(self):
        client = FakeClient([html_response("请登陆或注册")])
        with self.assertRaises(AuthRequired):
            DianpingFetcher(client, self.jar).fetch()

    def test_max_pages_warning(self):
        page1 = load_json("dianping_orders_page1.json")
        page2 = load_json("dianping_orders_page2.json")
        page2["data"]["hasNext"] = True
        client = FakeClient([json_response(page1), json_response(page2)])
        fetcher = DianpingFetcher(client, self.jar, max_pages=2, page_size=2)
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 2)
        self.assertTrue(any("最大页数" in warning for warning in fetcher.warnings))


class DianpingDaocanParserTests(unittest.TestCase):
    def test_page_urls(self):
        self.assertEqual(
            build_daocan_url(1), "https://t.dianping.com/account/daocan_orders"
        )
        self.assertEqual(
            build_daocan_url(2),
            "https://t.dianping.com/account/daocan_orders?pageno=2",
        )
        with self.assertRaises(ValueError):
            build_daocan_url(0)

    def test_extract_server_rendered_rows(self):
        orders = extract_daocan_orders(load_text("dianping_daocan_page1.html"))
        self.assertEqual(len(orders), 2)
        first = orders[0]
        self.assertEqual(first["order_id"], "0000000000000000001")
        self.assertEqual(first["title"], "示例商品：100元代金券1张，可叠加")
        self.assertEqual(first["occurred"], "2022-01-04")
        self.assertEqual(first["expire_at"], "2022-02-03")
        self.assertEqual(first["quantity"], "6")
        self.assertEqual(first["amount"], "¥200")
        self.assertEqual(first["status"], "支付成功")
        self.assertEqual(first["coupon_codes"], ["000000000001", "000000000002"])

    def test_next_page_detection(self):
        self.assertTrue(has_next_daocan_page(load_text("dianping_daocan_page1.html")))
        self.assertFalse(has_next_daocan_page(load_text("dianping_daocan_page2.html")))
        self.assertFalse(has_next_daocan_page("<html></html>"))

    def test_empty_page_has_no_orders(self):
        self.assertEqual(extract_daocan_orders("<html><body>暂无订单</body></html>"), [])


class DianpingDaocanFetcherTests(unittest.TestCase):
    def setUp(self):
        self.jar = CookieJar([Cookie(".dianping.com", "dper", "secret-value")])

    def test_fetches_two_html_pages_and_keeps_raw_html(self):
        page1 = load_text("dianping_daocan_page1.html")
        page2 = load_text("dianping_daocan_page2.html")
        client = FakeClient(
            [html_response(page1), html_response(page2, url=build_daocan_url(2))]
        )
        fetcher = DianpingDaocanFetcher(client, self.jar, max_pages=5)
        payload = fetcher.fetch()

        self.assertEqual(payload["kind"], "dianping_orders")
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(payload["pages"][0]["source"], "daocan")
        self.assertEqual(payload["pages"][0]["html"], page1)
        self.assertEqual(payload["pages"][1]["url"], build_daocan_url(2))
        self.assertEqual(summarize(payload)["orders"], 3)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call["method"] == "get" for call in client.calls))
        self.assertEqual(client.calls[0]["url"], DIANPING_DAOCAN_PAGE)
        self.assertIn("dper=secret-value", client.calls[0]["cookie_header"])
        self.assertEqual(
            client.calls[0]["headers"]["Referer"], DIANPING_DAOCAN_PAGE
        )
        self.assertNotIn(DIANPING_MTGSIG_HEADER, client.calls[0]["headers"])
        self.assertIn("text/html", client.calls[0]["headers"]["Accept"])

    def test_stops_when_no_next_link(self):
        page2 = load_text("dianping_daocan_page2.html")
        client = FakeClient([html_response(page2)])
        payload = DianpingDaocanFetcher(client, self.jar).fetch()
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(summarize(payload)["orders"], 1)

    def test_empty_page_stops(self):
        client = FakeClient([html_response("<html><body>暂无订单</body></html>")])
        fetcher = DianpingDaocanFetcher(client, self.jar)
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(summarize(payload)["orders"], 0)
        self.assertEqual(len(client.calls), 1)

    def test_repeated_page_stops(self):
        page1 = load_text("dianping_daocan_page1.html")
        client = FakeClient([html_response(page1), html_response(page1)])
        fetcher = DianpingDaocanFetcher(client, self.jar, max_pages=5)
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(summarize(payload)["orders"], 2)
        self.assertTrue(any("完全相同" in warning for warning in fetcher.warnings))

    def test_login_redirect_stops(self):
        client = FakeClient(
            [
                Response(
                    status=302,
                    url=DIANPING_DAOCAN_PAGE,
                    headers={
                        "location": "https://account.dianping.com/login?redir=x",
                        "content-type": "text/html",
                    },
                    body=b"",
                )
            ]
        )
        with self.assertRaises(AuthRequired):
            DianpingDaocanFetcher(client, self.jar).fetch()

    def test_risk_page_stops_with_clear_error(self):
        client = FakeClient([html_response("<html>请完成安全验证</html>")])
        with self.assertRaises(NeedsRiskSignature) as caught:
            DianpingDaocanFetcher(client, self.jar).fetch()
        self.assertIn("needs risk signature", str(caught.exception))

    def test_verify_redirect_stops(self):
        client = FakeClient(
            [
                Response(
                    status=302,
                    url=DIANPING_DAOCAN_PAGE,
                    headers={
                        "location": "https://verify.meituan.com/v2/app/general_page",
                        "content-type": "text/html",
                    },
                    body=b"",
                )
            ]
        )
        with self.assertRaises(NeedsRiskSignature):
            DianpingDaocanFetcher(client, self.jar).fetch()


class DianpingFetchWiringTests(unittest.TestCase):
    def test_fetch_uses_ten_second_pacer_and_no_redirect_opener(self):
        captured = {}
        jar = CookieJar([Cookie(".dianping.com", "dper", "v")])

        class FakeHttpClient:
            def __init__(self, *, pacer=None, timeout=30, opener=None, cookie_jar=None):
                captured["pacer"] = pacer
                captured["opener"] = opener
                captured["timeout"] = timeout

        class FakeFetcher:
            warnings = []

            def __init__(self, client, jar, *, max_pages=10):
                captured["max_pages"] = max_pages

            def fetch(self):
                return {"kind": "dianping_orders", "pages": []}

        args = SimpleNamespace(page_size=20, max_pages=100, delay=1.2, timeout=30)
        with mock.patch("connectors.dianping.HttpClient", FakeHttpClient), mock.patch(
            "connectors.dianping.DianpingDaocanFetcher", FakeFetcher
        ):
            payload, warnings = fetch_dianping(args, jar)
        self.assertEqual(payload["kind"], "dianping_orders")
        self.assertEqual(captured["pacer"].min_interval, 10.0)
        self.assertIsNotNone(captured["opener"])
        self.assertEqual(captured["max_pages"], 100)

    def test_fetch_checks_login_before_any_request(self):
        args = SimpleNamespace(page_size=20, max_pages=100, delay=1.2, timeout=30)
        with mock.patch("connectors.dianping.HttpClient", side_effect=AssertionError("request")):
            with self.assertRaises(AuthRequired):
                fetch_dianping(args, CookieJar())

    def test_default_max_pages_is_high_enough_for_full_pull(self):
        self.assertEqual(CONNECTOR.max_pages, 100)


if __name__ == "__main__":
    unittest.main()
