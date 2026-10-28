"""Offline tests for the 12306 incremental archive (state, gap plan, limits)."""

import datetime
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.errors import ApiError, AuthRequired, NetworkError
from connectors.kit.http import Response
from connectors.rail12306.reader import (
    BEIJING,
    RAIL_MAX_REQUESTS,
    RAIL_MIN_REQUEST_INTERVAL,
    RAIL_ORDER_ENDPOINT,
    RAIL_ORDER_PAGE_URL,
    RAIL_STATE_FILENAME,
    Rail12306Fetcher,
    default_state_path,
    coverage_summary,
    empty_state,
    load_state,
    plan_incremental_windows,
    probe_session,
    save_state,
)

NOW = datetime.datetime(2026, 10, 26, 12, 0, tzinfo=BEIJING)
TODAY = NOW.date()
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
            {"method": "GET", "url": url, "cookie_header": cookie_header,
             "headers": dict(headers or {})}
        )
        return self._take()


class FakeClock:
    """Monotonic clock that only moves when the injected sleep is called."""

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


def state_file(tmpdir):
    return Path(tmpdir) / RAIL_STATE_FILENAME


def empty_response():
    return response(fixture("rail12306_empty.json"))


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = state_file(self._tmp.name)

    def test_missing_state_is_a_clean_first_run(self):
        state, warnings = load_state(self.path)
        self.assertEqual(warnings, [])
        self.assertEqual(state["covered_days"], [])
        self.assertEqual(state["windows"], [])

    def test_save_is_atomic_0600_and_round_trips(self):
        state = empty_state()
        state["covered_days"] = ["2026-10-01", "2026-10-02"]
        state["known_order_ids"] = ["E123"]
        save_state(state, self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700
        )
        reloaded, warnings = load_state(self.path)
        self.assertEqual(warnings, [])
        self.assertEqual(reloaded["covered_days"], ["2026-10-01", "2026-10-02"])
        self.assertEqual(reloaded["known_order_ids"], ["E123"])
        # No temporary files are left behind.
        self.assertEqual(
            [p.name for p in Path(self._tmp.name).iterdir()],
            [RAIL_STATE_FILENAME],
        )

    def test_corrupt_state_is_quarantined_not_fatal(self):
        self.path.write_text("{not json", encoding="utf-8")
        state, warnings = load_state(self.path)
        self.assertEqual(state["covered_days"], [])
        self.assertTrue(any("损坏" in warning for warning in warnings))
        self.assertFalse(self.path.exists())
        self.assertTrue(list(Path(self._tmp.name).glob("*.corrupt-*")))

    def test_wrong_root_type_is_quarantined_not_fatal(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        state, warnings = load_state(self.path)
        self.assertEqual(state["covered_days"], [])
        self.assertTrue(any("损坏" in warning for warning in warnings))

    def test_partial_garbage_is_dropped_without_crashing(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "covered_days": ["2026-10-01", "nonsense", None, 42],
                    "known_order_ids": ["E1", "", None],
                    "windows": [
                        {"start_date": "bad", "end_date": "2026-10-02"},
                        {"start_date": "2026-10-01", "end_date": "2026-10-02",
                         "status": "ok", "pages": "2", "orders": None},
                        "not-a-dict",
                    ],
                }
            ),
            encoding="utf-8",
        )
        state, warnings = load_state(self.path)
        self.assertEqual(warnings, [])
        self.assertEqual(state["covered_days"], ["2026-10-01"])
        self.assertEqual(state["known_order_ids"], ["E1"])
        self.assertEqual(len(state["windows"]), 1)
        self.assertEqual(state["windows"][0]["pages"], 2)

    def test_state_dir_env_override(self):
        with mock.patch.dict(
            os.environ, {"PFIN_STATE_DIR": self._tmp.name}
        ):
            self.assertEqual(
                default_state_path(), Path(self._tmp.name) / RAIL_STATE_FILENAME
            )


