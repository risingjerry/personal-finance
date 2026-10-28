"""The didi connector as a whole: rows, the shared CLI shape, status, checks, extra actions (offline, no browser)."""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.didi import CONNECTOR, PREFIX, SPEC, check, probe_http, rows
from connectors.kit.cookies import Cookie, CookieJar, CookieProvider
from connectors.kit.errors import NeedsRiskSignature
from connectors.kit.output import file_mode
from connectors.kit.rows import normalize_amount
from connectors.kit.runner import main as _main
from connectors.kit.spec import inspect_site
from connectors.kit.testing import mock_sessions

mock_sessions()

EPOCH_1601 = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
REQUIRED_KEYS = {"source", "record_type", "id", "occurred", "amount", "direction", "merchant", "title", "status", "extra"}
OCCURRED_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
AMOUNT_RE = re.compile(r"^\d+\.\d{2}$")


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = _main(CONNECTOR, argv)
    return code, out.getvalue(), err.getvalue()


def http_payload():
    return {
        "kind": "didi_trips",
        "pages": [{"orders": [{
            "order_id": "D1", "occurred": "2024-06-01 08:00:00", "product_name": "快车", "status": "已完成",
            "from": "起点", "to": "终点", "scene": ["快车"], "fare": "25.50", "source": "history",
        }]}],
    }


def browser_payload():
    return {
        "kind": "didi_trips", "source": "browser", "login_state": "profile", "pages": [],
        "trips": [
            {"order_id": "B1", "occurred": "2024-06-02 09:00:00", "product_name": "专车", "status": "已完成",
             "from": "甲", "to": "乙", "fare": "40.00", "detail": {}, "fare_detail": {}},
            {"order_id": "B2", "occurred": "2024-06-03 09:00:00", "product_name": "快车", "status": "已关闭",
             "from": "丙", "to": "丁", "fare": None},
            {"order_id": "B1", "occurred": "2024-06-02 09:00:00", "fare": "40.00"},
        ],
    }


class FakeProvider:
    def __init__(self, jar):
        self.jar = jar
        self.calls = 0

    def load(self):
        self.calls += 1
        return self.jar


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url))
        raise AssertionError("unexpected network call")


class FakeFetcher:
    captured = {}

    def __init__(self, client, token, **kwargs):
        FakeFetcher.captured = dict(kwargs)

    def fetch(self):
        return {"pages": [{"orders": [{"order_id": "D1"}]}]}


class RowTests(unittest.TestCase):
    def test_rows_have_the_required_shape_in_both_modes(self):
        for payload in (http_payload(), browser_payload()):
            result = rows(payload)
            self.assertTrue(result)
            for row in result:
                self.assertEqual(set(row), REQUIRED_KEYS)
                self.assertEqual(row["source"], "didi")
                self.assertEqual(row["record_type"], "trip")
                self.assertTrue(row["id"].startswith("didi:"))
                self.assertRegex(row["occurred"], OCCURRED_RE)
                self.assertEqual(row["direction"], "out")
                if row["amount"] is not None:
                    self.assertRegex(row["amount"], AMOUNT_RE)

    def test_ids_are_unique_and_stable(self):
        first = [r["id"] for r in rows(browser_payload())]
        self.assertEqual(first, ["didi:trip:B1", "didi:trip:B2"])
        self.assertEqual(first, [r["id"] for r in rows(browser_payload())])

    def test_unknown_fare_is_none_not_zero(self):
        self.assertIsNone(rows(browser_payload())[1]["amount"])
        self.assertEqual(rows(http_payload())[0]["amount"], normalize_amount("25.50"))

    def test_title_is_route_when_known(self):
        self.assertEqual(rows(http_payload())[0]["title"], "起点→终点")


