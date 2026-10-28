import json
import unittest

from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.errors import ApiError, AuthRequired
from connectors.kit.http import Response
from connectors.jd.baitiao import (
    BILL_ENDPOINT,
    TRANSACTION_ENDPOINT,
    UNBILLED_ENDPOINT,
    BaitiaoFetcher,
    _amount,
    _date,
    _month,
    _timestamp,
    normalize_bill,
    normalize_transaction,
    normalize_unbilled,
    summarize,
)


def json_response(payload, *, url=BILL_ENDPOINT, status=200):
    return Response(
        status=status,
        url=url,
        headers={},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def html_response(text, *, url="https://passport.jd.com/new/login.aspx", status=200):
    return Response(
        status=status, url=url, headers={}, body=text.encode("utf-8")
    )


def pc_jar(**extra):
    cookies = [
        Cookie(".jd.com", "thor", "thor-value"),
        Cookie(".jd.com", "pin", "pin-value"),
    ]
    for host, name, value in extra.get("cookies", []):
        cookies.append(Cookie(host, name, value))
    return CookieJar(cookies)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cookie_headers = []

    def post_form(self, url, form, *, cookie_header="", headers=None):
        self.calls.append(("post", url, dict(form)))
        self.cookie_headers.append(cookie_header)
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)


BILL_ROW = {
    "billId": "202406000001",
    "billAmt": 123.4,
    "payedAmt": 123.4,
    "refundAmt": 0E-8,
    "refundSdpAmt": 0E-8,
    "refundRepayAmt": 0E-8,
    "planedNum": 0,
    "planedAmt": 0E-8,
    "restPlanAmt": 0E-8,
    "status": 2,
    "billDate": "2024-06-12 00:00:00",
    "billLimitDate": "2024-07-21 23:59:59",
    "sdpAmt": 0E-8,
    "minPayAmt": 0E-8,
    "nextSdpOverAmt": 0E-8,
    "isCurBill": 0,
    "isInPayDay": 0,
    "realOverDays": 0,
    "isOverdueDay": 0,
    "overStatus": 0,
    "flag": 1,
    "sdpOverSum": 0E-8,
    "lateStatus": 0,
    "billDateStart": "2024-05-12 00:00:00",
    "billDateEnd": "2024-06-11 23:59:59",
    "result": {"isSuccess": True},
}

TRANSACTION_ROW = {
    "productName": "测试商品",
    "orderUrl": "https://item.jd.com/1.html",
    "orderAmount": 12.3,
    "loanAmountDiscount": 0E-8,
    "sysCode": 1,
    "bizCode": 2,
    "consumerDate": "2024-05-15 12:34:56",
    "orderId": "123456789",
}

UNBILLED_PAYLOAD = {
    "billNOTOut": {
        "billId": "202408000003",
        "billAmt": 0E-8,
        "billDate": "2024-08-12 00:00:00",
        "sdpAmt": 0E-8,
        "minPayAmt": 0E-8,
        "planedAmt": 0,
        "payedAmt": 0E-8,
        "refundAmt": 0E-8,
        "isCurBill": 1,
        "canRepay": True,
        "nextOverAmt": 0E-8,
        "nextDayAmt": 0E-8,
        "isCanAutoPlan": False,
        "realOverDays": 0,
        "lateStatus": 0,
        "nextSdpLateAmt": 0E-8,
        "billDateStart": "2024-05-12 00:00:00",
        "billDateEnd": "2024-08-11 23:59:59",
    },
    "sdpAmt": 0E-8,
    "sdpLoanAmt": 0E-8,
    "sdpPlanFee": 0E-8,
    "sdpDayAmt": 0E-8,
    "sdpOverAmt": 0E-8,
    "isCanRepayAll": True,
}


class AmountAndDateTests(unittest.TestCase):
    def test_amount_normalization(self):
        self.assertIsNone(_amount(None))
        self.assertEqual(_amount("1,234.5"), "1234.50")
        self.assertEqual(_amount(0E-8), "0.00")
        self.assertEqual(_amount(12), "12.00")
        self.assertEqual(_amount("not-a-number"), "not-a-number")

    def test_dates(self):
        self.assertEqual(_date("2024-06-12 00:00:00"), "2024-06-12")
        self.assertEqual(_month("2024-06-12 00:00:00"), "2024-06")
        self.assertEqual(
            _timestamp("2024-05-15T12:34:56"), "2024-05-15 12:34:56"
        )
        self.assertEqual(_timestamp("2024-05-15 12:34"), "2024-05-15 12:34:00")
        self.assertEqual(_timestamp("2024-05-15"), "2024-05-15")


