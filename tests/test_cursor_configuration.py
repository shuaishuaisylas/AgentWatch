from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentwatch.configuration import ConfigurationError, write_json
from agentwatch.cursor_bridge import EVENTS
from agentwatch.cursor_configuration import CursorConfiguration


class CursorConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cursor-config-")
        self.root = Path(self.temporary.name)
        self.config = CursorConfiguration(self.root / "cursor", self.root / "data")

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, value):
        self.config.settings.parent.mkdir(parents=True, exist_ok=True)
        self.config.settings.write_text(json.dumps(value))

    def test_preview_is_read_only_and_displays_only_own_native_hooks(self):
        self.write({"version": 1, "hooks": {"stop": [{"command": "third-party PRIVATE_TOKEN"}]}})
        original = self.config.settings.read_bytes()
        preview = self.config.preview()
        self.assertEqual(self.config.settings.read_bytes(), original)
        self.assertFalse(self.config.data_dir.exists())
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(preview))
        self.assertEqual(preview["configuration"]["version"], 1)
        self.assertIn(str(self.config.settings), preview["settings_label"])
        self.assertEqual(set(preview["configuration"]["hooks"]), set(EVENTS))
        for definitions in preview["configuration"]["hooks"].values():
            self.assertEqual(definitions, [{"command": self.config.command, "timeout": 3, "failClosed": False}])
            self.assertNotIn("hooks", definitions[0])
        self.assertNotIn("preToolUse", preview["events"])

    def test_install_preserves_third_party_settings_and_restore_keeps_later_changes(self):
        existing = {"version": 1, "team_note": "keep", "hooks": {
            "stop": [{"command": "third-party-audit", "timeout": 7}],
            "beforeSubmitPrompt": [{"type": "prompt", "prompt": "Check company policy", "timeout": 30}],
            "preToolUse": [{"command": "third-party-permission", "failClosed": True}],
            "futureEvent": [{"command": "future-tool", "custom": True}]}}
        self.write(existing)
        original = self.config.settings.read_bytes()
        installed = self.config.install(self.config.preview()["hash"])
        self.assertTrue(installed["complete"])
        self.assertFalse(installed["review_required"])
        manifest = self.config.load_manifest()
        self.assertEqual(Path(manifest["backup"]).read_bytes(), original)
        self.assertEqual(os.stat(manifest["backup"]).st_mode & 0o777, 0o600)
        current = json.loads(self.config.settings.read_text())
        self.assertEqual(current["hooks"]["preToolUse"], existing["hooks"]["preToolUse"])
        self.assertEqual(current["hooks"]["beforeSubmitPrompt"][0], existing["hooks"]["beforeSubmitPrompt"][0])
        current["later"] = 42
        current["hooks"]["stop"].append({"command": "added-later"})
        self.write(current)
        result = self.config.uninstall()
        self.assertEqual(result["removed"], len(EVENTS))
        expected = dict(existing, later=42)
        expected["hooks"]["stop"].append({"command": "added-later"})
        self.assertEqual(json.loads(self.config.settings.read_text()), expected)
        self.assertTrue(Path(manifest["backup"]).exists())

    def test_install_is_idempotent_and_replaces_only_old_owned_commands(self):
        self.config.install(self.config.preview()["hash"])
        backup = self.config.load_manifest()["backup"]
        current = json.loads(self.config.settings.read_text())
        current["hooks"]["stop"][0]["command"] = "old-owned-command"
        current["hooks"]["stop"].append({"command": "keep-similar-old-owned-command"})
        self.write(current)
        manifest = self.config.load_manifest()
        manifest["command"] = "old-owned-command"
        self.config.manifest.write_text(json.dumps(manifest))
        self.config.install(self.config.preview()["hash"])
        self.config.install(self.config.preview()["hash"])
        current = json.loads(self.config.settings.read_text())
        for event in EVENTS:
            self.assertEqual(sum(item.get("command") == self.config.command for item in current["hooks"][event]), 1)
        self.assertEqual(current["hooks"]["stop"][1]["command"], "keep-similar-old-owned-command")
        self.assertEqual(self.config.load_manifest()["backup"], backup)

    def test_new_file_is_private_and_removed_when_no_other_settings_exist(self):
        self.config.install(self.config.preview()["hash"])
        self.assertEqual(os.stat(self.config.settings).st_mode & 0o777, 0o600)
        self.config.uninstall()
        self.assertFalse(self.config.settings.exists())
        self.assertFalse(self.config.status()["enabled"])

    def test_new_file_with_later_third_party_hooks_retains_schema_version(self):
        self.config.install(self.config.preview()["hash"])
        current = json.loads(self.config.settings.read_text())
        current["hooks"]["stop"].append({"command": "later-hook"})
        self.write(current)
        self.config.uninstall()
        self.assertEqual(json.loads(self.config.settings.read_text()),
                         {"version": 1, "hooks": {"stop": [{"command": "later-hook"}]}})

    def test_existing_empty_file_is_restored_and_permissions_preserved(self):
        self.write({})
        os.chmod(self.config.settings, 0o640)
        self.config.install(self.config.preview()["hash"])
        self.config.uninstall()
        self.assertEqual(json.loads(self.config.settings.read_text()), {})
        self.assertEqual(os.stat(self.config.settings).st_mode & 0o777, 0o640)

    def test_stale_preview_and_changed_file_during_save_are_not_overwritten(self):
        self.write({"version": 1})
        old_hash = self.config.preview()["hash"]
        self.write({"version": 1, "later": True})
        with self.assertRaises(ConfigurationError):
            self.config.install(old_hash)
        self.assertTrue(json.loads(self.config.settings.read_text())["later"])
        preview = self.config.preview()
        read = self.config.read
        calls = []
        def racing_read():
            calls.append(1)
            if len(calls) == 2:
                self.write({"version": 1, "concurrent": "preserve"})
            return read()
        with patch.object(self.config, "read", side_effect=racing_read), self.assertRaises(ConfigurationError):
            self.config.install(preview["hash"])
        self.assertEqual(json.loads(self.config.settings.read_text()), {"version": 1, "concurrent": "preserve"})

    def test_manifest_write_race_preserves_new_third_party_policy_and_rebaselines(self):
        self.write({"version": 1, "before": "preserve"})
        original = self.config.settings.read_bytes()
        concurrent = {"version": 1, "before": "preserve", "hooks": {
            "preToolUse": [{"command": "new-third-party-policy", "failClosed": True}]}}
        injected = []
        def racing_write(path, value, mode=0o600):
            write_json(path, value, mode)
            if path == self.config.manifest and not injected:
                injected.append(True)
                self.write(concurrent)
        with patch("agentwatch.cursor_configuration.write_json", side_effect=racing_write), self.assertRaises(ConfigurationError):
            self.config.install(self.config.preview()["hash"])
        self.assertEqual(json.loads(self.config.settings.read_text()), concurrent)
        prepared = self.config.load_manifest()
        self.assertFalse(prepared["active"])
        self.assertTrue(prepared["pending"])
        self.assertFalse(self.config.status()["enabled"])
        self.assertEqual(Path(prepared["backup"]).read_bytes(), original)
        self.config.install(self.config.preview()["hash"])
        active = self.config.load_manifest()
        self.assertTrue(active["active"])
        self.assertFalse(active["pending"])
        self.assertIn(prepared["backup"], active["previous_backups"])
        self.config.uninstall()
        self.assertEqual(json.loads(self.config.settings.read_text()), concurrent)

    def test_manifest_or_settings_write_failure_cannot_claim_prepared_hooks_are_enabled(self):
        for failing_path in ["manifest", "settings"]:
            with self.subTest(failing_path=failing_path):
                self.write({"version": 1, "independent": "keep"})
                original = self.config.settings.read_bytes()
                def failing_write(path, value, mode=0o600):
                    if path == getattr(self.config, failing_path):
                        raise OSError("artificial write failure")
                    write_json(path, value, mode)
                with patch("agentwatch.cursor_configuration.write_json", side_effect=failing_write), self.assertRaises(OSError):
                    self.config.install(self.config.preview()["hash"])
                self.assertEqual(self.config.settings.read_bytes(), original)
                self.assertFalse(self.config.status()["enabled"])
                if self.config.manifest.exists():
                    prepared = self.config.load_manifest()
                    self.assertFalse(prepared["active"])
                    self.assertTrue(Path(prepared["backup"]).exists())
                self.config.install(self.config.preview()["hash"])
                self.assertTrue(self.config.status()["complete"])
                self.config.uninstall()
                self.assertEqual(json.loads(self.config.settings.read_text()), json.loads(original))

    def test_final_manifest_failure_reports_actual_settings_and_can_recover(self):
        self.write({"version": 1, "independent": "keep"})
        writes = []
        def failing_final_write(path, value, mode=0o600):
            if path == self.config.manifest:
                writes.append(True)
                if len(writes) == 2:
                    raise OSError("artificial final manifest failure")
            write_json(path, value, mode)
        with patch("agentwatch.cursor_configuration.write_json", side_effect=failing_final_write), self.assertRaises(OSError):
            self.config.install(self.config.preview()["hash"])
        self.assertFalse(self.config.load_manifest()["active"])
        self.assertTrue(self.config.load_manifest()["pending"])
        self.assertTrue(self.config.status()["enabled"])
        self.config.install(self.config.preview()["hash"])
        self.assertTrue(self.config.load_manifest()["active"])
        self.config.uninstall()
        self.assertEqual(json.loads(self.config.settings.read_text()), {"version": 1, "independent": "keep"})

    def test_failed_command_upgrade_keeps_old_exact_ownership_until_recovery(self):
        self.config.install(self.config.preview()["hash"])
        old_command = self.config.command
        with patch("agentwatch.cursor_configuration.sys.executable", "/example/new-python"):
            upgraded = CursorConfiguration(self.config.settings.parent, self.config.data_dir)
        injected = []
        def racing_write(path, value, mode=0o600):
            write_json(path, value, mode)
            if path == upgraded.manifest and not injected:
                injected.append(True)
                current = json.loads(upgraded.settings.read_text())
                current["later"] = "keep"
                self.write(current)
        with patch("agentwatch.cursor_configuration.write_json", side_effect=racing_write), self.assertRaises(ConfigurationError):
            upgraded.install(upgraded.preview()["hash"])
        self.assertTrue(upgraded.status()["enabled"])
        self.assertEqual(upgraded.load_manifest()["pending_previous_command"], old_command)
        upgraded.install(upgraded.preview()["hash"])
        installed = json.loads(upgraded.settings.read_text())
        self.assertNotIn(old_command, [item.get("command") for group in installed["hooks"].values() for item in group])
        upgraded.uninstall()
        self.assertEqual(json.loads(upgraded.settings.read_text()), {"version": 1, "later": "keep"})

    def test_malformed_and_unknown_schema_is_never_replaced(self):
        for value in [[], {"version": 2}, {"version": True}, {"version": 1.0},
                      {"hooks": {}}, {"version": 1, "hooks": []},
                      {"version": 1, "hooks": {"stop": "bad"}},
                      {"version": 1, "hooks": {"stop": ["bad"]}},
                      {"version": 1, "hooks": {"stop": [{"hooks": []}]}},
                      {"version": 1, "hooks": {"stop": [{"command": []}]}}]:
            with self.subTest(value=value):
                self.write(value)
                original = self.config.settings.read_bytes()
                with self.assertRaises(ConfigurationError):
                    self.config.preview()
                self.assertEqual(self.config.settings.read_bytes(), original)
                self.assertFalse(self.config.status()["complete"])
        self.config.settings.write_text("{broken")
        with self.assertRaises(ConfigurationError):
            self.config.preview()
        self.assertEqual(self.config.settings.read_text(), "{broken")

    def test_symlinks_are_not_followed_for_configuration_or_manifest(self):
        target = self.root / "target.json"
        target.write_text("{}")
        self.config.settings.parent.mkdir()
        self.config.settings.symlink_to(target)
        with self.assertRaises(ConfigurationError):
            self.config.preview()
        self.assertEqual(target.read_text(), "{}")
        self.config.settings.unlink()
        self.config.data_dir.mkdir()
        self.config.manifest.symlink_to(target)
        with self.assertRaises(ConfigurationError):
            self.config.install(self.config.preview()["hash"])
        self.assertEqual(target.read_text(), "{}")
        self.assertFalse(self.config.settings.exists())

    def test_uninstall_without_ownership_does_not_create_configuration(self):
        self.assertEqual(self.config.uninstall()["removed"], 0)
        self.assertFalse(self.config.settings.exists())
        self.assertFalse(self.config.data_dir.exists())

    def test_repeated_uninstall_does_not_delete_independent_new_configuration(self):
        self.config.install(self.config.preview()["hash"])
        self.config.uninstall()
        self.write({"version": 1})
        original = self.config.settings.read_bytes()
        self.assertEqual(self.config.uninstall()["removed"], 0)
        self.assertEqual(self.config.settings.read_bytes(), original)
        self.assertFalse(self.config.load_manifest()["active"])

    def test_reinstall_uses_current_baseline_and_new_backup(self):
        self.config.install(self.config.preview()["hash"])
        first_backup = self.config.load_manifest()["backup"]
        self.config.uninstall()
        self.write({"version": 1, "independent": "keep"})
        original = self.config.settings.read_bytes()
        self.config.install(self.config.preview()["hash"])
        latest = self.config.load_manifest()
        self.assertTrue(latest["active"])
        self.assertTrue(latest["existed"])
        self.assertFalse(latest["added_version"])
        self.assertNotEqual(latest["backup"], first_backup)
        self.assertIn(first_backup, latest["previous_backups"])
        self.assertEqual(Path(latest["backup"]).read_bytes(), original)
        self.config.uninstall()
        self.assertEqual(json.loads(self.config.settings.read_text()), json.loads(original))

    def test_changed_owned_handler_remains_restorable_and_is_repaired(self):
        self.config.install(self.config.preview()["hash"])
        current = json.loads(self.config.settings.read_text())
        for handlers in current["hooks"].values():
            handlers[0]["failClosed"] = True
        self.write(current)
        self.assertTrue(self.config.status()["enabled"])
        self.assertFalse(self.config.status()["complete"])
        self.config.install(self.config.preview()["hash"])
        self.assertTrue(self.config.status()["complete"])

    def test_manually_removed_hooks_start_a_new_baseline_with_or_without_restore(self):
        for restore in [False, True]:
            with self.subTest(restore=restore):
                self.config.install(self.config.preview()["hash"])
                self.write({"version": 1})
                original = self.config.settings.read_bytes()
                if restore:
                    self.assertEqual(self.config.uninstall()["removed"], 0)
                    self.assertEqual(self.config.settings.read_bytes(), original)
                    self.assertFalse(self.config.load_manifest()["active"])
                self.config.install(self.config.preview()["hash"])
                self.assertTrue(self.config.load_manifest()["existed"])
                self.assertFalse(self.config.load_manifest()["added_version"])
                self.config.uninstall()
                self.assertEqual(json.loads(self.config.settings.read_text()), {"version": 1})

    def test_command_shell_fallback_does_not_block_even_when_python_cannot_start(self):
        self.assertTrue(self.config.command.endswith(" >/dev/null 2>&1 || :"))
        for code in [1, 2, 127]:
            executable = self.root / ("failing-python-" + str(code))
            executable.write_text("#!/bin/sh\necho private-diagnostic >&2\nexit " + str(code) + "\n")
            executable.chmod(0o700)
            with patch("agentwatch.cursor_configuration.sys.executable", str(executable)):
                configuration = CursorConfiguration(self.root / "cursor", self.root / "data")
            result = subprocess.run(configuration.command, input=b"{}", shell=True, capture_output=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        with patch("agentwatch.cursor_configuration.sys.executable", str(self.root / "missing-python")):
            configuration = CursorConfiguration(self.root / "cursor", self.root / "data")
        result = subprocess.run(configuration.command, input=b"{}", shell=True, capture_output=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))


if __name__ == "__main__":
    unittest.main()
