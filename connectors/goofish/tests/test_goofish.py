import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from connectors.goofish import CONNECTOR
from connectors.goofish.blocking import check_goofish_html, check_goofish_ret
from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.errors import (
    ApiError,
    AuthRequired,
    NeedsRiskSignature,
    RiskBlocked,
)
from connectors.goofish.reader import (
    GOOFISH_APP_KEY,
    GOOFISH_BOUGHT_API,
    GOOFISH_SOLD_API,
    GoofishFetcher,
    build_bought_data,
    build_request,
    build_sold_data,
    date_range,
    parse_orders,
    summarize,
)
from connectors.kit.http import Response
from connectors.kit.output import file_mode
from connectors.kit.runner import main as run_main
from connectors.kit.testing import mock_sessions
from connectors.taobao.reader import mtop_sign

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def response(payload, *, status=200, set_cookies=None, url=None):
    return Response(
        status=status,
        url=url or "https://h5api.m.goofish.com/h5/test/1.0/",
        headers={},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        set_cookies=list(set_cookies or []),
    )


class FakeClient:
    user_agent = "pfin-goofish-test-agent"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_form(self, url, form, *, cookie_header="", headers=None):
        self.calls.append(
            {
                "url": url,
                "form": dict(form),
                "cookie_header": cookie_header,
                "headers": dict(headers or {}),
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)


def login_jar(token=None):
    cookies = [
        Cookie(".goofish.com", "cookie2", "test-cookie2"),
        Cookie(".goofish.com", "unb", "123456"),
    ]
    if token is not None:
        cookies.append(Cookie(".goofish.com", "_m_h5_tk", token))
    return CookieJar(cookies)


class GoofishRequestTests(unittest.TestCase):
    def test_bought_data_first_page_omits_offset(self):
        self.assertEqual(
            build_bought_data(1, "ALL"),
            {"pageNumber": 1, "orderStatus": "ALL"},
        )
        self.assertEqual(
            build_bought_data(2, "SHIPPED", "row-9"),
            {"pageNumber": 2, "orderStatus": "SHIPPED", "offsetRow": "row-9"},
        )

    def test_sold_data_shape(self):
        self.assertEqual(
            build_sold_data(3, "ALL", rows_per_page=30),
            {"queryCode": "ALL", "pageNumber": 3, "rowsPerPage": 30},
        )

    def test_request_shape_and_signature(self):
        data = build_bought_data(2, "ALL", "row-1")
        plan = build_request(
            GOOFISH_BOUGHT_API,
            "1.0",
            data,
            "token123",
            timestamp_ms=1700000000000,
            user_agent="ua-test",
        )
        parsed = urllib.parse.urlparse(plan.url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "h5api.m.goofish.com")
        self.assertEqual(parsed.path, "/h5/mtop.idle.web.trade.bought.list/1.0/")
        self.assertEqual(query["appKey"], [GOOFISH_APP_KEY])
        self.assertEqual(query["api"], [GOOFISH_BOUGHT_API])
        self.assertEqual(query["v"], ["1.0"])
        self.assertEqual(query["type"], ["json"])
        self.assertEqual(query["accountSite"], ["xianyu"])
        self.assertEqual(query["dataType"], ["json"])
        self.assertEqual(query["valueType"], ["string"])
        self.assertEqual(query["t"], ["1700000000000"])
        self.assertEqual(
            query["sign"],
            [mtop_sign("token123", 1700000000000, GOOFISH_APP_KEY, plan.data_json)],
        )
        self.assertEqual(plan.form["data"], plan.data_json)
        self.assertEqual(plan.form["ua"], "ua-test")
        self.assertIn('"offsetRow":"row-1"', plan.data_json)

    def test_sold_endpoint(self):
        plan = build_request(
            GOOFISH_SOLD_API,
            "1.0",
            build_sold_data(1, "ALL"),
            "token123",
            timestamp_ms=1700000000000,
            user_agent="ua-test",
        )
        self.assertIn(
            "/h5/mtop.taobao.idle.trade.merchant.sold.get/1.0/", plan.url
        )


class GoofishParserTests(unittest.TestCase):
    def setUp(self):
        self.bought = load_fixture("goofish_bought_page.json")
        self.sold = load_fixture("goofish_sold_page.json")

    def test_bought_orders(self):
        pages = [{"side": "bought", "response": self.bought}]
        orders = parse_orders(pages)
        self.assertEqual(len(orders), 2)
        first = orders[0]
        self.assertEqual(first["order_id"], "9001")
        self.assertEqual(first["occurred"], "2026-09-01 10:00:00")
        self.assertEqual(first["counterparty"], "卖家甲")
        self.assertEqual(first["items"][0]["title"], "测试商品A")
        self.assertEqual(first["items"][0]["quantity"], 2)
        self.assertEqual(first["paid"], "12.34")
        self.assertEqual(first["status"], "交易成功")
        self.assertEqual(date_range(orders), ("2026-09-01 10:00:00", "2026-09-02 11:30:00"))

    def test_sold_orders(self):
        pages = [{"side": "sold", "response": self.sold}]
        orders = parse_orders(pages)
        self.assertEqual(len(orders), 2)
        first = orders[0]
        self.assertEqual(first["order_id"], "8001")
        self.assertEqual(first["occurred"], "2026-08-01 09:00:00")
        self.assertEqual(first["counterparty"], "买家乙")
        self.assertEqual(first["items"][0]["title"], "测试商品C")
        self.assertEqual(first["items"][0]["quantity"], 2)
        self.assertEqual(first["paid"], "40.00")
        self.assertEqual(first["status"], "交易成功")
        self.assertTrue(orders[1]["in_refund"])

    def test_payload_summary(self):
        payload = {
            "pages": [
                {"side": "bought", "response": self.bought},
                {"side": "sold", "response": self.sold},
            ]
        }
        summary = summarize(payload)
        self.assertEqual(summary["pages"], 2)
        self.assertEqual(summary["orders"], 4)
        self.assertEqual(summary["bought_orders"], 2)
        self.assertEqual(summary["sold_orders"], 2)


class GoofishBlockingTests(unittest.TestCase):
    def test_success(self):
        check_goofish_ret({"ret": ["SUCCESS::调用成功"]})

    def test_login_required(self):
        with self.assertRaises(AuthRequired):
            check_goofish_ret({"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]})

    def test_risk_block(self):
        with self.assertRaises(RiskBlocked):
            check_goofish_ret({"ret": ["FAIL_SYS_USER_VALIDATE::需要验证"]})

    def test_needs_risk_signature(self):
        with self.assertRaises(NeedsRiskSignature) as caught:
            check_goofish_ret({"ret": ["FAIL_SYS_NEED_RISK_SIGNATURE::需要风控签名"]})
        self.assertIn("needs risk signature", caught.exception.message)
        self.assertIn("fyOBJ", caught.exception.hint)

    def test_generic_failure(self):
        with self.assertRaises(ApiError):
            check_goofish_ret({"ret": ["FAIL_SYS_SERVICE_UNAVAILABLE::busy"]})

    def test_x5sec_html_is_risk_blocked(self):
        with self.assertRaises(RiskBlocked):
            check_goofish_html(
                "<html><script>var url='.../_____tmd_____/punish?x5secdata=...'</script></html>",
                "https://h5.m.goofish.com/_____tmd_____/punish?x5secdata=x",
            )


class GoofishFetcherTests(unittest.TestCase):
    def setUp(self):
        self.bought = load_fixture("goofish_bought_page.json")
        self.sold = load_fixture("goofish_sold_page.json")
        self.jar = login_jar("token_1")

    def test_fetch_both_sides(self):
        client = FakeClient([response(self.bought), response(self.sold)])
        fetcher = GoofishFetcher(client, self.jar, sides=("bought", "sold"))
        payload = fetcher.fetch()
        self.assertEqual(payload["kind"], "goofish_orders")
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(payload["sides"], ["bought", "sold"])
        self.assertEqual(len(payload["orders"]["bought"]), 2)
        self.assertEqual(len(payload["orders"]["sold"]), 2)
        self.assertIn("SUCCESS", payload["pages"][0]["raw"])
        self.assertEqual(client.calls[0]["headers"]["Referer"], "https://www.goofish.com/bought")
        self.assertEqual(client.calls[1]["headers"]["Referer"], "https://seller.goofish.com/")

    def test_bought_pagination_uses_last_end_row(self):
        page1 = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "items": [self.bought["data"]["items"][0]],
                "nextPage": "true",
                "lastEndRow": "row-1",
            },
        }
        page2 = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "items": [self.bought["data"]["items"][1]],
                "nextPage": "false",
                "lastEndRow": "row-2",
            },
        }
        client = FakeClient([response(page1), response(page2)])
        fetcher = GoofishFetcher(client, self.jar, sides=("bought",))
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 2)
        first_data = json.loads(client.calls[0]["form"]["data"])
        second_data = json.loads(client.calls[1]["form"]["data"])
        self.assertNotIn("offsetRow", first_data)
        self.assertEqual(second_data["offsetRow"], "row-1")
        self.assertEqual(summarize(payload)["orders"], 2)

    def test_sold_pagination(self):
        page1 = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "module": {
                    "items": [self.sold["data"]["module"]["items"][0]],
                    "nextPage": "true",
                }
            },
        }
        page2 = {
            "ret": ["SUCCESS::ok"],
            "data": {
                "module": {
                    "items": [self.sold["data"]["module"]["items"][1]],
                    "nextPage": "false",
                }
            },
        }
        client = FakeClient([response(page1), response(page2)])
        fetcher = GoofishFetcher(client, self.jar, sides=("sold",))
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 2)
        first_data = json.loads(client.calls[0]["form"]["data"])
        second_data = json.loads(client.calls[1]["form"]["data"])
        self.assertEqual(first_data["pageNumber"], 1)
        self.assertEqual(second_data["pageNumber"], 2)

    def test_sold_empty_success_is_a_real_empty_page(self):
        empty = {
            "ret": ["SUCCESS::调用成功"],
            "data": {
                "module": {
                    "items": [],
                    "nextPage": "false",
                    "totalCount": "0",
                }
            },
        }
        client = FakeClient([response(empty)])
        fetcher = GoofishFetcher(client, self.jar, sides=("sold",))
        payload = fetcher.fetch()
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(payload["orders"]["sold"], [])
        self.assertEqual(fetcher.warnings, [])
        sent = json.loads(client.calls[0]["form"]["data"])
        # The public bundle defaults sessionData to empty strings, and this
        # reader deliberately omits those routing hints instead of inventing
        # an IM session.  A SUCCESS + totalCount=0 answer is a real empty list.
        self.assertEqual(set(sent), {"queryCode", "pageNumber", "rowsPerPage"})
        self.assertNotIn("sessionId", sent)
        self.assertNotIn("sessionType", sent)
        # idle_site_biz_code is added by the page only when
        # __PC_SELLER_WORKBENCH__.userInfo actually carries it.
        self.assertNotIn("idle_site_biz_code", client.calls[0]["headers"])
        self.assertNotIn("idle_user_group_member_id", client.calls[0]["headers"])

    def test_token_bootstrap_then_retry(self):
        jar = login_jar()  # deliberately no _m_h5_tk
        client = FakeClient(
            [
                response(
                    {"ret": ["FAIL_SYS_TOKEN_EMPTY::令牌为空"], "data": {}},
                    set_cookies=[
                        "_m_h5_tk=fresh_123; Domain=.goofish.com; Path=/",
                        "_m_h5_tk_enc=enc_456; Domain=.goofish.com; Path=/",
                    ],
                ),
                response(self.bought),
                response(self.sold),
            ]
        )
        fetcher = GoofishFetcher(client, jar, sides=("bought", "sold"))
        fetcher.fetch()
        self.assertEqual(jar.get("_m_h5_tk", "h5api.m.goofish.com"), "fresh_123")
        self.assertEqual(fetcher.token_rotations, 1)
        self.assertEqual(fetcher.token_retries, 1)
        first = urllib.parse.parse_qs(
            urllib.parse.urlparse(client.calls[0]["url"]).query
        )
        second = urllib.parse.parse_qs(
            urllib.parse.urlparse(client.calls[1]["url"]).query
        )
        self.assertEqual(
            first["sign"],
            [mtop_sign("", int(first["t"][0]), GOOFISH_APP_KEY,
                       client.calls[0]["form"]["data"])],
        )
        self.assertEqual(
            second["sign"],
            [mtop_sign("fresh", int(second["t"][0]), GOOFISH_APP_KEY,
                       client.calls[1]["form"]["data"])],
        )

    def test_token_error_without_new_cookie_does_not_retry(self):
        jar = login_jar("old_777")
        client = FakeClient(
            [response({"ret": ["FAIL_SYS_TOKEN_EMPTY::令牌为空"]})]
        )
        with self.assertRaises(AuthRequired) as caught:
            GoofishFetcher(client, jar, sides=("bought",)).fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertIn("没有提供新的令牌", str(caught.exception))

    def test_missing_login_cookies_fails_before_network(self):
        jar = CookieJar([Cookie(".goofish.com", "_m_h5_tk", "only_token_1")])
        client = FakeClient([])
        with self.assertRaises(AuthRequired) as caught:
            GoofishFetcher(client, jar, sides=("bought",)).fetch()
        message = str(caught.exception)
        self.assertIn("cookie2", message)
        self.assertIn("unb", message)
        self.assertEqual(client.calls, [])

    def test_risk_response_stops(self):
        client = FakeClient(
            [response({"ret": ["FAIL_SYS_USER_VALIDATE::需要验证"]})]
        )
        with self.assertRaises(RiskBlocked):
            GoofishFetcher(client, self.jar, sides=("bought",)).fetch()

    def test_probe_reads_user_nav(self):
        nav = load_fixture("goofish_nav.json")
        client = FakeClient([response(nav)])
        result = GoofishFetcher(client, self.jar, sides=("bought",)).probe()
        self.assertTrue(result["has_user"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["purchase_count"], 42)
        self.assertEqual(result["sold_count"], 0)
        self.assertIn("/mtop.idle.web.user.page.nav/1.0/", client.calls[0]["url"])
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(client.calls[0]["url"]).query
        )
        self.assertEqual(query["type"], ["originaljson"])
        self.assertNotIn("valueType", query)

    def test_probe_rejects_nav_without_user(self):
        client = FakeClient([response({"ret": ["SUCCESS::ok"], "data": {}})])
        with self.assertRaises(AuthRequired):
            GoofishFetcher(client, self.jar, sides=("bought",)).probe()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_main(CONNECTOR, argv)
    return code, out.getvalue(), err.getvalue()


