import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.kit.cookies import Cookie, CookieJar, CookieProvider
from connectors.kit.errors import NeedsRiskSignature
from connectors.kit.output import file_mode
from connectors.kit.runner import main as run_main
from connectors.kit.spec import inspect_site, probe
from connectors.meituan import CONNECTOR, SPEC, check, rows
from connectors.kit.testing import make_cookie_db, mock_sessions

mock_sessions()

FUTURE = 13_000_000_000_000_000


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url))
        raise AssertionError("unexpected network call")


class FakeFetcher:
    info = {}

    def __init__(self, client, *args, **kwargs):
        pass

    def probe(self):
        return dict(self.info)


class FakeProvider:
    def __init__(self, jar):
        self.jar = jar

    def load(self):
        return self.jar


def run(connector, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_main(connector, argv)
    return code, out.getvalue(), err.getvalue()


def make_db(path, cookies):
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE cookies (host_key TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', "
            "encrypted_value BLOB NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '/', expires_utc INTEGER NOT NULL DEFAULT 0)"
        )
        for host_key, name, value, expires in cookies:
            connection.execute(
                "INSERT INTO cookies (host_key, name, value, path, expires_utc) VALUES (?, ?, ?, '/', ?)",
                (host_key, name, value, expires),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def meituan_payload():
    return {
        "kind": "meituan_yuefu",
        "pages": [
            {
                "response": {
                    "status": "success",
                    "data": {
                        "monthData": [
                            {
                                "month": "2024-03",
                                "billItemList": [
                                    {
                                        "tradeNo": "M1",
                                        "payId": "P1",
                                        "orderTitle": "外卖",
                                        "businessName": "外卖",
                                        "orderAmount": 1234,
                                        "payTime": "03-04 12:00",
                                        "moneyFlow": 2,
                                        "tradeType": 1,
                                    }
                                ],
                            }
                        ]
                    },
                }
            }
        ],
    }


def meituan_repayment_payload():
    return {
        "kind": "meituan_yuefu",
        "pages": [],
        "repayments": {
            "source": "https://npay.meituan.com/trade/bill/getBillList",
            "filter": {"financialTransType": 4},
            "pages": [
                {
                    "response": {
                        "status": "success",
                        "data": {
                            "nextPage": -1,
                            "monthData": [
                                {
                                    "month": "202609",
                                    "billItemList": [
                                        {
                                            "tradeNo": "R1",
                                            "payId": "RP1",
                                            "orderTitle": "【美团月付】主动还款2026年9月账单",
                                            "businessName": "还款",
                                            "orderAmount": 12345,
                                            "payTime": "09-01 11:00",
                                            "moneyFlow": 0,
                                            "tradeType": 1,
                                            "payDetailTag": "测试银行储蓄卡(1234)",
                                            "payDetails": (
                                                '[{"fundType":"BANKCARD",'
                                                '"moneyCent":12345,'
                                                '"cardNoSuffix":"1234",'
                                                '"bankName":"测试银行",'
                                                '"cardType":"DEBIT"}]'
                                            ),
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                }
            ],
        },
    }


class MeituanRowsTests(unittest.TestCase):
    def test_bill_row(self):
        out = rows(meituan_payload())
        self.assertEqual(out[0]["direction"], "out")
        self.assertEqual(out[0]["amount"], "12.34")
        self.assertEqual(out[0]["source"], "meituan-yuefu")
        self.assertEqual(out[0]["record_type"], "bill")

    def test_rows_are_stable(self):
        self.assertEqual(rows(meituan_payload()), rows(meituan_payload()))

    def test_repayment_is_transaction_in(self):
        out = rows(meituan_repayment_payload())
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["source"], "meituan-yuefu")
        self.assertEqual(row["record_type"], "transaction")
        self.assertEqual(row["id"], "meituan-yuefu:transaction:R1")
        self.assertEqual(row["occurred"], "2026-09-01 11:00:00")
        self.assertEqual(row["amount"], "123.45")
        self.assertEqual(row["direction"], "in")
        self.assertEqual(row["title"], "主动还款")
        self.assertEqual(row["status"], "202609")
        self.assertEqual(row["extra"]["pay_detail_tag"], "测试银行储蓄卡(1234)")
        self.assertEqual(row["extra"]["pay_details"][0]["cardNoSuffix"], "1234")


class MeituanSpecTests(unittest.TestCase):
    def test_session_key_is_kept(self):
        self.assertEqual(SPEC.key, "meituan-yuefu")
        self.assertEqual(CONNECTOR.key, "meituan")

    def test_missing_token_needs_login(self):
        with tempfile.TemporaryDirectory() as temp:
            db = make_db(Path(temp) / "Cookies", [(".meituan.com", "userId", "secret-user", FUTURE)])
            inspection = inspect_site(SPEC, CookieProvider(db, host_suffixes=SPEC.host_suffixes))
        self.assertTrue(inspection.needs_login)
        self.assertIn("token", inspection.missing_required)

    def test_probe_reports_userinfo(self):
        jar = CookieJar([Cookie(".meituan.com", "token", "v")])
        with mock.patch("connectors.meituan.MeituanYuefuFetcher", FakeFetcher):
            FakeFetcher.info = {"has_data": True}
            result = probe(SPEC, FakeProvider(jar), FakeClient(), check)
        self.assertEqual(result.status, "ok")
        self.assertIn("用户信息", result.detail)

    def test_probe_without_login_makes_no_request(self):
        client = FakeClient()
        result = probe(SPEC, FakeProvider(CookieJar()), client, check)
        self.assertEqual(result.status, "logged_out")
        self.assertEqual(client.calls, [])


class MeituanCliTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("connectors.kit.runner.provider", return_value=FakeProvider(CookieJar()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_manifest(self):
        code, text, _ = run(CONNECTOR, ["manifest"])
        data = json.loads(text)
        self.assertEqual((code, data["key"], data["login"]), (0, "meituan", "browser"))
        self.assertEqual(data["outputs"]["raw"], "meituan-yuefu-<ts>.json")

    def test_dry_run_is_offline(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                mock.patch("subprocess.run", side_effect=AssertionError("keychain")):
            code, text, errors = run(CONNECTOR, ["fetch", "--dry-run", "--page-size", "50"])
        self.assertEqual(code, 0, errors)
        self.assertIn("dry-run", text)
        self.assertIn('"pageSize":50', text)
        self.assertIn("jsonl", text)

    def test_missing_out_and_bad_page_size_are_usage_errors(self):
        code, _t, errors = run(CONNECTOR, ["fetch"])
        self.assertEqual(code, 2)
        self.assertIn("--out", errors)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(CONNECTOR, "fetch", side_effect=AssertionError("fetched")):
            code, _t, _e = run(CONNECTOR, ["fetch", "--out", temp, "--page-size", "500"])
        self.assertEqual(code, 2)

    def test_writes_raw_and_rows_with_same_stamp_and_repayments_line(self):
        payload = meituan_repayment_payload()
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(CONNECTOR, "fetch", return_value=(payload, [])), \
                    mock.patch.object(CONNECTOR, "validate", lambda args: None):
                code, text, errors = run(CONNECTOR, ["fetch", "--out", temp, "--no-repayments"])
            self.assertEqual(code, 0, errors)
            raw = list(Path(temp).glob("meituan-yuefu-*.json"))
            rows_files = list(Path(temp).glob("meituan-rows-*.jsonl"))
            self.assertEqual((len(raw), len(rows_files)), (1, 1))
            self.assertEqual(file_mode(raw[0]), 0o600)
            self.assertEqual(file_mode(rows_files[0]), 0o600)
            stamp = raw[0].name[len("meituan-yuefu-"):-5]
            self.assertEqual(rows_files[0].name, f"meituan-rows-{stamp}.jsonl")
            self.assertIn("repayments: count=1", text)
            first = json.loads(rows_files[0].read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first["source"], "meituan-yuefu")

    def test_signature_wall_exits_4_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(CONNECTOR, "fetch", side_effect=NeedsRiskSignature("meituan needs mtgsig")):
                code, _text, errors = run(CONNECTOR, ["fetch", "--out", temp])
            self.assertEqual(code, 4)
            self.assertIn("mtgsig", errors)
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
