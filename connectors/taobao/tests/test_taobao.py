import json
import unittest
import urllib.parse

from connectors.taobao.blocking import check_taobao_ret
from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.errors import ApiError, AuthRequired, RiskBlocked
from connectors.kit.http import Response
from connectors.taobao.reader import (
    TAOBAO_APP_KEY,
    TaobaoFetcher,
    build_page_data,
    build_request,
    date_range,
    extract_components,
    is_token_error,
    mtop_sign,
    page_order_ids,
    parse_orders,
    refresh_tokens_from_response,
    summarize,
)

from connectors.kit.testing import FIXTURES, load_fixture


def response(payload, *, status=200, set_cookies=None):
    return Response(
        status=status,
        url="https://h5api.m.taobao.com/h5/test/1.0/",
        headers={},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        set_cookies=list(set_cookies or []),
    )


class FakeClient:
    user_agent = "pfin-test-agent"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_form(self, url, form, *, cookie_header="", headers=None):
        self.calls.append(
            {"url": url, "form": dict(form), "cookie_header": cookie_header,
             "headers": dict(headers or {})}
        )
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)


@unittest.skipUnless((FIXTURES / "taobao_two_pages.json").exists(), "fixture missing")
class TaobaoParserTests(unittest.TestCase):
    def setUp(self):
        self.payload = load_fixture("taobao_two_pages.json")

    def test_fixture_orders(self):
        orders = parse_orders(self.payload["pages"])
        self.assertEqual(len(orders), 20)
        self.assertEqual(len({order["order_id"] for order in orders}), 20)
        dates = [order["occurred"] for order in orders if order["occurred"]]
        self.assertEqual(date_range(orders), (min(dates), max(dates)))
        self.assertTrue(all(order["paid"] for order in orders))
        components = extract_components(self.payload["pages"][0])
        expected_ids = [
            str(value["fields"]["orderId"])
            for key, value in components.items() if key.startswith("MainGroup_")
        ]
        self.assertEqual(page_order_ids(self.payload["pages"][0]), expected_ids)

    def test_summary(self):
        summary = summarize(self.payload)
        self.assertEqual(summary["pages"], 2)
        self.assertEqual(summary["orders"], 20)
        self.assertEqual(summary["date_range"], date_range(parse_orders(self.payload["pages"])))

    def test_accepts_full_mtop_response(self):
        wrapped = [
            {"api": "mtop.test", "ret": ["SUCCESS::ok"], "data": page}
            for page in self.payload["pages"]
        ]
        self.assertEqual(len(parse_orders(wrapped)), 20)
        self.assertEqual(
            extract_components(wrapped[0]),
            extract_components(self.payload["pages"][0]),
        )


class TaobaoRequestTests(unittest.TestCase):
    def test_page_data(self):
        self.assertEqual(
            build_page_data(3, "all"),
            {
                "tabCode": "all",
                "appVersion": "3.0",
                "OrderType": "OrderList",
                "condition": {
                    "inner": 1,
                    "deviceLevel": "high",
                    "version": "1.0.0",
                    "reqFrom": "tbpc",
                },
                "appName": "tborder",
                "templateConfigVersion": 0,
                "forceBoughtlist4": 4,
                "page": 3,
            },
        )

    def test_sign_vector(self):
        data = '{"a":1}'
        self.assertEqual(
            mtop_sign("tok", 1700000000000, TAOBAO_APP_KEY, data),
            "807afcd60b992c88954b79d8d24d911d",
        )

    def test_request_shape(self):
        plan = build_request(
            2,
            "token123",
            timestamp_ms=1700000000000,
            user_agent="ua-test",
        )
        parsed = urllib.parse.urlparse(plan.url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "h5api.m.taobao.com")
        self.assertEqual(query["appKey"], [TAOBAO_APP_KEY])
        self.assertEqual(query["api"], ["mtop.taobao.order.queryboughtlistv2"])
        self.assertEqual(query["v"], ["1.0"])
        self.assertEqual(query["type"], ["originaljson"])
        self.assertEqual(query["ttid"], ["201200@taobao_iphone_99.99.99"])
        self.assertEqual(query["t"], ["1700000000000"])
        self.assertEqual(query["sign"], [mtop_sign("token123", 1700000000000, TAOBAO_APP_KEY, plan.data_json)])
        self.assertEqual(plan.form["data"], plan.data_json)
        self.assertIn('"page":2', plan.data_json)
        self.assertEqual(plan.form["ua"], "ua-test")


