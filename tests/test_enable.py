import importlib.util
import plistlib
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("iphone_mirroring_enable", ROOT / "enable.py")
enable = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(enable)


def sample_plist():
    return {
        "Unrelated": {"keep": [1, 2, 3]},
        enable.DOMAIN_KEY: {
            enable.ANSWER_KEY: 2,
            enable.STATUS_KEY: {
                "OS_ELIGIBILITY_INPUT_DEVICE_CLASS": 3,
                enable.BILLING_KEY: 2,
                enable.LOCATION_KEY: 2,
            },
        },
    }


def plist_format(raw):
    return plistlib.FMT_BINARY if raw.startswith(b"bplist00") else plistlib.FMT_XML


class EligibilityFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.target = self.directory / "eligibility.plist"
        self.backup = self.directory / "eligibility.plist.backup"

    def write_plist(self, value, selected_format):
        self.target.write_bytes(
            plistlib.dumps(value, fmt=selected_format, sort_keys=False)
        )
        self.target.chmod(0o640)

    def test_updates_both_formats_and_preserves_unrelated_data(self):
        for selected_format in (plistlib.FMT_XML, plistlib.FMT_BINARY):
            with self.subTest(selected_format=selected_format):
                self.target.unlink(missing_ok=True)
                self.backup.unlink(missing_ok=True)
                original = sample_plist()
                self.write_plist(original, selected_format)
                original_raw = self.target.read_bytes()
                original_mode = stat.S_IMODE(self.target.stat().st_mode)

                changed, backup_path = enable.update_eligibility_file(
                    self.target, self.backup
                )

                self.assertTrue(changed)
                self.assertEqual(backup_path, self.backup)
                self.assertEqual(self.backup.read_bytes(), original_raw)
                self.assertEqual(plist_format(self.target.read_bytes()), selected_format)
                self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), original_mode)
                updated = plistlib.loads(self.target.read_bytes())
                self.assertEqual(updated["Unrelated"], original["Unrelated"])
                domain = updated[enable.DOMAIN_KEY]
                self.assertEqual(domain[enable.ANSWER_KEY], 4)
                self.assertEqual(domain[enable.STATUS_KEY][enable.BILLING_KEY], 3)
                self.assertEqual(domain[enable.STATUS_KEY][enable.LOCATION_KEY], 3)

    def test_preserves_extended_attributes_via_cp_p(self):
        self.write_plist(sample_plist(), plistlib.FMT_BINARY)
        attribute = "com.example.iphone-mirroring-test"
        enable.subprocess.run(
            ["/usr/bin/xattr", "-w", attribute, "preserve-me", str(self.target)],
            check=True,
            env={"PATH": enable.SAFE_PATH},
        )

        enable.update_eligibility_file(self.target, self.backup)

        for path in (self.target, self.backup):
            result = enable.subprocess.run(
                ["/usr/bin/xattr", "-p", attribute, str(path)],
                check=True,
                capture_output=True,
                env={"PATH": enable.SAFE_PATH},
            )
            self.assertEqual(result.stdout, b"preserve-me\n")

    def test_invalid_or_incomplete_structure_is_rejected_before_backup(self):
        missing_location = sample_plist()
        del missing_location[enable.DOMAIN_KEY][enable.STATUS_KEY][enable.LOCATION_KEY]
        boolean_answer = sample_plist()
        boolean_answer[enable.DOMAIN_KEY][enable.ANSWER_KEY] = True

        for invalid in ({"Unrelated": True}, missing_location, boolean_answer):
            with self.subTest(invalid=invalid):
                self.backup.unlink(missing_ok=True)
                self.write_plist(invalid, plistlib.FMT_XML)
                original_raw = self.target.read_bytes()
                with self.assertRaises(enable.EligibilityError):
                    enable.update_eligibility_file(self.target, self.backup)
                self.assertEqual(self.target.read_bytes(), original_raw)
                self.assertFalse(self.backup.exists())

    def test_second_run_is_idempotent_and_keeps_first_backup(self):
        self.write_plist(sample_plist(), plistlib.FMT_BINARY)
        enable.update_eligibility_file(self.target, self.backup)
        backup_raw = self.backup.read_bytes()

        changed, _ = enable.update_eligibility_file(self.target, self.backup)

        self.assertFalse(changed)
        self.assertEqual(self.backup.read_bytes(), backup_raw)

    def test_change_immediately_before_swap_is_preserved(self):
        self.write_plist(sample_plist(), plistlib.FMT_XML)
        concurrent = sample_plist()
        concurrent["Concurrent"] = "newer state"
        concurrent_raw = plistlib.dumps(concurrent, fmt=plistlib.FMT_XML)
        original_swap = enable._atomic_swap
        calls = 0

        def change_then_swap(first_path, second_path):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.target.write_bytes(concurrent_raw)
            return original_swap(first_path, second_path)

        with mock.patch.object(enable, "_atomic_swap", side_effect=change_then_swap):
            with self.assertRaises(enable.EligibilityError):
                enable.update_eligibility_file(self.target, self.backup)

        self.assertEqual(self.target.read_bytes(), concurrent_raw)

    def test_failed_swap_reversal_retains_displaced_state(self):
        self.write_plist(sample_plist(), plistlib.FMT_XML)
        concurrent = sample_plist()
        concurrent["Concurrent"] = "must survive"
        concurrent_raw = plistlib.dumps(concurrent, fmt=plistlib.FMT_XML)
        original_swap = enable._atomic_swap
        calls = 0

        def change_swap_then_fail(first_path, second_path):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.target.write_bytes(concurrent_raw)
                return original_swap(first_path, second_path)
            raise OSError("forced reversal failure")

        with mock.patch.object(enable, "_atomic_swap", side_effect=change_swap_then_fail):
            with self.assertRaises(enable.RecoveryError) as raised:
                enable.update_eligibility_file(self.target, self.backup)

        retained = list(self.directory.glob(".*.update.*.tmp"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), concurrent_raw)
        self.assertIn(str(retained[0]), str(raised.exception))

    def test_change_after_swap_is_retained_when_previous_target_is_restored(self):
        self.write_plist(sample_plist(), plistlib.FMT_XML)
        original_raw = self.target.read_bytes()
        concurrent = sample_plist()
        concurrent["Concurrent"] = "written after swap"
        concurrent_raw = plistlib.dumps(concurrent, fmt=plistlib.FMT_XML)
        original_assert = enable._assert_metadata
        update_temporary_checks = 0

        def change_after_swap(path, expected):
            nonlocal update_temporary_checks
            if ".update." in Path(path).name:
                update_temporary_checks += 1
                if update_temporary_checks == 2:
                    self.target.write_bytes(concurrent_raw)
            return original_assert(path, expected)

        with mock.patch.object(enable, "_assert_metadata", side_effect=change_after_swap):
            with self.assertRaises(enable.RecoveryError) as raised:
                enable.update_eligibility_file(self.target, self.backup)

        retained = list(self.directory.glob(".*.update.*.tmp"))
        self.assertEqual(self.target.read_bytes(), original_raw)
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), concurrent_raw)
        self.assertIn(str(retained[0]), str(raised.exception))

    def test_post_write_failure_restores_current_not_stale_backup(self):
        old = sample_plist()
        old["State"] = "old"
        self.write_plist(old, plistlib.FMT_BINARY)
        enable.update_eligibility_file(self.target, self.backup)
        old_backup_raw = self.backup.read_bytes()

        current = sample_plist()
        current["State"] = "new OS state"
        self.write_plist(current, plistlib.FMT_BINARY)
        current_raw = self.target.read_bytes()
        original_assert = enable._assert_metadata
        failed = False

        def fail_after_swap(path, expected):
            nonlocal failed
            if Path(path) == self.target and not failed:
                failed = True
                raise enable.EligibilityError("forced post-write failure")
            return original_assert(path, expected)

        with mock.patch.object(enable, "_assert_metadata", side_effect=fail_after_swap):
            with self.assertRaises(enable.EligibilityError):
                enable.update_eligibility_file(self.target, self.backup)

        self.assertEqual(self.target.read_bytes(), current_raw)
        self.assertEqual(self.backup.read_bytes(), old_backup_raw)

    def test_restore_reinstalls_exact_backup(self):
        self.write_plist(sample_plist(), plistlib.FMT_BINARY)
        original_raw = self.target.read_bytes()
        enable.update_eligibility_file(self.target, self.backup)

        enable.restore_backup(self.target, self.backup)

        self.assertEqual(self.target.read_bytes(), original_raw)

    def test_restore_rejects_backup_changed_while_copying(self):
        self.write_plist(sample_plist(), plistlib.FMT_BINARY)
        enable.update_eligibility_file(self.target, self.backup)
        target_before = self.target.read_bytes()
        replacement = sample_plist()
        replacement["Changed"] = True
        replacement_raw = plistlib.dumps(replacement, fmt=plistlib.FMT_BINARY)
        original_copy = enable._copy_preserved

        def change_then_copy(source, destination):
            if Path(source) == self.backup:
                self.backup.write_bytes(replacement_raw)
            return original_copy(source, destination)

        with mock.patch.object(enable, "_copy_preserved", side_effect=change_then_copy):
            with self.assertRaises(enable.EligibilityError):
                enable.restore_backup(self.target, self.backup)

        self.assertEqual(self.target.read_bytes(), target_before)


