import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors import okx
from connectors.kit.runner import main
from connectors.okx import CONNECTOR, api

ORDER = {"id": "1", "side": "buy", "base_currency": "USDT", "base_amount": "100.00", "quote_amount": "800.00",
         "status": "completed", "counterparty": "某人", "payment_method": "支付宝", "created_ms": 1640995200000,
         "created_at": "2022-01-01T00:00:00.000Z"}
FLOWS = [
    {"key": "deposit:9", "type": "deposit", "currency": "USDT", "amount": "2000.0", "time_ms": 1641081600000},
    {"key": "withdraw:8", "type": "withdraw", "currency": "USDC", "amount": "5", "time_ms": 1641081600000},
]


def proc(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class OkxRowsTests(unittest.TestCase):
    def test_c2c_buy_row_uses_epoch_for_beijing_time(self):
        rows = okx.order_rows([ORDER])
        self.assertEqual(rows[0]["occurred"], "2022-01-01 08:00:00")
        self.assertEqual((rows[0]["amount"], rows[0]["direction"]), ("800.00", "out"))
        self.assertEqual(rows[0]["id"], "okx-c2c:order:1")

    def test_flow_rows_keep_coin_and_direction(self):
        rows = okx.flow_rows(FLOWS)
        self.assertEqual([r["direction"] for r in rows], ["in", "out"])
        self.assertEqual(rows[1]["extra"]["currency"], "USDC")


class OkxCliTests(unittest.TestCase):
    def test_fetch_writes_raw_and_rows(self):
        out = Path(tempfile.mkdtemp())
        answers = [proc(json.dumps(ORDER) + "\n"), proc("\n".join(json.dumps(f) for f in FLOWS) + "\n")]
        with mock.patch("connectors.okx.subprocess.run", side_effect=answers), mock.patch("sys.stdout"):
            code = main(CONNECTOR, ["fetch", "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertTrue(list(out.glob("okx-bills-*.json")))
        lines = next(out.glob("okx-rows-*.jsonl")).read_text().splitlines()
        self.assertEqual([json.loads(x)["source"] for x in lines], ["okx-c2c", "okx-flows", "okx-flows"])

    def test_sync_runs_first(self):
        out = Path(tempfile.mkdtemp())
        with mock.patch("connectors.okx.subprocess.run", side_effect=[proc(), proc(), proc()]) as run, mock.patch("sys.stdout"):
            main(CONNECTOR, ["fetch", "--out", str(out), "--sync"])
        self.assertEqual(run.call_args_list[0].args[0][1], "sync")

    def test_expired_login_is_exit_3(self):
        need = json.dumps({"ok": False, "error": {"code": "need_login"}})
        with mock.patch("connectors.okx.subprocess.run", return_value=proc("", 1, need)), mock.patch("sys.stderr"):
            code = main(CONNECTOR, ["fetch", "--out", tempfile.mkdtemp()])
        self.assertEqual(code, 3)

    def test_status_runs_doctor_and_auth(self):
        with mock.patch("connectors.okx.subprocess.run", side_effect=[proc("doctor ok"), proc("auth ok")]) as run, \
                mock.patch("sys.stdout"):
            self.assertEqual(main(CONNECTOR, ["status"]), 0)
        self.assertEqual([c.args[0][1] for c in run.call_args_list], ["doctor", "auth"])

    def test_api_action_exit_code_follows_coverage(self):
        with mock.patch.object(api, "fetch", return_value=[{"status": "collected"}, {"status": "gap"}]), mock.patch("sys.stdout"):
            self.assertEqual(main(CONNECTOR, ["api", "--out", tempfile.mkdtemp()]), 5)
        with mock.patch.object(api, "fetch", return_value=[{"status": "collected"}]), mock.patch("sys.stdout"):
            self.assertEqual(main(CONNECTOR, ["api", "--out", tempfile.mkdtemp()]), 0)

    def test_api_allow_list_has_no_write_endpoint(self):
        for word in ("place", "cancel", "apply", "transfer", "batch"):
            self.assertFalse([p for p in api.ALLOW if word in p], word)

    def test_api_signature_is_hmac_sha256_base64(self):
        self.assertEqual(api.sign("s", "2026-01-01T00:00:00.000Z", "/api/v5/account/config"),
                         "sb454WB/ghiUX637bo5Xlppk8Ljvnbdj6A0xwLuay7E=")

    def test_manifest_and_dry_run(self):
        self.assertEqual(CONNECTOR.manifest()["login"], "qr")
        with mock.patch("sys.stdout"), mock.patch("connectors.okx.subprocess.run") as run:
            self.assertEqual(main(CONNECTOR, ["fetch", "--dry-run"]), 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