class TaobaoTokenMarkerTests(unittest.TestCase):
    def test_all_library_token_markers_are_recognised(self):
        for ret in (
            "FAIL_SYS_TOKEN_EMPTY::令牌为空",
            "FAIL_SYS_TOKEN_EXOIRED::令牌过期",
            "FAIL_SYS_TOKEN_EXPIRED::令牌过期",
            "FAIL_SYS_TOKEN_ILLEGAL::令牌非法",
            "FAIL_SYS_ILLEGAL_ACCESS::非法请求",
        ):
            with self.subTest(ret=ret):
                self.assertTrue(is_token_error({"ret": [ret]}))

    def test_other_errors_are_not_token_errors(self):
        self.assertFalse(is_token_error({"ret": ["FAIL_SYS_SESSION_EXPIRED::登录过期"]}))
        self.assertFalse(is_token_error({"ret": ["SUCCESS::调用成功"]}))


class TaobaoBlockingTests(unittest.TestCase):
    def test_success(self):
        check_taobao_ret({"ret": ["SUCCESS::调用成功"]})

    def test_login_required(self):
        with self.assertRaises(AuthRequired):
            check_taobao_ret({"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]})

    def test_risk_block(self):
        with self.assertRaises(RiskBlocked):
            check_taobao_ret({"ret": ["FAIL_SYS_USER_VALIDATE::需要验证"]})

    def test_generic_failure(self):
        with self.assertRaises(ApiError):
            check_taobao_ret({"ret": ["FAIL_SYS_SERVICE_UNAVAILABLE::busy"]})


def login_jar(token=None):
    cookies = [
        Cookie(".taobao.com", "cookie2", "test-cookie2"),
        Cookie(".taobao.com", "unb", "test-unb"),
    ]
    if token is not None:
        cookies.append(Cookie(".taobao.com", "_m_h5_tk", token))
    return CookieJar(cookies)


class TaobaoFetcherTests(unittest.TestCase):
    def setUp(self):
        self.pages = load_fixture("taobao_two_pages.json")["pages"]
        self.jar = login_jar("token_1")

    def test_sign_uses_first_part_of_token_cookie(self):
        client = FakeClient(
            [response({"ret": ["SUCCESS::ok"], "data": {"data": {}}})]
        )
        TaobaoFetcher(client, self.jar).fetch()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(client.calls[0]["url"]).query)
        timestamp = int(query["t"][0])
        expected = mtop_sign("token", timestamp, TAOBAO_APP_KEY, client.calls[0]["form"]["data"])
        self.assertEqual(query["sign"], [expected])

    def test_stops_on_empty_page(self):
        client = FakeClient(
            [
                response({"ret": ["SUCCESS::ok"], "data": self.pages[0]}),
                response({"ret": ["SUCCESS::ok"], "data": self.pages[1]}),
                response({"ret": ["SUCCESS::ok"], "data": {"data": {}}}),
            ]
        )
        payload = TaobaoFetcher(client, self.jar).fetch()
        self.assertEqual(payload["kind"], "boughtlist_mtop")
        self.assertEqual(len(payload["pages"]), 3)
        self.assertEqual(summarize(payload)["orders"], 20)
        self.assertEqual(len(client.calls), 3)

    def test_stops_on_repeated_page(self):
        client = FakeClient(
            [
                response({"ret": ["SUCCESS::ok"], "data": self.pages[0]}),
                response({"ret": ["SUCCESS::ok"], "data": self.pages[0]}),
            ]
        )
        payload = TaobaoFetcher(client, self.jar).fetch()
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(summarize(payload)["orders"], 10)

    def test_token_bootstrap_then_retry(self):
        jar = login_jar()
        client = FakeClient(
            [
                response(
                    {"ret": ["FAIL_SYS_TOKEN_EMPTY::令牌为空"], "data": {}},
                    set_cookies=[
                        "_m_h5_tk=refreshed_123; Domain=.taobao.com; Path=/",
                        "_m_h5_tk_enc=enc; Domain=.taobao.com; Path=/",
                    ],
                ),
                response({"ret": ["SUCCESS::ok"], "data": self.pages[0]}),
                response({"ret": ["SUCCESS::ok"], "data": self.pages[1]}),
                response({"ret": ["SUCCESS::ok"], "data": {"data": {}}}),
            ]
        )
        payload = TaobaoFetcher(client, jar).fetch()
        self.assertEqual(jar.get("_m_h5_tk", "h5api.m.taobao.com"), "refreshed_123")
        self.assertEqual(len(payload["pages"]), 3)
        self.assertEqual(len(client.calls), 4)

    def test_login_response_stops(self):
        client = FakeClient(
            [response({"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]})]
        )
        with self.assertRaises(AuthRequired):
            TaobaoFetcher(client, self.jar).fetch()

    def test_risk_response_stops(self):
        client = FakeClient(
            [response({"ret": ["FAIL_SYS_USER_VALIDATE::需要验证"]})]
        )
        with self.assertRaises(RiskBlocked):
            TaobaoFetcher(client, self.jar).fetch()

    def test_refresh_tokens_from_set_cookie(self):
        jar = CookieJar()
        resp = response({}, set_cookies=["_m_h5_tk=a_b; Domain=.taobao.com"])
        self.assertTrue(refresh_tokens_from_response(jar, resp))
        self.assertEqual(jar.get("_m_h5_tk"), "a_b")


class TaobaoTokenRefreshTests(unittest.TestCase):
    """Synthetic responses for lib-mtop's token bootstrap/refresh path."""

    def setUp(self):
        self.pages = load_fixture("taobao_two_pages.json")["pages"]
        self.success_empty = {
            "ret": ["SUCCESS::ok"],
            "data": {"data": {}},
        }

    def _query(self, call):
        return urllib.parse.parse_qs(urllib.parse.urlparse(call["url"]).query)

    def test_missing_token_signs_empty_then_uses_set_cookie(self):
        jar = login_jar()  # deliberately has no _m_h5_tk
        client = FakeClient(
            [
                response(
                    {"ret": ["FAIL_SYS_TOKEN_EMPTY::令牌为空"], "data": {}},
                    set_cookies=[
                        "_m_h5_tk=fresh_123; Domain=.taobao.com; Path=/",
                        "_m_h5_tk_enc=enc_456; Domain=.taobao.com; Path=/",
                    ],
                ),
                response(self.success_empty),
            ]
        )
        fetcher = TaobaoFetcher(client, jar)
        fetcher.fetch()

        self.assertEqual(len(client.calls), 2)
        first = self._query(client.calls[0])
        second = self._query(client.calls[1])
        self.assertEqual(
            first["sign"],
            [mtop_sign("", int(first["t"][0]), TAOBAO_APP_KEY,
                       client.calls[0]["form"]["data"])],
        )
        self.assertEqual(
            second["sign"],
            [mtop_sign("fresh", int(second["t"][0]), TAOBAO_APP_KEY,
                       client.calls[1]["form"]["data"])],
        )
        self.assertNotIn("_m_h5_tk=", client.calls[0]["cookie_header"])
        self.assertEqual(jar.get("_m_h5_tk", "h5api.m.taobao.com"), "fresh_123")
        self.assertEqual(
            jar.get("_m_h5_tk_enc", "h5api.m.taobao.com"), "enc_456"
        )
        self.assertEqual(fetcher.token_rotations, 1)
        self.assertEqual(fetcher.token_retries, 1)

    def test_expired_token_retries_same_page_once(self):
        jar = login_jar("old_111")
        client = FakeClient(
            [
                response(
                    {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"], "data": {}},
                    set_cookies=[
                        "_m_h5_tk=new_222; Domain=.taobao.com; Path=/",
                        "_m_h5_tk_enc=enc_222; Domain=.taobao.com; Path=/",
                    ],
                ),
                response({"ret": ["SUCCESS::ok"], "data": self.pages[0]}),
                response({"ret": ["SUCCESS::ok"], "data": self.pages[1]}),
                response(self.success_empty),
            ]
        )
        fetcher = TaobaoFetcher(client, jar)
        payload = fetcher.fetch()

        self.assertEqual(len(client.calls), 4)
        first = self._query(client.calls[0])
        second = self._query(client.calls[1])
        self.assertEqual(first["sign"], [mtop_sign(
            "old", int(first["t"][0]), TAOBAO_APP_KEY,
            client.calls[0]["form"]["data"])])
        self.assertEqual(second["sign"], [mtop_sign(
            "new", int(second["t"][0]), TAOBAO_APP_KEY,
            client.calls[1]["form"]["data"])])
        # The retry stayed on page 1; page 2 was only requested after success.
        self.assertIn('"page":1', client.calls[0]["form"]["data"])
        self.assertIn('"page":1', client.calls[1]["form"]["data"])
        self.assertIn('"page":2', client.calls[2]["form"]["data"])
        self.assertEqual(summarize(payload)["orders"], 20)

    def test_refreshed_domain_token_overrides_host_only_old_token(self):
        jar = CookieJar(
            [
                Cookie(".taobao.com", "cookie2", "test-cookie2"),
                Cookie(".taobao.com", "unb", "test-unb"),
                Cookie("h5api.m.taobao.com", "_m_h5_tk", "old_1"),
            ]
        )
        client = FakeClient(
            [
                response(
                    {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]},
                    set_cookies=["_m_h5_tk=new_2; Domain=.taobao.com; Path=/"],
                ),
                response(self.success_empty),
            ]
        )
        TaobaoFetcher(client, jar).fetch()
        self.assertEqual(len(client.calls), 2)
        second = self._query(client.calls[1])
        self.assertEqual(second["sign"], [mtop_sign(
            "new", int(second["t"][0]), TAOBAO_APP_KEY,
            client.calls[1]["form"]["data"])])

    def test_token_illegal_also_refreshes(self):
        jar = login_jar("old_333")
        client = FakeClient(
            [
                response(
                    {"ret": ["FAIL_SYS_TOKEN_ILLEGAL::令牌非法"], "data": {}},
                    set_cookies=["_m_h5_tk=new_444; Domain=.taobao.com"],
                ),
                response(self.success_empty),
            ]
        )
        fetcher = TaobaoFetcher(client, jar)
        fetcher.fetch()
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(jar.get("_m_h5_tk", "h5api.m.taobao.com"), "new_444")

    def test_not_logged_in_returns_clear_error(self):
        jar = login_jar("old_555")
        client = FakeClient(
            [response({"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]})]
        )
        with self.assertRaises(AuthRequired) as caught:
            TaobaoFetcher(client, jar).fetch()
        message = caught.exception.render()
        self.assertIn("登录", message)
        self.assertIn("扫码", message)
        self.assertEqual(len(client.calls), 1)

    def test_missing_login_cookies_fails_before_network(self):
        jar = CookieJar([Cookie(".taobao.com", "_m_h5_tk", "only_token_1")])
        client = FakeClient([])
        with self.assertRaises(AuthRequired) as caught:
            TaobaoFetcher(client, jar).fetch()
        message = str(caught.exception)
        self.assertIn("cookie2", message)
        self.assertIn("unb", message)
        self.assertEqual(client.calls, [])

    def test_retry_is_capped_at_two_requests_per_page(self):
        jar = login_jar("old_666")
        client = FakeClient(
            [
                response(
                    {"ret": ["FAIL_SYS_TOKEN_EMPTY::令牌为空"]},
                    set_cookies=["_m_h5_tk=one_1; Domain=.taobao.com"],
                ),
                response(
                    {"ret": ["FAIL_SYS_TOKEN_EMPTY::令牌为空"]},
                    set_cookies=["_m_h5_tk=two_2; Domain=.taobao.com"],
                ),
            ]
        )
        with self.assertRaises(AuthRequired) as caught:
            TaobaoFetcher(client, jar).fetch()
        self.assertEqual(len(client.calls), 2)
        self.assertIn("每页最多重试 1 次", str(caught.exception))

    def test_token_error_without_new_cookie_does_not_retry(self):
        jar = login_jar("old_777")
        client = FakeClient(
            [response({"ret": ["FAIL_SYS_TOKEN_EMPTY::令牌为空"]})]
        )
        with self.assertRaises(AuthRequired) as caught:
            TaobaoFetcher(client, jar).fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertIn("没有提供新的令牌", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class DomainAttributeTests(unittest.TestCase):
    def test_domain_without_leading_dot_is_a_domain_cookie(self):
        from connectors.kit.cookies import domain_matches
        r = Response(200, "https://h5api.m.taobao.com/h5/test/1.0/", {}, b"",
                     ["_m_h5_tk=abc_1; Path=/; Domain=taobao.com; Max-Age=3600"])
        jar = CookieJar()
        jar.apply_response(r)
        cookie = jar.entries()[0]
        self.assertEqual((cookie.name, cookie.host_key), ("_m_h5_tk", ".taobao.com"))
        self.assertTrue(domain_matches(cookie.host_key, "h5api.m.taobao.com"))