class IncrementalPlanTests(unittest.TestCase):
    def state_with(self, covered_days, windows=()):
        state = empty_state()
        state["covered_days"] = sorted(covered_days)
        state["windows"] = list(windows)
        return state

    def test_first_run_covers_the_whole_legal_window(self):
        plan = plan_incremental_windows(empty_state(), TODAY, now=NOW)
        self.assertEqual(
            plan["windows"],
            [
                {
                    "start_date": "2026-09-26",
                    "end_date": "2026-10-25",
                    "days": 30,
                }
            ],
        )
        self.assertEqual(plan["oldest_allowed"], "2026-09-26")
        self.assertEqual(plan["newest_allowed"], "2026-10-25")
        self.assertEqual(plan["gap_days"], 30)
        self.assertEqual(plan["deferred"], [])

    def test_next_day_only_queries_the_new_day(self):
        covered = [
            (TODAY - datetime.timedelta(days=offset)).isoformat()
            for offset in range(30, 0, -1)
        ]
        plan = plan_incremental_windows(
            self.state_with(covered),
            TODAY + datetime.timedelta(days=1),
            now=NOW + datetime.timedelta(days=1),
        )
        self.assertEqual(
            plan["windows"],
            [{"start_date": "2026-10-26", "end_date": "2026-10-26", "days": 1}],
        )
        self.assertEqual(plan["covered_days"], 29)
        self.assertEqual(plan["gap_days"], 1)

    def test_fully_covered_range_sends_nothing(self):
        covered = [
            (TODAY - datetime.timedelta(days=offset)).isoformat()
            for offset in range(30, 0, -1)
        ]
        plan = plan_incremental_windows(self.state_with(covered), TODAY, now=NOW)
        self.assertEqual(plan["windows"], [])
        self.assertEqual(plan["deferred"], [])
        self.assertTrue(any("已全部归档" in w for w in plan["warnings"]))
        self.assertTrue(any("--refresh" in w for w in plan["warnings"]))

    def test_requested_range_is_clamped_and_gap_filtered(self):
        plan = plan_incremental_windows(
            empty_state(), TODAY, ("2026-08-27", "2026-10-25"), now=NOW
        )
        self.assertEqual(plan["target_start_date"], "2026-09-26")
        self.assertEqual(plan["target_end_date"], "2026-10-25")
        self.assertEqual(len(plan["windows"]), 1)

    def test_out_of_horizon_range_is_never_planned(self):
        plan = plan_incremental_windows(
            empty_state(), TODAY, ("2026-08-27", "2026-09-25"), now=NOW
        )
        self.assertEqual(plan["windows"], [])
        self.assertTrue(any("没有交集" in w for w in plan["warnings"]))

    def test_force_ignores_coverage(self):
        covered = [
            (TODAY - datetime.timedelta(days=offset)).isoformat()
            for offset in range(30, 0, -1)
        ]
        plan = plan_incremental_windows(
            self.state_with(covered), TODAY, now=NOW, force=True
        )
        self.assertEqual(len(plan["windows"]), 1)
        self.assertEqual(plan["windows"][0]["days"], 30)
        self.assertTrue(any("force refresh" in w for w in plan["warnings"]))

    def test_gap_in_the_middle_is_split_into_legal_windows(self):
        # Cover everything except 2026-10-05..2026-10-07 (a 3-day hole).
        covered = [
            (TODAY - datetime.timedelta(days=offset)).isoformat()
            for offset in range(30, 0, -1)
            if not 19 <= offset <= 21
        ]
        plan = plan_incremental_windows(self.state_with(covered), TODAY, now=NOW)
        self.assertEqual(
            plan["windows"],
            [{"start_date": "2026-10-05", "end_date": "2026-10-07", "days": 3}],
        )

    def test_windows_are_oldest_first_and_at_most_30_days(self):
        plan = plan_incremental_windows(
            empty_state(), TODAY, now=NOW, retention_days=90
        )
        spans = [
            (window["start_date"], window["end_date"]) for window in plan["windows"]
        ]
        self.assertEqual(
            spans,
            [
                ("2026-07-28", "2026-08-26"),
                ("2026-08-27", "2026-09-25"),
                ("2026-09-26", "2026-10-25"),
            ],
        )
        for window in plan["windows"]:
            start = datetime.date.fromisoformat(window["start_date"])
            end = datetime.date.fromisoformat(window["end_date"])
            self.assertLessEqual((end - start).days + 1, 30)
            self.assertGreaterEqual(start.isoformat(), plan["oldest_allowed"])
            self.assertLessEqual(end.isoformat(), plan["newest_allowed"])

    def test_failed_window_keeps_gap_and_backs_off(self):
        state = self.state_with(
            [],
            windows=[
                {
                    "start_date": "2026-09-26",
                    "end_date": "2026-10-25",
                    "status": "throttled",
                    "retry_after": (
                        NOW + datetime.timedelta(hours=12)
                    ).isoformat(timespec="seconds"),
                }
            ],
        )
        plan = plan_incremental_windows(state, TODAY, now=NOW)
        self.assertEqual(plan["windows"], [])
        self.assertEqual(plan["deferred_days"], 30)
        self.assertEqual(plan["deferred"][0]["reason"], "backoff")
        self.assertTrue(any("退避" in w for w in plan["warnings"]))
        # After the backoff has elapsed the window is planned again.
        later = NOW + datetime.timedelta(hours=12, minutes=1)
        plan = plan_incremental_windows(state, TODAY, now=later)
        self.assertEqual(len(plan["windows"]), 1)
        self.assertEqual(plan["deferred"], [])

    def test_partial_overlap_defers_only_the_backoff_days(self):
        state = self.state_with(
            [],
            windows=[
                {
                    "start_date": "2026-10-20",
                    "end_date": "2026-10-25",
                    "status": "error_page",
                    "retry_after": (
                        NOW + datetime.timedelta(hours=6)
                    ).isoformat(timespec="seconds"),
                }
            ],
        )
        plan = plan_incremental_windows(state, TODAY, now=NOW)
        self.assertEqual(
            plan["windows"],
            [{"start_date": "2026-09-26", "end_date": "2026-10-19", "days": 24}],
        )
        self.assertEqual(plan["deferred_days"], 6)

    def test_coverage_summary_counts_contiguous_ranges(self):
        state = self.state_with(
            ["2026-09-26", "2026-09-27", "2026-10-01", "2026-10-02"]
        )
        summary = coverage_summary(state, "2026-09-26", "2026-10-25")
        self.assertEqual(summary["oldest_covered"], "2026-09-26")
        self.assertEqual(summary["newest_covered"], "2026-10-02")
        self.assertEqual(summary["contiguous_ranges"], 2)
        self.assertEqual(summary["covered_days"], 4)
        self.assertEqual(summary["target_days"], 30)
        self.assertEqual(summary["gap_days"], 26)


class IncrementalFetchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = state_file(self._tmp.name)

    def fetcher(self, client, jar=None, **kwargs):
        kwargs.setdefault("state_path", self.state_path)
        kwargs.setdefault("min_interval", 0.0)
        kwargs.setdefault("now", NOW)
        return Rail12306Fetcher(client, jar or jar_with("tk"), **kwargs)

    def test_first_run_archives_the_full_window(self):
        client = FakeClient([empty_response()])
        payload = self.fetcher(client).fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            (client.calls[0]["form"]["queryStartDate"],
             client.calls[0]["form"]["queryEndDate"]),
            ("2026-09-26", "2026-10-25"),
        )
        query = payload["query"]
        self.assertTrue(query["incremental"])
        self.assertEqual(query["requests_sent"], 1)
        self.assertEqual(query["windows_planned"], 1)
        self.assertEqual(query["windows_fetched"], 1)
        self.assertEqual(query["windows_deferred"], 0)
        self.assertEqual(
            query["coverage"]["covered_days"], 30
        )
        self.assertEqual(query["coverage"]["gap_days"], 0)
        self.assertEqual(
            query["coverage"]["ranges"], [["2026-09-26", "2026-10-25"]]
        )
        self.assertTrue(Path(query["state_file"]).exists())

        state, _ = load_state(self.state_path)
        self.assertEqual(len(state["covered_days"]), 30)
        self.assertEqual(state["windows"][0]["status"], "ok")

    def test_second_run_same_day_sends_nothing(self):
        self.fetcher(FakeClient([empty_response()])).fetch()
        client = FakeClient([])
        fetcher = self.fetcher(client)
        payload = fetcher.fetch()
        self.assertEqual(client.calls, [])
        self.assertFalse(payload["query"]["queried"])
        self.assertEqual(payload["query"]["requests_sent"], 0)
        self.assertEqual(payload["query"]["windows_planned"], 0)
        self.assertEqual(payload["query"]["coverage"]["gap_days"], 0)
        self.assertTrue(any("已全部归档" in w for w in fetcher.warnings))

    def test_next_day_queries_only_the_new_day(self):
        self.fetcher(FakeClient([empty_response()])).fetch()
        client = FakeClient([empty_response()])
        tomorrow = NOW + datetime.timedelta(days=1)
        payload = self.fetcher(
            client,
            start_date="2026-09-27",
            end_date="2026-10-26",
            now=tomorrow,
        ).fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            (client.calls[0]["form"]["queryStartDate"],
             client.calls[0]["form"]["queryEndDate"]),
            ("2026-10-26", "2026-10-26"),
        )
        self.assertEqual(payload["query"]["requests_sent"], 1)
        self.assertEqual(payload["query"]["coverage"]["covered_days"], 30)

    def test_zero_order_window_is_recorded_as_covered(self):
        payload = self.fetcher(FakeClient([empty_response()])).fetch()
        self.assertEqual(payload["query"]["windows"], 1)
        self.assertEqual(payload["windows"][0]["orders"], 0)
        state, _ = load_state(self.state_path)
        self.assertEqual(state["windows"][0]["status"], "ok")
        self.assertEqual(state["windows"][0]["orders"], 0)
        self.assertEqual(len(state["covered_days"]), 30)
        # A re-plan now has no gap, so a second run cannot loop on it.
        plan = plan_incremental_windows(state, TODAY, now=NOW)
        self.assertEqual(plan["windows"], [])

    def test_error_page_stops_immediately_without_retry(self):
        client = FakeClient(
            [
                response(
                    text="\ufeff<html><body>网络可能存在问题，请您重试一下！</body></html>",
                    url="https://kyfw.12306.cn/otn/error/error.html",
                ),
                empty_response(),
                empty_response(),
            ]
        )
        fetcher = self.fetcher(client)
        with self.assertRaises(ApiError) as raised:
            fetcher.fetch()
        self.assertEqual(len(client.calls), 1)  # no retry, no second window
        self.assertIn("错误页", raised.exception.message)
        state, _ = load_state(self.state_path)
        record = state["windows"][0]
        self.assertEqual(record["status"], "throttled")
        self.assertIsNotNone(record["retry_after"])
        self.assertEqual(state["covered_days"], [])
        self.assertTrue(
            any("服务端错误页" in warning for warning in fetcher.warnings)
        )
        self.assertTrue(any("不重试" in warning for warning in fetcher.warnings))

    def test_failed_window_is_deferred_on_the_next_run(self):
        client = FakeClient(
            [
                response(
                    text="\ufeff<html><body>查询过于频繁，请稍后再试</body></html>",
                    url="https://kyfw.12306.cn/otn/error/error.html",
                )
            ]
        )
        with self.assertRaises(ApiError):
            self.fetcher(client).fetch()
        # Immediately afterwards: the window is still a gap but backed off.
        second = FakeClient([])
        payload = self.fetcher(second).fetch()
        self.assertEqual(second.calls, [])
        self.assertEqual(payload["query"]["windows_deferred"], 1)
        self.assertEqual(payload["query"]["requests_sent"], 0)

    def test_non_json_body_stops_and_records_error_page(self):
        client = FakeClient([response(text="gateway timeout")])
        with self.assertRaises(ApiError):
            self.fetcher(client).fetch()
        self.assertEqual(len(client.calls), 1)
        state, _ = load_state(self.state_path)
        self.assertEqual(state["windows"][0]["status"], "error_page")
        self.assertEqual(state["covered_days"], [])

    def test_logged_out_stops_before_any_request_and_touches_no_state(self):
        client = FakeClient([])
        fetcher = self.fetcher(client, jar=jar_with("JSESSIONID"))
        with self.assertRaises(AuthRequired):
            fetcher.fetch()
        self.assertEqual(client.calls, [])
        self.assertFalse(self.state_path.exists())

    def test_request_budget_defers_remaining_windows(self):
        client = FakeClient([empty_response(), empty_response()])
        payload = self.fetcher(
            client,
            max_requests=1,
            retention_days=90,
            start_date="2026-07-29",
            end_date="2026-10-25",
        ).fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(payload["query"]["budget_exhausted"])
        self.assertEqual(payload["query"]["requests_sent"], 1)
        self.assertEqual(payload["query"]["windows_planned"], 3)
        self.assertEqual(payload["query"]["windows_deferred"], 2)
        self.assertEqual(
            [item["reason"] for item in payload["query"]["deferred_windows"]],
            ["budget", "budget"],
        )
        state, _ = load_state(self.state_path)
        self.assertEqual(len(state["covered_days"]), 30)  # first chunk only
        plan = plan_incremental_windows(
            state,
            TODAY,
            ("2026-07-29", "2026-10-25"),
            now=NOW,
            retention_days=90,
        )
        self.assertEqual(len(plan["windows"]), 2)

    def test_requests_are_spaced_by_the_configured_interval(self):
        clock = FakeClock()
        client = FakeClient([empty_response() for _ in range(3)])
        self.fetcher(
            client,
            retention_days=90,
            start_date="2026-07-29",
            end_date="2026-10-25",
            min_interval=RAIL_MIN_REQUEST_INTERVAL,
            clock=clock.clock,
            sleep=clock.sleep,
        ).fetch()
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(clock.sleeps), 2)
        for seconds in clock.sleeps:
            self.assertGreaterEqual(seconds, RAIL_MIN_REQUEST_INTERVAL - 1e-9)
        self.assertAlmostEqual(sum(clock.sleeps), 2 * RAIL_MIN_REQUEST_INTERVAL)

    def test_default_limits_are_documented_values(self):
        self.assertEqual(RAIL_MIN_REQUEST_INTERVAL, 2.0)
        self.assertEqual(RAIL_MAX_REQUESTS, 12)

    def test_incomplete_pagination_is_not_covered(self):
        client = FakeClient([response(fixture("rail12306_page1.json"))])
        fetcher = self.fetcher(client, max_pages=1, page_size=1)
        payload = fetcher.fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(any("最大页数" in w for w in fetcher.warnings))
        self.assertTrue(any("未取完" in w for w in fetcher.warnings))
        state, _ = load_state(self.state_path)
        self.assertEqual(state["windows"][0]["status"], "incomplete")
        self.assertEqual(state["covered_days"], [])
        self.assertEqual(payload["query"]["coverage"]["gap_days"], 30)

    def test_disabled_incremental_keeps_legacy_behaviour(self):
        client = FakeClient([empty_response(), empty_response(), empty_response()])
        payload = self.fetcher(
            client,
            incremental=False,
            retention_days=90,
            start_date="2026-07-29",
            end_date="2026-10-25",
        ).fetch()
        self.assertFalse(payload["query"]["incremental"])
        self.assertEqual(len(payload["windows"]), 3)
        self.assertEqual(payload["query"]["state_file"], None)
        self.assertFalse(self.state_path.exists())

    def test_force_refresh_requeries_a_covered_range(self):
        self.fetcher(FakeClient([empty_response()])).fetch()
        client = FakeClient([empty_response()])
        payload = self.fetcher(client, force_refresh=True).fetch()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(payload["query"]["requests_sent"], 1)

    def test_state_file_mode_is_0600(self):
        self.fetcher(FakeClient([empty_response()])).fetch()
        self.assertEqual(stat.S_IMODE(os.stat(self.state_path).st_mode), 0o600)

    def test_order_ids_are_remembered_for_deduplication(self):
        payload = self.fetcher(
            FakeClient([response(fixture("rail12306_page1.json")), response(fixture("rail12306_page2.json"))]),
            page_size=1,
            max_pages=5,
        ).fetch()
        self.assertEqual(len(payload["pages"]), 2)
        state, _ = load_state(self.state_path)
        self.assertEqual(state["known_order_ids"], ["E123456789", "E987654321"])


