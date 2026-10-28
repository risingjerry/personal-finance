import json
import unittest

import urllib.parse

from connectors.jd.blocking import check_jd_html
from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.errors import ApiError, AuthRequired, RiskBlocked, UsageError
from connectors.kit.http import Response
from connectors.jd.reader import (
    JD_API_QUERY_KEYS,
    JD_STATUS_ALL,
    JdFetcher,
    d_for_year,
    date_range,
    detect_login_set,
    html_url,
    missing_login_cookies,
    parse_api_orders,
    parse_html_orders,
    parse_years,
    require_login,
    summarize,
)

from connectors.kit.testing import FIXTURES, load_fixture

JD_PAGE = None
if (FIXTURES / "jd_2024_page1.json").exists():
    JD_PAGE = load_fixture("jd_2024_page1.json")["html"]


def html_response(html, *, status=200, url="https://order.jd.com/center/list.action?d=2024&s=4096&page=1"):
    return Response(status=status, url=url, headers={}, body=html.encode("utf-8"))


def json_response(payload, *, status=200):
    return Response(
        status=status,
        url="https://api.m.jd.com/api",
        headers={},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


class FakeClient:
    """Records every request so tests can assert the exact wire count."""

    def __init__(self, html_responses=None, api_responses=None):
        self.html_responses = list(html_responses or [])
        self.api_responses = list(api_responses or [])
        self.calls = []
        self.cookie_headers = []
        self.request_headers = []

    def get(self, url, *, cookie_header="", headers=None):
        self.calls.append(("get", url))
        self.cookie_headers.append(("get", url, cookie_header))
        self.request_headers.append(("get", url, dict(headers or {})))
        if not self.html_responses:
            raise AssertionError("unexpected extra HTML request")
        return self.html_responses.pop(0)

    def post_form(self, url, form, *, cookie_header="", headers=None):
        self.calls.append(("post", url, dict(form)))
        self.cookie_headers.append(("post", url, cookie_header))
        self.request_headers.append(("post", url, dict(headers or {})))
        if not self.api_responses:
            raise AssertionError("unexpected extra API request")
        return self.api_responses.pop(0)

    def count(self, method):
        return sum(1 for call in self.calls if call[0] == method)


def api_payload(order_ids, *, total_num=None, occurred="2026-05-01 10:00:00",
                shop="API店"):
    """Minimal pcorder_center_list response: data.orderList + data.totalNum."""

    data = {
        "orderList": [
            {
                "orderId": order_id,
                "dateSubmit": occurred,
                "shopInfo": {"shopName": shop},
                "statusInfo": {"orderStateName": "已完成"},
                "itemPayInfo": {"fistPrice": "1.00"},
                "wareList": [{"name": "商品", "num": 1}],
            }
            for order_id in order_ids
        ]
    }
    if total_num is not None:
        data["totalNum"] = total_num
    return {"code": 0, "data": data}


@unittest.skipUnless(JD_PAGE is not None, "fixture missing")
class JdParserTests(unittest.TestCase):
    def setUp(self):
        self.orders = parse_html_orders(JD_PAGE)

    def test_fixture_orders(self):
        self.assertEqual(len(self.orders), 20)
        self.assertEqual(len({order["order_id"] for order in self.orders}), 20)
        dates = [order["occurred"] for order in self.orders if order["occurred"]]
        self.assertEqual(date_range(self.orders), (min(dates), max(dates)))

    def test_no_false_risk_on_normal_page(self):
        check_jd_html(JD_PAGE, "https://order.jd.com/center/list.action?d=2024")

    def test_login_page_detected(self):
        with self.assertRaises(AuthRequired):
            check_jd_html(
                "<html><title>京东登录</title><body>账户登录</body></html>",
                "https://passport.jd.com/new/login.aspx",
            )

    def test_captcha_page_detected(self):
        with self.assertRaises(RiskBlocked):
            check_jd_html("<html><body>安全验证 请完成验证</body></html>", "https://order.jd.com/x")


class JdSyntheticParserTests(unittest.TestCase):
    def order(self, *, quantity=1):
        products = "".join(
            '<div class="productItem-test"><a class="goodImg-test" title="测试商品"></a>'
            '<span class="countNum-test">1</span></div>'
            for _ in range(quantity)
        )
        html = (
            '<div class="orderCard-test"><div class="orderTime-test">'
            '<span class="metaValue-test">2000-01-01 00:00:00</span></div>'
            '<div class="orderMetas-test"><span class="metaValue-test">00000001</span></div>'
            '<span class="shopNames-test">测试店</span><span class="statusText-test">已完成</span>'
            '<div class="priceColumn-test"><span class="priceInteger-test">1</span>'
            '<span class="priceDecimal-test">.23</span></div>'
            + products + '</div>'
        )
        return parse_html_orders(html)[0]

    def test_first_order_fields(self):
        order = self.order()
        self.assertEqual(order["order_id"], "00000001")
        self.assertEqual(order["occurred"], "2000-01-01 00:00:00")
        self.assertEqual(order["shop"], "测试店")
        self.assertEqual(order["status"], "已完成")
        self.assertEqual(order["paid"], "1.23")
        self.assertEqual(order["items"][0]["quantity"], 1)
        self.assertEqual(order["items"][0]["title"], "测试商品")

    def test_multi_item_order(self):
        order = self.order(quantity=3)
        self.assertEqual(order["paid"], "1.23")
        self.assertEqual(len(order["items"]), 3)
        self.assertEqual(order["occurred"], "2000-01-01 00:00:00")


class JdYearTests(unittest.TestCase):
    def test_parse_years(self):
        self.assertEqual(parse_years(None, 2026), [2026])
        self.assertEqual(parse_years("2024-2026", 2026), [2024, 2025, 2026])
        self.assertEqual(parse_years("2018,2020,2022-2023", 2026), [2018, 2020, 2022, 2023])

    def test_parse_years_errors(self):
        with self.assertRaises(UsageError):
            parse_years("2026-2024", 2026)
        with self.assertRaises(UsageError):
            parse_years("1999", 2026)
        with self.assertRaises(UsageError):
            parse_years("2027", 2026)
        with self.assertRaises(UsageError):
            parse_years("abc", 2026)

    def test_d_mapping(self):
        self.assertEqual(d_for_year(2026, 2026), "2")
        self.assertEqual(d_for_year(2024, 2026), "2024")
        self.assertEqual(d_for_year(2013, 2026), "3")

    def test_html_url(self):
        self.assertEqual(
            html_url("2024", 2),
            "https://order.jd.com/center/list.action?d=2024&s=4096&page=2",
        )


class JdApiTests(unittest.TestCase):
    def test_parse_api_payload(self):
        payload = {
            "code": 0,
            "data": {
                "orderList": [
                    {
                        "orderId": "123456789",
                        "dateSubmit": "2020-05-01 10:00:00",
                        "shopInfo": {"shopName": "测试店"},
                        "statusInfo": {"orderStateName": "已完成"},
                        "itemPayInfo": {"fistPrice": "12.30"},
                        "wareList": [
                            {"name": "商品A", "num": 2, "productId": 99}
                        ],
                    }
                ]
            },
        }
        orders = parse_api_orders(payload)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["order_id"], "123456789")
        self.assertEqual(orders[0]["occurred"], "2020-05-01 10:00:00")
        self.assertEqual(orders[0]["paid"], "12.30")
        self.assertEqual(orders[0]["items"][0]["quantity"], 2)
        self.assertEqual(orders[0]["source"], "api")

    def test_parse_api_amount_fallback(self):
        payload = {
            "code": 0,
            "data": {
                "orderList": [
                    {
                        "orderId": "1",
                        "orderTime": 1580000000000,
                        "orderTotalPrice": "¥ 9.90",
                    }
                ]
            },
        }
        order = parse_api_orders(payload)[0]
        self.assertEqual(order["paid"], "9.90")
        self.assertTrue(order["occurred"].startswith("2020-"))


