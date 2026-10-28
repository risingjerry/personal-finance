"""京东连接器：rows、登录检查、在线检查、命令行（全部离线，不碰 Keychain / 浏览器 / 网络）。"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.jd import CONNECTOR, SPEC, fetch, plan, rows as build_rows
from connectors.jd.reader import apply_login_cookie_rotations
from connectors.kit.cookies import Cookie, CookieJar, CookieProvider
from connectors.kit.errors import AuthRequired
from connectors.kit.http import Response
from connectors.kit.runner import main
from connectors.kit.spec import inspect_site, probe, render_inspection, render_keepalive
from connectors.kit.testing import PASSWORD, make_cookie_db, mock_sessions

EPOCH_1601 = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
OCCURRED_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
AMOUNT_RE = re.compile(r"^\d+\.\d{2}$")
REQUIRED_KEYS = {"source", "record_type", "id", "occurred", "amount", "direction", "merchant", "title", "status", "extra"}


def setUpModule():
    mock_sessions()


def chromium_expiry(when):
    return int((when - EPOCH_1601).total_seconds() * 1_000_000)


FUTURE = chromium_expiry(datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc))


def make_site_db(path, cookies):
    """cookies: (host_key, name, value, expires_utc) tuples."""
    make_cookie_db(path, [(host, name, value, b"v10") for host, name, value, _ in cookies])
    connection = sqlite3.connect(path)
    try:
        for _, name, _, expires in cookies:
            connection.execute("UPDATE cookies SET expires_utc=? WHERE name=?", (expires, name))
        connection.commit()
    finally:
        connection.close()
    return path


def jd_provider(db):
    return CookieProvider(db, password=PASSWORD, host_suffixes=SPEC.host_suffixes)


class FakeClient:
    user_agent = "pfin-jd-test"

    def __init__(self, *, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.calls = []

    def get(self, url, *, cookie_header="", headers=None):
        self.calls.append(("get", url))
        if not self.get_responses:
            raise AssertionError("unexpected extra GET")
        return self.get_responses.pop(0)

    def post_form(self, url, form, *, cookie_header="", headers=None):
        self.calls.append(("post", url, dict(form)))
        if not self.post_responses:
            raise AssertionError("unexpected extra POST")
        return self.post_responses.pop(0)


def json_response(payload):
    return Response(status=200, url="https://api.m.jd.com/api", headers={}, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def html_response(html, *, url="https://order.jd.com/center/list.action?d=2&s=4096&page=1"):
    return Response(status=200, url=url, headers={}, body=html.encode("utf-8"))


def order_list(total):
    return {"revision": 2, "success": True, "data": {"orderList": [], "parentOrderList": [], "totalNum": total, "extMap": {}}}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(CONNECTOR, argv)
    return code, out.getvalue(), err.getvalue()


def jd_payload():
    return {
        "kind": "jd_pages",
        "pages": [
            {
                "orders": [
                    {
                        "order_id": "J1",
                        "occurred": "2024-01-02 03:04:05",
                        "shop": "京东店",
                        "status": "已完成",
                        "paid": "99.00",
                        "items": [{"title": "京东商品"}],
                        "source": "html",
                    }
                ]
            }
        ],
    }


def baitiao_payload():
    return {
        "kind": "jd_baitiao",
        "bills": [
            {
                "bill_id": "B1",
                "bill_month": "2024-04",
                "bill_date": "2024-04-05",
                "amount": "100.00",
                "paid": "0.00",
                "status": 2,
            }
        ],
        "transactions": [
            {
                "order_id": "BT1",
                "occurred": "2024-03-01 10:00:00",
                "product": "白条商品",
                "amount": "20.00",
                "discount": "0.00",
            }
        ],
        "unbilled": None,
        "pages": {"bills": 1, "transactions": 1},
    }


class RowsTests(unittest.TestCase):
    def test_orders_alone_have_the_required_shape(self):
        rows = build_rows(jd_payload())
        self.assertEqual(len(rows), 1)
        for row in rows:
            self.assertEqual(set(row), REQUIRED_KEYS)
            self.assertEqual((row["source"], row["record_type"], row["direction"]), ("jd", "order", "out"))
            self.assertTrue(row["id"].startswith("jd:"))
            self.assertRegex(row["occurred"], OCCURRED_RE)
            self.assertRegex(row["amount"], AMOUNT_RE)

    def test_rows_include_baitiao_fetched_in_the_same_run(self):
        jd = jd_payload()
        alone = build_rows(jd)
        merged = build_rows({**jd, "baitiao": baitiao_payload()})
        self.assertGreater(len(merged), len(alone))
        baitiao = [r for r in merged if r["source"] == "jd-baitiao"]
        self.assertEqual({r["record_type"] for r in baitiao}, {"bill", "transaction"})
        for row in baitiao:
            self.assertEqual(set(row), REQUIRED_KEYS)
            self.assertTrue(row["id"].startswith("jd-baitiao:"))
            self.assertEqual(row["direction"], "out")
            self.assertRegex(row["amount"], AMOUNT_RE)

    def test_ids_are_unique_and_stable_across_runs(self):
        payload = {**jd_payload(), "baitiao": baitiao_payload()}
        first = [r["id"] for r in build_rows(payload)]
        self.assertEqual(first, [r["id"] for r in build_rows(payload)])
        self.assertEqual(len(first), len(set(first)))

    def test_duplicate_orders_across_pages_are_dropped(self):
        payload = jd_payload()
        payload["pages"].append(payload["pages"][0])
        self.assertEqual(len(build_rows(payload)), 1)

    def test_unbilled_becomes_a_bill_row(self):
        baitiao = baitiao_payload()
        baitiao["unbilled"] = {"bill_id": None, "bill_date": "2024-05-01", "amount": "7.00", "should_pay": "7.00"}
        rows = build_rows({"pages": [], "baitiao": baitiao})
        self.assertIn("unbilled", [r["status"] for r in rows])


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "Cookies"
        make_site_db(self.db, [
            (".jd.com", "pt_key", "secret-pt-key", FUTURE),
            (".jd.com", "pt_pin", "secret-pt-pin", 0),
            (".jd.com", "thor", "secret-thor", FUTURE),
        ])

    def test_missing_required_cookie_needs_login(self):
        connection = sqlite3.connect(self.db)
        connection.execute("DELETE FROM cookies WHERE name='pt_pin'")
        connection.commit()
        connection.close()
        inspection = inspect_site(SPEC, jd_provider(self.db))
        self.assertTrue(inspection.needs_login)
        self.assertIn("pt_pin", inspection.missing_required)
        self.assertIn("扫码登录", "\n".join(render_inspection(inspection)))

    def test_pc_login_set_without_mobile_cookies(self):
        db = Path(self.temp.name) / "jd-pc-only.db"
        make_site_db(db, [
            (".jd.com", "thor", "secret-thor", FUTURE),
            (".jd.com", "pin", "secret-pin", 0),
            (".jd.com", "pinId", "secret-pin-id", FUTURE),
            (".jd.com", "unick", "secret-nick", 0),
            (".jd.com", "TrackID", "secret-track-id", FUTURE),
        ])
        inspection = inspect_site(SPEC, jd_provider(db))
        self.assertFalse(inspection.needs_login)
        self.assertEqual(inspection.login_set.key, "pc")
        lines = "\n".join(render_inspection(inspection))
        self.assertIn("登录方式: PC（thor + pin）", lines)
        self.assertIn("结论: 登录 Cookie 存在且未过期", lines)
        self.assertNotIn("secret-", lines)

    def test_neither_login_set_needs_login(self):
        db = Path(self.temp.name) / "jd-no-login.db"
        make_site_db(db, [(".jd.com", "thor", "secret-thor", FUTURE), (".jd.com", "pinId", "secret-pin-id", FUTURE)])
        inspection = inspect_site(SPEC, jd_provider(db))
        self.assertTrue(inspection.needs_login)
        self.assertIn("pin", inspection.missing_required)
        self.assertIn("pt_pin", inspection.missing_required)
        lines = "\n".join(render_inspection(inspection))
        self.assertIn("未检测到完整组合", lines)
        self.assertIn("PC（thor + pin）", lines)
        self.assertIn("移动端（pt_key + pt_pin）", lines)
        self.assertNotIn("secret-", lines)

    def test_cli_status_offline_reports_pc_login_set_and_hides_values(self):
        db = Path(self.temp.name) / "jd-pc-status.db"
        make_site_db(db, [
            (".jd.com", "thor", "secret-thor", FUTURE),
            (".jd.com", "pin", "secret-pin", 0),
        ])
        with mock.patch("subprocess.run", side_effect=AssertionError("keychain")):
            code, out, _ = run(["status", "--cookie-db", str(db)])
        self.assertEqual(code, 0)
        self.assertIn("登录方式: PC（thor + pin）", out)
        self.assertNotIn("secret-", out)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "Cookies"
        make_site_db(self.db, [
            (".jd.com", "pt_key", "secret-pt-key", FUTURE),
            (".jd.com", "pt_pin", "secret-pt-pin", FUTURE),
            (".jd.com", "thor", "secret-thor", FUTURE),
        ])

    def probe(self, provider, client):
        return probe(SPEC, provider, client, CONNECTOR.check)

    def test_reads_the_api_once(self):
        client = FakeClient(post_responses=[json_response(order_list(16))])
        result = self.probe(jd_provider(self.db), client)
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "post")
        self.assertIn("totalNum=16", result.detail)

    def test_accepts_pc_login_set_only(self):
        db = Path(self.temp.name) / "jd-pc-probe.db"
        make_site_db(db, [(".jd.com", "thor", "secret-thor", FUTURE), (".jd.com", "pin", "secret-pin", FUTURE)])
        client = FakeClient(post_responses=[json_response(order_list(0))])
        result = self.probe(jd_provider(db), client)
        self.assertEqual(result.status, "ok")
        self.assertIn("PC（thor + pin）", result.detail)
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("secret-", result.detail)

    def test_login_page_is_logged_out(self):
        client = FakeClient(post_responses=[html_response(
            "<html><title>京东登录</title><body>账户登录</body></html>", url="https://passport.jd.com/new/login.aspx")])
        result = self.probe(jd_provider(self.db), client)
        self.assertEqual(result.status, "logged_out")
        self.assertIn("扫码登录", result.detail)

    def test_empty_cookie_db_is_reported_as_needing_login(self):
        empty = Path(self.temp.name) / "EmptyCookies"
        make_cookie_db(empty, [])
        client = FakeClient()
        result = self.probe(jd_provider(empty), client)
        self.assertEqual(result.status, "logged_out")
        self.assertIn("扫码登录", render_keepalive(result))
        self.assertEqual(client.calls, [])

    def test_missing_login_cookie_is_logged_out_without_request(self):
        connection = sqlite3.connect(self.db)
        connection.execute("DELETE FROM cookies WHERE name='pt_pin'")
        connection.commit()
        connection.close()
        client = FakeClient()
        result = self.probe(jd_provider(self.db), client)
        self.assertEqual(result.status, "logged_out")
        self.assertIn("pt_pin", result.detail)
        self.assertEqual(client.calls, [])

    def test_unexpected_error_has_code_location_and_hides_the_message(self):
        jar = CookieJar([Cookie(".jd.com", "thor", "x"), Cookie(".jd.com", "pin", "x")])
        with mock.patch("connectors.jd.JdFetcher.probe_api", side_effect=AttributeError("PRIVATE_SENTINEL")):
            result = probe(SPEC, mock.Mock(load=lambda: jar), mock.Mock(), CONNECTOR.check)
        self.assertEqual(result.status, "internal_error")
        self.assertIn("AttributeError", result.detail)
        self.assertNotIn("PRIVATE_SENTINEL", result.detail)

    def test_rotated_login_cookie_is_used_in_memory_only(self):
        jar = CookieJar([Cookie(".jd.com", "thor", "old")])
        rotated = Response(
            status=200, url="https://order.jd.com/center/list.action?page=1", headers={}, body=b"<html></html>",
            set_cookies=["thor=new-thor; Domain=.jd.com; Path=/", "unrelated=ignored; Domain=.jd.com; Path=/"],
        )
        self.assertEqual(apply_login_cookie_rotations(jar, rotated), 1)
        self.assertEqual(jar.get("thor", "order.jd.com"), "new-thor")
        self.assertEqual(jar.get("unrelated", "order.jd.com"), "ignored")

    def test_baitiao_bill_probe_is_one_read_request(self):
        from connectors.jd.baitiao import BILL_ENDPOINT, BaitiaoFetcher
        jar = CookieJar([Cookie(".jd.com", "thor", "private"), Cookie(".jd.com", "pin", "private")])
        client = mock.Mock()
        client.post_form.return_value = Response(200, BILL_ENDPOINT, {}, b'{"billList":[],"totalCount":0}')
        info = BaitiaoFetcher(client, jar, max_pages=1).probe()
        self.assertIn("orders", info)
        client.post_form.assert_called_once()
        self.assertEqual(client.post_form.call_args.args[0], BILL_ENDPOINT)


class CliTests(unittest.TestCase):
    def test_manifest(self):
        code, out, _ = run(["manifest"])
        manifest = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual((manifest["key"], manifest["label"], manifest["keepalive_hours"]), ("jd", "京东", 1.0))
        self.assertEqual(manifest["outputs"], {"raw": "jd-orders-<ts>.json", "rows": "jd-rows-<ts>.jsonl"})
        self.assertTrue(manifest["read_only"])

    def test_dry_run_touches_nothing_and_mentions_baitiao(self):
        with mock.patch("connectors.kit.runner.provider", side_effect=AssertionError("cookies")):
            code, out, _ = run(["fetch", "--dry-run", "--years", "2023-2024"])
        self.assertEqual(code, 0)
        self.assertIn("years: 2023, 2024", out)
        self.assertIn("getBillList", out)
        code, out, _ = run(["fetch", "--dry-run", "--no-baitiao", "--html-only", "--concurrency", "3"])
        self.assertIn("baitiao: skipped", out)
        self.assertIn("HTML fallback: disabled", out)
        self.assertIn("concurrency: 3", out)

    def test_bad_years_is_a_usage_error(self):
        code, _, err = run(["fetch", "--out", tempfile.gettempdir(), "--years", "abc"])
        self.assertEqual(code, 2)

    def test_fetch_writes_orders_and_baitiao_rows_and_summary(self):
        payload = {**jd_payload(), "baitiao": baitiao_payload()}
        seen = {}

        def fake_fetch(args, jar):
            seen["no_baitiao"] = args.no_baitiao
            return payload, ["提醒"]

        fake_provider = mock.Mock(load=lambda: CookieJar())
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("connectors.kit.runner.provider", return_value=fake_provider), \
             mock.patch.object(CONNECTOR, "fetch", fake_fetch), \
             mock.patch.object(CONNECTOR, "summary", lambda p: "jd summary"):
            code, out, err = run(["fetch", "--out", tmp])
            self.assertEqual(code, 0)
            self.assertFalse(seen["no_baitiao"])
            self.assertIn("提醒", err)
            raw = list(Path(tmp).glob("jd-orders-*.json"))
            rows_files = list(Path(tmp).glob("jd-rows-*.jsonl"))
            self.assertEqual((len(raw), len(rows_files)), (1, 1))
            sources = {json.loads(line)["source"] for line in rows_files[0].read_text().splitlines()}
            self.assertEqual(sources, {"jd", "jd-baitiao"})

    def test_summary_includes_baitiao_only_when_present(self):
        from connectors.jd import summary
        self.assertNotIn("jd-baitiao", summary(jd_payload()))
        self.assertIn("jd-baitiao: bills=1", summary({**jd_payload(), "baitiao": baitiao_payload()}))


class FetchTests(unittest.TestCase):
    def args(self, **over):
        import argparse
        base = dict(years="2024", html_only=False, concurrency=1, max_pages=3, delay=1.2, timeout=5.0, no_baitiao=False)
        base.update(over)
        return argparse.Namespace(**base)

    def test_baitiao_is_read_with_the_same_jar_and_failures_become_warnings(self):
        jar = CookieJar([Cookie(".jd.com", "thor", "x")])
        order_fetcher = mock.Mock()
        order_fetcher.fetch.return_value = {"kind": "jd_pages", "pages": []}
        order_fetcher.warnings = ["订单提醒"]
        with mock.patch("connectors.jd.JdFetcher", return_value=order_fetcher), \
             mock.patch("connectors.jd.fetch_baitiao", side_effect=AuthRequired("白条要重新登录")):
            payload, warnings = fetch(self.args(), jar)
        self.assertNotIn("baitiao", payload)
        self.assertEqual(warnings[0], "订单提醒")
        self.assertTrue(warnings[1].startswith("白条未读取："))

    def test_no_baitiao_skips_the_baitiao_read(self):
        order_fetcher = mock.Mock()
        order_fetcher.fetch.return_value = {"kind": "jd_pages", "pages": []}
        order_fetcher.warnings = []
        with mock.patch("connectors.jd.JdFetcher", return_value=order_fetcher), \
             mock.patch("connectors.jd.fetch_baitiao", side_effect=AssertionError("baitiao")):
            payload, warnings = fetch(self.args(no_baitiao=True), CookieJar())
        self.assertEqual((payload, warnings), ({"kind": "jd_pages", "pages": []}, []))

    def test_baitiao_payload_is_attached(self):
        order_fetcher = mock.Mock()
        order_fetcher.fetch.return_value = {"kind": "jd_pages", "pages": []}
        order_fetcher.warnings = []
        with mock.patch("connectors.jd.JdFetcher", return_value=order_fetcher), \
             mock.patch("connectors.jd.fetch_baitiao", return_value=(baitiao_payload(), ["白条提醒"])) as fb:
            payload, warnings = fetch(self.args(), CookieJar())
        self.assertEqual(payload["baitiao"]["kind"], "jd_baitiao")
        self.assertEqual(warnings, ["白条提醒"])
        self.assertEqual(fb.call_args.args[0].page_size, 10)
        self.assertFalse(fb.call_args.args[0].no_unbilled)


if __name__ == "__main__":
    unittest.main()
