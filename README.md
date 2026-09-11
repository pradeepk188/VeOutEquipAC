# dbus-outequipac

Venus OS driver bridging an OutEquipPro rooftop AC's built-in BLE module
onto D-Bus, packaged for [kwindrem/SetupHelper](https://github.com/kwindrem/SetupHelper)'s
PackageManager.

**Read `NOTES.md` before installing.** The BLE characteristic UUIDs and
register map are inferred from Velit-branded units on the same platform,
not yet confirmed by direct capture against an OutEquip-branded module.

## Install

Requires SetupHelper already installed (Settings -> PackageManager on the
GX device / Remote Console). Then, either:

- **Via PackageManager (recommended once this is on your own GitHub repo):**
  Package Manager -> Inactive packages -> new -> enter this repo's name,
  your GitHub user, and branch/tag (`latest`) -> Install. You'll be
  prompted for the AC's BLE MAC address during install.

- **From the command line**, same pattern as installing any other
  SetupHelper package:
  ```
  wget -qO - https://github.com/<your-user>/dbus-outequipac/archive/latest.tar.gz | tar -xzf - -C /data
  mv /data/dbus-outequipac-latest /data/dbus-outequipac
  /data/dbus-outequipac/setup
  ```

Before either, update `gitHubInfo` in this repo to `<your-user>:latest`
(it currently says `yourGitHubUser:latest` as a placeholder) and push a
`latest` tag once you've verified the BLE layer against your hardware.

## Uninstall

Package Manager -> Active packages -> OutEquipAc -> Uninstall. This stops
the service and removes `mac.conf`.

## Files

| Path | Purpose |
| --- | --- |
| `setup` | SetupHelper-compatible install/uninstall script |
| `version`, `gitHubInfo` | Package metadata PackageManager reads |
| `outequipac.py` | Main driver: BLE poll loop + VeDbusService publishing |
| `ac_protocol.py` | Frame codec + register map for the Kingtec/Velit/OutEquip protocol |
| `ble_client.py` | bluepy-based BLE transport |
| `services/OutEquipAc/` | Service directory SetupHelper installs to `/service/OutEquipAc` |
| `NOTES.md` | Protocol confidence, open questions, verification steps, packaging notes |


# dev-tools

Local test scripts for validating assumptions against real hardware.
**Not part of the Venus OS package** -- SetupHelper doesn't install this
directory; it never ships to the Pi.

## ble_scan_test.py

Windows-side BLE probe using `bleak` (cross-platform: Windows/macOS/Linux),
as opposed to `ble_client.py`'s `bluepy` (Linux/BlueZ only -- what actually
runs on the Pi). Confirms whether `ac_protocol.py`'s frame codec and
`ble_client.py`'s assumed service/characteristic UUIDs (FFE0/FFE1/FFE2)
match your actual OutEquipPro unit, before trusting either on the Pi.

### Setup

```
pip install bleak
```

### Usage

```
# 1. Find the AC's BLE address. Look for a name starting with KT, or
#    containing OutEquip/Velit.
python ble_scan_test.py scan

# 2. Confirm the GATT layout (service/characteristic UUIDs) matches what
#    ble_client.py assumes.
python ble_scan_test.py dump <ADDRESS>

# 3. Send one read-only query frame for a register, decoded through the
#    real ac_protocol.py codec.
python ble_scan_test.py query <ADDRESS> --register power
```

`query` only ever sends read frames (value=0) -- it can't change the AC's
actual state, so it's safe to run before you trust anything else here.

If `dump` shows different UUIDs than expected, update
`ble_client.py`'s `SERVICE_UUID` / `NOTIFY_CHAR_UUID` / `WRITE_CHAR_UUID`
constants to match before doing anything else with this project.



## Refs

- https://github.com/bobbaboui/outequip-ha
- https://github.com/gongloo/OutEquipAC
- https://github.com/JohnFreeborg/velit-hass
- https://github.com/bobbaboui/outequip-ha/blob/main/PROTOCOL.md