class JdLoginTests(unittest.TestCase):
    def test_pc_session_accepts_thor_and_pin_only(self):
        jar = CookieJar(
            [
                Cookie(".jd.com", "thor", "thor-value"),
                Cookie(".jd.com", "pin", "pin-value"),
                Cookie(".jd.com", "pinId", "pin-id"),
                Cookie(".jd.com", "unick", "nick"),
                Cookie(".jd.com", "TrackID", "track-id"),
            ]
        )
        self.assertEqual(missing_login_cookies(jar), [])
        self.assertEqual(detect_login_set(jar).key, "pc")
        require_login(jar)

    def test_mobile_session_is_still_accepted(self):
        jar = CookieJar(
            [
                Cookie(".jd.com", "pt_key", "pt-key"),
                Cookie(".jd.com", "pt_pin", "pt-pin"),
            ]
        )
        self.assertEqual(missing_login_cookies(jar), [])
        self.assertEqual(detect_login_set(jar).key, "mobile")
        require_login(jar)

    def test_neither_login_set_is_rejected(self):
        jar = CookieJar(
            [
                Cookie(".jd.com", "thor", "thor-value"),
                Cookie(".jd.com", "pinId", "pin-id"),
            ]
        )
        self.assertEqual(missing_login_cookies(jar), ["pin", "pt_key", "pt_pin"])
        self.assertIsNone(detect_login_set(jar))
        with self.assertRaises(AuthRequired):
            require_login(jar)