class FakeProvider:
    def __init__(self, *args, **kwargs):
        pass

    def load(self):
        return CookieJar([Cookie(".goofish.com", "cookie2", "v"), Cookie(".goofish.com", "unb", "v")])


class GoofishCliTests(unittest.TestCase):
    def test_dry_run_does_not_touch_network_or_keychain(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             mock.patch("subprocess.run", side_effect=AssertionError("keychain")):
            code, text, _ = run(["fetch", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("mtop.idle.web.trade.bought.list", text)
        self.assertIn("mtop.taobao.idle.trade.merchant.sold.get", text)
        self.assertIn("34839810", text)
        self.assertIn("sign: md5", text)

    def test_writes_goofish_orders_0600(self):
        payload = {
            "kind": "goofish_orders",
            "source": {},
            "sides": ["bought", "sold"],
            "pages": [
                {"side": "bought", "response": load_fixture("goofish_bought_page.json")},
                {"side": "sold", "response": load_fixture("goofish_sold_page.json")},
            ],
            "orders": {"bought": [], "sold": []},
            "warnings": [],
        }
        replaced = dataclasses.replace(CONNECTOR, fetch=lambda args, jar: (payload, ["test warning"]))
        with tempfile.TemporaryDirectory() as temp, mock.patch("connectors.kit.runner.provider", FakeProvider):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = run_main(replaced, ["fetch", "--out", temp])
            self.assertEqual(code, 0)
            files = list(Path(temp).glob("goofish-orders-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(file_mode(files[0]), 0o600)
            saved = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["kind"], "goofish_orders")
            rows = list(Path(temp).glob("goofish-rows-*.jsonl"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(file_mode(rows[0]), 0o600)
            self.assertIn("orders=4", out.getvalue())
            self.assertIn("bought: orders=2", out.getvalue())
            self.assertIn("sold:   orders=2", out.getvalue())
            self.assertIn("test warning", err.getvalue())

    def test_rows_per_page_must_be_positive(self):
        code, _, _ = run(["fetch", "--out", "/tmp", "--rows-per-page", "0"])
        self.assertEqual(code, 2)

    def test_manifest_and_keepalive_hours(self):
        code, text, _ = run(["manifest"])
        manifest = json.loads(text)
        self.assertEqual((code, manifest["key"], manifest["keepalive_hours"]), (0, "goofish", 4.0))
        self.assertEqual(manifest["outputs"]["raw"], "goofish-orders-<ts>.json")


def setUpModule():
    mock_sessions()


if __name__ == "__main__":
    unittest.main()