class ManifestAndSpecTests(unittest.TestCase):
    def test_manifest(self):
        code, out, _ = run_main(["manifest"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual((data["key"], data["label"], data["login"]), ("didi", "滴滴", "browser"))
        self.assertEqual(data["outputs"], {"raw": "didi-trips-<ts>.json", "rows": "didi-rows-<ts>.jsonl"})
        self.assertEqual(data["record_types"], ["trip"])
        self.assertTrue(data["login_url"].endswith("/general/webEntry"))

    def test_spec_key_stays_didi_for_keychain_sessions(self):
        self.assertEqual(SPEC.key, "didi")

    def test_env_token_counts_as_login(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "Cookies"
            connection = sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE cookies (host_key TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', "
                "encrypted_value BLOB NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '/', expires_utc INTEGER NOT NULL DEFAULT 0)"
            )
            connection.commit()
            connection.close()
            provider = CookieProvider(db, host_suffixes=SPEC.host_suffixes)
            with mock.patch.dict(os.environ, {SPEC.env_token: "synthetic-token"}):
                inspection = inspect_site(SPEC, provider)
        self.assertTrue(inspection.env_token_present)
        self.assertFalse(inspection.needs_login)


class CliShapeTests(unittest.TestCase):
    def test_dry_run_is_offline_for_both_modes(self):
        for extra in ([], ["--browser"]):
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                 mock.patch("subprocess.run", side_effect=AssertionError("keychain or browser")), \
                 mock.patch("connectors.didi.browser.detect_browser_engine", side_effect=AssertionError("browser")):
                code, text, errors = run_main(["fetch", "--dry-run", *extra])
            self.assertEqual(code, 0, errors)
            self.assertIn("dry-run", text)
            self.assertIn("jsonl", text)

    def test_missing_out_is_usage_error(self):
        code, _text, errors = run_main(["fetch"])
        self.assertEqual(code, 2)
        self.assertIn("--out", errors)

    def test_writes_raw_and_rows_with_one_stamp(self):
        for payload in (http_payload(), browser_payload()):
            with tempfile.TemporaryDirectory() as temp:
                with mock.patch("connectors.didi.fetch_didi", return_value=(payload, [], False)), \
                     mock.patch("connectors.didi.fetch_didi_browser", return_value=(payload, [], None)):
                    argv = ["fetch", "--out", temp] + (["--browser"] if payload.get("trips") else [])
                    code, text, errors = run_main(argv)
                self.assertEqual(code, 0, errors)
                raw = list(Path(temp).glob(f"{PREFIX}-*.json"))
                out_rows = list(Path(temp).glob("didi-rows-*.jsonl"))
                self.assertEqual((len(raw), len(out_rows)), (1, 1))
                self.assertEqual((file_mode(raw[0]), file_mode(out_rows[0])), (0o600, 0o600))
                self.assertEqual(out_rows[0].name, "didi-rows-" + raw[0].name[len(PREFIX) + 1:-5] + ".jsonl")
                self.assertIn("rows:", text)

    def test_signature_wall_saves_the_list_then_exits_4_without_leaking_the_exception_into_json(self):
        payload = http_payload()
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch("connectors.didi.fetch_didi", return_value=(payload, ["wsgsig blocked"], True)):
                code, _out, errors = run_main(["fetch", "--out", temp])
            self.assertEqual(code, 4)
            raw = list(Path(temp).glob(f"{PREFIX}-*.json"))
            self.assertEqual(len(raw), 1)
            self.assertNotIn("_blocked", raw[0].read_text(encoding="utf-8"))
            self.assertEqual(len(list(Path(temp).glob("didi-rows-*.jsonl"))), 1)
            self.assertIn("needs risk signature", errors.lower())
            self.assertIn("wsgsig blocked", errors)


class StatusTests(unittest.TestCase):
    def test_status_without_any_login_exits_3_and_never_reads_a_real_profile(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"PFIN_DIDI_STATE_DIR": temp}, clear=True), \
             mock.patch("connectors.didi._provider", return_value=FakeProvider(CookieJar())):
            code, out, _ = run_main(["status"])
        self.assertEqual(code, 3)
        self.assertIn("browser profile: missing", out)
        self.assertIn("ready: no", out)

    def test_status_with_browser_ticket_is_ready_and_prints_no_values(self):
        jar = CookieJar([Cookie("common.diditaxi.com.cn", "ticket", "SYNTH_SECRET_VALUE")])
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"PFIN_DIDI_STATE_DIR": temp}, clear=True), \
             mock.patch("connectors.didi._provider", return_value=FakeProvider(jar)):
            code, out, _ = run_main(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("ready: yes", out)
        self.assertNotIn("SYNTH_SECRET_VALUE", out)

    def test_unreadable_browser_database_does_not_hide_the_local_state(self):
        from connectors.kit.errors import CookieError

        class Broken:
            def load(self):
                raise CookieError("Cookie 数据库读取失败")

        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"PFIN_DIDI_STATE_DIR": temp}, clear=True), \
             mock.patch("connectors.didi._provider", return_value=Broken()):
            code, out, _ = run_main(["status"])
        self.assertEqual(code, 3)
        self.assertIn("浏览器 Cookie", out)
        self.assertIn("browser profile: missing", out)