@unittest.skipUnless(JD_PAGE is not None, "fixture missing")
class JdFetcherTests(unittest.TestCase):
    def setUp(self):
        self.jar = CookieJar(
            [
                Cookie(".jd.com", "pt_key", "x"),
                Cookie(".jd.com", "pt_pin", "y"),
                Cookie(".jd.com", "__jda", "uuid"),
            ]
        )

    def test_html_pages_stop_when_empty(self):
        client = FakeClient([html_response(JD_PAGE), html_response("<html></html>")])
        fetcher = JdFetcher(
            client, self.jar, [2024], max_pages=5, api_fallback=False
        )
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(summarize(payload)["orders"], 20)
        self.assertEqual(payload["pages"][0]["d"], "2024")
        self.assertEqual(payload["pages"][0]["page"], 1)

    def test_empty_year_fallback_is_ignored(self):
        client = FakeClient([html_response(JD_PAGE)])
        fetcher = JdFetcher(
            client, self.jar, [2020], max_pages=5, api_fallback=False
        )
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(payload["pages"][0]["orders"], [])
        self.assertEqual(summarize(payload)["orders"], 0)
        self.assertTrue(any("回退" in warning for warning in fetcher.warnings))

    def test_api_first_does_not_read_html_shell(self):
        client = FakeClient(
            [html_response("<html><body>shell</body></html>")],
            [
                json_response(
                    api_payload(["9001"], total_num=1, occurred="2024-03-04 05:06:07")
                )
            ],
        )
        fetcher = JdFetcher(client, self.jar, [2024], max_pages=1)
        payload = fetcher.fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "post")
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(payload["pages"][0]["orders"][0]["source"], "api")
        # The raw API JSON is still stored under the historical "html" key so
        # rows/benchmark tooling keeps working.
        self.assertIn("orderList", payload["pages"][0]["html"])
        self.assertEqual(summarize(payload)["orders"], 1)

    def test_requests_send_cookies_for_both_list_hosts(self):
        jar = CookieJar(
            [
                Cookie(".jd.com", "thor", "thor-value"),
                Cookie(".jd.com", "pin", "pin-value"),
                Cookie("order.jd.com", "order_only", "order-value"),
                Cookie("api.m.jd.com", "api_only", "api-value"),
            ]
        )

        api_client = FakeClient(
            api_responses=[
                json_response(
                    api_payload(["8001"], total_num=1, occurred="2024-06-01 12:00:00")
                )
            ]
        )
        JdFetcher(api_client, jar, [2024], max_pages=1).fetch()
        self.assertEqual([call[0] for call in api_client.calls], ["post"])
        post_header = api_client.cookie_headers[0][2]
        self.assertIn("thor=thor-value", post_header)
        self.assertIn("pin=pin-value", post_header)
        self.assertIn("api_only=api-value", post_header)
        self.assertNotIn("order_only=order-value", post_header)

        html_client = FakeClient([html_response(JD_PAGE)])
        JdFetcher(
            html_client, jar, [2024], max_pages=1, api_fallback=False
        ).fetch()
        self.assertEqual([call[0] for call in html_client.calls], ["get"])
        get_header = html_client.cookie_headers[0][2]
        self.assertIn("thor=thor-value", get_header)
        self.assertIn("pin=pin-value", get_header)
        self.assertIn("order_only=order-value", get_header)
        self.assertNotIn("api_only=api-value", get_header)

    def test_repeated_page_stops(self):
        client = FakeClient([html_response(JD_PAGE), html_response(JD_PAGE)])
        fetcher = JdFetcher(
            client, self.jar, [2024], max_pages=5, api_fallback=False
        )
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(summarize(payload)["orders"], 20)