class ProbeSessionTests(unittest.TestCase):
    def test_probe_reports_logged_in(self):
        client = FakeClient(
            [response(text="<html>我的订单</html>", url=RAIL_ORDER_PAGE_URL)]
        )
        jar = jar_with("tk", "JSESSIONID")
        info = probe_session(client, jar)
        self.assertTrue(info["logged_in"])
        self.assertEqual(info["http_status"], 200)
        self.assertFalse(info["redirected_to_passport"])
        self.assertIsInstance(info["elapsed_ms"], int)
        self.assertEqual(client.calls[0]["method"], "GET")
        self.assertEqual(client.calls[0]["url"], RAIL_ORDER_PAGE_URL)

    def test_probe_missing_tk_makes_no_request(self):
        client = FakeClient([])
        info = probe_session(client, jar_with("JSESSIONID"))
        self.assertFalse(info["logged_in"])
        self.assertEqual(info["reason"], "missing_tk")
        self.assertEqual(client.calls, [])

    def test_probe_detects_passport_redirect(self):
        login_html = (FIXTURES / "rail12306_login.html").read_text(encoding="utf-8")
        client = FakeClient(
            [
                response(
                    text=login_html,
                    url="https://kyfw.12306.cn/otn/passport?redirect=/otn/queryOrder/queryMyOrder.html",
                )
            ]
        )
        jar = jar_with("tk")
        info = probe_session(client, jar)
        self.assertFalse(info["logged_in"])
        self.assertTrue(info["redirected_to_passport"])

    def test_probe_reports_error_page_without_raising(self):
        client = FakeClient(
            [
                response(
                    text="<html>网络可能存在问题</html>",
                    url="https://kyfw.12306.cn/otn/error/error.html",
                )
            ]
        )
        info = probe_session(client, jar_with("tk"))
        self.assertFalse(info["logged_in"])
        self.assertEqual(info["reason"], "error_page")

    def test_probe_never_leaks_cookie_values(self):
        client = FakeClient(
            [response(text="<html>我的订单</html>", url=RAIL_ORDER_PAGE_URL)]
        )
        jar = jar_with("tk")
        info = probe_session(client, jar)
        self.assertNotIn("test-value", json.dumps(info))

    def test_probe_network_error_propagates(self):
        class FailingClient:
            def get(self, url, *, cookie_header="", headers=None):
                raise NetworkError("boom")

        with self.assertRaises(NetworkError):
            probe_session(FailingClient(), jar_with("tk"))


if __name__ == "__main__":
    unittest.main()

