"""The Python wrapper that gives the Node alipay-cli the five connector verbs (Node is never run here)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.alipay import CONNECTOR
from connectors.kit.runner import main

BILL = {"trade_no": "2026010122001400000000000001", "amount": "12.50", "direction": "out", "counterparty": "某店",
        "item": "咖啡", "status": "交易成功", "paid_at": "2026-01-01T10:00:00+08:00", "merchant_order_no": "M1", "fee": "0.00"}
FUND = {"id": "1000000000000000001", "occurred_at": "2026-01-02 09:00:00", "income": "50.00", "expense": "0.00",
        "counterparty": "**某", "type": "交易", "raw": {"备注": ""}, "order_id": "TP1"}


def proc(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


class AlipayConnectorTests(unittest.TestCase):
    def run_fetch(self, argv, responses):
        out = Path(tempfile.mkdtemp())
        with mock.patch("connectors.alipay._node", side_effect=responses), mock.patch("sys.stdout"):
            code = main(CONNECTOR, ["fetch", "--out", str(out), *argv])
        return code, out

    def test_personal_bills_become_transaction_rows(self):
        code, out = self.run_fetch([], [proc(json.dumps(BILL) + "\n")])
        self.assertEqual(code, 0)
        row = json.loads(next(out.glob("alipay-rows-*.jsonl")).read_text().splitlines()[0])
        self.assertEqual((row["source"], row["direction"], row["amount"], row["extra"]["trade_no"]), ("alipay", "out", "12.50", BILL["trade_no"]))
        self.assertTrue(list(out.glob("alipay-bills-*.json")))

    def test_keepalive_reads_the_session_field(self):
        for session, want in (("ok", 0), ("need_login", 3), ("browser_down", 3)):
            reply = proc(json.dumps({"ok": True, "cmd": "auth.status", "data": {"session": session}}))
            with mock.patch("connectors.alipay._node", return_value=reply), mock.patch("sys.stdout"):
                self.assertEqual(main(CONNECTOR, ["keepalive"]), want, session)

    def test_merchant_funds_use_their_own_file_names(self):
        ok = json.dumps({"ok": True, "data": {}})
        listed = json.dumps({"ok": True, "data": {"items": [FUND]}})
        code, out = self.run_fetch(["--merchant"], [proc(ok), proc(listed)])
        self.assertEqual(code, 0)
        row = json.loads(next(out.glob("alipay-merchant-rows-*.jsonl")).read_text().splitlines()[0])
        self.assertEqual((row["source"], row["direction"], row["amount"]), ("alipay-merchant", "in", "50.00"))
        self.assertTrue(list(out.glob("alipay-merchant-funds-*.json")))

    def test_expired_login_is_exit_3(self):
        need = json.dumps({"ok": False, "error": {"code": "need_login"}})
        with mock.patch("sys.stderr"):
            code, _ = self.run_fetch(["--sync"], [proc(need, 1)])
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