class ProcessBoundaryTests(unittest.TestCase):
    def test_open_uses_absolute_path_and_clean_environment(self):
        account = SimpleNamespace(pw_dir="/Users/tester", pw_name="tester")
        with mock.patch.object(enable.subprocess, "run") as run:
            enable.launch_mirroring_app(account)
        run.assert_called_once_with(
            ["/usr/bin/open", enable.APP_PATH],
            check=True,
            env={
                "HOME": "/Users/tester",
                "LOGNAME": "tester",
                "PATH": enable.SAFE_PATH,
                "USER": "tester",
            },
        )

    def test_invoking_user_requires_sudo(self):
        with mock.patch.object(enable.os, "geteuid", return_value=501):
            with self.assertRaises(enable.EligibilityError):
                enable._invoking_user()

    def test_drop_privileges_uses_safe_order(self):
        account = SimpleNamespace(pw_name="tester")
        calls = []
        with mock.patch.object(
            enable.os, "initgroups", side_effect=lambda *args: calls.append("groups")
        ), mock.patch.object(
            enable.os, "setgid", side_effect=lambda *args: calls.append("gid")
        ), mock.patch.object(
            enable.os, "setuid", side_effect=lambda *args: calls.append("uid")
        ), mock.patch.object(
            enable.os, "geteuid", return_value=501
        ), mock.patch.object(
            enable.os, "getegid", return_value=20
        ):
            enable._drop_privileges(501, 20, account)
        self.assertEqual(calls, ["groups", "gid", "uid"])

    def test_main_drops_privileges_before_launch(self):
        account = SimpleNamespace(pw_name="tester")
        calls = []
        with mock.patch.object(
            enable, "_invoking_user", return_value=(501, 20, account)
        ), mock.patch.object(
            enable,
            "update_eligibility_file",
            return_value=(False, Path("/private/tmp/test.backup")),
        ), mock.patch.object(
            enable, "_drop_privileges", side_effect=lambda *args: calls.append("drop")
        ), mock.patch.object(
            enable, "launch_mirroring_app", side_effect=lambda *args: calls.append("open")
        ):
            self.assertEqual(enable.main([]), 0)
        self.assertEqual(calls, ["drop", "open"])


if __name__ == "__main__":
    unittest.main()
