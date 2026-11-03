# iphone-mirroring-eu-enabler

Enables iPhone Mirroring in the EU by changing the local macOS eligibility
database. This is an unsupported system modification. Review `enable.py` and
keep its backup. SIP can remain enabled.

## Usage

1. Temporarily grant Terminal **Full Disk Access** in System Settings.
2. Run with Apple's system Python in isolated mode:

   ```bash
   sudo /usr/bin/python3 -I enable.py
   ```

3. Confirm that iPhone Mirroring works, then revoke Terminal's Full Disk Access.

The script validates the plist, preserves its format and metadata, writes via
an atomic macOS file swap, rejects concurrent changes, and keeps this
non-overwriting first-run backup:

```text
/private/var/db/os_eligibility/eligibility.plist.iphone-mirroring-eu-enabler.backup
```

To restore that snapshot, temporarily grant Full Disk Access and run:

```bash
sudo /usr/bin/python3 -I enable.py --restore
```

The snapshot may be older than the current macOS state. Revoke Full Disk Access
again when finished.

## Tests

Tests use temporary files only:

```bash
/usr/bin/python3 -I -m unittest discover -s tests -v
```

To automate repeated application of the eligibility change, see the separate
[loop project](https://github.com/Pauli1Go/iphone-mirroring-eu-enabler-loop).
