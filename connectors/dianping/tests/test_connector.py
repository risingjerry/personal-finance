"""Connector-level tests: rows, status/probe hooks, CLI shape (offline)."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.kit.cookies import Cookie, CookieJar, CookieProvider
from connectors.kit.output import file_mode
from connectors.kit.runner import main as run_main
from connectors.kit.spec import inspect_site, probe
from connectors.kit.testing import mock_sessions
from connectors.dianping import CONNECTOR, SPEC, rows

EPOCH_1601 = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
FUTURE = int((datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc) - EPOCH_1601).total_seconds() * 1_000_000)


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


class FakeClient:
    def __init__(self):
        self.calls = []
        self.cookie_jar = None

    def get(self, url, **kwargs):
        self.calls.append(("get", url))
        raise AssertionError("unexpected network call")


class FakeFetcher:
    info = {}

    def __init__(self, client, *args, **kwargs):
        pass

    def probe(self):
        return dict(self.info)

    def fetch(self):
        return {"pages": [{"orders": [{"order_id": "D1"}]}]}


class FakeProvider:
    def __init__(self, jar):
        self.jar = jar

    def load(self):
        return self.jar


def run(argv, connector=CONNECTOR):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_main(connector, argv)
    return code, out.getvalue(), err.getvalue()


def setUpModule():
    mock_sessions()


def dianping_payload():
    return {
        "kind": "dianping_orders",
        "pages": [
            {
                "source": "daocan",
                "page": 1,
                "order_id": "DP1",
                "orders": [
                    {
                        "order_id": "DP1",
                        "title": "示例商品：100元代金券1张，可叠加",
                        "occurred": "2021-01-01",
                        "expire_at": "2022-02-01",
                        "quantity": "2",
                        "amount": "¥100",
                        "status": "支付成功",
                        "coupon_codes": ["000000000001"],
                    }
                ],
            }
        ],
    }


class DianpingConnectorTests(unittest.TestCase):
    def test_rows(self):
        out = rows(dianping_payload())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "dianping:order:DP1")
        self.assertEqual(out[0]["amount"], "100.00")
        self.assertEqual(out[0]["direction"], "out")

    def test_probe_reads_one_html_page(self):
        jar = CookieJar([Cookie(".dianping.com", "dper", "v")])
        with mock.patch("connectors.dianping.DianpingDaocanFetcher", FakeFetcher):
            result = probe(SPEC, FakeProvider(jar), FakeClient(), CONNECTOR.check)
        self.assertEqual(result.status, "ok")
        self.assertIn("新餐饮订单", result.detail)

    def test_missing_cookie_returns_logged_out_without_network(self):
        client = FakeClient()
        result = probe(SPEC, FakeProvider(CookieJar()), client, CONNECTOR.check)
        self.assertEqual(result.status, "logged_out")
        self.assertEqual(client.calls, [])

    def test_manifest_and_ten_second_floor(self):
        code, text, _ = run(["manifest"])
        manifest = json.loads(text)
        self.assertEqual((code, manifest["key"], manifest["keepalive_hours"]), (0, "dianping", 24.0))
        self.assertEqual(CONNECTOR.min_delay, 10.0)
        self.assertEqual(CONNECTOR.max_pages, 100)

    def test_dry_run_offline_and_mentions_rows(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             mock.patch("subprocess.run", side_effect=AssertionError("keychain")):
            code, text, errors = run(["fetch", "--dry-run"])
        self.assertEqual(code, 0, errors)
        self.assertIn("dry-run", text)
        self.assertIn("rows:", text)
        self.assertIn("jsonl", text)
        self.assertIn("max_pages=100", text)

    def test_missing_out_is_usage_error_before_keychain(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("keychain")), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            code, _text, errors = run(["fetch"])
        self.assertEqual(code, 2, errors)
        self.assertIn("--out", errors)

    def test_page_size_must_be_positive(self):
        jar = CookieJar([Cookie(".dianping.com", "dper", "v")])
        with mock.patch("connectors.kit.runner.provider", lambda c, a: FakeProvider(jar)):
            code, _, _ = run(["fetch", "--out", "/tmp", "--page-size", "0"])
        self.assertEqual(code, 2)

    def test_writes_raw_and_rows_with_same_stamp(self):
        replaced = dataclasses.replace(CONNECTOR, fetch=lambda args, jar: (dianping_payload(), []))
        jar = CookieJar([Cookie(".dianping.com", "dper", "v")])
        with tempfile.TemporaryDirectory() as temp, mock.patch("connectors.kit.runner.provider", lambda c, a: FakeProvider(jar)):
            code, text, errors = run(["fetch", "--out", temp], replaced)
            self.assertEqual(code, 0, errors)
            raw = list(Path(temp).glob("dianping-orders-*.json"))
            out = list(Path(temp).glob("dianping-rows-*.jsonl"))
            self.assertEqual((len(raw), len(out)), (1, 1))
            self.assertEqual((file_mode(raw[0]), file_mode(out[0])), (0o600, 0o600))
            stamp = raw[0].name[len("dianping-orders-"):-5]
            self.assertEqual(out[0].name, f"dianping-rows-{stamp}.jsonl")
            first = json.loads(out[0].read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first["source"], "dianping")


if __name__ == "__main__":
    unittest.main()
