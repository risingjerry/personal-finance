"""The shared CLI shape, exercised with a fake connector (no network, no Keychain)."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.kit.rows import make_record, stable_id
from connectors.kit.runner import Connector, main, provider
from connectors.kit.output import file_mode


def fake(**over):
    def fetch(args, jar):
        return {"pages": [{"id": "1", "amount": "9.90"}]}, ["一条提醒"]

    def rows(payload):
        return [make_record(source="fake", record_type="order", id=stable_id("fake", "order", p["id"]),
                            occurred="2026-10-01 10:00:00", amount=p["amount"], direction="out",
                            merchant="店", title="货", status="完成") for p in payload["pages"]]

    base = dict(key="fake", label="假站", prefix="fake-orders", fetch=fetch, summary=lambda p: "fake: 1 单", rows=rows,
                login_kind="api-key")
    base.update(over)
    return Connector(**base)


def run(connector, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(connector, argv)
    return code, out.getvalue(), err.getvalue()


class RunnerTests(unittest.TestCase):
    def test_login_hints_do_not_treat_session_keys_as_module_names(self):
        from connectors.kit.spec import login_hint

        for key in ("12306", "meituan-yuefu"):
            with self.subTest(key=key):
                spec = mock.Mock(key=key, login_url="https://example.test/login")
                hint = login_hint(spec)
                self.assertIn(spec.login_url, hint)
                self.assertIn("该连接器的 `status --online`", hint)
                self.assertNotIn(f"connectors.{key}", hint)

    def test_login_opens_the_selected_app_without_live_access(self):
        spec = mock.Mock(login_url="https://example.test/login", key="fake", host_suffixes=("example.test",))
        with mock.patch("connectors.kit.runner.BROWSER_APP", "Test Browser"), \
                mock.patch("connectors.kit.runner.browser_label", return_value="Test Browser"), \
                mock.patch("connectors.kit.runner.subprocess.run") as launch, \
                mock.patch("connectors.kit.runner.provider"), \
                mock.patch("connectors.kit.runner.inspect_site", return_value=mock.Mock(error=None, needs_login=False)):
            code, out, _ = run(fake(spec=spec), ["login"])
        self.assertEqual(code, 0)
        launch.assert_called_once_with(["open", "-a", "Test Browser", spec.login_url], check=False)
        self.assertIn("Test Browser", out)

    def test_explicit_cookie_and_keychain_flags_override_defaults(self):
        from connectors.kit.runner import build_parser

        connector = fake(spec=mock.Mock(key="fake", host_suffixes=("example.test",)))
        args = build_parser(connector).parse_args([
            "status", "--cookie-db", "/synthetic/Cookies", "--keychain-service", "Test Storage",
        ])
        selected = provider(connector, args)
        self.assertEqual(selected.db_path, Path("/synthetic/Cookies"))
        self.assertEqual(selected.keychain_service, "Test Storage")

    def test_manifest_describes_the_contract(self):
        code, out, _ = run(fake(), ["manifest"])
        m = json.loads(out)
        self.assertEqual((code, m["key"], m["verbs"], m["read_only"]), (0, "fake", ["manifest", "status", "login", "fetch", "keepalive"], True))
        self.assertEqual(m["outputs"]["rows"], "fake-rows-<ts>.jsonl")

    def test_fetch_writes_raw_and_rows_with_one_stamp_privately(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run(fake(), ["fetch", "--out", tmp])
            self.assertEqual(code, 0)
            self.assertIn("一条提醒", err)
            raw = list(Path(tmp).glob("fake-orders-*.json"))
            rows = list(Path(tmp).glob("fake-rows-*.jsonl"))
            self.assertEqual((len(raw), len(rows)), (1, 1))
            self.assertEqual(rows[0].name, "fake-rows-" + raw[0].name[len("fake-orders-"):-5] + ".jsonl")
            self.assertEqual((file_mode(raw[0]), file_mode(rows[0])), (0o600, 0o600))
            self.assertEqual(json.loads(rows[0].read_text().splitlines()[0])["amount"], "9.90")

    def test_fetch_needs_out_and_dry_run_touches_nothing(self):
        self.assertEqual(run(fake(), ["fetch"])[0], 2)
        code, out, _ = run(fake(plan=lambda args: "plan: fake"), ["fetch", "--dry-run"])
        self.assertEqual((code, out.strip()), (0, "plan: fake"))

    def test_keepalive_without_a_check_is_a_no_op(self):
        code, out, _ = run(fake(), ["keepalive", "--json"])
        self.assertEqual((code, json.loads(out)["status"]), (0, "skip"))

    def test_connector_errors_become_exit_codes_without_tracebacks(self):
        def boom(args, jar):
            raise RuntimeError("secret-value-should-not-leak")

        code, _, err = run(fake(fetch=boom), ["fetch", "--out", tempfile.mkdtemp()])
        self.assertEqual(code, 70)
        self.assertNotIn("secret-value-should-not-leak", err)


if __name__ == "__main__":
    unittest.main()
