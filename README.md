# iPhone Mirroring EU enabler

This experimental workaround changes macOS's local eligibility data and opens
iPhone Mirroring. Apple currently lists iPhone Mirroring as unavailable in the
European Union; this project is not affiliated with or supported by Apple.

> [!CAUTION]
> The script rewrites a private macOS system plist. Back up the file first and
> expect macOS updates or eligibility checks to overwrite the change. The plist
> format is undocumented and may change between macOS releases.

## Requirements

The devices must meet [Apple's iPhone Mirroring requirements](https://support.apple.com/120421):

- a Mac with Apple silicon or the Apple T2 Security Chip, running macOS
  Sequoia 15 or later;
- an iPhone running iOS 18 or later, protected by a passcode;
- the same Apple Account with two-factor authentication on both devices;
- Wi-Fi and Bluetooth enabled, with the locked iPhone near the Mac; and
- no active Internet Sharing, AirPlay, or Sidecar session on the Mac.

This workaround additionally requires a US App Store account on the iPhone.
Changing the App Store account on the Mac is not required for a one-time run.

## Run

1. In **System Settings > Privacy & Security > Full Disk Access**, enable the
   terminal application you will use, then restart that application.
2. Back up the eligibility plist:

   ```sh
   sudo cp -p -n /private/var/db/os_eligibility/eligibility.plist \
     ~/Desktop/eligibility.plist.backup
   ```

   The `-n` flag prevents a later run from overwriting that backup.

3. From this repository, run:

   ```sh
   sudo python3 enable.py
   ```

The script changes the iPhone Mirroring eligibility values in
`OS_ELIGIBILITY_DOMAIN_IRON`, saves the plist, and opens the iPhone Mirroring
app.

## Troubleshooting and persistence

- `Operation not permitted`: confirm both Full Disk Access and `sudo`; either
  one alone may be insufficient.
- The app still reports a country or region restriction: confirm that the US
  App Store account is active on the iPhone and that all Apple requirements
  above are satisfied.
- The change disappears after closing the app or after an eligibility refresh:
  macOS can regenerate the plist. See the separate
  [loop-based companion project](https://github.com/Pauli1Go/iphone-mirroring-eu-enabler-loop)
  for its persistence workflow.

To restore the saved plist, run the following command and restart the Mac:

```sh
sudo cp -p ~/Desktop/eligibility.plist.backup \
  /private/var/db/os_eligibility/eligibility.plist
```

## License

Licensed under the [Apache License 2.0](LICENSE).
