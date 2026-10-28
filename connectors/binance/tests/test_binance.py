import argparse
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.binance import CONNECTOR, add_extra, add_fetch_args, reader
from connectors.kit import apikey as common
from connectors.kit.errors import AuthRequired
from connectors.kit.runner import main

COVERAGE = [{"category": "deposits", "status": "collected", "records": 3}, {"category": "pay_transfers", "status": "gap", "reason": "x"}]


class BinanceTests(unittest.TestCase):
    def test_default_windows_follow_today(self):
        today = datetime.date(2024, 3, 1)
        with mock.patch("connectors.binance.datetime.date") as date:
            date.today.return_value = today
            parser = argparse.ArgumentParser()
            add_fetch_args(parser)
            self.assertEqual(parser.parse_args([]).since, "2023-03-02")
            parser = argparse.ArgumentParser()
            add_extra(parser.add_subparsers(dest="action"))
            args = parser.parse_args(["futures-archive", "--out", "."])
            self.assertEqual((args.since, args.split), ("2023-03-02", "2024-03-01"))

    def test_allow_list_has_no_write_endpoints(self):
        paths = {p for ps in reader.ALLOW.values() for p in ps}
        for word in ("order/place", "cancel", "withdraw/apply", "transfer", "trade/order", "batch"):
            self.assertFalse([p for p in paths if word in p and "history" not in p and "asyn" not in p], word)

    def test_endpoint_outside_allow_list_is_refused(self):
        with mock.patch.object(reader.Client, "__init__", lambda self, out: None):
            with self.assertRaises(ValueError):
                reader.Client(Path("."))  .get("api.binance.com", "/api/v3/order", {})

    def test_missing_key_asks_to_store_it(self):
        with mock.patch("connectors.kit.apikey.read_secret", return_value=None):
            with self.assertRaises(AuthRequired):
                reader.Client(Path(tempfile.mkdtemp()))

    def test_windows_and_since(self):
        self.assertEqual(list(common.windows(0, 9, 4)), [(0, 3), (4, 7), (8, 9)])
        self.assertEqual(common.since_ms("2026-01-01"), 1767196800000)

    def test_write_private_is_0600(self):
        path = common.write_private(Path(tempfile.mkdtemp()) / "a.json", {"a": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_fetch_writes_coverage_payload_and_empty_rows(self):
        out = Path(tempfile.mkdtemp())
        with mock.patch.object(reader, "fetch", return_value=COVERAGE) as fetch, mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = main(CONNECTOR, ["fetch", "--out", str(out), "--since", "2026-01-01"])
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_args.args, (out, 1767196800000))
        raw = json.loads(next(out.glob("binance-api-*.json")).read_text())
        self.assertEqual(raw["coverage"], COVERAGE)
        self.assertEqual(next(out.glob("binance-rows-*.jsonl")).read_text(), "")

    def test_fetch_without_key_is_exit_3(self):
        with mock.patch("connectors.kit.apikey.read_secret", return_value=None), mock.patch("sys.stderr"), \
                mock.patch.object(reader.urllib.request, "OpenerDirector"):
            code = main(CONNECTOR, ["fetch", "--out", tempfile.mkdtemp()])
        self.assertEqual(code, 3)

    def test_futures_archive_action(self):
        with mock.patch.object(reader, "futures_archive", return_value=[{"kind": "order", "status": "requested"}]) as fa, \
                mock.patch("sys.stdout"):
            code = main(CONNECTOR, ["futures-archive", "--out", tempfile.mkdtemp(), "--since", "2026-01-01", "--split", "2026-07-01"])
        self.assertEqual(code, 0)
        self.assertEqual(fa.call_args.args[1:], (1767196800000, common.since_ms("2026-07-01")))

    def test_status_reports_names_only(self):
        with mock.patch("connectors.kit.apikey.secret_names", return_value=["BINANCE_API_KEY"]), mock.patch("sys.stdout"):
            self.assertEqual(main(CONNECTOR, ["status"]), 3)
        with mock.patch("connectors.kit.apikey.secret_names", return_value=list(CONNECTOR.secrets)), mock.patch("sys.stdout"):
            self.assertEqual(main(CONNECTOR, ["status"]), 0)

    def test_manifest_and_dry_run(self):
        m = CONNECTOR.manifest()
        self.assertEqual((m["login"], m["secrets"]), ("api-key", ["BINANCE_API_KEY", "BINANCE_SECRET_KEY"]))
        with mock.patch("sys.stdout"), mock.patch("connectors.kit.apikey.read_secret") as rs:
            self.assertEqual(main(CONNECTOR, ["fetch", "--dry-run"]), 0)
        rs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
