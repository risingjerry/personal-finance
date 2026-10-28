import contextlib
import copy
import datetime
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.ctrip import CONNECTOR, SPEC, check
from connectors.ctrip.blocking import check_ctrip_api
from connectors.ctrip.reader import (
    CTRIP_ENDPOINT,
    CTRIP_HISTORY_ENDPOINT,
    CTRIP_REFERER,
    CtripFetcher,
    build_history_body,
    build_recent_body,
    build_request,
    classify_biz,
    parse_order,
    sanitized_raw,
    summarize,
)
from connectors.kit.cookies import Cookie, CookieJar, CookieProvider
from connectors.kit.errors import ApiError, AuthRequired, NeedsRiskSignature
from connectors.kit.http import Response
from connectors.kit.output import file_mode
from connectors.kit.runner import main
from connectors.kit.spec import inspect_site, probe, render_inspection
from connectors.kit.testing import PASSWORD, make_cookie_db, mock_sessions

mock_sessions()

CTRIP = SPEC
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EPOCH_1601 = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def chromium_expiry(when):
    return int((when - EPOCH_1601).total_seconds() * 1_000_000)


def json_response(payload, *, status=200, url=CTRIP_ENDPOINT):
    return Response(
        status=status,
        url=url,
        headers={},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


class FakeClient:
    user_agent = "pfin-ctrip-test"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post_json(self, url, payload, *, cookie_header="", headers=None):
        self.calls.append(
            {
                "url": url,
                "payload": copy.deepcopy(payload),
                "headers": dict(headers or {}),
                "cookie_header": cookie_header,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra Ctrip request")
        return self.responses.pop(0)

    def get(self, *args, **kwargs):
        raise AssertionError("Ctrip fetch must not use GET")

    def post_form(self, *args, **kwargs):
        raise AssertionError("Ctrip fetch must not use form POST")


def make_jar(**cookies):
    host = ".ctrip.com"
    return CookieJar([Cookie(host, name, value) for name, value in cookies.items()])


def make_site_db(path, cookies):
    """cookies: (host_key, name, value, expires_utc) tuples."""

    make_cookie_db(
        path,
        [(host, name, value, b"v10") for host, name, value, _ in cookies],
    )
    connection = sqlite3.connect(path)
    try:
        for _, name, _, expires in cookies:
            connection.execute(
                "UPDATE cookies SET expires_utc=? WHERE name=?", (expires, name)
            )
        connection.commit()
    finally:
        connection.close()
    return path


class RequestTests(unittest.TestCase):
    def test_recent_body_matches_pc_request_shape(self):
        body = build_recent_body(
            2, 15, cid="GUID123", sequence="0000000000000001"
        )
        self.assertEqual(body["PageIndex"], 2)
        self.assertEqual(body["PageSize"], 15)
        self.assertEqual(body["OrderStatusClassify"], "All")
        self.assertEqual(body["BizTypes"], [])
        self.assertIs(body["IsMergeTrainItem"], True)
        self.assertIs(body["NeedOrderAmountDetail"], True)
        self.assertEqual(body["OrderIDs"], "")
        self.assertEqual(body["Channel"], "Online")
        self.assertEqual(body["ClientVersion"], "99.99")
        self.assertEqual(body["Locale"], "")
        self.assertEqual(body["head"]["auth"], "")
        self.assertEqual(body["head"]["cid"], "GUID123")
        self.assertEqual(body["head"]["cver"], "1.0")
        self.assertEqual(body["head"]["syscode"], "09")
        self.assertNotIn("xsid", body["head"])
        self.assertEqual(
            body["head"]["extension"],
            [
                {"name": "protocal", "value": "https"},
                {"name": "sequence", "value": "0000000000000001"},
            ],
        )

    def test_history_body_matches_pc_request_shape(self):
        body = build_history_body(
            1,
            300,
            cid="GUID123",
            sequence="0000000000000001",
            begin_booking="2000-01-01 00:00:00",
            end_booking="2026-12-31 23:59:59",
        )
        self.assertEqual(body["PageIndex"], 1)
        self.assertEqual(body["PageSize"], 300)
        self.assertEqual(body["BeginBookingDateTime"], "2000-01-01 00:00:00")
        self.assertEqual(body["EndBookingDateTime"], "2026-12-31 23:59:59")
        self.assertEqual(body["PreviousPageLastBookingDateTime"], "")
        self.assertEqual(body["OrderStatusClassify"], "All")
        self.assertEqual(body["Channel"], "Online")
        self.assertEqual(body["ClientVersion"], "99.99")
        self.assertEqual(body["Locale"], "")
        self.assertEqual(body["head"]["auth"], "")

    def test_request_urls_and_headers_match_page(self):
        request = build_request(
            1, 15, cid="GUID123", sequence="0000000000000001"
        )
        self.assertEqual(request.url, CTRIP_ENDPOINT)
        self.assertNotIn("?", request.url)
        self.assertEqual(request.headers["Referer"], CTRIP_REFERER)
        self.assertEqual(request.headers["Origin"], "https://my.ctrip.com")
        self.assertEqual(
            request.headers["Content-Type"], "application/json;charset=UTF-8"
        )
        self.assertEqual(request.headers["cookieOrigin"], "https://my.ctrip.com")
        self.assertNotIn("GUID123", repr(request))
        self.assertNotIn("GUID123", request.url)
        history = build_request(
            1, 300, cid="GUID123", kind="history", sequence="0000000000000001"
        )
        self.assertEqual(history.url, CTRIP_HISTORY_ENDPOINT)

    def test_page_validation(self):
        with self.assertRaises(ValueError):
            build_recent_body(0, 15, cid="x")
        with self.assertRaises(ValueError):
            build_recent_body(1, 301, cid="x")


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.recent1 = load("ctrip_orders_page1.json")
        self.recent2 = load("ctrip_orders_page2.json")
        self.history1 = load("ctrip_history_page1.json")
        self.history2 = load("ctrip_history_page2.json")

    def test_parse_recent_order_fields_and_categories(self):
        first = parse_order(self.recent1["OrderEnities"][0])
        second = parse_order(self.recent1["OrderEnities"][1])
        third = parse_order(self.recent2["OrderEnities"][0])
        self.assertEqual(first["category"], "flight")
        self.assertEqual(second["category"], "hotel")
        self.assertEqual(third["category"], "train")
        self.assertEqual(first["order_id"], "CTRIP-FLIGHT-0001")
        self.assertEqual(first["occurred"], "2021-01-01 08:00:00")
        self.assertEqual(first["paid"], "1000.00")
        self.assertEqual(first["currency"], "CNY")
        self.assertEqual(first["items"][0]["title"], "示例航班 经济舱")

    def test_parse_history_dates_with_timezone_suffix(self):
        first = parse_order(self.history1["OrderEnities"][0])
        second = parse_order(self.history1["OrderEnities"][1])
        third = parse_order(self.history2["OrderEnities"][0])
        self.assertEqual(first["occurred"], "2025-01-01 08:00:00")
        self.assertEqual(second["occurred"], "2020-01-01 08:00:00")
        self.assertEqual(third["occurred"], "2016-01-01 08:00:00")
        self.assertEqual(first["paid"], "1000.00")
        self.assertEqual(second["paid"], "200.00")

    def test_parse_datetime_variants(self):
        self.assertEqual(
            parse_order(
                {"OrderID": "1", "BookingDate": 1577836800000}
            )["occurred"],
            "2020-01-01 08:00:00",
        )
        self.assertEqual(
            parse_order(
                {"OrderID": "2", "BookingDate": "/Date(1577836800000)/"}
            )["occurred"],
            "2020-01-01 08:00:00",
        )
        self.assertEqual(
            parse_order(
                {"OrderID": "3", "BookingDate": "/Date(1577836800000+0800)/"}
            )["occurred"],
            "2020-01-01 08:00:00",
        )
        self.assertEqual(
            parse_order({"OrderID": "4", "BookingDate": "2024-06-20"})["occurred"],
            "2024-06-20 00:00:00",
        )
        self.assertIsNone(parse_order({"BookingDate": "not a date"}))

    def test_classify_biz(self):
        self.assertEqual(classify_biz("FlightInternational"), "flight")
        self.assertEqual(classify_biz("HotelInternational"), "hotel")
        self.assertEqual(classify_biz("TrainInternational"), "train")
        self.assertEqual(classify_biz("Piao"), "other")
        self.assertEqual(classify_biz(None), "other")

    def test_redacts_echoed_auth_from_raw(self):
        raw = sanitized_raw(
            {"Auth": "secret-echoed-ticket", "Result": {"ResultCode": 0}}
        )
        self.assertNotIn("secret-echoed-ticket", raw)
        self.assertIn("<redacted>", raw)
        self.assertIn("ResultCode", raw)


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self.jar = make_jar(cticket="secret-cticket", GUID="GUID123456789012345")

    def responses(self):
        return [
            json_response(load("ctrip_orders_page1.json")),
            json_response(load("ctrip_orders_page2.json")),
            json_response(
                load("ctrip_history_page1.json"), url=CTRIP_HISTORY_ENDPOINT
            ),
            json_response(
                load("ctrip_history_page2.json"), url=CTRIP_HISTORY_ENDPOINT
            ),
        ]

    def test_fetches_recent_and_history_and_deduplicates(self):
        client = FakeClient(self.responses())
        fetcher = CtripFetcher(
            client,
            self.jar,
            page_size=2,
            history_page_size=2,
            max_pages=10,
        )
        payload = fetcher.fetch()
        self.assertEqual(payload["kind"], "ctrip_orders")
        self.assertEqual(payload["source"], CTRIP_ENDPOINT)
        self.assertIn(CTRIP_HISTORY_ENDPOINT, payload["endpoints"])
        self.assertEqual(len(payload["pages"]), 4)
        self.assertEqual(
            [page["kind"] for page in payload["pages"]],
            ["recent", "recent", "history", "history"],
        )
        self.assertEqual(
            [page["page_index"] for page in payload["pages"]], [1, 2, 1, 2]
        )
        self.assertEqual(
            [page["page_size"] for page in payload["pages"]], [2, 2, 2, 2]
        )
        summary = summarize(payload)
        self.assertEqual(summary["orders"], 6)
        self.assertEqual(summary["by_source"], {"recent": 3, "history": 3})
        self.assertEqual(
            summary["by_type"],
            {"flight": 2, "hotel": 2, "train": 1, "other": 1},
        )
        self.assertEqual(
            summary["date_range"], ("2016-01-01 08:00:00", "2025-01-01 08:00:00")
        )
        self.assertEqual(client.calls[0]["payload"]["head"]["auth"], "")
        self.assertEqual(
            client.calls[0]["payload"]["head"]["cid"], "GUID123456789012345"
        )
        self.assertIn("secret-cticket", client.calls[0]["cookie_header"])
        self.assertNotIn(
            "secret-echoed-ticket", json.dumps(payload, ensure_ascii=False)
        )

    def test_recent_uses_total_count_to_continue(self):
        client = FakeClient(
            [
                json_response(load("ctrip_orders_page1.json")),
                json_response(load("ctrip_orders_page2.json")),
                json_response(
                    {"Result": {"ResultCode": 0}, "ResponseStatus": {"Ack": "Success"}},
                    url=CTRIP_HISTORY_ENDPOINT,
                ),
            ]
        )
        fetcher = CtripFetcher(
            client, self.jar, page_size=20, history_page_size=300, max_pages=5
        )
        payload = fetcher.fetch()
        recent = [page for page in payload["pages"] if page["kind"] == "recent"]
        self.assertEqual([page["page_index"] for page in recent], [1, 2])
        self.assertEqual(summarize(payload)["orders"], 3)

    def test_short_history_page_stops(self):
        client = FakeClient(self.responses())
        fetcher = CtripFetcher(
            client, self.jar, page_size=20, history_page_size=2, max_pages=5
        )
        payload = fetcher.fetch()
        history = [page for page in payload["pages"] if page["kind"] == "history"]
        self.assertEqual([page["page_index"] for page in history], [1, 2])
        self.assertEqual(len(history[1]["orders"]), 1)

    def test_repeated_page_stops_and_does_not_duplicate(self):
        page = load("ctrip_orders_page1.json")
        client = FakeClient(
            [
                json_response(page),
                json_response(page),
                json_response(
                    {"Result": {"ResultCode": 0}, "ResponseStatus": {"Ack": "Success"}},
                    url=CTRIP_HISTORY_ENDPOINT,
                ),
            ]
        )
        fetcher = CtripFetcher(
            client, self.jar, page_size=2, history_page_size=2, max_pages=10
        )
        payload = fetcher.fetch()
        recent = [page for page in payload["pages"] if page["kind"] == "recent"]
        self.assertEqual(len(recent), 2)
        self.assertEqual(summarize(payload)["orders"], 2)

    def test_max_pages_reached_warns(self):
        page = load("ctrip_orders_page1.json")
        client = FakeClient(
            [
                json_response(page),
                json_response(
                    {"Result": {"ResultCode": 0}, "ResponseStatus": {"Ack": "Success"}},
                    url=CTRIP_HISTORY_ENDPOINT,
                ),
            ]
        )
        fetcher = CtripFetcher(
            client, self.jar, page_size=2, history_page_size=2, max_pages=1
        )
        payload = fetcher.fetch()
        self.assertEqual(
            len([p for p in payload["pages"] if p["kind"] == "recent"]), 1
        )
        self.assertTrue(any("最大页数" in warning for warning in fetcher.warnings))

    def test_missing_cticket_stops_before_network(self):
        client = FakeClient()
        fetcher = CtripFetcher(client, make_jar(GUID="GUID123"))
        with self.assertRaises(AuthRequired):
            fetcher.fetch()
        self.assertEqual(client.calls, [])

    def test_signature_http_status_is_reported(self):
        client = FakeClient(
            [
                Response(
                    status=431,
                    url=CTRIP_ENDPOINT,
                    headers={},
                    body=b"Request Header Fields Too Large",
                )
            ]
        )
        fetcher = CtripFetcher(client, self.jar, page_size=2)
        with self.assertRaises(NeedsRiskSignature) as context:
            fetcher.fetch()
        self.assertIn("needs risk signature", str(context.exception))

    def test_only_read_only_endpoints_are_used(self):
        client = FakeClient(self.responses())
        fetcher = CtripFetcher(
            client, self.jar, page_size=2, history_page_size=2, max_pages=10
        )
        fetcher.fetch()
        self.assertEqual(len(client.calls), 4)
        for call in client.calls:
            self.assertIn(
                call["url"], (CTRIP_ENDPOINT, CTRIP_HISTORY_ENDPOINT)
            )
            self.assertNotIn("getAllOrders", call["url"])
            self.assertNotIn("/OrderManagement", call["url"])
            self.assertNotIn("/HandleOrderPayment", call["url"])

    def test_probe_uses_one_read_only_request(self):
        client = FakeClient([json_response(load("ctrip_orders_page2.json"))])
        fetcher = CtripFetcher(client, self.jar, page_size=1, max_pages=1)
        info = fetcher.probe()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["url"], CTRIP_ENDPOINT)
        self.assertEqual(client.calls[0]["payload"]["PageSize"], 1)
        self.assertEqual(info["total"], 3)


class BlockingTests(unittest.TestCase):
    def test_signature_message_becomes_needs_risk_signature(self):
        payload = {
            "Result": {"ResultCode": 1001, "ResultMsg": "签名校验失败"},
            "ResponseStatus": {"Ack": "Failure"},
        }
        with self.assertRaises(NeedsRiskSignature):
            check_ctrip_api(payload)

    def test_login_message_becomes_auth_required(self):
        payload = {
            "Result": {"ResultCode": 1002, "ResultMsg": "用户未登录"},
            "ResponseStatus": {"Ack": "Failure"},
        }
        with self.assertRaises(AuthRequired):
            check_ctrip_api(payload)

    def test_success_payload(self):
        check_ctrip_api(load("ctrip_orders_page1.json"))

    def test_parameter_error_mentions_channel_hint(self):
        payload = {
            "Result": {"ResultCode": -2, "ResultMsg": "request parameter error"},
            "ResponseStatus": {"Ack": "Success"},
        }
        with self.assertRaises(ApiError) as context:
            check_ctrip_api(payload)
        self.assertIn("Channel", context.exception.render())

    def test_http_error_with_success_shaped_body_is_not_silently_empty(self):
        with self.assertRaises(ApiError):
            check_ctrip_api(
                {"Result": {"ResultCode": 0}, "ResponseStatus": {"Ack": "Success"}},
                404,
            )


class CliTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("connectors.kit.runner.provider", return_value=mock.Mock(load=lambda: CookieJar()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dry_run_does_not_touch_network_or_keychain(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             mock.patch("subprocess.run", side_effect=AssertionError("keychain")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                code = main(CONNECTOR, ["fetch", "--dry-run", "--page-size", "10"])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("GetOrderWithBM.json", text)
        self.assertIn("GetHistoryOrderList.json", text)
        self.assertIn("Channel=Online", text)
        self.assertIn("needs risk signature", text)
        self.assertIn("cticket", text)
        self.assertNotIn("secret", text)

    def test_signature_error_exits_4_with_clear_text(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(
                CONNECTOR, "fetch",
                side_effect=NeedsRiskSignature("携程要求风控签名 (needs risk signature)"),
            ):
                errors = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                    code = main(CONNECTOR, ["fetch", "--out", temp])
        self.assertEqual(code, 4)
        self.assertIn("needs risk signature", errors.getvalue())
        self.assertEqual(list(Path(temp).glob("ctrip-orders-*.json")), [])

    def test_writes_one_ctrip_orders_file_with_0600(self):
        payload = {
            "kind": "ctrip_orders",
            "source": CTRIP_ENDPOINT,
            "endpoints": [CTRIP_ENDPOINT, CTRIP_HISTORY_ENDPOINT],
            "pages": [
                {
                    "kind": "recent",
                    "page_index": 1,
                    "page_size": 15,
                    "total": 1,
                    "orders": [
                        {
                            "order_id": "1",
                            "category": "flight",
                            "occurred": "2024-01-01 00:00:00",
                        }
                    ],
                    "raw": "{}",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(
                CONNECTOR, "fetch", return_value=(payload, ["test warning"])
            ):
                output = io.StringIO()
                errors = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = main(CONNECTOR, ["fetch", "--out", temp])
            self.assertEqual(code, 0)
            files = list(Path(temp).glob("ctrip-orders-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(file_mode(files[0]), 0o600)
            saved = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["kind"], "ctrip_orders")
            self.assertIn("orders=1", output.getvalue())
            self.assertIn("by_type: flight=1", output.getvalue())
            self.assertIn("by_source: recent=1 history=0", output.getvalue())
            self.assertIn("test warning", errors.getvalue())


class StatusAndKeepaliveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "Cookies"
        future = chromium_expiry(
            datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
        )
        self.future = future
        make_site_db(
            self.db,
            [
                (".taobao.com", "cookie2", "secret-cookie2", future),
                (".taobao.com", "unb", "secret-unb", future),
                (".jd.com", "pt_key", "secret-pt-key", future),
                (".jd.com", "pt_pin", "secret-pt-pin", future),
                (".ctrip.com", "cticket", "secret-cticket-value", future),
                (".ctrip.com", "GUID", "secret-guid-value", future),
                (".ctrip.com", "_RF1", "secret-rf1-value", 0),
            ],
        )
        self.provider = CookieProvider(
            self.db, password=PASSWORD, host_suffixes=CTRIP.host_suffixes
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_status_reports_cticket_and_expiry_without_values(self):
        inspection = inspect_site(CTRIP, self.provider)
        self.assertFalse(inspection.needs_login)
        names = {check.name for check in inspection.cookies}
        self.assertEqual(names, {"cticket", "GUID", "_RF1", "DUID"})
        lines = "\n".join(render_inspection(inspection))
        self.assertIn("携程", lines)
        self.assertIn("cticket", lines)
        self.assertIn("过期", lines)
        self.assertNotIn("secret-cticket-value", lines)
        self.assertNotIn("secret-guid-value", lines)

    def test_status_expired_cticket_needs_login(self):
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE cookies SET expires_utc=1 WHERE name='cticket'")
        connection.commit()
        connection.close()
        inspection = inspect_site(CTRIP, self.provider)
        self.assertTrue(inspection.needs_login)
        self.assertIn("cticket", inspection.unusable_required)

    def test_inspect_does_not_call_keychain(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("keychain")):
            inspect_site(CTRIP, self.provider)

    def test_keepalive_probe_uses_one_read_only_request(self):
        client = FakeClient([json_response(load("ctrip_orders_page2.json"))])
        result = probe(CTRIP, self.provider, client, check)
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["url"], CTRIP_ENDPOINT)
        self.assertEqual(client.calls[0]["payload"]["PageSize"], 1)
        self.assertIn("GetOrderWithBM", result.detail)

    def test_keepalive_probe_signature_requirement_is_needs_signature(self):
        client = FakeClient(
            [
                Response(
                    status=432,
                    url=CTRIP_ENDPOINT,
                    headers={},
                    body=b"anti-bot",
                )
            ]
        )
        result = probe(CTRIP, self.provider, client, check)
        self.assertEqual(result.status, "needs_signature")
        self.assertIn("needs risk signature", result.detail)

    def test_missing_cticket_is_logged_out_without_request(self):
        connection = sqlite3.connect(self.db)
        connection.execute("DELETE FROM cookies WHERE name='cticket'")
        connection.commit()
        connection.close()
        client = FakeClient()
        result = probe(CTRIP, self.provider, client, check)
        self.assertEqual(result.status, "logged_out")
        self.assertIn("cticket", result.detail)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
