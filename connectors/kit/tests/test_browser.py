"""Browser selection uses only synthetic profiles, never cookies or Keychain."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from connectors.kit import cookies
from connectors.kit.errors import UsageError


class BrowserSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": temp.name}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def db(self, key):
        return self.home / "Library" / "Application Support" / cookies.BROWSERS[key][0] / "Default" / "Cookies"

    def test_env_override_wins_even_without_a_profile(self):
        for key, (_, service, app) in cookies.BROWSERS.items():
            with self.subTest(browser=key):
                os.environ["PFIN_BROWSER"] = key
                self.assertEqual(cookies.select_browser(), (self.db(key), service, app))
        self.db("chrome").parent.mkdir(parents=True)
        self.db("chrome").touch()
        os.environ["PFIN_BROWSER"] = "edge"
        self.assertEqual(cookies.select_browser()[0], self.db("edge"))

    def test_auto_detection_order(self):
        self.assertEqual(list(cookies.BROWSERS), ["dia", "chrome", "edge", "brave", "arc", "chromium"])
        for key in cookies.BROWSERS:
            self.db(key).parent.mkdir(parents=True)
            self.db(key).touch()
        for key, (_, service, app) in cookies.BROWSERS.items():
            with self.subTest(browser=key):
                self.assertEqual(cookies.select_browser(), (self.db(key), service, app))
                self.db(key).unlink()

    def test_no_profiles_preserves_first_default(self):
        self.assertEqual(cookies.select_browser()[0], self.db(next(iter(cookies.BROWSERS))))

    def test_unknown_browser_has_a_clear_error(self):
        for value in ("unknown", ""):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"PFIN_BROWSER": value}):
                with self.assertRaisesRegex(UsageError, "PFIN_BROWSER must be one of:"):
                    cookies.select_browser()

    def test_exported_defaults_and_history_use_the_selected_profile(self):
        code = (
            "import json; from connectors.kit.cookies import "
            "DEFAULT_COOKIE_DB, DEFAULT_KEYCHAIN_SERVICE, BROWSER_APP, browser_label; "
            "from connectors.didi.session import default_browser_history_path; "
            "print(json.dumps([str(DEFAULT_COOKIE_DB), DEFAULT_KEYCHAIN_SERVICE, "
            "BROWSER_APP, browser_label(), str(default_browser_history_path())]))"
        )
        for key, (_, service, app) in cookies.BROWSERS.items():
            with self.subTest(browser=key):
                result = subprocess.run(
                    [sys.executable, "-c", code], check=True, capture_output=True, text=True,
                    env={**os.environ, "PFIN_BROWSER": key},
                )
                self.assertEqual(json.loads(result.stdout), [
                    str(self.db(key)), service, app, app, str(self.db(key).with_name("History")),
                ])
