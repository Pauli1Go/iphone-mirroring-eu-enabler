#!/usr/bin/env python3
"""Safely update macOS eligibility data for iPhone Mirroring."""

import argparse
import copy
import ctypes
import hashlib
import os
import plistlib
import pwd
import stat
import subprocess
import tempfile
from pathlib import Path


ELIGIBILITY_PATH = Path("/private/var/db/os_eligibility/eligibility.plist")
BACKUP_SUFFIX = ".iphone-mirroring-eu-enabler.backup"
APP_PATH = "/System/Applications/iPhone Mirroring.app"
OPEN_TOOL = "/usr/bin/open"
COPY_TOOL = "/bin/cp"
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
AT_FDCWD = -2
RENAME_SWAP = 0x00000002

DOMAIN_KEY = "OS_ELIGIBILITY_DOMAIN_IRON"
ANSWER_KEY = "os_eligibility_answer_t"
STATUS_KEY = "status"
BILLING_KEY = "OS_ELIGIBILITY_INPUT_COUNTRY_BILLING"
LOCATION_KEY = "OS_ELIGIBILITY_INPUT_COUNTRY_LOCATION"


class EligibilityError(RuntimeError):
    """Raised when the protected eligibility file cannot be changed safely."""


class RecoveryError(EligibilityError):
    """Raised when a preserved file needs manual recovery."""


def _read_regular(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(fd)
        raise EligibilityError("Refusing to operate on a non-regular file: {}".format(path))
    with os.fdopen(fd, "rb") as source:
        return source.read(), file_stat


def _parse(raw):
    if raw.startswith(b"bplist00"):
        plist_format = plistlib.FMT_BINARY
    elif raw.lstrip().startswith(b"<?xml"):
        plist_format = plistlib.FMT_XML
    else:
        raise EligibilityError("The eligibility file is not an XML or binary plist.")

    try:
        data = plistlib.loads(raw)
    except Exception as exc:
        raise EligibilityError("The eligibility plist could not be parsed.") from exc
    if not isinstance(data, dict):
        raise EligibilityError("The eligibility plist root must be a dictionary.")

    domain = data.get(DOMAIN_KEY)
    if not isinstance(domain, dict) or not isinstance(domain.get(STATUS_KEY), dict):
        raise EligibilityError("The expected iPhone Mirroring eligibility domain is missing.")
    status_data = domain[STATUS_KEY]
    for container, key in (
        (domain, ANSWER_KEY),
        (status_data, BILLING_KEY),
        (status_data, LOCATION_KEY),
    ):
        value = container.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise EligibilityError("{} is missing or is not an integer.".format(key))
    return data, plist_format


def _updated_bytes(data, plist_format):
    updated = copy.deepcopy(data)
    domain = updated[DOMAIN_KEY]
    domain[ANSWER_KEY] = 4
    domain[STATUS_KEY][BILLING_KEY] = 3
    domain[STATUS_KEY][LOCATION_KEY] = 3
    raw = plistlib.dumps(updated, fmt=plist_format, sort_keys=False)
    parsed, parsed_format = _parse(raw)
    if parsed_format != plist_format or parsed != updated:
        raise EligibilityError("The serialized replacement did not pass validation.")
    return updated, raw


def _identity(raw, file_stat):
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        hashlib.sha256(raw).digest(),
    )


def _metadata(file_stat):
    values = {
        "mode": stat.S_IMODE(file_stat.st_mode),
        "uid": file_stat.st_uid,
        "gid": file_stat.st_gid,
    }
    if hasattr(file_stat, "st_flags"):
        values["flags"] = file_stat.st_flags
    return values


def _assert_metadata(path, expected):
    actual = _metadata(os.stat(str(path), follow_symlinks=False))
    if actual != expected:
        raise EligibilityError("Ownership, mode, or file flags changed unexpectedly.")


def _copy_preserved(source, destination):
    subprocess.run(
        [COPY_TOOL, "-p", str(source), str(destination)],
        check=True,
        env={"PATH": SAFE_PATH},
    )