class NormalizeTests(unittest.TestCase):
    def test_bill(self):
        bill = normalize_bill(BILL_ROW)
        self.assertEqual(bill["bill_id"], "202406000001")
        self.assertEqual(bill["bill_month"], "2024-06")
        self.assertEqual(bill["bill_date"], "2024-06-12")
        self.assertEqual(bill["due_date"], "2024-07-21")
        self.assertEqual(bill["period_start"], "2024-05-12")
        self.assertEqual(bill["period_end"], "2024-06-11")
        self.assertFalse(bill["is_current"])
        self.assertEqual(bill["status"], 2)
        self.assertEqual(bill["amount"], "123.40")
        self.assertEqual(bill["paid"], "123.40")
        self.assertEqual(bill["refund"], "0.00")
        self.assertEqual(bill["should_pay"], "0.00")
        self.assertFalse(bill["overdue"])
        self.assertEqual(bill["raw"], BILL_ROW)

    def test_transaction(self):
        transaction = normalize_transaction(TRANSACTION_ROW)
        self.assertEqual(transaction["order_id"], "123456789")
        self.assertEqual(transaction["occurred"], "2024-05-15 12:34:56")
        self.assertEqual(transaction["product"], "测试商品")
        self.assertEqual(transaction["amount"], "12.30")
        self.assertEqual(transaction["discount"], "0.00")
        self.assertEqual(transaction["sys_code"], 1)
        self.assertEqual(transaction["biz_code"], 2)
        self.assertEqual(
            transaction["order_url"], "https://item.jd.com/1.html"
        )

    def test_unbilled(self):
        unbilled = normalize_unbilled(UNBILLED_PAYLOAD)
        self.assertEqual(unbilled["bill_id"], "202408000003")
        self.assertEqual(unbilled["bill_date"], "2024-08-12")
        self.assertTrue(unbilled["is_current"])
        self.assertTrue(unbilled["can_repay"])
        self.assertEqual(unbilled["amount"], "0.00")
        self.assertEqual(unbilled["should_pay"], "0.00")

    def test_unbilled_absent(self):
        self.assertIsNone(normalize_unbilled({}))
        self.assertIsNone(normalize_unbilled({"billNOTOut": None}))


