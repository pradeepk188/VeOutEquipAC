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


## Refs

- https://github.com/bobbaboui/outequip-ha
- https://github.com/gongloo/OutEquipAC
- https://github.com/JohnFreeborg/velit-hass
- https://github.com/bobbaboui/outequip-ha/blob/main/PROTOCOL.md