def _fsync_file(path):
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(directory):
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_swap(first_path, second_path):
    renameatx_np = ctypes.CDLL(None, use_errno=True).renameatx_np
    renameatx_np.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameatx_np.restype = ctypes.c_int
    if renameatx_np(
        AT_FDCWD,
        os.fsencode(str(first_path)),
        AT_FDCWD,
        os.fsencode(str(second_path)),
        RENAME_SWAP,
    ) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _temporary_path(target_path, label):
    fd, name = tempfile.mkstemp(
        prefix=".{}.{}.".format(target_path.name, label),
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    os.close(fd)
    return Path(name)


def _ensure_backup(target_path, backup_path, original_raw, original_metadata):
    try:
        backup_stat = os.lstat(str(backup_path))
    except FileNotFoundError:
        backup_stat = None
    if backup_stat is not None:
        if not stat.S_ISREG(backup_stat.st_mode):
            raise EligibilityError("Refusing to use a non-regular backup.")
        backup_raw, _ = _read_regular(backup_path)
        _parse(backup_raw)
        return

    temporary_path = _temporary_path(target_path, "backup")
    try:
        _copy_preserved(target_path, temporary_path)
        _fsync_file(temporary_path)
        copied_raw, _ = _read_regular(temporary_path)
        if copied_raw != original_raw:
            raise EligibilityError("The backup does not match the original plist.")
        _assert_metadata(temporary_path, original_metadata)
        try:
            os.link(str(temporary_path), str(backup_path), follow_symlinks=False)
        except FileExistsError:
            if not stat.S_ISREG(os.lstat(str(backup_path)).st_mode):
                raise EligibilityError("Refusing to use a non-regular backup.")
        _fsync_directory(target_path.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _reverse_swap_and_preserve(temporary_path, target_path, message):
    try:
        _atomic_swap(temporary_path, target_path)
        _fsync_directory(target_path.parent)
    except Exception as exc:
        raise RecoveryError(
            "{} The displaced state remains at {}.".format(message, temporary_path)
        ) from exc
    raise RecoveryError(
        "{} The previous target was restored and the swapped-out state remains at {}.".format(
            message,
            temporary_path,
        )
    )


def _write_update(target_path, updated, updated_raw, original_raw, original_stat):
    original_metadata = _metadata(original_stat)
    temporary_path = _temporary_path(target_path, "update")
    swapped = False
    preserve_temporary = False
    try:
        _copy_preserved(target_path, temporary_path)
        if hasattr(os, "chflags"):
            os.chflags(str(temporary_path), 0, follow_symlinks=False)
        fd = os.open(
            str(temporary_path),
            os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(fd, "wb") as output:
            output.write(updated_raw)
            output.flush()
            os.fsync(output.fileno())
        if "flags" in original_metadata and hasattr(os, "chflags"):
            os.chflags(
                str(temporary_path),
                original_metadata["flags"],
                follow_symlinks=False,
            )

        candidate_raw, _ = _read_regular(temporary_path)
        candidate_data, _ = _parse(candidate_raw)
        if candidate_raw != updated_raw or candidate_data != updated:
            raise EligibilityError("The temporary replacement failed validation.")
        _assert_metadata(temporary_path, original_metadata)

        current_raw, current_stat = _read_regular(target_path)
        if _identity(current_raw, current_stat) != _identity(original_raw, original_stat):
            raise EligibilityError("The eligibility file changed; no replacement was made.")

        _atomic_swap(temporary_path, target_path)
        swapped = True
        displaced_raw, displaced_stat = _read_regular(temporary_path)
        if _identity(displaced_raw, displaced_stat) != _identity(original_raw, original_stat):
            _reverse_swap_and_preserve(
                temporary_path,
                target_path,
                "A concurrent change was detected.",
            )
        _assert_metadata(temporary_path, original_metadata)

        installed_raw, _ = _read_regular(target_path)
        installed_data, _ = _parse(installed_raw)
        if installed_raw != updated_raw or installed_data != updated:
            raise EligibilityError("The installed replacement failed read-back validation.")
        _assert_metadata(target_path, original_metadata)
        _fsync_directory(target_path.parent)
    except RecoveryError:
        preserve_temporary = True
        raise
    except Exception as exc:
        if swapped:
            preserve_temporary = True
            _reverse_swap_and_preserve(
                temporary_path,
                target_path,
                "The failed update could not be reversed.",
            )
        raise EligibilityError("The update was aborted without losing the previous state.") from exc
    finally:
        if not preserve_temporary:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def update_eligibility_file(target_path=ELIGIBILITY_PATH, backup_path=None):
    target_path = Path(target_path)
    backup_path = Path(backup_path or str(target_path) + BACKUP_SUFFIX)
    original_raw, original_stat = _read_regular(target_path)
    original_data, original_format = _parse(original_raw)
    updated, updated_raw = _updated_bytes(original_data, original_format)
    if updated == original_data:
        return False, backup_path

    original_metadata = _metadata(original_stat)
    _ensure_backup(target_path, backup_path, original_raw, original_metadata)
    _write_update(target_path, updated, updated_raw, original_raw, original_stat)
    return True, backup_path


def restore_backup(target_path=ELIGIBILITY_PATH, backup_path=None):
    target_path = Path(target_path)
    backup_path = Path(backup_path or str(target_path) + BACKUP_SUFFIX)
    backup_raw, backup_stat = _read_regular(backup_path)
    _parse(backup_raw)
    backup_metadata = _metadata(backup_stat)
    current_raw, current_stat = _read_regular(target_path)
    current_metadata = _metadata(current_stat)

    temporary_path = _temporary_path(target_path, "restore")
    swapped = False
    preserve_temporary = False
    try:
        _copy_preserved(backup_path, temporary_path)
        _fsync_file(temporary_path)
        candidate_raw, _ = _read_regular(temporary_path)
        if candidate_raw != backup_raw:
            raise EligibilityError("The restore candidate changed during validation.")
        _assert_metadata(temporary_path, backup_metadata)

        _atomic_swap(temporary_path, target_path)
        swapped = True
        displaced_raw, displaced_stat = _read_regular(temporary_path)
        if _identity(displaced_raw, displaced_stat) != _identity(current_raw, current_stat):
            _reverse_swap_and_preserve(
                temporary_path,
                target_path,
                "A concurrent target change was detected.",
            )
        _assert_metadata(temporary_path, current_metadata)

        restored_raw, _ = _read_regular(target_path)
        if restored_raw != backup_raw:
            raise EligibilityError("The restored plist failed read-back validation.")
        _assert_metadata(target_path, backup_metadata)
        _fsync_directory(target_path.parent)
    except RecoveryError:
        preserve_temporary = True
        raise
    except Exception as exc:
        if swapped:
            preserve_temporary = True
            _reverse_swap_and_preserve(
                temporary_path,
                target_path,
                "The failed restore could not be reversed.",
            )
        raise EligibilityError("Restore was aborted without losing the previous state.") from exc
    finally:
        if not preserve_temporary:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _invoking_user():
    if os.geteuid() != 0:
        raise EligibilityError("Run this script through sudo as documented.")
    try:
        uid = int(os.environ["SUDO_UID"])
        gid = int(os.environ["SUDO_GID"])
    except (KeyError, ValueError) as exc:
        raise EligibilityError("A non-root sudo invoking user is required.") from exc
    if uid <= 0 or gid < 0:
        raise EligibilityError("A non-root sudo invoking user is required.")
    try:
        return uid, gid, pwd.getpwuid(uid)
    except KeyError as exc:
        raise EligibilityError("The sudo invoking user does not exist.") from exc


def _drop_privileges(uid, gid, account):
    os.initgroups(account.pw_name, gid)
    os.setgid(gid)
    os.setuid(uid)
    if os.geteuid() != uid or os.getegid() != gid:
        raise EligibilityError("Could not drop administrator privileges.")


def launch_mirroring_app(account):
    subprocess.run(
        [OPEN_TOOL, APP_PATH],
        check=True,
        env={
            "HOME": account.pw_dir,
            "LOGNAME": account.pw_name,
            "PATH": SAFE_PATH,
            "USER": account.pw_name,
        },
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore", action="store_true")
    arguments = parser.parse_args(argv)
    uid, gid, account = _invoking_user()
    if arguments.restore:
        restore_backup()
        print("Eligibility plist restored from the retained backup.")
        return 0

    changed, backup_path = update_eligibility_file()
    if changed:
        print("Eligibility plist updated safely. Backup: {}".format(backup_path))
    else:
        print("Eligibility values were already configured; no rewrite was needed.")
    _drop_privileges(uid, gid, account)
    launch_mirroring_app(account)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EligibilityError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("Error: {}".format(exc))