class OnlineCheckTests(unittest.TestCase):
    def test_probe_uses_existing_token_and_one_list_read(self):
        jar = CookieJar([Cookie("common.diditaxi.com.cn", "ticket", "v")])
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("connectors.didi.DidiFetcher", FakeFetcher):
            result = probe_http(SPEC, FakeProvider(jar), FakeClient())
        self.assertEqual(result.status, "ok")
        self.assertIn("行程", result.detail)
        self.assertFalse(FakeFetcher.captured["with_detail"])
        self.assertFalse(FakeFetcher.captured["with_fare"])
        self.assertEqual(FakeFetcher.captured["max_pages"], 1)

    def test_probe_without_a_token_is_logged_out(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(check(SPEC, FakeClient(), CookieJar()).status, "logged_out")
            self.assertEqual(check(SPEC, FakeClient(), None).status, "logged_out")

    def test_signature_wall_maps_to_needs_signature(self):
        class Walled(FakeFetcher):
            def fetch(self):
                raise NeedsRiskSignature("wsgsig")

        jar = CookieJar([Cookie("common.diditaxi.com.cn", "ticket", "v")])
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("connectors.didi.DidiFetcher", Walled):
            result = probe_http(SPEC, FakeProvider(jar), FakeClient())
        self.assertEqual(result.status, "needs_signature")


class BrowserActionTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        patcher = mock.patch.dict(os.environ, {"PFIN_DIDI_STATE_DIR": self.state.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        # No test may start a browser or a node process.
        for target in ("subprocess.run", "connectors.didi.browser.detect_browser_engine", "connectors.didi.actions.detect_browser_engine"):
            guard = mock.patch(target, side_effect=AssertionError("no browser"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_fastfill_and_keepalive_need_a_pinned_login(self):
        with tempfile.TemporaryDirectory() as out:
            code, _o, errors = run_main(["fastfill", "--out", out])
            self.assertEqual(code, 3)
            self.assertIn("login", errors)
        code, _o, errors = run_main(["keepalive"])
        self.assertEqual(code, 4)
        self.assertIn("login", errors)

    def test_merge_prefers_known_fares_and_report_prints_a_table(self):
        with tempfile.TemporaryDirectory() as out:
            (Path(out) / f"{PREFIX}-20260101-000000.json").write_text(json.dumps({"trips": [
                {"order_id": "1", "occurred": "2024-01-01 10:00:00", "fare": "", "from": "甲", "to": "乙"}]}), encoding="utf-8")
            (Path(out) / f"{PREFIX}-20260102-000000.json").write_text(json.dumps({"trips": [
                {"order_id": "1", "occurred": "2024-01-01 10:00:00", "fare": "12.00"},
                {"order_id": "2", "occurred": "2024-02-01 10:00:00", "fare": "8.00", "from": "丙", "to": "丁"}]}), encoding="utf-8")
            code, text, _ = run_main(["merge", "--out", out])
            self.assertEqual(code, 0)
            self.assertIn("2 单，含金额 2 单", text)
            merged = list(Path(out).glob(f"{PREFIX}-merged-*.json"))
            self.assertEqual(len(merged), 1)
            self.assertEqual(file_mode(merged[0]), 0o600)
            data = json.loads(merged[0].read_text(encoding="utf-8"))
            first = {t["order_id"]: t for t in data["trips"]}["1"]
            self.assertEqual((first["fare"], first["from"]), ("12.00", "甲"))
            code, text, _ = run_main(["report", "--out", out, "--file", str(merged[0]), "--csv"])
            self.assertEqual(code, 0)
            self.assertIn("occurred,fare,from,to,status,order_id", text)

    def test_merge_without_files_is_usage_error(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(run_main(["merge", "--out", out])[0], 2)

    def test_state_dir_uses_the_shared_default(self):
        from connectors.didi.session import state_dir
        from connectors.kit.session import DEFAULT_STATE_DIR

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PFIN_DIDI_STATE_DIR", None)
            self.assertEqual(state_dir(), Path(DEFAULT_STATE_DIR).expanduser() / "didi")

    def test_fastfill_output_writer_matches_fetch_naming(self):
        from connectors.didi import write_fastfill_outputs

        with tempfile.TemporaryDirectory() as out:
            with contextlib.redirect_stdout(io.StringIO()):
                write_fastfill_outputs(mock.Mock(out=Path(out)), browser_payload())
            raw = list(Path(out).glob(f"{PREFIX}-*.json"))
            out_rows = list(Path(out).glob("didi-rows-*.jsonl"))
            self.assertEqual((len(raw), len(out_rows)), (1, 1))
            self.assertEqual(out_rows[0].name, "didi-rows-" + raw[0].name[len(PREFIX) + 1:-5] + ".jsonl")


if __name__ == "__main__":
    unittest.main()
