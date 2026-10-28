import json
import tempfile
import unittest
from pathlib import Path

from connectors.meituan.blocking import check_meituan_api
from connectors.kit.cookies import Cookie, CookieJar, CookieProvider
from connectors.kit.errors import (
    ApiError,
    AuthRequired,
    NeedsRiskSignature,
    RiskBlocked,
    UsageError,
)
from connectors.kit.http import Response
from connectors.kit.testing import PASSWORD, make_cookie_db

from connectors.meituan.reader import (
    MEITUAN_BILL_ENDPOINT,
    MEITUAN_PAY_ATTR,
    MEITUAN_REPAYMENT_FILTER,
    MEITUAN_REPAYMENT_START,
    MEITUAN_USERINFO_ENDPOINT,
    MeituanYuefuFetcher,
    build_page_payload,
    build_repayment_payload,
    build_userinfo_payload,
    parse_page_size,
    parse_pay_details,
    parse_repayments,
    parse_transactions,
    repayment_mode,
    summarize,
    user_id_from_jar,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def output_payload(pages):
    return {
        "kind": "meituan_yuefu",
        "pages": [
            {"page": index, "response": payload}
            for index, payload in enumerate(pages)
        ],
    }


def json_response(payload, *, status=200, url=MEITUAN_BILL_ENDPOINT):
    return Response(
        status=status,
        url=url,
        headers={},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def repayment_api_response(*, next_page=-1):
    return {
        "status": "success",
        "data": {
            "nextPage": next_page,
            "pageSize": 20,
            "billListVersion": "v2",
            "monthData": [
                {
                    "month": "202609",
                    "incomeAmount": 0,
                    "outcomeAmount": 0,
                    "promoAmount": 0,
                    "billItemList": [
                        {
                            "tradeNo": "TEST-REPAY-1",
                            "payId": "TEST-REPAY-PAY-1",
                            "tradeType": 1,
                            "orderTitle": "【美团月付】主动还款2026年9月账单",
                            "businessName": "还款",
                            "orderAmount": 12345,
                            "orderPromoAmount": 0,
                            "payTime": "09-01 11:00",
                            "orderTag": None,
                            "orderLabel": None,
                            "hasAward": False,
                            "moneyFlow": 0,
                            "orderType": None,
                            "payDetailTag": "测试银行储蓄卡(1234)",
                            "payDetails": json.dumps(
                                [
                                    {
                                        "fundType": "BANKCARD",
                                        "moneyCent": 12345,
                                        "cardNoSuffix": "1234",
                                        "cardType": "DEBIT",
                                        "bankName": "测试银行",
                                        "cardNoPrefix": "622200",
                                    }
                                ],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    ],
                }
            ],
        },
    }


def repayment_output_payload():
    return {
        "kind": "meituan_yuefu",
        "pages": [],
        "repayments": {
            "source": MEITUAN_BILL_ENDPOINT,
            "filter": {
                "financialTransType": MEITUAN_REPAYMENT_FILTER,
                "startTime": "20240101",
                "endTime": "20261031",
            },
            "pages": [
                {"page": 0, "response": repayment_api_response()},
            ],
        },
    }


class FakeClient:
    user_agent = "pfin-test-agent"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_json(self, url, payload, *, cookie_header="", headers=None):
        self.calls.append(
            {
                "url": url,
                "payload": dict(payload),
                "cookie_header": cookie_header,
                "headers": dict(headers or {}),
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)


def login_jar(*, token="secret-token", user_id="secret-user"):
    cookies = []
    if token is not None:
        cookies.append(Cookie(".meituan.com", "token", token))
    if user_id is not None:
        cookies.append(Cookie(".meituan.com", "userId", user_id))
    return CookieJar(cookies)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.page0 = load_fixture("meituan_yuefu_pages.json")["page0"]
        self.page1 = load_fixture("meituan_yuefu_pages.json")["page1"]
        self.payload = output_payload([self.page0, self.page1])

    def test_flattens_and_normalizes_transactions(self):
        transactions = parse_transactions(self.payload)
        self.assertEqual(len(transactions), 3)
        first = transactions[0]
        self.assertEqual(first["trade_no"], "MT202610240001")
        self.assertEqual(first["occurred"], "2026-10-24 12:00:00")
        self.assertEqual(first["business"], "外卖")
        self.assertEqual(first["amount_yuan"], "12.00")
        self.assertEqual(first["money_flow"], 2)

    def test_accepts_a_single_api_response(self):
        transactions = parse_transactions(self.page0)
        self.assertEqual(len(transactions), 2)
        self.assertEqual(transactions[1]["occurred"], "2026-10-01 08:00:00")

    def test_summary_counts_unique_transactions_and_months(self):
        summary = summarize(self.payload)
        self.assertEqual(summary["pages"], 2)
        self.assertEqual(summary["orders"], 3)
        self.assertEqual(summary["transactions"], 3)
        self.assertEqual(summary["months"], 2)
        self.assertEqual(
            summary["date_range"],
            ("2026-09-06 12:00:00", "2026-10-24 12:00:00"),
        )

    def test_display_pay_time_falls_back_to_month(self):
        payload = {
            "status": "success",
            "data": {
                "monthData": [
                    {
                        "month": "202610",
                        "billItemList": [
                            {
                                "tradeNo": "T-DISPLAY",
                                "payId": "P-DISPLAY",
                                "payTime": "10-24 12:00",
                                "orderAmount": 100,
                            }
                        ],
                    }
                ]
            },
        }
        transaction = parse_transactions(payload)[0]
        self.assertEqual(transaction["occurred"], "2026-10-24 12:00:00")

    def test_page_payload_matches_public_bill_request(self):
        payload = build_page_payload(0, user_id="secret-user")
        self.assertEqual(payload["payAttr"], MEITUAN_PAY_ATTR)
        self.assertEqual(payload["currentPage"], 0)
        self.assertEqual(payload["pageSize"], 20)
        self.assertEqual(payload["nb_app"], "web")
        self.assertEqual(payload["nb_platform"], "touch")
        self.assertEqual(payload["ctype"], "")

    def test_userinfo_payload_is_minimal(self):
        self.assertEqual(
            build_userinfo_payload("u1"),
            {
                "userid": "u1",
                "nb_app": "web",
                "nb_platform": "touch",
                "ctype": "",
            },
        )

    def test_repayment_payload_omits_pay_attr_and_has_filter(self):
        payload = build_repayment_payload(
            0,
            user_id="u1",
            page_size=20,
            start_time="20240101",
            end_time="20261031",
        )
        self.assertNotIn("payAttr", payload)
        self.assertEqual(payload["financialTransType"], MEITUAN_REPAYMENT_FILTER)
        self.assertEqual(payload["startTime"], "20240101")
        self.assertEqual(payload["endTime"], "20261031")
        self.assertEqual(payload["currentPage"], 0)
        self.assertEqual(payload["userid"], "u1")

    def test_repayment_parser_flattens_and_keeps_payment_method(self):
        repayments = parse_repayments(repayment_output_payload())
        self.assertEqual(len(repayments), 1)
        item = repayments[0]
        self.assertEqual(item["trade_no"], "TEST-REPAY-1")
        self.assertEqual(item["occurred"], "2026-09-01 11:00:00")
        self.assertEqual(item["amount_yuan"], "123.45")
        self.assertEqual(item["money_flow"], 0)
        self.assertEqual(item["pay_detail_tag"], "测试银行储蓄卡(1234)")
        self.assertEqual(item["pay_details"][0]["cardNoSuffix"], "1234")
        self.assertEqual(item["repay_mode"], "主动还款")
        self.assertIsNone(item["pay_details_raw"])

    def test_repayment_parser_accepts_a_single_api_response(self):
        repayments = parse_repayments(repayment_api_response())
        self.assertEqual(len(repayments), 1)
        self.assertEqual(repayments[0]["repay_mode"], "主动还款")

    def test_summary_counts_repayments_separately(self):
        payload = repayment_output_payload()
        payload["pages"] = []
        summary = summarize(payload)
        self.assertEqual(summary["orders"], 0)
        self.assertEqual(summary["repayments"], 1)
        self.assertEqual(summary["repayment_pages"], 1)
        self.assertEqual(
            summary["repayment_date_range"],
            ("2026-09-01 11:00:00", "2026-09-01 11:00:00"),
        )


class RepaymentFieldTests(unittest.TestCase):
    def test_auto_repayment_title_is_verbatim(self):
        self.assertEqual(
            repayment_mode({"orderTitle": "【美团月付】自动还款2026年9月账单"}),
            "自动还款",
        )
        self.assertEqual(
            repayment_mode({"orderTitle": "【美团月付】代扣还款-2026年9月账单"}),
            "代扣还款",
        )
        self.assertEqual(repayment_mode({"repayType": "主动还款"}), "主动还款")

    def test_bad_pay_details_are_preserved_raw(self):
        details, raw = parse_pay_details("not-json")
        self.assertIsNone(details)
        self.assertEqual(raw, "not-json")

    def test_empty_pay_details(self):
        self.assertEqual(parse_pay_details(None), (None, None))
        self.assertEqual(parse_pay_details(""), (None, None))


class BlockingTests(unittest.TestCase):
    def test_not_logged_in_code(self):
        with self.assertRaises(AuthRequired):
            check_meituan_api(load_fixture("meituan_yuefu_not_logged_in.json"))

    def test_needs_signature_is_distinct(self):
        with self.assertRaises(NeedsRiskSignature):
            check_meituan_api(load_fixture("meituan_yuefu_needs_signature.json"))

    def test_normal_success(self):
        check_meituan_api(load_fixture("meituan_yuefu_pages.json")["page0"])

    def test_risk_marker(self):
        with self.assertRaises(RiskBlocked):
            check_meituan_api(
                {"status": "fail", "error": {"code": "X", "message": "请完成验证码"}}
            )

    def test_generic_failure(self):
        with self.assertRaises(ApiError):
            check_meituan_api(
                {"status": "fail", "error": {"code": "X", "message": "系统繁忙"}}
            )


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self.page0 = load_fixture("meituan_yuefu_pages.json")["page0"]
        self.page1 = load_fixture("meituan_yuefu_pages.json")["page1"]
        self.repayment = repayment_api_response()

    def test_pagination_and_raw_responses(self):
        client = FakeClient(
            [
                json_response(self.page0),
                json_response(self.page1),
                json_response(self.repayment),
            ]
        )
        fetcher = MeituanYuefuFetcher(
            client,
            login_jar(),
            max_pages=5,
            repayment_start="20240101",
            repayment_end="20261031",
        )
        payload = fetcher.fetch()

        self.assertEqual(payload["kind"], "meituan_yuefu")
        self.assertEqual(payload["source"], MEITUAN_BILL_ENDPOINT)
        self.assertEqual(payload["pay_attr"], 4)
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(len(payload["repayments"]["pages"]), 1)
        self.assertEqual(summarize(payload)["orders"], 3)
        self.assertEqual(summarize(payload)["repayments"], 1)
        self.assertEqual(payload["pages"][0]["raw"], json.dumps(
            self.page0, ensure_ascii=False
        ))
        self.assertEqual(
            json.loads(payload["pages"][1]["raw"])["data"]["nextPage"], -1
        )
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[0]["payload"]["currentPage"], 0)
        self.assertEqual(client.calls[1]["payload"]["currentPage"], 1)
        self.assertEqual(client.calls[2]["payload"]["currentPage"], 0)
        self.assertEqual(client.calls[0]["payload"]["payAttr"], 4)
        self.assertNotIn("payAttr", client.calls[2]["payload"])
        self.assertEqual(
            client.calls[2]["payload"]["financialTransType"],
            MEITUAN_REPAYMENT_FILTER,
        )
        self.assertEqual(client.calls[2]["payload"]["startTime"], "20240101")
        self.assertEqual(client.calls[2]["payload"]["endTime"], "20261031")
        self.assertEqual(client.calls[0]["payload"]["userid"], "secret-user")
        self.assertEqual(client.calls[0]["url"], MEITUAN_BILL_ENDPOINT)
        self.assertEqual(client.calls[0]["headers"]["Origin"], "https://npay.meituan.com")
        self.assertEqual(
            payload["repayments"]["filter"]["financialTransType"],
            MEITUAN_REPAYMENT_FILTER,
        )
        self.assertEqual(payload["repayments"]["pages"][0]["request"]["userid"],
                         "<from cookie>")

    def test_repayment_default_window_starts_at_bundle_min_date(self):
        client = FakeClient(
            [
                json_response(self.page0),
                json_response(self.page1),
                json_response(self.repayment),
            ]
        )
        payload = MeituanYuefuFetcher(client, login_jar(), max_pages=5).fetch()
        self.assertEqual(client.calls[2]["payload"]["startTime"],
                         MEITUAN_REPAYMENT_START)
        self.assertEqual(len(client.calls[2]["payload"]["endTime"]), 8)
        self.assertEqual(
            payload["repayments"]["filter"]["startTime"],
            MEITUAN_REPAYMENT_START,
        )

    def test_bill_only_mode_skips_repayment_request(self):
        client = FakeClient([json_response(self.page0), json_response(self.page1)])
        payload = MeituanYuefuFetcher(
            client, login_jar(), max_pages=5, include_repayments=False
        ).fetch()
        self.assertIsNone(payload["repayments"])
        self.assertEqual(len(client.calls), 2)

    def test_cookie_value_is_not_written_to_output(self):
        client = FakeClient(
            [
                json_response(self.page0),
                json_response(self.page1),
                json_response(self.repayment),
            ]
        )
        fetcher = MeituanYuefuFetcher(client, login_jar(), max_pages=5)
        payload = fetcher.fetch()
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret-user", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertEqual(payload["pages"][0]["request"]["userid"], "<from cookie>")
        self.assertEqual(
            payload["repayments"]["pages"][0]["request"]["userid"],
            "<from cookie>",
        )

    def test_not_logged_in_response_stops_with_one_request(self):
        client = FakeClient(
            [json_response(load_fixture("meituan_yuefu_not_logged_in.json"))]
        )
        with self.assertRaises(AuthRequired) as caught:
            MeituanYuefuFetcher(client, login_jar()).fetch()
        self.assertIn("登录", str(caught.exception))
        self.assertEqual(len(client.calls), 1)

    def test_signature_request_stops_clearly(self):
        client = FakeClient(
            [json_response(load_fixture("meituan_yuefu_needs_signature.json"))]
        )
        with self.assertRaises(NeedsRiskSignature) as caught:
            MeituanYuefuFetcher(client, login_jar()).fetch()
        message = str(caught.exception)
        self.assertIn("mtgsig", message)
        self.assertIn("不会伪造", message)
        self.assertEqual(len(client.calls), 1)

    def test_missing_token_fails_before_network(self):
        client = FakeClient([])
        with self.assertRaises(AuthRequired) as caught:
            MeituanYuefuFetcher(client, login_jar(token=None)).fetch()
        self.assertIn("token", str(caught.exception))
        self.assertEqual(client.calls, [])

    def test_probe_uses_read_only_userinfo(self):
        client = FakeClient(
            [
                json_response(
                    {"status": "success", "data": {"userName": "owner", "phone": "x"}},
                    url=MEITUAN_USERINFO_ENDPOINT,
                )
            ]
        )
        info = MeituanYuefuFetcher(client, login_jar()).probe()
        self.assertTrue(info["has_data"])
        self.assertEqual(client.calls[0]["url"], MEITUAN_USERINFO_ENDPOINT)
        self.assertEqual(client.calls[0]["payload"]["ctype"], "")
        self.assertEqual(len(client.calls), 1)

    def test_probe_not_logged_in(self):
        client = FakeClient(
            [
                json_response(
                    load_fixture("meituan_yuefu_not_logged_in.json"),
                    url=MEITUAN_USERINFO_ENDPOINT,
                )
            ]
        )
        with self.assertRaises(AuthRequired):
            MeituanYuefuFetcher(client, login_jar()).probe()

    def test_repeated_next_page_stops(self):
        repeated = json.loads(json.dumps(self.page0))
        client = FakeClient(
            [
                json_response(repeated),
                json_response(repeated),
                json_response(self.repayment),
            ]
        )
        payload = MeituanYuefuFetcher(client, login_jar(), max_pages=5).fetch()
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(summarize(payload)["orders"], 2)
        self.assertEqual(len(client.calls), 3)


class CookieProviderTests(unittest.TestCase):
    def test_meituan_cookies_are_read_by_the_shared_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "Cookies"
            make_cookie_db(
                db,
                [
                    (".meituan.com", "token", "secret-token", b"v10"),
                    (".meituan.com", "userId", "secret-user", b"v10"),
                ],
            )
            provider = CookieProvider(
                db, password=PASSWORD, host_suffixes=("meituan.com",)
            )
            jar = provider.load()
            self.assertEqual(user_id_from_jar(jar), "secret-user")
            self.assertEqual(jar.get("token", "npay.meituan.com"), "secret-token")
            self.assertNotIn("secret-token", repr(jar))
            self.assertNotIn("secret-user", repr(jar))


class PageSizeTests(unittest.TestCase):
    def test_default_and_valid(self):
        self.assertEqual(parse_page_size(None), 20)
        self.assertEqual(parse_page_size("50"), 50)

    def test_invalid(self):
        for value in ("0", "-1", "101", "abc"):
            with self.assertRaises(UsageError):
                parse_page_size(value)


if __name__ == "__main__":
    unittest.main()
