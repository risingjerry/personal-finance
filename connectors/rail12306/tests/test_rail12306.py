import contextlib
import datetime
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.rail12306.blocking import check_rail12306_html, check_rail12306_json
from connectors.kit.runner import main as run_main
from connectors.rail12306 import CONNECTOR, SPEC as RAIL, check as rail_check
from connectors.kit.cookies import Cookie, CookieJar, CookieProvider
from connectors.kit.errors import ApiError, AuthRequired, NeedsRiskSignature, UsageError
from connectors.kit.spec import inspect_site, probe
from connectors.kit.testing import mock_sessions
from connectors.kit.http import Response
from connectors.kit.output import file_mode
from connectors.rail12306.reader import (
    BEIJING,
    RAIL_HOST,
    RAIL_MAX_SPAN_DAYS,
    RAIL_ORDER_ENDPOINT,
    RAIL_ORDER_PAGE_URL,
    RAIL_RETENTION_DAYS,
    Rail12306Fetcher,
    allowed_date_bounds,
    build_order_form,
    date_range,
    normalize_amount,
    normalize_datetime,
    parse_order,
    parse_orders,
    plan_query_windows,
    resolve_date_range,
    summarize,
)

NOW = datetime.datetime(2026, 10, 26, 12, 0, tzinfo=BEIJING)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def response(payload=None, *, text=None, status=200, url=RAIL_ORDER_ENDPOINT,
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
        self.calls.append(
            {
                "method": "GET",
                "url": url,
                "cookie_header": cookie_header,
                "headers": dict(headers or {}),
            }
        )
        return self._take()


def jar_with(*names):
    jar = CookieJar()
    for name in names:
        jar.add(Cookie(".12306.cn", name, "test-value"))
    return jar


def chromium_micros(moment):
    base = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
    return int((moment - base).total_seconds() * 1_000_000)


def make_cookie_db(path, rows):
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE cookies (
                host_key TEXT NOT NULL,
                name TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                encrypted_value BLOB NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '/',
                expires_utc INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        for host_key, name, value, expires_utc in rows:
            connection.execute(
                "INSERT INTO cookies "
                "(host_key, name, value, encrypted_value, path, expires_utc) "
                "VALUES (?, ?, ?, ?, '/', ?)",
                (host_key, name, value, b"", expires_utc),
            )
        connection.commit()
    finally:
        connection.close()
    return path


class RailRequestTests(unittest.TestCase):
    def test_form_matches_bundle(self):
        form = build_order_form(
            0, "2026-09-01", "2026-09-30", page_size=8, query_type="1"
        )
        self.assertEqual(
            form,
            {
                "come_from_flag": "my_order",
                "pageIndex": "0",
                "pageSize": "8",
                "query_where": "H",
                "queryStartDate": "2026-09-01",
                "queryEndDate": "2026-09-30",
                "queryType": "1",
                "sequeue_train_name": "",
            },
        )
        self.assertNotIn("_json_att", form)

    def test_page_index_is_zero_based(self):
        self.assertEqual(
            build_order_form(2, "2026-09-01", "2026-09-30")["pageIndex"], "2"
        )


class RailParsingTests(unittest.TestCase):
    def test_amounts_and_dates(self):
        self.assertEqual(normalize_amount("¥553.00"), "553.00")
        self.assertEqual(normalize_amount("1,234.50元"), "1234.50")
        self.assertEqual(normalize_amount("￥174.5"), "174.5")
        self.assertIsNone(normalize_amount(""))
        self.assertIsNone(normalize_amount(None))
        self.assertEqual(
            normalize_datetime("2026-09-20 09:00"), "2026-09-20 09:00:00"
        )
        self.assertEqual(
            normalize_datetime("2026-09-20"), "2026-09-20 00:00:00"
        )
        self.assertIsNone(normalize_datetime("not-a-date"))

    def test_orders_and_fares(self):
        orders = parse_orders(
            [fixture("rail12306_page1.json"), fixture("rail12306_page2.json")]
        )
        self.assertEqual(len(orders), 2)
        self.assertEqual(
            date_range(orders),
            ("2026-09-18 10:12:00", "2026-09-21 08:30:00"),
        )
        first = orders[0]
        self.assertEqual(first["order_id"], "E123456789")
        self.assertEqual(first["paid"], "553.00")
        self.assertEqual(first["tickets"][0]["train_code"], "G1")
        self.assertEqual(first["tickets"][0]["from_station"], "北京南")
        self.assertEqual(first["tickets"][0]["passenger"], "张三")
        second = orders[1]
        self.assertEqual(second["paid"], "349.00")
        self.assertEqual([t["passenger"] for t in second["tickets"]], ["李四", "王五"])
        self.assertEqual(
            [t["price"] for t in second["tickets"]], ["174.50", "174.50"]
        )

    def test_order_total_falls_back_to_ticket_sum(self):
        raw = fixture("rail12306_page2.json")["data"]["OrderDTODataList"][0]
        raw = dict(raw)
        raw.pop("ticket_total_price_page")
        parsed = parse_order(raw)
        self.assertEqual(parsed["paid"], "349.00")

    def test_summary(self):
        payload = {
            "kind": "rail_12306",
            "pages": [
                {
                    "orders": parse_orders(
                        [
                            fixture("rail12306_page1.json"),
                            fixture("rail12306_page2.json"),
                        ]
                    )
                }
            ],
        }
        summary = summarize(payload)
        self.assertEqual(summary["pages"], 1)
        self.assertEqual(summary["orders"], 2)
        self.assertEqual(summary["date_range"][0], "2026-09-18 10:12:00")

    def test_summary_reports_no_query_for_out_of_horizon_range(self):
        summary = summarize(
            {"kind": "rail_12306", "query": {"queried": False}, "windows": [], "pages": []}
        )
        self.assertEqual(summary["pages"], 0)
        self.assertEqual(summary["windows"], 0)
        self.assertFalse(summary["queried"])
        self.assertEqual(summary["orders"], 0)
        self.assertIsNone(summary["date_range"])


class RailFetchTests(unittest.TestCase):
    """Fetcher tests: each one gets a fresh archive state and no real sleeping."""

    def setUp(self):
        self._state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_tmp.cleanup)

    def fetcher(self, client, jar, **kwargs):
        kwargs.setdefault("state_dir", self._state_tmp.name)
        kwargs.setdefault("min_interval", 0.0)
        kwargs.setdefault("now", NOW)
        return Rail12306Fetcher(client, jar, **kwargs)

    def test_fetch_paginates_until_total(self):
        page1 = fixture("rail12306_page1.json")
        page2 = fixture("rail12306_page2.json")
        client = FakeClient([response(page1), response(page2)])
        fetcher = self.fetcher(
            client,
            jar_with("tk", "JSESSIONID"),
            max_pages=5,
            page_size=1,
            start_date="2026-09-01",
            end_date="2026-09-30",
            now=NOW,
        )
        payload = fetcher.fetch()
        self.assertEqual(payload["kind"], "rail_12306")
        self.assertEqual(payload["source"], RAIL_ORDER_ENDPOINT)
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual([c["form"]["pageIndex"] for c in client.calls], ["0", "1"])
        self.assertEqual(client.calls[0]["method"], "POST")
        self.assertEqual(
            client.calls[0]["headers"]["X-Requested-With"], "XMLHttpRequest"
        )
        self.assertEqual(client.calls[0]["form"]["query_where"], "H")
        self.assertEqual(
            payload["pages"][0]["raw"],
            json.dumps(page1, ensure_ascii=False),
        )
        self.assertEqual(len(parse_orders(payload["pages"])), 2)

    def test_fetch_clamps_form_to_retention_horizon(self):
        client = FakeClient([response(fixture("rail12306_empty.json"))])
        fetcher = self.fetcher(
            client,
            jar_with("tk"),
            start_date="2026-08-27",
            end_date="2026-10-25",
            now=NOW,
        )
        payload = fetcher.fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["form"]["queryStartDate"], "2026-09-26")
        self.assertEqual(client.calls[0]["form"]["queryEndDate"], "2026-10-25")
        self.assertEqual(
            payload["windows"],
            [
                {
                    "window": 1,
                    "start_date": "2026-09-26",
                    "end_date": "2026-10-25",
                    "pages": 1,
                    "orders": 0,
                    "order_total_number": 0,
                }
            ],
        )
        self.assertTrue(payload["query"]["queried"])

    def test_fetch_skips_range_older_than_retention_without_request(self):
        client = FakeClient([])
        fetcher = self.fetcher(
            client,
            jar_with("tk"),
            start_date="2026-08-27",
            end_date="2026-09-25",
            now=NOW,
        )
        payload = fetcher.fetch()
        self.assertEqual(client.calls, [])
        self.assertEqual(payload["pages"], [])
        self.assertEqual(payload["windows"], [])
        self.assertFalse(payload["query"]["queried"])
        self.assertTrue(any("没有交集" in warning for warning in fetcher.warnings))

    def test_fetch_iterates_multiple_windows_when_horizon_is_longer(self):
        # Pins the legacy (non-incremental) clamp planner: newest-first windows
        # anchored at the requested end date.  Incremental gap planning is
        # covered by tests/test_rail_incremental.py.
        client = FakeClient(
            [response(fixture("rail12306_empty.json")) for _ in range(3)]
        )
        fetcher = self.fetcher(
            client,
            jar_with("tk"),
            start_date="2026-07-29",
            end_date="2026-10-25",
            now=NOW,
            retention_days=90,
            incremental=False,
        )
        payload = fetcher.fetch()
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(
            [
                (call["form"]["queryStartDate"], call["form"]["queryEndDate"])
                for call in client.calls
            ],
            [
                ("2026-09-26", "2026-10-25"),
                ("2026-08-27", "2026-09-25"),
                ("2026-07-29", "2026-08-26"),
            ],
        )
        self.assertEqual([call["form"]["pageIndex"] for call in client.calls], ["0", "0", "0"])
        self.assertEqual(len(payload["windows"]), 3)
        self.assertEqual([page["page"] for page in payload["pages"]], [1, 2, 3])
        self.assertEqual([page["window"] for page in payload["pages"]], [1, 2, 3])
        self.assertTrue(payload["query"]["queried"])

    def test_fetch_stops_on_first_empty_page(self):
        client = FakeClient([response(fixture("rail12306_empty.json"))])
        fetcher = self.fetcher(
            client,
            jar_with("tk"),
            start_date="2026-09-01",
            end_date="2026-09-30",
            now=NOW,
        )
        payload = fetcher.fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(payload["pages"][0]["orders"], [])

    def test_fetch_warns_at_max_pages(self):
        client = FakeClient([response(fixture("rail12306_page1.json"))])
        fetcher = self.fetcher(
            client,
            jar_with("tk"),
            max_pages=1,
            page_size=1,
            start_date="2026-09-01",
            end_date="2026-09-30",
            now=NOW,
        )
        fetcher.fetch()
        self.assertTrue(any("最大页数" in warning for warning in fetcher.warnings))

    def test_missing_tk_fails_before_any_request(self):
        client = FakeClient([])
        fetcher = self.fetcher(
            client,
            jar_with("JSESSIONID"),
            start_date="2026-09-01",
            end_date="2026-09-30",
            now=NOW,
        )
        with self.assertRaises(AuthRequired) as raised:
            fetcher.fetch()
        self.assertIn("tk", raised.exception.message)
        self.assertEqual(client.calls, [])

    def test_risk_response_needs_signature(self):
        client = FakeClient([response(fixture("rail12306_risk.json"))])
        fetcher = self.fetcher(
            client,
            jar_with("tk"),
            start_date="2026-09-01",
            end_date="2026-09-30",
            now=NOW,
        )
        with self.assertRaises(NeedsRiskSignature) as raised:
            fetcher.fetch()
        self.assertIn("needs risk signature", raised.exception.message)
        self.assertIn("不会伪造", raised.exception.hint)

    def test_bom_prefixed_html_is_reported_as_html(self):
        client = FakeClient(
            [response(text="\ufeff<html><body>系统维护中</body></html>")]
        )
        fetcher = self.fetcher(
            client,
            jar_with("tk"),
            start_date="2026-09-26",
            end_date="2026-10-25",
            now=NOW,
        )
        with self.assertRaises(ApiError) as raised:
            fetcher.fetch()
        self.assertIn("HTML", raised.exception.message)
        self.assertIn("系统维护中", raised.exception.message)

    def test_logged_out_json(self):
        with self.assertRaises(AuthRequired):
            check_rail12306_json(fixture("rail12306_logged_out.json"))

    def test_error_page_reports_a_safe_preview(self):
        with self.assertRaises(ApiError) as raised:
            check_rail12306_html(
                "<html><body><h1>查询失败</h1><p>操作过于频繁，请稍后再试</p></body></html>",
                "https://kyfw.12306.cn/otn/error/error.html",
                200,
            )
        self.assertIn("错误页", raised.exception.message)
        self.assertIn("查询失败", raised.exception.message)
        self.assertIn("操作过于频繁", raised.exception.message)

    def test_error_page_preview_never_echoes_cookies(self):
        with self.assertRaises(ApiError) as raised:
            check_rail12306_html(
                "<html><body>JSESSIONID=SECRET; tk=SECRET; 查询失败</body></html>",
                "https://kyfw.12306.cn/otn/error/error.html",
                200,
            )
        self.assertNotIn("SECRET", raised.exception.message)

    def test_probe_ok_and_cookie_refresh(self):
        client = FakeClient(
            [
                response(
                    text="<html><body>我的订单</body></html>",
                    url=RAIL_ORDER_PAGE_URL,
                    set_cookies=["JSESSIONID=NEW; Path=/otn; HttpOnly"],
                )
            ]
        )
        jar = jar_with("tk", "JSESSIONID")
        fetcher = self.fetcher(client, jar)
        info = fetcher.probe()
        self.assertTrue(info["logged_in"])
        self.assertEqual(jar.get("JSESSIONID", RAIL_HOST), "NEW")
        self.assertEqual(client.calls[0]["method"], "GET")

    def test_probe_redirect_to_login(self):
        login_html = (FIXTURES / "rail12306_login.html").read_text(encoding="utf-8")
        client = FakeClient(
            [
                response(
                    text=login_html,
                    url="https://kyfw.12306.cn/otn/passport?redirect=/otn/queryOrder/queryMyOrder.html",
                )
            ]
        )
        fetcher = self.fetcher(client, jar_with("tk"))
        with self.assertRaises(AuthRequired):
            fetcher.probe()


