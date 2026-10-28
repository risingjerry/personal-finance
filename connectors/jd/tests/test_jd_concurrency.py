"""Controlled concurrency for the JD reader (offline).

The reader may read independent years in parallel, but the *request rate* must
stay capped by the pacer and the shared Cookie jar must never be read
half-applied.  These tests pin all three properties without touching the
network.
"""

from __future__ import annotations

import json
import threading
import unittest

from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.errors import AuthRequired, NetworkError
from connectors.kit.http import HttpClient, Pacer
from connectors.jd.reader import JdFetcher


class _Response:
    def __init__(self, payload, url="https://api.m.jd.com/api?functionId=pcorder_center_list"):
        self.status = 200
        self.url = url
        self.headers = {}
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.set_cookies = []
        self.text = self.body.decode("utf-8")

    def json(self):
        return json.loads(self.text)


class _YearClient:
    """Answers ``pcorder_center_list`` with one order per requested year bucket."""

    def __init__(self, *, fail_years=(), tail_empty=True):
        self.calls = []
        self.lock = threading.Lock()
        self.fail_years = set(fail_years)
        self.tail_empty = tail_empty
        self.cookie_jar = None

    def post_form(self, url, form, *, cookie_header="", headers=None):
        body = json.loads(form["body"])
        d_value, page = body["d"], body["page"]
        with self.lock:
            self.calls.append((d_value, page))
        if d_value in self.fail_years:
            # AuthRequired never falls back to the HTML path, so this exercises
            # the worker-failure merge without a second transport.
            raise AuthRequired("simulated login failure")
        if page > 1 and self.tail_empty:
            return _Response({"revision": 2, "success": True,
                              "data": {"orderList": [], "totalNum": 1}})
        order_id = f"order-{d_value}"
        return _Response({
            "revision": 2, "success": True,
            "data": {
                "orderList": [{
                    "orderId": order_id,
                    "dateSubmit": "2020-05-01 10:00:00",
                    "itemPayInfo": {"shouldPay": "10.00", "totalPrice": 10.0},
                    "statusInfo": {"orderStateName": "已完成"},
                    "shopInfo": {"shopName": "京东"},
                    "wareList": [{"name": f"item-{d_value}"}],
                }],
                "totalNum": 1,
            },
        })

    def get(self, *_, **__):  # pragma: no cover - HTML path must not be used
        raise AssertionError("HTML path should not be used for API years")


def _jar():
    return CookieJar([Cookie(".jd.com", "thor", "t"), Cookie(".jd.com", "pin", "p")])


class ConcurrencyTests(unittest.TestCase):
    def _fetcher(self, client, years, concurrency):
        return JdFetcher(client, _jar(), years, concurrency=concurrency)

    def test_single_worker_is_unchanged(self):
        client = _YearClient()
        payload = self._fetcher(client, [2020], 1).fetch()
        self.assertEqual(payload["query"]["concurrency"], 1)
        # totalNum=1 with one row on page 1 means no empty-tail probe is needed.
        self.assertEqual(len(payload["pages"]), 1)
        self.assertEqual(payload["query"]["api_requests"], 1)

    def test_parallel_years_merge_in_requested_order(self):
        client = _YearClient()
        fetcher = self._fetcher(client, [2020, 2021, 2022], 3)
        payload = fetcher.fetch()
        self.assertEqual(payload["query"]["concurrency"], 3)
        buckets = [page["d"] for page in payload["pages"]]
        self.assertEqual(buckets, ["2020", "2021", "2022"])
        ids = [order["order_id"] for page in payload["pages"] for order in page["orders"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate orders across workers")
        self.assertEqual(payload["query"]["api_requests"], 3)

    def test_worker_count_is_capped_by_year_count(self):
        client = _YearClient()
        fetcher = self._fetcher(client, [2020], 8)
        payload = fetcher.fetch()
        self.assertEqual(payload["query"]["concurrency"], 1)
        fetcher = self._fetcher(_YearClient(), [2020, 2021], 8)
        self.assertEqual(fetcher.fetch()["query"]["concurrency"], 2)

    def test_one_failing_year_keeps_other_results_then_fails_closed(self):
        client = _YearClient(fail_years={"2021"})
        fetcher = self._fetcher(client, [2020, 2021, 2022], 3)
        with self.assertRaises(AuthRequired):
            fetcher.fetch()
        buckets = {page["d"] for page in fetcher.pages}
        self.assertEqual(buckets, {"2020", "2022"})

    def test_concurrency_must_be_positive(self):
        with self.assertRaises(ValueError):
            JdFetcher(_YearClient(), _jar(), [2020], concurrency=0)


class PacerSlotTests(unittest.TestCase):
    def test_slots_are_reserved_not_serialised(self):
        """Three callers must get staggered start slots without blocking each other."""

        holds = []
        release = threading.Event()

        class Clock:
            def __init__(self):
                self.value = 0.0
                self.lock = threading.Lock()

            def __call__(self):
                with self.lock:
                    return self.value

            def advance(self, delta):
                with self.lock:
                    self.value += delta

        clock = Clock()
        slept = []

        def sleep(delay):
            slept.append(delay)
            clock.advance(delay)

        pacer = Pacer(1.0, clock=clock, sleep=sleep)
        send_times = []
        send_lock = threading.Lock()

        def caller():
            pacer.wait()
            with send_lock:
                send_times.append(round(clock(), 3))

        threads = [threading.Thread(target=caller) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        send_times.sort()
        self.assertEqual(send_times, [0.0, 1.0, 2.0])

    def test_sequential_caller_still_waits_one_interval(self):
        now = {"t": 0.0}
        slept = []

        def clock():
            return now["t"]

        def sleep(delay):
            slept.append(delay)
            now["t"] += delay

        pacer = Pacer(2.0, clock=clock, sleep=sleep)
        pacer.wait()
        now["t"] += 0.5          # 0.5 s of real work between requests
        pacer.wait()
        self.assertEqual(slept, [1.5])


class CookieJarThreadSafetyTests(unittest.TestCase):
    def test_concurrent_rotation_does_not_corrupt_the_jar(self):
        jar = CookieJar([Cookie(".jd.com", "thor", "old")])

        class Response:
            def __init__(self, value):
                self.url = "https://order.jd.com/center/list.action"
                self.status = 200
                self.headers = {}
                self.body = b""
                self.set_cookies = [f"thor={value}; Domain=.jd.com; Path=/"]
                self._applied_jar = None
                self._applied_names = []

        def worker(index):
            for round_index in range(20):
                jar.apply_response(Response(f"v{index}-{round_index}"))
                jar.header_for("order.jd.com")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        names = jar.names()
        self.assertEqual(names, ["thor"])
        self.assertTrue(jar.get("thor", "order.jd.com"))


if __name__ == "__main__":
    unittest.main()
