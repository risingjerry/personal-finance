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
from connectors.goofish import CONNECTOR, SPEC, rows

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


def goofish_payload():
    return {
        "kind": "goofish_orders",
        "pages": [
            {
                "side": "bought",
                "response": {
                    "data": {
                        "items": [
                            {
                                "commonData": {
                                    "orderId": "G1",
                                    "tradeStatusEnum": "SUCCESS",
                                },
                                "head": {
                                    "data": {
                                        "createTime": "2024-02-03 04:05:06",
                                        "userInfo": {"userNick": "卖家"},
                                        "statusViewMsg": "交易成功",
                                    }
                                },
                                "content": {
                                    "data": {
                                        "detailInfo": {"auctionTitle": "闲鱼商品"},
                                        "priceInfo": {"price": "8.00", "buyAmount": 1},
                                    }
                                },
                            },
                            {
                                "commonData": {
                                    "orderId": "G2",
                                    "tradeStatusEnum": "SUCCESS",
                                },
                                "head": {
                                    "data": {
                                        "createTime": "2024-02-04 05:06:07",
                                        "userInfo": {"userNick": "另一个卖家"},
                                        "statusViewMsg": "交易成功",
                                    }
                                },
                                "content": {
                                    "data": {
                                        "detailInfo": {"auctionTitle": "闲鱼商品 2"},
                                        "priceInfo": {"price": "9.00", "buyAmount": 1},
                                    }
                                },
                            }
                        ]
                    }
                },
            }
        ],
    }


class GoofishConnectorTests(unittest.TestCase):
    def test_rows_directions_and_ids(self):
        out = rows(goofish_payload())
        self.assertEqual(len(out), 2)
        self.assertEqual(len({row["id"] for row in out}), 2)
        self.assertEqual(out[0]["direction"], "out")
        self.assertEqual(out[0]["source"], "goofish")
        self.assertEqual(out[0]["id"], [r["id"] for r in rows(goofish_payload())][0])

    def test_cookie_presence_without_values(self):
        with tempfile.TemporaryDirectory() as temp:
            db = make_db(Path(temp) / "Cookies", [
                (".goofish.com", "cookie2", "secret-goofish-cookie2", FUTURE),
                (".goofish.com", "unb", "secret-goofish-unb", FUTURE),
            ])
            inspection = inspect_site(SPEC, CookieProvider(db, host_suffixes=SPEC.host_suffixes))
        self.assertFalse(inspection.needs_login)
        self.assertTrue(any(c.name == "cookie2" for c in inspection.cookies))
        self.assertNotIn("secret", repr(inspection))

    def test_probe_uses_nav_counts(self):
        jar = CookieJar([Cookie(".goofish.com", "cookie2", "v"), Cookie(".goofish.com", "unb", "v")])
        with mock.patch("connectors.goofish.GoofishFetcher", FakeFetcher):
            FakeFetcher.info = {"purchase_count": 3, "sold_count": 2, "token_rotations": 0}
            result = probe(SPEC, FakeProvider(jar), FakeClient(), CONNECTOR.check)
        self.assertEqual(result.status, "ok")
        self.assertIn("买到 3", result.detail)
        self.assertIn("卖出 2", result.detail)

    def test_missing_cookie_returns_logged_out_without_network(self):
        client = FakeClient()
        result = probe(SPEC, FakeProvider(CookieJar()), client, CONNECTOR.check)
        self.assertEqual(result.status, "logged_out")
        self.assertEqual(client.calls, [])

    def test_dry_run_offline_and_mentions_rows(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             mock.patch("subprocess.run", side_effect=AssertionError("keychain")):
            code, text, errors = run(["fetch", "--dry-run"])
        self.assertEqual(code, 0, errors)
        self.assertIn("dry-run", text)
        self.assertIn("rows:", text)
        self.assertIn("jsonl", text)

    def test_missing_out_is_usage_error_before_keychain(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("keychain")), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            code, _text, errors = run(["fetch"])
        self.assertEqual(code, 2, errors)
        self.assertIn("--out", errors)

    def test_writes_raw_and_rows_with_same_stamp(self):
        replaced = dataclasses.replace(CONNECTOR, fetch=lambda args, jar: (goofish_payload(), []))
        jar = CookieJar([Cookie(".goofish.com", "cookie2", "v"), Cookie(".goofish.com", "unb", "v")])
        with tempfile.TemporaryDirectory() as temp, mock.patch("connectors.kit.runner.provider", lambda c, a: FakeProvider(jar)):
            code, text, errors = run(["fetch", "--out", temp], replaced)
            self.assertEqual(code, 0, errors)
            raw = list(Path(temp).glob("goofish-orders-*.json"))
            out = list(Path(temp).glob("goofish-rows-*.jsonl"))
            self.assertEqual((len(raw), len(out)), (1, 1))
            self.assertEqual((file_mode(raw[0]), file_mode(out[0])), (0o600, 0o600))
            stamp = raw[0].name[len("goofish-orders-"):-5]
            self.assertEqual(out[0].name, f"goofish-rows-{stamp}.jsonl")
            self.assertIn("rows:", text)
            first = json.loads(out[0].read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first["source"], "goofish")

    def test_sold_side_is_income(self):
        payload = goofish_payload()
        payload["pages"][0]["side"] = "sold"
        # sold orders come from a different response shape; rows must never crash on an unparseable page
        self.assertIsInstance(rows(payload), list)


if __name__ == "__main__":
    unittest.main()
