"""Offline tests for the 12306 e-invoice (电子发票) 180-day read-only reader.

Nothing here touches the network, browser's cookie database or the Keychain; the
HTTP client is a fake.  The APP-authorization gate is simulated both ways
(``psr_qr_code_result`` = Y/N and the server's 授权 message) because a real
grant must be done by the owner and is intentionally never triggered here.
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.errors import ApiError, AuthRequired, UsageError
from connectors.kit.http import Response
from connectors.rail12306.reader import (
    BEIJING,
    RAIL_INVOICE_FORBIDDEN_ENDPOINTS,
    RAIL_INVOICE_MAX_REQUESTS,
    RAIL_INVOICE_MIN_INTERVAL,
    RAIL_INVOICE_QUERY_URL,
    RAIL_LOGIN_CONF_URL,
    RailInvoiceFetcher,
    build_invoice_form,
    invoice_date_window,
    invoice_rows,
    invoice_summary,
    parse_invoice_records,
)

NOW = datetime.datetime(2026, 10, 26, 12, 0, tzinfo=BEIJING)


def response(payload=None, *, text=None, status=200, url=RAIL_INVOICE_QUERY_URL,
             set_cookies=None):
    if text is None:
        text = json.dumps(payload, ensure_ascii=False)
    return Response(
        status=status,
        url=url,
        headers={},
        body=text.encode("utf-8"),
        set_cookies=list(set_cookies or []),
    )


class FakeClient:
    user_agent = "pfin-test-agent"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _take(self):
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)

    def post_form(self, url, form, *, cookie_header="", headers=None):
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "form": dict(form),
                "cookie_header": cookie_header,
                "headers": dict(headers or {}),
            }
        )
        return self._take()

    def get(self, url, *, cookie_header="", headers=None):
        raise AssertionError("the invoice reader only uses POST")


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def jar_with(*names):
    jar = CookieJar()
    for name in names:
        jar.add(Cookie(".12306.cn", name, "test-value"))
    return jar


def gate_payload(*, logged_in=True, psr="Y", ei_email=True):
    return {
        "status": True,
        "data": {
            "is_login": "Y" if logged_in else "N",
            "psr_qr_code_result": psr,
            "ei_email": ei_email,
        },
    }


def invoice_record(sequence_no="E123456789", **overrides):
    record = {
        "create_time": "2026-04-20 10:00:00",
        "sequence_no": sequence_no,
        "board_train_code": "G1",
        "from_station_name": "北京南",
        "to_station_name": "上海虹桥",
        "local_start_time": "2026-04-25 08:00:00",
        "start_time": "08:00",
        "passenger_name": "张三",
        "sale_price": "553.00",
        "return_serial_price": "0.00",
        "resign_serial_price": "0.00",
        "einvoice_status": "待开票",
        "invoice_state": "0",
    }
    record.update(overrides)
    return record


def page_payload(records, total):
    return {"status": True, "data": {"results": list(records), "total": total}}


def records(count, *, prefix="E100", start=0):
    return [
        invoice_record(
            sequence_no=f"{prefix}{start + index}",
            local_start_time="2026-04-%02d 08:00:00" % (1 + (index % 28)),
        )
        for index in range(count)
    ]


class InvoiceFormTests(unittest.TestCase):
    def test_form_matches_the_page(self):
        form = build_invoice_form(
            "2026-04-30", "2026-10-26", 1, invoice_type="2"
        )
        self.assertEqual(
            form,
            {
                "from_date": "20260430",
                "end_date": "20261026",
                "order_num": "",
                "pageIndex": "1",
                "ticket_type": "",
                "type": "2",
            },
        )

    def test_form_accepts_compact_dates(self):
        form = build_invoice_form("20260430", "20261026", 2)
        self.assertEqual(form["from_date"], "20260430")
        self.assertEqual(form["end_date"], "20261026")
        self.assertEqual(form["pageIndex"], "2")

    def test_forbidden_endpoints_are_listed(self):
        # Auditability: the write/download/mail endpoints of the same bundle.
        for name in ("npRecordVoucher", "getImg", "sendEmail", "deleteInvoiceHeader"):
            self.assertTrue(
                any(name in endpoint for endpoint in RAIL_INVOICE_FORBIDDEN_ENDPOINTS)
            )


class InvoiceWindowTests(unittest.TestCase):
    def test_default_window_is_180_days_ending_today(self):
        window = invoice_date_window(NOW)
        self.assertEqual(window["start_date"], "2026-04-30")
        self.assertEqual(window["end_date"], "2026-10-26")
        self.assertEqual(window["days"], 180)
        self.assertTrue(window["queried"])
        self.assertEqual(window["oldest_allowed"], "2026-04-30")
        self.assertEqual(window["newest_allowed"], "2026-10-26")

    def test_old_start_is_clamped_with_warning(self):
        window = invoice_date_window(NOW, "2025-01-01", "2026-10-01")
        self.assertEqual(window["start_date"], "2026-04-30")
        self.assertEqual(window["end_date"], "2026-10-01")
        self.assertTrue(any("下限" in w for w in window["warnings"]))

    def test_future_end_is_clamped_to_today(self):
        window = invoice_date_window(NOW, None, "2026-12-31")
        self.assertEqual(window["end_date"], "2026-10-26")
        self.assertTrue(any("晚于今天" in w for w in window["warnings"]))

    def test_policy_floor_never_moves_before_2024_11_01(self):
        old_today = datetime.datetime(2025, 1, 10, 12, 0, tzinfo=BEIJING)
        window = invoice_date_window(old_today)
        self.assertEqual(window["oldest_allowed"], "2024-11-01")

    def test_no_overlap_sends_nothing(self):
        window = invoice_date_window(NOW, "2024-01-01", "2024-06-01")
        self.assertFalse(window["queried"])
        self.assertIsNone(window["start_date"])
        self.assertTrue(any("没有交集" in w for w in window["warnings"]))

    def test_backwards_range_is_a_usage_error(self):
        with self.assertRaises(UsageError):
            invoice_date_window(NOW, "2026-10-01", "2026-09-01")


class InvoiceGateTests(unittest.TestCase):
    def fetcher(self, client, **kwargs):
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("min_interval", 0.0)
        return RailInvoiceFetcher(client, jar_with("tk", "JSESSIONID"), **kwargs)

    def test_authorized_gate_reports_flags_without_cookie_values(self):
        client = FakeClient(
            [response(gate_payload(), url=RAIL_LOGIN_CONF_URL)]
        )
        info = self.fetcher(client).check_gate()
        self.assertTrue(info["logged_in"])
        self.assertTrue(info["psr_authorized"])
        self.assertTrue(info["ei_email"])
        self.assertEqual(info["http_status"], 200)
        self.assertIsInstance(info["elapsed_ms"], int)
        self.assertNotIn("test-value", json.dumps(info))
        self.assertEqual(client.calls[0]["url"], RAIL_LOGIN_CONF_URL)
        self.assertEqual(client.calls[0]["form"], {})

    def test_not_authorized_is_reported_not_raised(self):
        client = FakeClient([response(gate_payload(psr="N"), url=RAIL_LOGIN_CONF_URL)])
        info = self.fetcher(client).check_gate()
        self.assertTrue(info["logged_in"])
        self.assertFalse(info["psr_authorized"])
        self.assertEqual(info["psr_qr_code_result"], "N")

    def test_not_logged_in_raises_auth_required(self):
        client = FakeClient(
            [response(gate_payload(logged_in=False), url=RAIL_LOGIN_CONF_URL)]
        )
        with self.assertRaises(AuthRequired):
            self.fetcher(client).check_gate()

    def test_boolean_flags_are_understood(self):
        client = FakeClient(
            [
                response(
                    {"status": True, "data": {"is_login": True, "psr_qr_code_result": True}},
                    url=RAIL_LOGIN_CONF_URL,
                )
            ]
        )
        info = self.fetcher(client).check_gate()
        self.assertTrue(info["logged_in"])
        self.assertTrue(info["psr_authorized"])

    def test_logged_out_json_raises_auth_required(self):
        client = FakeClient(
            [
                response(
                    {"status": False, "messages": ["未登录"]},
                    url=RAIL_LOGIN_CONF_URL,
                )
            ]
        )
        with self.assertRaises(AuthRequired):
            self.fetcher(client).check_gate()

    def test_missing_tk_sends_no_request(self):
        client = FakeClient([])
        fetcher = RailInvoiceFetcher(client, jar_with("JSESSIONID"), now=NOW)
        with self.assertRaises(AuthRequired):
            fetcher.fetch()
        self.assertEqual(client.calls, [])


class InvoiceFetchTests(unittest.TestCase):
    def fetcher(self, client, jar=None, **kwargs):
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("min_interval", 0.0)
        return RailInvoiceFetcher(client, jar or jar_with("tk"), **kwargs)

    def test_fails_closed_when_app_authorization_missing(self):
        client = FakeClient([response(gate_payload(psr="N"), url=RAIL_LOGIN_CONF_URL)])
        fetcher = self.fetcher(client)
        with self.assertRaises(AuthRequired) as raised:
            fetcher.fetch()
        self.assertIn("授权", raised.exception.message)
        self.assertIn("APP", raised.exception.hint)
        self.assertIn("授权", raised.exception.hint)
        # Fail-closed: only the gate request, never queryPsr.
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["url"], RAIL_LOGIN_CONF_URL)
        self.assertEqual(fetcher.requests_sent, 1)

    def test_three_pages_until_total(self):
        client = FakeClient(
            [
                response(gate_payload(), url=RAIL_LOGIN_CONF_URL),
                response(page_payload(records(10), 25)),
                response(page_payload(records(10, start=10), 25)),
                response(page_payload(records(5, start=20), 25)),
            ]
        )
        fetcher = self.fetcher(client)
        payload = fetcher.fetch()
        query = payload["query"]
        self.assertEqual(query["requested_start_date"], "2026-04-30")
        self.assertEqual(query["start_date"], "2026-04-30")
        self.assertEqual(query["end_date"], "2026-10-26")
        self.assertEqual(query["window"]["days"], 180)
        self.assertEqual(query["total"], 25)
        self.assertEqual(query["collected"], 25)
        self.assertEqual(query["pages"], 3)
        self.assertEqual(query["windows"], 1)
        self.assertEqual(query["requests_sent"], 4)  # gate + 3 pages
        self.assertFalse(query["budget_exhausted"])
        self.assertEqual(len(payload["pages"]), 3)
        self.assertEqual(
            [call["form"].get("pageIndex", "") for call in client.calls],
            ["", "1", "2", "3"],
        )
        for call in client.calls[1:]:
            self.assertEqual(call["url"], RAIL_INVOICE_QUERY_URL)
            self.assertEqual(call["form"]["type"], "2")
            self.assertEqual(call["form"]["from_date"], "20260430")
            self.assertEqual(call["form"]["end_date"], "20261026")
            self.assertEqual(call["form"]["ticket_type"], "")
            self.assertEqual(call["form"]["order_num"], "")
        self.assertIn('"results"', payload["pages"][0]["raw"])
        self.assertEqual(len(payload["pages"][0]["records"]), 10)

    def test_empty_page_before_total_stops_without_retry(self):
        client = FakeClient(
            [
                response(gate_payload(), url=RAIL_LOGIN_CONF_URL),
                response(page_payload(records(10), 25)),
                response(page_payload(records(10, start=10), 25)),
                response(page_payload([], 25)),
            ]
        )
        fetcher = self.fetcher(client)
        payload = fetcher.fetch()
        self.assertEqual(len(client.calls), 4)  # gate + 3 list calls, no retry
        self.assertEqual(payload["query"]["collected"], 20)
        self.assertEqual(payload["query"]["total"], 25)
        self.assertTrue(any("提前停止" in w for w in fetcher.warnings))

    def test_old_start_is_clamped_and_form_never_earlier(self):
        client = FakeClient(
            [
                response(gate_payload(), url=RAIL_LOGIN_CONF_URL),
                response(page_payload(records(2), 2)),
            ]
        )
        fetcher = self.fetcher(client)
        payload = fetcher.fetch("2025-01-01", "2026-10-01")
        self.assertEqual(payload["query"]["start_date"], "2026-04-30")
        self.assertEqual(client.calls[1]["form"]["from_date"], "20260430")
        self.assertTrue(any("下限" in w for w in fetcher.warnings))

    def test_future_end_is_clamped_and_form_never_later(self):
        client = FakeClient(
            [
                response(gate_payload(), url=RAIL_LOGIN_CONF_URL),
                response(page_payload(records(1), 1)),
            ]
        )
        fetcher = self.fetcher(client)
        payload = fetcher.fetch(None, "2026-12-31")
        self.assertEqual(payload["query"]["end_date"], "2026-10-26")
        self.assertEqual(client.calls[1]["form"]["end_date"], "20261026")
        self.assertTrue(any("晚于今天" in w for w in fetcher.warnings))

    def test_no_overlap_sends_no_request_at_all(self):
        client = FakeClient([])
        fetcher = self.fetcher(client)
        payload = fetcher.fetch("2024-01-01", "2024-06-01")
        self.assertEqual(client.calls, [])
        self.assertFalse(payload["query"]["queried"])
        self.assertEqual(payload["query"]["requests_sent"], 0)
        self.assertEqual(payload["pages"], [])
        self.assertIsNone(payload["query"]["gate"])
        self.assertTrue(any("没有交集" in w for w in fetcher.warnings))

    def test_compact_dates_are_accepted(self):
        client = FakeClient(
            [
                response(gate_payload(), url=RAIL_LOGIN_CONF_URL),
                response(page_payload([], 0)),
            ]
        )
        fetcher = self.fetcher(client)
        fetcher.fetch("20260601", "20260630")
        self.assertEqual(client.calls[1]["form"]["from_date"], "20260601")
        self.assertEqual(client.calls[1]["form"]["end_date"], "20260630")

    def test_server_authorization_message_raises_auth_required(self):
        client = FakeClient(
            [
                response(
                    {
                        "status": False,
                        "messages": ["当前用户未通过铁路12306APP授权查询"],
                    }
                )
            ]
        )
        fetcher = self.fetcher(client)
        with self.assertRaises(AuthRequired) as raised:
            fetcher.fetch(gate={"logged_in": True, "psr_authorized": True})
        self.assertIn("授权", raised.exception.message)
        self.assertIn("APP", raised.exception.hint)
        self.assertEqual(len(client.calls), 1)  # stopped, no retry

    def test_precomputed_gate_skips_login_conf(self):
        client = FakeClient([response(page_payload(records(1), 1))])
        fetcher = self.fetcher(client)
        payload = fetcher.fetch(gate={"logged_in": True, "psr_authorized": True})
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["url"], RAIL_INVOICE_QUERY_URL)
        self.assertEqual(payload["query"]["requests_sent"], 1)

    def test_error_page_stops_without_retry(self):
        client = FakeClient(
            [
                response(
                    text="\ufeff<html><body>网络可能存在问题，请您重试一下！</body></html>",
                    url="https://kyfw.12306.cn/otn/error/error.html",
                )
            ]
        )
        fetcher = self.fetcher(client)
        with self.assertRaises(ApiError) as raised:
            fetcher.fetch(gate={"logged_in": True, "psr_authorized": True})
        self.assertIn("错误页", raised.exception.message)
        self.assertEqual(len(client.calls), 1)

    def test_non_json_stops_without_retry(self):
        client = FakeClient([response(text="gateway timeout")])
        fetcher = self.fetcher(client)
        with self.assertRaises(ApiError):
            fetcher.fetch(gate={"logged_in": True, "psr_authorized": True})
        self.assertEqual(len(client.calls), 1)

    def test_request_budget_stops_pagination(self):
        pages = [response(page_payload(records(10, start=index * 10), 100))
                 for index in range(3)]
        client = FakeClient(pages)
        fetcher = self.fetcher(client, max_requests=2)
        payload = fetcher.fetch(gate={"logged_in": True, "psr_authorized": True})
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(payload["query"]["budget_exhausted"])
        self.assertEqual(payload["query"]["requests_sent"], 2)
        self.assertTrue(any("请求上限" in w for w in fetcher.warnings))

    def test_max_pages_stops_pagination(self):
        client = FakeClient(
            [
                response(page_payload(records(10), 100)),
                response(page_payload(records(10, start=10), 100)),
            ]
        )
        fetcher = self.fetcher(client, max_pages=1)
        payload = fetcher.fetch(gate={"logged_in": True, "psr_authorized": True})
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(payload["query"]["pages"], 1)
        self.assertTrue(any("最大页数" in w for w in fetcher.warnings))

    def test_requests_are_spaced_by_the_configured_interval(self):
        clock = FakeClock()
        client = FakeClient(
            [
                response(gate_payload(), url=RAIL_LOGIN_CONF_URL),
                response(page_payload(records(10), 20)),
                response(page_payload(records(10, start=10), 20)),
            ]
        )
        fetcher = self.fetcher(
            client, min_interval=RAIL_INVOICE_MIN_INTERVAL,
            clock=clock.clock, sleep=clock.sleep,
        )
        fetcher.fetch()
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(clock.sleeps), 2)
        for seconds in clock.sleeps:
            self.assertGreaterEqual(seconds, RAIL_INVOICE_MIN_INTERVAL - 1e-9)

    def test_default_limits(self):
        self.assertEqual(RAIL_INVOICE_MIN_INTERVAL, 2.0)
        self.assertEqual(RAIL_INVOICE_MAX_REQUESTS, 8)

    def test_issued_tab_uses_type_one(self):
        client = FakeClient(
            [
                response(gate_payload(), url=RAIL_LOGIN_CONF_URL),
                response(page_payload(records(1), 1)),
            ]
        )
        fetcher = self.fetcher(client, invoice_type="1")
        payload = fetcher.fetch()
        self.assertEqual(client.calls[1]["form"]["type"], "1")
        self.assertEqual(payload["query"]["type_label"], "issued")
        self.assertEqual(payload["query"]["type"], "1")

    def test_cookie_rotations_are_remembered_in_memory(self):
        client = FakeClient(
            [
                response(
                    gate_payload(),
                    url=RAIL_LOGIN_CONF_URL,
                    set_cookies=["JSESSIONID=NEW; Path=/otn; HttpOnly"],
                ),
                response(page_payload([], 0)),
            ]
        )
        jar = jar_with("tk", "JSESSIONID")
        fetcher = self.fetcher(client, jar=jar)
        fetcher.fetch()
        self.assertEqual(jar.get("JSESSIONID", "kyfw.12306.cn"), "NEW")


class InvoiceRowTests(unittest.TestCase):
    def rows_payload(self, records_list, *, invoice_type="2"):
        return {
            "kind": "rail_12306_invoice",
            "query": {"type": invoice_type},
            "pages": [
                {"page": 1, "records": list(records_list), "raw": "{}"},
            ],
        }

    def test_basic_mapping(self):
        rows = invoice_rows(self.rows_payload([invoice_record()]))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            set(row),
            {
                "source",
                "record_type",
                "id",
                "occurred",
                "amount",
                "direction",
                "merchant",
                "title",
                "status",
                "extra",
            },
        )
        self.assertEqual(row["source"], "12306")
        self.assertEqual(row["record_type"], "trip")
        self.assertEqual(row["id"], "12306:invoice:E123456789")
        self.assertEqual(row["occurred"], "2026-04-25 08:00:00")
        self.assertEqual(row["amount"], "553.00")
        self.assertEqual(row["direction"], "out")
        self.assertEqual(row["merchant"], "中国铁路 12306")
        self.assertEqual(row["title"], "G1 北京南→上海虹桥")
        self.assertEqual(row["status"], "待开票")
        self.assertEqual(row["extra"]["amount_source"], "sale_price")
        self.assertEqual(row["extra"]["invoice_type"], "2")
        self.assertEqual(row["extra"]["board_train_code"], "G1")
        self.assertEqual(row["extra"]["from_station_name"], "北京南")
        self.assertEqual(row["extra"]["to_station_name"], "上海虹桥")
        self.assertEqual(row["extra"]["einvoice_status"], "待开票")

    def test_id_is_stable_across_runs(self):
        payload = self.rows_payload([invoice_record()])
        first = invoice_rows(payload)[0]["id"]
        second = invoice_rows(payload)[0]["id"]
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("12306:invoice:"))

    def test_amount_falls_back_and_is_annotated(self):
        record = invoice_record(sale_price="", return_serial_price="174.50")
        row = invoice_rows(self.rows_payload([record]))[0]
        self.assertEqual(row["amount"], "174.50")
        self.assertEqual(row["extra"]["amount_source"], "return_serial_price")
        record = invoice_record(
            sale_price=None, return_serial_price=None, resign_serial_price="88"
        )
        row = invoice_rows(self.rows_payload([record]))[0]
        self.assertEqual(row["amount"], "88.00")
        self.assertEqual(row["extra"]["amount_source"], "resign_serial_price")

    def test_missing_amount_and_sequence_still_yield_a_stable_row(self):
        record = invoice_record(
            sequence_no=None,
            sale_price=None,
            return_serial_price=None,
            resign_serial_price=None,
        )
        payload = self.rows_payload([record])
        first = invoice_rows(payload)
        second = invoice_rows(payload)
        self.assertEqual(len(first), 1)
        self.assertIsNone(first[0]["amount"])
        self.assertTrue(first[0]["id"].startswith("12306:invoice:h:"))
        self.assertEqual(first[0]["id"], second[0]["id"])

    def test_clock_only_start_time_does_not_hide_create_time(self):
        row = invoice_rows(
            self.rows_payload(
                [
                    invoice_record(
                        local_start_time=None,
                        start_time="08:00",
                        create_time="2026-04-20 10:30:00",
                    )
                ]
            )
        )[0]
        self.assertEqual(row["occurred"], "2026-04-20 10:30:00")

    def test_multi_passenger_same_order_keeps_every_ticket(self):
        payload = self.rows_payload(
            [
                invoice_record(sequence_no="E9", passenger_name="张三"),
                invoice_record(
                    sequence_no="E9", passenger_name="李四", sale_price="174.50"
                ),
            ]
        )
        rows = invoice_rows(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"].split(":")[2], "E9")
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        # Stable across re-runs.
        again = invoice_rows(payload)
        self.assertEqual([r["id"] for r in rows], [r["id"] for r in again])

    def test_single_ticket_keeps_the_documented_id_format(self):
        rows = invoice_rows(self.rows_payload([invoice_record(sequence_no="E7")]))
        self.assertEqual(rows[0]["id"], "12306:invoice:E7")
        self.assertEqual(rows[0]["extra"]["order_row_id"], "12306:trip:E7")

    def test_occurred_fallbacks_and_compact_dates(self):
        row = invoice_rows(
            self.rows_payload([invoice_record(local_start_time="20260425")])
        )[0]
        self.assertEqual(row["occurred"], "2026-04-25 00:00:00")
        row = invoice_rows(
            self.rows_payload(
                [
                    invoice_record(
                        local_start_time=None,
                        start_time=None,
                        create_time="2026-04-20 10:30:00",
                    )
                ]
            )
        )[0]
        self.assertEqual(row["occurred"], "2026-04-20 10:30:00")

    def test_rows_dedupe_across_overlapping_pages(self):
        record = invoice_record()
        payload = {
            "query": {"type": "2"},
            "pages": [
                {"page": 1, "records": [record]},
                {"page": 2, "records": [dict(record)]},
            ],
        }
        rows = invoice_rows(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(parse_invoice_records(payload["pages"])), 1)

    def test_rows_from_raw_json_string(self):
        record = invoice_record()
        payload = {
            "query": {"type": "2"},
            "pages": [
                {"page": 1, "raw": json.dumps(page_payload([record], 1), ensure_ascii=False)}
            ],
        }
        rows = invoice_rows(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "12306:invoice:E123456789")

    def test_empty_payload_yields_no_rows(self):
        self.assertEqual(invoice_rows({}), [])
        self.assertEqual(invoice_rows({"pages": []}), [])
        self.assertEqual(
            invoice_rows({"pages": [{"records": [None, "x"], "raw": "not-json"}]}),
            [],
        )

    def test_rows_shape_for_the_ledger(self):
        payload = self.rows_payload(
            [
                invoice_record(sequence_no="E1", sale_price="553.00"),
                invoice_record(sequence_no="E2", sale_price="174.50", local_start_time="2026-05-02 09:00:00"),
            ]
        )
        rows = invoice_rows(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["source"] for row in rows}, {"12306"})
        self.assertEqual({row["record_type"] for row in rows}, {"trip"})
        self.assertEqual({row["direction"] for row in rows}, {"out"})
        self.assertEqual(sorted(row["occurred"] for row in rows), ["2026-04-25 08:00:00", "2026-05-02 09:00:00"])
        self.assertEqual(invoice_rows(payload), rows)  # stable ids: same payload, same rows

    def test_fetched_payload_flows_into_rows(self):
        client = FakeClient(
            [
                response(gate_payload(), url=RAIL_LOGIN_CONF_URL),
                response(page_payload([invoice_record()], 1)),
            ]
        )
        fetcher = RailInvoiceFetcher(client, jar_with("tk"), now=NOW, min_interval=0.0)
        rows = invoice_rows(fetcher.fetch())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "12306:invoice:E123456789")

    def test_summary_reports_gate_and_counts_without_personal_data(self):
        payload = self.rows_payload([invoice_record()])
        payload["query"].update(
            {
                "total": 1,
                "requests_sent": 2,
                "pages": 1,
                "queried": True,
                "gate": {
                    "logged_in": True,
                    "psr_authorized": True,
                    "ei_email": True,
                },
                "window": {"start_date": "2026-04-30", "end_date": "2026-10-26", "days": 180},
            }
        )
        summary = invoice_summary(payload)
        self.assertEqual(summary["records"], 1)
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["gate"]["psr_authorized"], True)
        self.assertEqual(summary["window"]["days"], 180)
        self.assertNotIn("张三", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()

