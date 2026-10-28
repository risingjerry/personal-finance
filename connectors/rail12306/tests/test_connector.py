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
from connectors.rail12306 import CONNECTOR, SPEC, check, rows
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


def rail_payload():
    return {
        "kind": "rail_12306",
        "pages": [
            {
                "orders": [
                    {
                        "order_id": "R1",
                        "sequence_no": "S1",
                        "occurred": "2024-05-01 09:00:00",
                        "paid": "553.00",
                        "status": "已支付",
                        "tickets": [
                            {
                                "train_code": "G1",
                                "from_station": "北京南",
                                "to_station": "上海虹桥",
                                "travel_date": "2024-05-02 00:00:00",
                                "price": "553.00",
                            }
                        ],
                    }
                ]
            }
        ],
    }


def run_with_state(connector, argv):
    with tempfile.TemporaryDirectory() as state, mock.patch.dict("os.environ", {"PFIN_STATE_DIR": state}):
        return run(connector, argv)


class RailRowsTests(unittest.TestCase):
    def test_trip_row(self):
        out = rows(rail_payload())
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["source"], "12306")
        self.assertEqual(row["record_type"], "trip")
        self.assertEqual(row["id"], "12306:trip:R1")
        self.assertEqual(row["amount"], "553.00")
        self.assertEqual(row["direction"], "out")
        self.assertEqual(row["title"], "G1 北京南→上海虹桥")
        self.assertEqual(rows(rail_payload()), out)

    def test_row_without_order_time_uses_travel_date(self):
        payload = rail_payload()
        payload["pages"][0]["orders"][0]["occurred"] = None
        self.assertEqual(rows(payload)[0]["occurred"], "2024-05-02 00:00:00")


class RailSpecTests(unittest.TestCase):
    def test_session_key_is_kept(self):
        self.assertEqual(SPEC.key, "12306")
        self.assertEqual(CONNECTOR.key, "rail12306")
        self.assertEqual(CONNECTOR.login_kind, "qr")

    def test_missing_tk_needs_login(self):
        with tempfile.TemporaryDirectory() as temp:
            db = make_db(Path(temp) / "Cookies", [
                ("kyfw.12306.cn", "JSESSIONID", "session", FUTURE),
                ("kyfw.12306.cn", "uamtk", "sso", FUTURE),
            ])
            inspection = inspect_site(SPEC, CookieProvider(db, host_suffixes=SPEC.host_suffixes))
        self.assertTrue(inspection.needs_login)
        self.assertIn("tk", inspection.missing_required)

    def test_probe_reports_http_status(self):
        jar = CookieJar([Cookie("kyfw.12306.cn", "tk", "v")])
        with mock.patch("connectors.rail12306.Rail12306Fetcher", FakeFetcher):
            FakeFetcher.info = {"status": 200}
            result = probe(SPEC, FakeProvider(jar), FakeClient(), check)
        self.assertEqual(result.status, "ok")
        self.assertIn("HTTP 200", result.detail)


class RailCliTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("connectors.kit.runner.provider", return_value=FakeProvider(CookieJar()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_manifest(self):
        code, text, _ = run(CONNECTOR, ["manifest"])
        data = json.loads(text)
        self.assertEqual((code, data["key"], data["login"]), (0, "rail12306", "qr"))
        self.assertEqual(data["outputs"]["raw"], "12306-orders-<ts>.json")

    def test_dry_runs_are_offline_and_leave_no_state(self):
        for argv in (["fetch", "--dry-run"], ["invoice", "--dry-run", "--type", "issued"]):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as state, \
                    mock.patch.dict("os.environ", {"PFIN_STATE_DIR": state}), \
                    mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                    mock.patch("subprocess.run", side_effect=AssertionError("keychain")):
                code, text, errors = run(CONNECTOR, argv)
                self.assertEqual(code, 0, errors)
                self.assertIn("dry-run", text)
                self.assertEqual(list(Path(state).iterdir()), [])

    def test_usage_errors(self):
        code, _t, errors = run(CONNECTOR, ["fetch"])
        self.assertEqual(code, 2)
        self.assertIn("--out", errors)
        code, _t, errors = run(CONNECTOR, ["invoice"])
        self.assertEqual(code, 2)
        self.assertIn("--out", errors)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(CONNECTOR, "fetch", side_effect=AssertionError("fetched")):
            code, _t, _e = run(CONNECTOR, ["fetch", "--out", temp, "--page-size", "0"])
        self.assertEqual(code, 2)

    def test_writes_raw_and_rows_with_same_stamp(self):
        payload = rail_payload()
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(CONNECTOR, "fetch", return_value=(payload, [])):
                code, text, errors = run(CONNECTOR, ["fetch", "--out", temp])
            self.assertEqual(code, 0, errors)
            raw = list(Path(temp).glob("12306-orders-*.json"))
            rows_files = list(Path(temp).glob("rail12306-rows-*.jsonl"))
            self.assertEqual((len(raw), len(rows_files)), (1, 1))
            self.assertEqual(file_mode(raw[0]), 0o600)
            stamp = raw[0].name[len("12306-orders-"):-5]
            self.assertEqual(rows_files[0].name, f"rail12306-rows-{stamp}.jsonl")
            first = json.loads(rows_files[0].read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first["source"], "12306")

    def test_invoice_without_app_authorization_exits_3_and_writes_nothing(self):
        class Gate:
            def __init__(self, *a, **k):
                pass

            def check_gate(self):
                return {"psr_authorized": False}

        class Provider:
            def load(self):
                return CookieJar([Cookie("kyfw.12306.cn", "tk", "v")])

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("connectors.rail12306.RailInvoiceFetcher", Gate), \
                mock.patch("connectors.rail12306.provider", return_value=Provider()):
            code, _text, errors = run(CONNECTOR, ["invoice", "--out", temp])
            self.assertEqual(code, 3)
            self.assertIn("授权", errors)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_invoice_writes_raw_and_rows_and_passes_default_end_date(self):
        record = {
            "sequence_no": "E1", "sale_price": "553.00", "local_start_time": "2026-04-25 08:00:00",
            "board_train_code": "G1", "from_station_name": "北京南", "to_station_name": "上海虹桥",
        }
        seen = {}

        class Fetcher:
            def __init__(self, client, jar, **kwargs):
                seen.update(kwargs)

            def check_gate(self):
                return {"psr_authorized": True}

            def fetch(self, start, end, gate=None):
                seen["window"] = (start, end)
                return {"kind": "rail_12306_invoice", "query": {"type": "pending", "total": 1, "requests_sent": 2},
                        "pages": [{"records": [record]}]}

        class Provider:
            def load(self):
                return CookieJar([Cookie("kyfw.12306.cn", "tk", "v")])

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("connectors.rail12306.RailInvoiceFetcher", Fetcher), \
                mock.patch("connectors.rail12306.provider", return_value=Provider()):
            code, text, errors = run(CONNECTOR, ["invoice", "--out", temp, "--json"])
            self.assertEqual(code, 0, errors)
            summary = json.loads(text)
            self.assertEqual((summary["total"], summary["rows"], summary["requests"]), (1, 1, 2))
            self.assertEqual(len(list(Path(temp).glob("12306-invoice-*.json"))), 1)
            rows_files = list(Path(temp).glob("12306-invoice-rows-*.jsonl"))
            self.assertEqual(len(rows_files), 1)
            self.assertEqual(file_mode(rows_files[0]), 0o600)
            self.assertEqual(json.loads(rows_files[0].read_text(encoding="utf-8").splitlines()[0])["source"], "12306")
        self.assertIsNotNone(seen["window"][1])
        self.assertGreaterEqual(seen["min_interval"], 2.0)


if __name__ == "__main__":
    unittest.main()