class RailDateTests(unittest.TestCase):
    def test_default_range_is_30_days(self):
        self.assertEqual(
            resolve_date_range(None, None, now=NOW),
            ("2026-09-26", "2026-10-25"),
        )

    def test_explicit_range(self):
        self.assertEqual(
            resolve_date_range("2026-08-01", "2026-08-31", now=NOW),
            ("2026-08-01", "2026-08-31"),
        )

    def test_rejects_backwards_or_future_range(self):
        with self.assertRaises(UsageError):
            resolve_date_range("2026-09-02", "2026-09-01", now=NOW)
        with self.assertRaises(UsageError):
            resolve_date_range("2026-09-01", "2099-01-01", now=NOW)

    def test_allowed_bounds_match_history_picker(self):
        oldest, newest = allowed_date_bounds(NOW)
        self.assertEqual(oldest.isoformat(), "2026-09-26")
        self.assertEqual(newest.isoformat(), "2026-10-25")
        self.assertEqual(RAIL_RETENTION_DAYS, 30)

    def test_plan_clamps_older_start_to_retention(self):
        plan = plan_query_windows("2026-08-27", "2026-10-25", now=NOW)
        self.assertEqual(
            plan["windows"], [{"start_date": "2026-09-26", "end_date": "2026-10-25"}]
        )
        self.assertTrue(any("最早可查" in warning for warning in plan["warnings"]))
        self.assertEqual(plan["oldest_allowed"], "2026-09-26")
        self.assertEqual(plan["newest_allowed"], "2026-10-25")

    def test_plan_old_range_sends_nothing(self):
        plan = plan_query_windows("2026-08-27", "2026-09-25", now=NOW)
        self.assertEqual(plan["windows"], [])
        self.assertIsNone(plan["start_date"])
        self.assertIsNone(plan["end_date"])
        self.assertTrue(any("没有交集" in warning for warning in plan["warnings"]))

    def test_plan_clamps_today_end_to_yesterday(self):
        plan = plan_query_windows("2026-09-26", "2026-10-26", now=NOW)
        self.assertEqual(
            plan["windows"], [{"start_date": "2026-09-26", "end_date": "2026-10-25"}]
        )
        self.assertTrue(any("上限" in warning for warning in plan["warnings"]))

    def test_plan_splits_into_30_day_windows_newest_first(self):
        plan = plan_query_windows(
            "2026-07-01",
            "2026-10-25",
            now=NOW,
            retention_days=120,
            max_span_days=30,
        )
        self.assertEqual(
            plan["windows"],
            [
                {"start_date": "2026-09-26", "end_date": "2026-10-25"},
                {"start_date": "2026-08-27", "end_date": "2026-09-25"},
                {"start_date": "2026-07-28", "end_date": "2026-08-26"},
                {"start_date": "2026-07-01", "end_date": "2026-07-27"},
            ],
        )
        self.assertEqual(plan["oldest_allowed"], "2026-06-28")
        for window in plan["windows"]:
            start = datetime.date.fromisoformat(window["start_date"])
            end = datetime.date.fromisoformat(window["end_date"])
            self.assertLessEqual((end - start).days + 1, 30)
            self.assertLessEqual(start, end)

    def test_plan_rejects_backwards_range(self):
        with self.assertRaises(UsageError):
            plan_query_windows("2026-09-02", "2026-09-01", now=NOW)


class RailStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "Cookies"
        self.now = datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc)
        future = chromium_micros(self.now + datetime.timedelta(days=20))
        self.future = future

    def provider(self):
        return CookieProvider(self.db, host_suffixes=("12306.cn",))

    def test_status_reports_tk_and_expiry(self):
        make_cookie_db(
            self.db,
            [
                (".12306.cn", "tk", "secret", self.future),
                (".12306.cn", "uamtk", "secret", 0),
                ("kyfw.12306.cn", "JSESSIONID", "secret", self.future),
                (".12306.cn", "RAIL_DEVICEID", "secret", self.future),
            ],
        )
        inspection = inspect_site(RAIL, self.provider(), now=self.now)
        self.assertFalse(inspection.needs_login)
        self.assertEqual(inspection.missing_required, [])
        names = [check.name for check in inspection.cookies]
        self.assertIn("tk", names)
        self.assertIn("RAIL_DEVICEID", names)
        tk = next(check for check in inspection.cookies if check.name == "tk")
        self.assertFalse(tk.expired)
        self.assertIsNotNone(tk.expires_at)

    def test_missing_tk_needs_login(self):
        make_cookie_db(
            self.db,
            [("kyfw.12306.cn", "JSESSIONID", "secret", self.future)],
        )
        inspection = inspect_site(RAIL, self.provider(), now=self.now)
        self.assertTrue(inspection.needs_login)
        self.assertIn("tk", inspection.missing_required)

    def test_expired_tk_needs_login(self):
        past = chromium_micros(self.now - datetime.timedelta(days=1))
        make_cookie_db(self.db, [(".12306.cn", "tk", "secret", past)])
        inspection = inspect_site(RAIL, self.provider(), now=self.now)
        self.assertTrue(inspection.needs_login)
        self.assertEqual(inspection.unusable_required, ["tk"])


class RailCliTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("connectors.kit.runner.provider", return_value=mock.Mock(load=lambda: CookieJar()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dry_run_does_not_touch_network_or_keychain(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             mock.patch("subprocess.run", side_effect=AssertionError("keychain")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), \
                 contextlib.redirect_stderr(io.StringIO()):
                code = run_main(
                    CONNECTOR,
                    [
                        "fetch",
                        "--dry-run",
                        "--start-date",
                        "2026-09-01",
                        "--end-date",
                        "2026-09-30",
                    ]
                )
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("queryMyOrder", text)
        self.assertIn(RAIL_ORDER_ENDPOINT, text)
        self.assertIn("needs risk signature", text)

    def test_missing_out_is_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = run_main(
                CONNECTOR,
                [
                    "fetch",
                    "--start-date",
                    "2026-09-01",
                    "--end-date",
                    "2026-09-30",
                ]
            )
        self.assertEqual(code, 2)

    def test_writes_one_0600_file_and_summary(self):
        page1 = fixture("rail12306_page1.json")
        payload = {
            "kind": "rail_12306",
            "source": RAIL_ORDER_ENDPOINT,
            "pages": [{"orders": [parse_order(page1["data"]["OrderDTODataList"][0])]}],
        }
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(CONNECTOR, "fetch", return_value=(payload, [])):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), \
                     contextlib.redirect_stderr(io.StringIO()):
                    code = run_main(
                        CONNECTOR,
                        [
                            "fetch",
                            "--out",
                            temp,
                            "--start-date",
                            "2026-09-01",
                            "--end-date",
                            "2026-09-30",
                        ]
                    )
            self.assertEqual(code, 0)
            files = list(Path(temp).glob("12306-orders-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(file_mode(files[0]), 0o600)
            self.assertIn("orders=1", output.getvalue())
            self.assertIn("12306: pages=1", output.getvalue())


if __name__ == "__main__":
    unittest.main()


class RailProbeIntegrationTests(unittest.TestCase):
    def test_probe_site_ok(self):
        class Provider:
            def load(self):
                return jar_with("tk", "JSESSIONID")

        client = FakeClient(
            [response(text="<html>我的订单</html>", url=RAIL_ORDER_PAGE_URL)]
        )
        result = probe_site_rail(RAIL, Provider(), client)
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["method"], "GET")

    def test_probe_site_missing_tk_without_request(self):
        class Provider:
            def load(self):
                return jar_with("JSESSIONID")

        client = FakeClient([])
        result = probe_site_rail(RAIL, Provider(), client)
        self.assertEqual(result.status, "logged_out")
        self.assertEqual(client.calls, [])


def probe_site_rail(spec, provider, client):
    # Imported lazily so the rest of the module stays focused on the fetcher.
    return probe(spec, provider, client, rail_check)


mock_sessions()