class BaitiaoFetcherTests(unittest.TestCase):
    def test_fetches_bills_transactions_and_unbilled(self):
        client = FakeClient(
            [
                json_response(
                    {
                        "billList": [BILL_ROW],
                        "currentPage": 1,
                        "pageSize": 10,
                        "pageCount": 2,
                        "totalCount": 2,
                        "result": {"isSuccess": True},
                    },
                    url=BILL_ENDPOINT,
                ),
                json_response(
                    {
                        "billList": [dict(BILL_ROW, billId="202407000002")],
                        "currentPage": 2,
                        "pageSize": 10,
                        "pageCount": 2,
                        "totalCount": 2,
                        "result": {"isSuccess": True},
                    },
                    url=BILL_ENDPOINT,
                ),
                json_response(
                    {
                        "detailsInfo": [TRANSACTION_ROW],
                        "pageSize": 10,
                        "pageNum": 1,
                        "totalCount": 1,
                        "pageCount": 1,
                    },
                    url=TRANSACTION_ENDPOINT,
                ),
                json_response(UNBILLED_PAYLOAD, url=UNBILLED_ENDPOINT),
            ]
        )
        fetcher = BaitiaoFetcher(client, pc_jar())
        payload = fetcher.fetch()

        self.assertEqual(payload["kind"], "jd_baitiao")
        self.assertEqual(len(payload["bills"]), 2)
        self.assertEqual(len(payload["transactions"]), 1)
        self.assertIsNotNone(payload["unbilled"])
        self.assertEqual(payload["pages"], {"bills": 2, "transactions": 1})
        self.assertEqual(fetcher.warnings, [])

        self.assertEqual(
            client.calls,
            [
                ("post", BILL_ENDPOINT, {"pageNum": "1", "pageSize": "10"}),
                ("post", BILL_ENDPOINT, {"pageNum": "2", "pageSize": "10"}),
                (
                    "post",
                    TRANSACTION_ENDPOINT,
                    {"pageNum": "1", "pageSize": "10"},
                ),
                ("post", UNBILLED_ENDPOINT, {}),
            ],
        )

    def test_empty_bill_envelope_is_not_an_error(self):
        client = FakeClient(
            [
                json_response({"result": {"isSuccess": True}}),
                json_response(
                    {
                        "pageSize": 10,
                        "pageNum": 1,
                        "totalCount": 0,
                        "pageCount": 0,
                    },
                    url=TRANSACTION_ENDPOINT,
                ),
                json_response({"result": {"isSuccess": True}}, url=UNBILLED_ENDPOINT),
            ]
        )
        fetcher = BaitiaoFetcher(client, pc_jar())
        payload = fetcher.fetch()
        self.assertEqual(payload["bills"], [])
        self.assertEqual(payload["transactions"], [])
        self.assertIsNone(payload["unbilled"])

    def test_page_count_stops_without_extra_request(self):
        client = FakeClient(
            [
                json_response(
                    {
                        "billList": [BILL_ROW],
                        "pageCount": 1,
                        "totalCount": 1,
                        "result": {"isSuccess": True},
                    }
                ),
                json_response(
                    {"detailsInfo": [], "pageCount": 0, "totalCount": 0},
                    url=TRANSACTION_ENDPOINT,
                ),
                json_response(UNBILLED_PAYLOAD, url=UNBILLED_ENDPOINT),
            ]
        )
        fetcher = BaitiaoFetcher(client, pc_jar())
        payload = fetcher.fetch()
        self.assertEqual(len(payload["bills"]), 1)
        self.assertEqual(payload["transactions"], [])
        self.assertEqual(len(client.calls), 3)

    def test_empty_page_stops(self):
        client = FakeClient(
            [
                json_response(
                    {
                        "billList": [BILL_ROW],
                        "pageCount": 5,
                        "totalCount": 99,
                        "result": {"isSuccess": True},
                    }
                ),
                json_response(
                    {"billList": [], "pageCount": 5, "totalCount": 99}
                ),
                json_response(
                    {"detailsInfo": [], "pageCount": 0, "totalCount": 0},
                    url=TRANSACTION_ENDPOINT,
                ),
                json_response(UNBILLED_PAYLOAD, url=UNBILLED_ENDPOINT),
            ]
        )
        fetcher = BaitiaoFetcher(client, pc_jar())
        payload = fetcher.fetch()
        self.assertEqual(len(payload["bills"]), 1)
        self.assertEqual(payload["pages"]["bills"], 2)

    def test_repeated_rows_stop_and_are_not_duplicated(self):
        client = FakeClient(
            [
                json_response(
                    {
                        "billList": [BILL_ROW],
                        "pageCount": 5,
                        "totalCount": 99,
                        "result": {"isSuccess": True},
                    }
                ),
                json_response(
                    {
                        "billList": [BILL_ROW],
                        "pageCount": 5,
                        "totalCount": 99,
                        "result": {"isSuccess": True},
                    }
                ),
                json_response(
                    {"detailsInfo": [], "pageCount": 0, "totalCount": 0},
                    url=TRANSACTION_ENDPOINT,
                ),
                json_response(UNBILLED_PAYLOAD, url=UNBILLED_ENDPOINT),
            ]
        )
        fetcher = BaitiaoFetcher(client, pc_jar())
        payload = fetcher.fetch()
        self.assertEqual(len(payload["bills"]), 1)
        self.assertEqual(payload["pages"]["bills"], 2)

    def test_max_pages_warns(self):
        client = FakeClient(
            [
                json_response(
                    {
                        "billList": [BILL_ROW],
                        "pageCount": 5,
                        "totalCount": 99,
                        "result": {"isSuccess": True},
                    }
                ),
                json_response(
                    {"detailsInfo": [], "pageCount": 0, "totalCount": 0},
                    url=TRANSACTION_ENDPOINT,
                ),
                json_response(UNBILLED_PAYLOAD, url=UNBILLED_ENDPOINT),
            ]
        )
        fetcher = BaitiaoFetcher(client, pc_jar(), max_pages=1)
        fetcher.fetch()
        self.assertTrue(any("最大页数" in warning for warning in fetcher.warnings))

    def test_missing_login_blocks_before_request(self):
        jar = CookieJar([Cookie(".jd.com", "thor", "only-thor")])
        client = FakeClient([])
        fetcher = BaitiaoFetcher(client, jar)
        with self.assertRaises(AuthRequired):
            fetcher.fetch()
        self.assertEqual(client.calls, [])

    def test_not_login_payload(self):
        client = FakeClient(
            [json_response({"error": "NotLogin"}, url=BILL_ENDPOINT)]
        )
        fetcher = BaitiaoFetcher(client, pc_jar())
        with self.assertRaises(AuthRequired):
            fetcher.fetch()

    def test_top_level_code_three_is_auth_required(self):
        client = FakeClient(
            [
                json_response(
                    {"code": 3, "message": "请先登录"}, url=BILL_ENDPOINT
                )
            ]
        )
        fetcher = BaitiaoFetcher(client, pc_jar())
        with self.assertRaises(AuthRequired):
            fetcher.fetch()

    def test_top_level_error_is_api_error(self):
        client = FakeClient(
            [json_response({"error": "系统异常"}, url=BILL_ENDPOINT)]
        )
        fetcher = BaitiaoFetcher(client, pc_jar())
        with self.assertRaises(ApiError):
            fetcher.fetch()

    def test_result_failure_is_api_error(self):
        client = FakeClient(
            [
                json_response(
                    {"result": {"isSuccess": False, "info": "系统繁忙"}},
                    url=BILL_ENDPOINT,
                )
            ]
        )
        fetcher = BaitiaoFetcher(client, pc_jar())
        with self.assertRaises(ApiError):
            fetcher.fetch()

    def test_login_html_is_auth_required(self):
        client = FakeClient([html_response("<html>京东登录</html>")])
        fetcher = BaitiaoFetcher(client, pc_jar())
        with self.assertRaises(AuthRequired):
            fetcher.fetch()

    def test_unbilled_failure_is_only_a_warning(self):
        client = FakeClient(
            [
                json_response(
                    {
                        "billList": [BILL_ROW],
                        "pageCount": 1,
                        "totalCount": 1,
                        "result": {"isSuccess": True},
                    }
                ),
                json_response(
                    {
                        "detailsInfo": [TRANSACTION_ROW],
                        "pageCount": 1,
                        "totalCount": 1,
                    },
                    url=TRANSACTION_ENDPOINT,
                ),
                json_response(
                    {"result": {"isSuccess": False, "info": "暂不可用"}},
                    url=UNBILLED_ENDPOINT,
                ),
            ]
        )
        fetcher = BaitiaoFetcher(client, pc_jar())
        payload = fetcher.fetch()
        self.assertEqual(len(payload["bills"]), 1)
        self.assertEqual(len(payload["transactions"]), 1)
        self.assertIsNone(payload["unbilled"])
        self.assertTrue(any("未出账单" in warning for warning in fetcher.warnings))

    def test_no_unbilled_skips_endpoint(self):
        client = FakeClient(
            [
                json_response(
                    {
                        "billList": [],
                        "pageCount": 0,
                        "totalCount": 0,
                        "result": {"isSuccess": True},
                    }
                ),
                json_response(
                    {"detailsInfo": [], "pageCount": 0, "totalCount": 0},
                    url=TRANSACTION_ENDPOINT,
                ),
            ]
        )
        fetcher = BaitiaoFetcher(client, pc_jar(), include_unbilled=False)
        payload = fetcher.fetch()
        self.assertIsNone(payload["unbilled"])
        self.assertEqual(len(client.calls), 2)

    def test_cookie_header_targets_baitiao_host(self):
        jar = pc_jar(
            cookies=[
                ("other.example", "secret", "other-value"),
                ("baitiao.jd.com", "bt", "bt-value"),
            ]
        )
        client = FakeClient(
            [
                json_response(
                    {
                        "billList": [],
                        "pageCount": 0,
                        "totalCount": 0,
                        "result": {"isSuccess": True},
                    }
                ),
                json_response(
                    {"detailsInfo": [], "pageCount": 0, "totalCount": 0},
                    url=TRANSACTION_ENDPOINT,
                ),
                json_response(UNBILLED_PAYLOAD, url=UNBILLED_ENDPOINT),
            ]
        )
        BaitiaoFetcher(client, jar).fetch()
        header = client.cookie_headers[0]
        self.assertIn("thor=thor-value", header)
        self.assertIn("pin=pin-value", header)
        self.assertIn("bt=bt-value", header)
        self.assertNotIn("other-value", header)

    def test_page_size_range(self):
        with self.assertRaises(ValueError):
            BaitiaoFetcher(FakeClient([]), pc_jar(), page_size=0)
        with self.assertRaises(ValueError):
            BaitiaoFetcher(FakeClient([]), pc_jar(), page_size=51)


class SummaryTests(unittest.TestCase):
    def test_summary(self):
        payload = {
            "bills": [
                {"bill_date": "2024-06-12"},
                {"bill_date": "2024-07-12"},
            ],
            "transactions": [{"occurred": "2024-05-15 12:34:56"}],
            "unbilled": {"bill_id": "x"},
            "pages": {"bills": 2, "transactions": 1},
        }
        summary = summarize(payload)
        self.assertEqual(summary["bills"], 2)
        self.assertEqual(summary["transactions"], 1)
        self.assertEqual(
            summary["bill_range"], ("2024-06-12", "2024-07-12")
        )
        self.assertEqual(
            summary["transaction_range"],
            ("2024-05-15 12:34:56", "2024-05-15 12:34:56"),
        )
        self.assertTrue(summary["has_unbilled"])


if __name__ == "__main__":
    unittest.main()
