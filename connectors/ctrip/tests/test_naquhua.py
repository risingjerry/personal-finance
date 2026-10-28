import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.ctrip import CONNECTOR, _rows_naquhua, rows
from connectors.ctrip import naquhua as N
from connectors.kit.browser import BrowserEngine
from connectors.kit.cookies import Cookie, CookieJar
from connectors.kit.errors import AuthRequired, NetworkError
from connectors.kit.output import file_mode
from connectors.kit.runner import main
from connectors.kit.testing import mock_sessions

mock_sessions()

TRADE = {"billSerialNo": "B1", "productName": "某酒店", "platText": "携程消费", "amount": 100.0, "orderDate": 1640995200000, "amountDesc": "计入1月账单", "loanTerm": 1}
REPAY = {"repayStatus": "SUCCESS", "serialNo": "R1", "launchTypeDesc": "主动还款-2022年1月账单", "repayFinishDate": 1642809600000, "repaySuccAmount": 100.0}

ORDERS = {
    "kind": "ctrip_orders",
    "pages": [
        {"kind": "recent", "page_index": 1, "orders": [
            {"order_id": "C1", "category": "hotel", "biz_type": "HotelDomestic", "order_type": "Hotel", "title": "某酒店 1 晚",
             "occurred": "2021-02-01 12:00:00", "status": "已入住", "paid": "200.00", "currency": "CNY",
             "items": [{"title": "某酒店", "quantity": 1}]}]},
        {"kind": "history", "page_index": 1, "orders": [
            {"order_id": "C2", "category": "flight", "biz_type": "FlightDomestic", "order_type": "Flight", "title": "某地 → 某地",
             "occurred": "2021-01-01 08:00:00", "status": "已完成", "paid": "1000.00", "currency": "CNY", "items": []}]},
    ],
}


def result(**over):
    tabs = [
        {"tab": "trade", "ended": True, "responses": [{"api": "queryConsumerRecords", "status": 200, "body": {"data": {"records": [TRADE]}}},
                                                       {"api": "queryConsumerRecords", "status": 200, "body": {"data": {"records": [TRADE]}}}]},
        {"tab": "repay", "ended": True, "responses": [{"api": "queryRepayRecords", "status": 200, "body": {"data": {"records": [REPAY]}}}]},
    ]
    return json.dumps({"ok": True, "tabs": tabs, **over})


def fake_engine():
    return BrowserEngine("node", "", "")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(CONNECTOR, argv)
    return code, out.getvalue(), err.getvalue()


class NaquhuaTests(unittest.TestCase):
    def jar(self):
        return CookieJar([Cookie(".ctrip.com", "cticket", "secret"), Cookie(".taobao.com", "x", "y")])

    def test_only_ctrip_cookies_go_to_the_browser(self):
        self.assertEqual([c["name"] for c in N.playwright_cookies(self.jar())], ["cticket"])

    def test_cookies_travel_on_stdin_never_argv(self):
        seen = {}

        def runner(request, *, timeout, env):
            seen["request"] = request
            return result()

        with mock.patch("connectors.kit.browser.detect_browser_engine", lambda prefer="auto": fake_engine()):
            out = N.fetch(self.jar(), runner=runner)
        self.assertIn("cticket", seen["request"])
        self.assertEqual(N.summarize(out), {"消费": 1, "还款": 1, "退款": 0, "分期": 0})

    def test_login_page_raises_auth_required(self):
        with mock.patch("connectors.kit.browser.detect_browser_engine", lambda prefer="auto": fake_engine()):
            with self.assertRaises(AuthRequired):
                N.fetch(self.jar(), runner=lambda r, **k: json.dumps({"ok": True, "tabs": [{"tab": "trade", "login_required": True}]}))
            with self.assertRaises(AuthRequired):
                N.fetch(CookieJar([]), runner=lambda r, **k: result())

    def test_script_ships_next_to_the_module(self):
        self.assertTrue(N.SCRIPT.exists())
        self.assertEqual(N.SCRIPT.name, "naquhua.cjs")

    def test_rows_are_bills_out_and_repayments_in_beijing_time(self):
        out = _rows_naquhua({"naquhua": json.loads(result())})
        self.assertEqual([(r["record_type"], r["direction"]) for r in out], [("bill", "out"), ("transaction", "in")])
        self.assertEqual(out[0]["occurred"], "2022-01-01 08:00:00")
        self.assertEqual(out[1]["occurred"], "2022-01-22 08:00:00")
        self.assertEqual(out[0]["amount"], "100.00")


