import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.kit import apikey as common
from connectors.bitget import CONNECTOR, reader
from connectors.kit.errors import AuthRequired
from connectors.kit.runner import main

COVERAGE = [{"path": "/api/v3/trade/fills", "category": "SPOT", "status": "collected", "rows": 2}]


class BitgetTests(unittest.TestCase):
    def test_allow_list_has_no_write_endpoints(self):
        for word in ("place", "cancel", "withdraw", "transfer", "batch"):
            self.assertFalse([p for p in reader.ALLOW if word in p], word)

    def test_missing_key_asks_to_store_it(self):
        with mock.patch("connectors.kit.apikey.read_secret", return_value=None):
            with self.assertRaises(AuthRequired):
                reader.Client(Path(tempfile.mkdtemp()))

    def test_refuses_a_key_that_can_trade(self):
        with mock.patch("connectors.kit.apikey.read_secret", return_value="x"), \
                mock.patch.object(reader.Client, "get", return_value={"permType": "trade"}):
            with self.assertRaises(RuntimeError):
                reader.fetch(Path(tempfile.mkdtemp()))

    def test_cli_refuses_a_key_that_can_trade(self):
        with mock.patch("connectors.kit.apikey.read_secret", return_value="x"), \
                mock.patch.object(reader.Client, "get", return_value={"permType": "trade"}), mock.patch("sys.stderr"):
            self.assertEqual(main(CONNECTOR, ["fetch", "--out", tempfile.mkdtemp()]), 2)

    def test_fetch_writes_coverage_payload(self):
        out = Path(tempfile.mkdtemp())
        with mock.patch.object(reader, "fetch", return_value=COVERAGE) as fetch, mock.patch("sys.stdout"):
            code = main(CONNECTOR, ["fetch", "--out", str(out), "--days", "30"])
        self.assertEqual((code, fetch.call_args.args), (0, (out, 30)))
        self.assertEqual(json.loads(next(out.glob("bitget-api-*.json")).read_text())["coverage"], COVERAGE)
        self.assertTrue(list(out.glob("bitget-rows-*.jsonl")))

    def test_status_reports_names_only(self):
        with mock.patch("connectors.kit.apikey.secret_names", return_value=[]), mock.patch("sys.stdout"):
            self.assertEqual(main(CONNECTOR, ["status"]), 3)
        with mock.patch("connectors.kit.apikey.secret_names", return_value=list(CONNECTOR.secrets)), mock.patch("sys.stdout"):
            self.assertEqual(main(CONNECTOR, ["status"]), 0)

    def test_manifest_and_dry_run(self):
        m = CONNECTOR.manifest()
        self.assertEqual((m["login"], len(m["secrets"])), ("api-key", 3))
        with mock.patch("sys.stdout"), mock.patch("connectors.kit.apikey.read_secret") as rs:
            self.assertEqual(main(CONNECTOR, ["fetch", "--dry-run"]), 0)
        rs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
