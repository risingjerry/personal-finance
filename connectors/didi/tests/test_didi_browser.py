"""Synthetic, offline tests for browser-assisted Didi reads.

These tests never launch a browser, read browser, call Keychain, or use network.
They cover capture normalization, redaction, block classification, engine
discovery messages, and the CLI output contract.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.didi import CONNECTOR
from connectors.didi.actions import blocked_error
from connectors.didi.browser import (
    BrowserEngine,
    CaptureStore,
    DidiBrowserFetcher,
    build_browser_payload,
    classify_block,
    cookie_specs,
    detect_browser_engine,
    redact_browser,
    redact_url,
    summarize_browser,
)
from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.errors import BrowserUnavailable, RiskBlocked
from connectors.kit.output import file_mode
from connectors.kit.runner import main as _main

def main(argv):
    return _main(CONNECTOR, argv)


SECRET = "SYNTH_BROWSER_TOKEN_DO_NOT_LEAK"
TICKET = "SYNTH_BROWSER_TICKET_DO_NOT_LEAK"
SIGN = "SYNTH_WSGSIG_DO_NOT_LEAK"


def make_order(oid, *, ts=1700000000, product="快车", status="已完成",
               start="起点", end="终点", **extra):
    item = {
        "orderId": oid,
        "productId": 260,
        "product_name": product,
        "hisstatus": status,
        "setuptimestamp": ts,
        "fromAddress": start,
        "toAddress": end,
        "scene": [product],
        "classtype": 1,
    }
    item.update(extra)
    return item


def list_result():
    list_payload = {
        "errno": 0,
        "timemode": "c2",
        "havenext": 0,
        "mhavenext": 0,
        "order_waiting": [make_order("a", ts=1700000000)],
        "order_done": [make_order("b", ts=1700100000, product="专车")],
    }
    empty_payload = {"errno": 0, "order_waiting": [], "order_done": []}
    detail = {
        "errno": 0,
        "real_pay": {"total_fee": "12.30"},
        "token": SECRET,
        "wsgsig": SIGN,
        "nested": {"ticket": TICKET},
    }
    fare = {
        "errno": 0,
        "data": {"real_pay": {"total_fee": "8.88"}},
        "sign": SIGN,
        "ticket": TICKET,
    }
    return {
        "ok": True,
        "engine": "fake",
        "captures": [
            {
                "kind": "order_list",
                "url": "https://common.diditaxi.com.cn/passenger/history"
                f"?pagenum=0&token={SECRET}&wsgsig={SIGN}",
                "status": 200,
                "page": 0,
                "timemode": "",
                "response": list_payload,
            },
            {
                "kind": "order_list",
                "url": "https://common.diditaxi.com.cn/passenger/history"
                f"?pagenum=1&token={SECRET}&wsgsig={SIGN}",
                "status": 200,
                "page": 1,
                "timemode": "c1",
                "response": empty_payload,
            },
            {
                "kind": "order_detail",
                "url": "https://api.udache.com/gulfstream/passenger/v2/core/"
                f"pOrderDetail?order_id=a&token={SECRET}&wsgsig={SIGN}",
                "status": 200,
                "order_id": "a",
                "response": detail,
            },
            {
                "kind": "fare_detail",
                "url": "https://api.udache.com/gulfstream/passenger/v2/other/"
                f"pGetRealTimeFeeDetail?order_id=a&token={SECRET}&wsgsig={SIGN}",
                "status": 200,
                "order_id": "a",
                "response": fare,
            },
        ],
        "details": {"a": {"detail": detail, "fare": fare}},
        "warnings": [],
        "blocked": None,
    }


class RedactionTests(unittest.TestCase):
    def test_recursive_key_url_and_value_redaction(self):
        payload = {
            "token": SECRET,
            "ticket": TICKET,
            "wsgsig": SIGN,
            "sign": SIGN,
            "nested": {
                "authorization": SECRET,
                "url": f"https://x/y?token={SECRET}&keep=1",
                "text": f"prefix {SECRET} suffix",
            },
            "fare": {"real_pay": {"total_fee": "12.30"}},
            "phone": "13800000000",
            "openid": "OPAQUE_OPENID",
        }
        cleaned = redact_browser(payload, [SECRET, TICKET, SIGN])
        text = json.dumps(cleaned, ensure_ascii=False)
        for secret in (SECRET, TICKET, SIGN, "13800000000", "OPAQUE_OPENID"):
            self.assertNotIn(secret, text)
        self.assertEqual(cleaned["fare"]["real_pay"]["total_fee"], "12.30")
        self.assertEqual(cleaned["nested"]["url"], "https://x/y?token=[REDACTED]&keep=1")

    def test_redact_url_keeps_diagnostics_but_hides_credentials(self):
        url = (
            "https://common.diditaxi.com.cn/passenger/history"
            f"?pagenum=2&token={SECRET}&wsgsig={SIGN}#/g?ticket={TICKET}"
        )
        cleaned = redact_url(url, [SECRET, TICKET, SIGN])
        self.assertIn("pagenum=2", cleaned)
        for secret in (SECRET, TICKET, SIGN):
            self.assertNotIn(secret, cleaned)
        self.assertIn("token=", cleaned)

    def test_cookie_specs_keep_values_in_memory_and_mark_secure(self):
        jar = CookieJar(
            [
                Cookie("common.diditaxi.com.cn", "ticket", TICKET),
                Cookie(".diditaxi.com.cn", "openid", "OPENID"),
                Cookie("127.0.0.1", "ticket", "local"),
            ]
        )
        specs = cookie_specs(jar)
        by_name_domain = {(s["domain"], s["name"]): s for s in specs}
        remote = by_name_domain[("common.diditaxi.com.cn", "ticket")]
        local = by_name_domain[("127.0.0.1", "ticket")]
        self.assertTrue(remote["secure"])
        self.assertFalse(local["secure"])
        self.assertEqual(remote["value"], TICKET)
        self.assertIn("ticket", repr(jar))
        self.assertNotIn(TICKET, repr(jar))


class BlockTests(unittest.TestCase):
    def test_login_url_is_auth(self):
        kind, _ = classify_block("https://passport.diditaxi.com.cn/login", "")
        self.assertEqual(kind, "auth")

    def test_captcha_text_is_risk(self):
        kind, message = classify_block(
            "https://common.diditaxi.com.cn/general/webEntry?h=1#/order-list",
            "请完成验证",
        )
        self.assertEqual(kind, "risk")
        self.assertIn("验证", message)

    def test_selector_hit_is_risk(self):
        kind, _ = classify_block(
            "https://common.diditaxi.com.cn/general/webEntry", "", selector_hits=1
        )
        self.assertEqual(kind, "risk")

    def test_normal_order_page_is_not_blocked(self):
        self.assertIsNone(
            classify_block(
                "https://common.diditaxi.com.cn/general/webEntry?h=1#/order-list",
                "快车 已完成 起点 终点 没有更多",
            )
        )


class CaptureTests(unittest.TestCase):
    def test_capture_store_deduplicates_and_buckets_details(self):
        class Resp:
            def __init__(self, url, payload, status=200):
                self.url = url
                self.status = status
                self._payload = payload

            def json(self):
                return self._payload

            def text(self):
                return json.dumps(self._payload)

        store = CaptureStore()
        store.on_response(
            Resp(
                "https://common.diditaxi.com.cn/passenger/history?pagenum=0"
                f"&token={SECRET}",
                {"errno": 0, "order_done": [make_order("a")]},
            )
        )
        store.on_response(
            Resp(
                "https://common.diditaxi.com.cn/passenger/history?pagenum=0"
                f"&token={SECRET}",
                {"errno": 0, "order_done": [make_order("a")]},
            )
        )
        store.on_response(
            Resp(
                "https://api.udache.com/gulfstream/passenger/v2/core/"
                f"pOrderDetail?order_id=a&token={SECRET}",
                {"errno": 0, "real_pay": {"total_fee": "1.00"}},
            )
        )
        self.assertEqual(len(store.list_captures), 1)
        self.assertEqual(store.list_order_ids(), ["a"])
        self.assertTrue(store.has_detail("a"))


class PayloadTests(unittest.TestCase):
    def test_builds_spec_shape_fare_coverage_and_pages(self):
        payload = build_browser_payload(
            list_result(), secrets=[SECRET, TICKET, SIGN]
        )
        self.assertEqual(payload["kind"], "didi_trips")
        self.assertEqual(payload["source"], "browser")
        self.assertEqual([trip["order_id"] for trip in payload["trips"]], ["a", "b"])
        first, second = payload["trips"]
        self.assertEqual(first["product"], "快车")
        self.assertEqual(first["from"], "起点")
        self.assertEqual(first["to"], "终点")
        self.assertEqual(first["status"], "已完成")
        self.assertEqual(first["occurred"], "2023-11-15 06:13:20")
        self.assertEqual(first["fare"], "8.88")
        self.assertIsInstance(first["fare_detail"], dict)
        self.assertIsNone(second["fare_detail"])
        self.assertIsNone(second["fare"])
        self.assertEqual(len(payload["pages"]), 4)
        self.assertEqual(
            {page["kind"] for page in payload["pages"]},
            {"order_list", "order_detail", "fare_detail"},
        )
        text = json.dumps(payload, ensure_ascii=False)
        for secret in (SECRET, TICKET, SIGN):
            self.assertNotIn(secret, text)
        summary = summarize_browser(payload)
        self.assertEqual(summary["orders"], 2)
        self.assertEqual(summary["with_fare"], 1)
        self.assertEqual(
            summary["date_range"], ("2023-11-15 06:13:20", "2023-11-16 10:00:00")
        )

    def test_blocked_result_is_preserved_for_partial_save(self):
        result = list_result()
        result["blocked"] = {"kind": "risk", "message": "页面出现验证码"}
        payload = build_browser_payload(result, secrets=[SECRET])
        self.assertEqual(payload["blocked"]["kind"], "risk")
        self.assertIn("验证码", payload["blocked"]["message"])


class EngineTests(unittest.TestCase):
    def test_python_first_then_node(self):
        with mock.patch(
            "connectors.kit.browser._detect_python_playwright", return_value=None
        ), mock.patch(
            "connectors.kit.browser._detect_node_playwright",
            return_value=BrowserEngine("node", "/x/chromium"),
        ):
            engine = detect_browser_engine("auto")
        self.assertEqual(engine.kind, "node")

    def test_missing_engine_reports_both_and_never_downloads(self):
        with mock.patch(
            "connectors.kit.browser._detect_python_playwright", return_value=None
        ), mock.patch(
            "connectors.kit.browser._detect_node_playwright", return_value=None
        ), mock.patch(
            "connectors.kit.browser._python_missing_reason",
            return_value="Python 包 playwright 未安装",
        ), mock.patch(
            "connectors.kit.browser._node_missing_reason",
            return_value="node 已安装，但找不到 playwright 模块",
        ), mock.patch("subprocess.run", side_effect=AssertionError("no download")):
            with self.assertRaises(BrowserUnavailable) as caught:
                detect_browser_engine("auto")
        text = caught.exception.render()
        self.assertIn("Python Playwright", text)
        self.assertIn("Node Playwright", text)
        self.assertIn("不会自动下载", text)

    def test_prefer_python_does_not_fall_back_to_node(self):
        with mock.patch(
            "connectors.kit.browser._detect_python_playwright", return_value=None
        ), mock.patch(
            "connectors.kit.browser._detect_node_playwright",
            return_value=BrowserEngine("node", "/x/chromium"),
        ):
            with self.assertRaises(BrowserUnavailable):
                detect_browser_engine("python")


class FetcherTests(unittest.TestCase):
    def test_fake_driver_result_is_normalized_and_redacted(self):
        jar = CookieJar([Cookie("common.diditaxi.com.cn", "ticket", TICKET)])

        class Driver:
            def run(self, cookies):
                self.cookies = cookies
                return list_result()

        driver = Driver()
        fetcher = DidiBrowserFetcher(jar, driver=driver)
        payload = fetcher.fetch()
        self.assertEqual(len(driver.cookies), 1)
        self.assertEqual(driver.cookies[0]["value"], TICKET)
        self.assertEqual(len(payload["trips"]), 2)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(TICKET, text)
        self.assertNotIn(SECRET, text)

    def test_fake_driver_error_is_scrubbed(self):
        jar = CookieJar([Cookie("common.diditaxi.com.cn", "ticket", TICKET)])

        class Driver:
            def run(self, cookies):
                raise RiskBlocked(f"blocked with {TICKET}")

        with self.assertRaises(RiskBlocked) as caught:
            DidiBrowserFetcher(jar, driver=Driver()).fetch()
        self.assertNotIn(TICKET, caught.exception.render())
        self.assertNotIn(TICKET, str(caught.exception))


class BrowserCliTests(unittest.TestCase):
    def test_dry_run_does_not_launch_or_read_cookies(self):
        with mock.patch(
            "connectors.didi.browser.detect_browser_engine",
            side_effect=AssertionError("must not launch"),
        ), mock.patch(
            "connectors.kit.cookies.CookieProvider.load",
            side_effect=AssertionError("must not read cookie db"),
        ), mock.patch.dict(os.environ, {SECRET: SECRET}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                code = main(["fetch", "--browser", "--dry-run"])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("--browser", text)
        self.assertIn("wsgsig", text)
        self.assertIn("不会自动下载", text)
        self.assertIn("order-list", text)
        self.assertNotIn(TICKET, text)

    def test_browser_requires_cookies(self):
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["fetch", "--browser", "--no-cookie", "--out", "OUT"])
        self.assertEqual(code, 2)

    def test_browser_writes_json_and_rows_0600(self):
        payload = build_browser_payload(list_result(), secrets=[SECRET, TICKET, SIGN])
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch(
                "connectors.didi.fetch_didi_browser",
                return_value=(payload, [], blocked_error(payload)),
            ):
                output = io.StringIO()
                errors = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = main(["fetch", "--browser", "--out", temp])
            self.assertEqual(code, 0)
            files = list(Path(temp).glob("didi-trips-*.json"))
            rows = list(Path(temp).glob("didi-rows-*.jsonl"))
            self.assertEqual(len(files), 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(file_mode(files[0]), 0o600)
            self.assertEqual(file_mode(rows[0]), 0o600)
            saved = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["kind"], "didi_trips")
            self.assertEqual(len(saved["trips"]), 2)
            row_objects = [
                json.loads(line)
                for line in rows[0].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                [row["id"] for row in row_objects], ["didi:trip:a", "didi:trip:b"]
            )
            report = output.getvalue()
            self.assertIn("trips=2", report)
            self.assertIn("with_fare=1", report)
            self.assertIn("2023-11-15", report)
            combined = report + errors.getvalue()
            self.assertNotIn(SECRET, combined)
            self.assertNotIn(TICKET, combined)

    def test_partial_block_saves_then_exits_4(self):
        result = list_result()
        result["blocked"] = {"kind": "risk", "message": "页面出现了验证码"}
        payload = build_browser_payload(result, secrets=[SECRET])
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch(
                "connectors.didi.fetch_didi_browser",
                return_value=(payload, [], blocked_error(payload)),
            ):
                output = io.StringIO()
                errors = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = main(["fetch", "--browser", "--out", temp])
            self.assertEqual(code, 4)
            self.assertEqual(len(list(Path(temp).glob("didi-trips-*.json"))), 1)
            self.assertIn("验证码", errors.getvalue())
            self.assertIn("不会", errors.getvalue())

    def test_missing_engine_exits_3_without_output(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch(
                "connectors.didi.fetch_didi_browser",
                side_effect=BrowserUnavailable("缺少 Chromium", "不会自动下载"),
            ):
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    code = main(["fetch", "--browser", "--out", temp])
            self.assertEqual(code, 3)
            self.assertEqual(list(Path(temp).glob("didi-trips-*")), [])
            self.assertIn("Chromium", errors.getvalue())
            self.assertIn("不会自动下载", errors.getvalue())


def setUpModule():
    from connectors.kit.testing import mock_sessions
    mock_sessions()


if __name__ == "__main__":
    unittest.main()