class RowsTests(unittest.TestCase):
    def test_orders_become_out_rows_with_stable_ids(self):
        out = rows(ORDERS)
        self.assertEqual(len(out), 2)
        self.assertEqual({r["direction"] for r in out}, {"out"})
        self.assertEqual({r["id"] for r in out}, {"ctrip:order:C1", "ctrip:order:C2"})
        self.assertEqual([r["id"] for r in out], [r["id"] for r in rows(ORDERS)])

    def test_naquhua_rows_are_added_only_when_present(self):
        merged = rows({**ORDERS, "naquhua": json.loads(result())})
        self.assertEqual({r["source"] for r in merged}, {"ctrip", "ctrip-naquhua"})
        self.assertEqual(len(merged), 4)


class CliTests(unittest.TestCase):
    def payload(self, **extra):
        return {**ORDERS, **extra}

    def test_fetch_writes_raw_and_rows_and_summary_with_naquhua(self):
        payload = self.payload(naquhua=json.loads(result()))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(CONNECTOR, "fetch", return_value=(payload, [])), \
                mock.patch("connectors.kit.runner.provider"):
            code, out, err = run(["fetch", "--out", tmp])
            raw = list(Path(tmp).glob("ctrip-orders-*.json"))
            rws = list(Path(tmp).glob("ctrip-rows-*.jsonl"))
            self.assertEqual((code, len(raw), len(rws)), (0, 1, 1))
            self.assertEqual((file_mode(raw[0]), file_mode(rws[0])), (0o600, 0o600))
            self.assertEqual(len(rws[0].read_text(encoding="utf-8").splitlines()), 4)
        self.assertIn("orders=2", out)
        self.assertIn("by_type: flight=1 hotel=1", out)
        self.assertIn("by_source: recent=1 history=1", out)
        self.assertIn("拿去花: 消费=1 还款=1", out)

    def test_page_size_out_of_range_is_usage_error(self):
        for bad in ("0", "301"):
            with tempfile.TemporaryDirectory() as tmp, mock.patch("connectors.kit.runner.provider"):
                code, _, err = run(["fetch", "--out", tmp, "--page-size", bad])
            self.assertEqual(code, 2)
            self.assertIn("--page-size", err)

    def test_naquhua_failure_only_warns_and_no_naquhua_skips_it(self):
        import connectors.ctrip as C

        class Args:
            delay, timeout, page_size, max_pages, no_naquhua = 1.0, 5.0, 15, 1, False

        class FakeFetcher:
            warnings = ["orders warning"]

            def __init__(self, *a, **k):
                pass

            def fetch(self):
                return {"pages": []}

        with mock.patch.object(C, "CtripFetcher", FakeFetcher), \
                mock.patch.object(C, "fetch_naquhua", side_effect=NetworkError("boom")) as nq:
            payload, warnings = C.fetch(Args, CookieJar([]))
            self.assertNotIn("naquhua", payload)
            self.assertEqual(warnings[0], "orders warning")
            self.assertIn("拿去花未读取", warnings[1])
            nq.assert_called_once()
            nq.reset_mock()
            Args.no_naquhua = True
            C.fetch(Args, CookieJar([]))
            nq.assert_not_called()


if __name__ == "__main__":
    unittest.main()