RISK_HTML = "<html><body>x5sec 安全验证 请完成验证</body></html>"


class JdFastPathTests(unittest.TestCase):
    """API-first behaviour: one request per page, exact totalNum paging."""

    def setUp(self):
        self.jar = CookieJar(
            [
                Cookie(".jd.com", "pt_key", "x"),
                Cookie(".jd.com", "pt_pin", "y"),
                Cookie(".jd.com", "__jda", "uuid-value"),
            ]
        )

    def fetcher(self, client, years=(2026,), current_year=2026, **kwargs):
        fetcher = JdFetcher(client, self.jar, list(years), **kwargs)
        # Pin "this year" so d=2 is deterministic regardless of the clock.
        fetcher._current_year = current_year
        return fetcher

    def test_single_year_is_one_api_request(self):
        client = FakeClient(
            api_responses=[json_response(api_payload(["1", "2"], total_num=2))]
        )
        payload = self.fetcher(client).fetch()

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.count("post"), 1)
        self.assertEqual(client.count("get"), 0)
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(payload["pages"][0]["orders"][0]["source"], "api")

        query = payload["query"]
        self.assertEqual(query["transport"], "api")
        self.assertEqual(query["api_requests"], 1)
        self.assertEqual(query["html_requests"], 0)
        self.assertEqual(query["total_num"], 2)
        self.assertIsInstance(query["elapsed_ms"], int)
        self.assertGreaterEqual(query["elapsed_ms"], 0)
        self.assertEqual(query["years"][0]["year"], 2026)
        self.assertEqual(query["years"][0]["d"], "2")

    def test_total_num_zero_with_empty_page_stops_after_one_request(self):
        client = FakeClient(
            api_responses=[json_response({"code": 0, "data": {"orderList": [], "totalNum": 0}})]
        )
        payload = self.fetcher(client).fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(payload["pages"], [{"d": "2", "page": 1, "orders": [], "html": payload["pages"][0]["html"]}])
        self.assertEqual(summarize(payload)["orders"], 0)

    def test_total_num_zero_but_rows_present_keeps_probing(self):
        ids = [str(1000 + index) for index in range(20)]
        client = FakeClient(
            api_responses=[
                json_response(api_payload(ids, total_num=0)),
                json_response({"code": 0, "data": {"orderList": [], "totalNum": 0}}),
            ]
        )
        payload = self.fetcher(client).fetch()
        # A zero/stale totalNum is not trusted as a stop signal.
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(summarize(payload)["orders"], 20)

    def test_precise_paging_never_probes_an_empty_tail_page(self):
        pages = [
            api_payload([str(index) for index in range(20)], total_num=45),
            api_payload([str(index) for index in range(20, 40)], total_num=45),
            api_payload([str(index) for index in range(40, 45)], total_num=45),
        ]
        client = FakeClient(api_responses=[json_response(page) for page in pages])
        payload = self.fetcher(client, max_pages=10).fetch()
        # FakeClient raises on a fourth call, so this also proves there is none.
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(summarize(payload)["orders"], 45)
        self.assertEqual(payload["query"]["total_num"], 45)
        self.assertEqual(payload["query"]["years"][0]["pages"], 3)

    def test_non_monotonic_total_num_does_not_stop_early(self):
        pages = [
            api_payload([str(index) for index in range(20)], total_num=40),
            api_payload([str(index) for index in range(20, 40)], total_num=30),
        ]
        client = FakeClient(api_responses=[json_response(page) for page in pages])
        payload = self.fetcher(client, max_pages=10).fetch()
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(summarize(payload)["orders"], 40)

    def test_repeated_api_page_stops_without_total_num(self):
        page = api_payload(["1", "2"])
        client = FakeClient(
            api_responses=[json_response(page), json_response(page)]
        )
        payload = self.fetcher(client, max_pages=5).fetch()
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(summarize(payload)["orders"], 2)

    def test_multi_year_is_one_request_per_year(self):
        client = FakeClient(
            api_responses=[
                json_response(
                    api_payload(["201"], total_num=1, occurred="2025-03-01 08:00:00")
                ),
                json_response(
                    api_payload(["202"], total_num=1, occurred="2026-03-01 08:00:00")
                ),
            ]
        )
        payload = self.fetcher(client, years=(2025, 2026)).fetch()
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(payload["query"]["api_requests"], 2)
        # d=YYYY for a past year, d=2 for the current one.
        self.assertEqual([page["d"] for page in payload["pages"]], ["2025", "2"])
        self.assertEqual(summarize(payload)["orders"], 2)

    @unittest.skipUnless(JD_PAGE is not None, "fixture missing")
    def test_non_json_api_response_falls_back_to_html(self):
        client = FakeClient(
            [
                html_response(JD_PAGE, url=html_url("2024", 1)),
                html_response("<html></html>", url=html_url("2024", 2)),
            ],
            [html_response("<html><body>shell</body></html>")],
        )
        fetcher = self.fetcher(client, years=(2024,))
        payload = fetcher.fetch()

        self.assertEqual(
            [call[0] for call in client.calls], ["post", "get", "get"]
        )
        self.assertEqual(payload["query"]["transport"], "api+html")
        self.assertEqual(payload["query"]["api_requests"], 1)
        self.assertEqual(payload["query"]["html_requests"], 2)
        self.assertEqual(summarize(payload)["orders"], 20)
        self.assertTrue(
            any("回退 HTML" in warning for warning in fetcher.warnings)
        )
        # The failed API response is not stored; page 1 is the HTML fallback.
        self.assertTrue("orderCard-" in payload["pages"][0]["html"])

    def test_api_failure_on_second_page_is_not_retried_through_html(self):
        ids = [str(1000 + index) for index in range(20)]
        client = FakeClient(
            api_responses=[
                json_response(api_payload(ids, total_num=60)),
                html_response("<html><body>shell</body></html>"),
            ]
        )
        fetcher = self.fetcher(client, max_pages=5)
        with self.assertRaises(ApiError):
            fetcher.fetch()
        self.assertEqual([call[0] for call in client.calls], ["post", "post"])

    def test_risk_page_from_api_fails_closed_without_html_retry(self):
        client = FakeClient(api_responses=[html_response(RISK_HTML)])
        fetcher = self.fetcher(client)
        with self.assertRaises(RiskBlocked):
            fetcher.fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.count("get"), 0)

    def test_risk_json_from_api_fails_closed_without_html_retry(self):
        client = FakeClient(
            api_responses=[
                json_response({"code": 403, "message": "安全验证 请完成验证"})
            ]
        )
        fetcher = self.fetcher(client)
        with self.assertRaises(RiskBlocked):
            fetcher.fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.count("get"), 0)

    def test_rate_limit_http_403_fails_closed_on_api(self):
        client = FakeClient(
            api_responses=[
                Response(
                    status=403,
                    url="https://api.m.jd.com/api",
                    headers={},
                    body=b"forbidden",
                )
            ]
        )
        fetcher = self.fetcher(client)
        with self.assertRaises(RiskBlocked):
            fetcher.fetch()
        self.assertEqual(len(client.calls), 1)

    def test_login_lost_on_api_fails_closed(self):
        client = FakeClient(
            api_responses=[json_response({"code": 3, "message": "not login"})]
        )
        fetcher = self.fetcher(client)
        with self.assertRaises(AuthRequired):
            fetcher.fetch()
        self.assertEqual(len(client.calls), 1)

    @unittest.skipUnless(JD_PAGE is not None, "fixture missing")
    def test_html_only_never_calls_the_api(self):
        client = FakeClient(
            [
                html_response(JD_PAGE, url=html_url("2024", 1)),
                html_response("<html></html>", url=html_url("2024", 2)),
            ]
        )
        payload = self.fetcher(
            client, years=(2024,), api_fallback=False
        ).fetch()
        self.assertEqual({call[0] for call in client.calls}, {"get"})
        self.assertEqual(client.count("post"), 0)
        self.assertEqual(payload["query"]["transport"], "html")
        self.assertEqual(payload["query"]["api_requests"], 0)
        self.assertEqual(payload["query"]["html_requests"], 2)
        self.assertEqual(summarize(payload)["orders"], 20)

    def test_html_only_risk_page_fails_closed(self):
        client = FakeClient([html_response(RISK_HTML)])
        fetcher = self.fetcher(client, api_fallback=False)
        with self.assertRaises(RiskBlocked):
            fetcher.fetch()
        self.assertEqual(len(client.calls), 1)

    def test_year_fallback_on_api_discards_other_years_and_stops(self):
        client = FakeClient(
            api_responses=[
                json_response(
                    api_payload(["1"], total_num=1, occurred="2020-04-01 09:00:00")
                )
            ]
        )
        fetcher = self.fetcher(client, years=(2024,))
        payload = fetcher.fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(payload["pages"][0]["orders"], [])
        self.assertEqual(summarize(payload)["orders"], 0)
        self.assertTrue(any("回退" in warning for warning in fetcher.warnings))

    def test_api_request_matches_the_bundle_wire_format(self):
        client = FakeClient(
            api_responses=[json_response(api_payload(["1"], total_num=1))]
        )
        self.fetcher(client).fetch()

        method, url, form = client.calls[0]
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.m.jd.com")
        self.assertEqual(parsed.path, "/api")
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(list(query), list(JD_API_QUERY_KEYS))
        self.assertEqual(query["functionId"], "pcorder_center_list")
        self.assertEqual(query["source"], "pc_order_center")
        self.assertEqual(query["appid"], "order-jd-com")
        self.assertEqual(query["client"], "pc")
        self.assertEqual(query["clientVersion"], "1.0.0")
        self.assertEqual(query["uuid"], "uuid-value")
        self.assertEqual(query["loginType"], "3")
        self.assertTrue(query["t"].isdigit())
        for forbidden in ("h5st", "x-api-eid-token", "jsToken"):
            self.assertNotIn(forbidden, query)

        for name, value in client.request_headers[0][2].items():
            self.assertNotIn("h5st", name.lower())
            self.assertNotIn("eid-token", name.lower())
            self.assertNotIn("jstoken", name.lower())
            self.assertNotIn("h5st", str(value).lower())

        body = json.loads(form["body"])
        self.assertEqual(set(body), {"page", "d", "s"})
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["d"], "2")
        self.assertEqual(body["s"], JD_STATUS_ALL)
        self.assertNotIn("pageSize", body)
        self.assertNotIn("recycleType", body)

    def test_second_page_body_uses_incremented_page_and_year_d(self):
        pages = [
            api_payload(
                [str(index) for index in range(20)], total_num=40,
                occurred="2024-05-01 10:00:00",
            ),
            api_payload(
                [str(index) for index in range(20, 40)], total_num=40,
                occurred="2024-05-01 10:00:00",
            ),
        ]
        client = FakeClient(api_responses=[json_response(page) for page in pages])
        self.fetcher(client, years=(2024,), max_pages=5).fetch()
        bodies = [json.loads(call[2]["body"]) for call in client.calls]
        self.assertEqual([body["page"] for body in bodies], [1, 2])
        self.assertEqual({body["d"] for body in bodies}, {"2024"})

    def test_max_pages_warning_still_reported(self):
        ids = [str(index) for index in range(20)]
        client = FakeClient(
            api_responses=[
                json_response(api_payload(ids, total_num=100)),
                json_response(
                    api_payload([str(100 + index) for index in range(20)], total_num=100)
                ),
            ]
        )
        fetcher = self.fetcher(client, max_pages=2)
        fetcher.fetch()
        self.assertTrue(any("最大页数" in warning for warning in fetcher.warnings))

    def test_cookie_rotation_from_api_response_counted(self):
        client = FakeClient(
            api_responses=[
                Response(
                    status=200,
                    url="https://api.m.jd.com/api",
                    headers={},
                    body=json.dumps(api_payload(["1"], total_num=1)).encode("utf-8"),
                    set_cookies=["thor=new-thor; Domain=.jd.com; Path=/"],
                )
            ]
        )
        fetcher = self.fetcher(client)
        fetcher.fetch()
        self.assertEqual(fetcher.cookie_rotations, 1)
        self.assertEqual(self.jar.get("thor", "api.m.jd.com"), "new-thor")


if __name__ == "__main__":
    unittest.main()
