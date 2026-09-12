# Protocol confidence & open questions

This driver's protocol layer (`ac_protocol.py`) is compiled from two
published, cross-checked sources -- not from a direct capture against your
specific unit yet:

- **Register semantics** (what each key means, quirks, on/off polarity):
  `gongloo/OutEquipAC/protocol.md` -- OutEquipPro-specific, but documented
  over the wired UART interface (115200 baud), not BLE.
- **BLE transport** (service/characteristic UUIDs, framing, timing):
  `bobbaboui/outequip-ha/PROTOCOL.md`, itself built from Velit-branded units
  on what looks like the same Kingtec-ish ODM platform. No BLE capture from
  an OutEquip-branded module has been published anywhere I could find.

The wired-UART frame format (preamble/length/device-type/register/value/
checksum/postamble) and the BLE frame format described for Velit units are
identical, and the underlying board is documented (via the `AT+NAME?`
handshake, `KT<digits>`) as answering to the same module family. That's
reasonable circumstantial evidence this will just work, but "reasonable
circumstantial evidence" is exactly the kind of thing to verify with your
actual hardware before trusting it for automation (especially the write
paths -- turning your AC on/off/heat unattended).

## Confirmed against real hardware

- **GATT layout confirmed** (2026-09) against a real unit, name
  `KT2026050001550`, MAC `E6:87:25:EC:B3:84`, via `dev-tools/ble_scan_test.py
  dump`: service `FFE0`, notify `FFE1`, write `FFE2` all present with the
  expected properties. `ble_client.py`'s `SERVICE_UUID`/`NOTIFY_CHAR_UUID`/
  `WRITE_CHAR_UUID` constants need no changes.
- Register-level frame decoding (point 3 below) still needs the same
  `query` command run and its output compared against expectations.
- **Real app's connection stayed stable through a power toggle.** An ADB
  `logcat` capture (not an HCI snoop -- no characteristic payload bytes,
  only high-level GATT lifecycle calls) of the app connecting, toggling
  the AC on then off, and disconnecting showed the module holding one
  continuous connection the whole time: connect -> discoverServices ->
  MTU negotiated to 517 -> subscribe Service Changed (`2A05`) -> connection
  parameters updated (45ms interval / 5s supervision timeout) -> subscribe
  `FFE1`. No disconnect logged anywhere in that ~30s window. This is
  evidence *against* "the module is just flaky" as the root cause of the
  Windows-side instant-disconnect symptom -- something specific to that
  bleak/Windows session is the more likely explanation.
  `dev-tools/ble_scan_test.py` now subscribes to `2A05` before `FFE1` too,
  best-effort, mirroring the one concrete sequence difference this
  capture revealed. Unconfirmed whether the module's firmware actually
  needs it or the app just does it as generic Android BLE hygiene.
  Getting the actual write/notify *payload bytes* still needs an HCI
  snoop log (Developer Options -> Enable Bluetooth HCI snoop log -> pull
  `btsnoop_hci.log` -> open in Wireshark), not a plain `adb logcat`.
- **Register map and quirks independently confirmed.** Re-checked
  `bobbaboui/outequip-ha`'s README/PROTOCOL.md directly (not from memory):
  power `1=Off/2=On`, the cooling-before-power-off workaround, Light
  register being write-only-in-practice, and the undocumented voltage/
  current endianness (their code tries big-endian first, falls back to
  little-endian only if implausible) all match what's already implemented
  here. Nothing found there that requires a code change.
- **Direct-host BLE against this module family is known to be flaky --
  not just on Windows.** That same README explains the author didn't use
  their host machine's (an Intel N100 mini PC) onboard Bluetooth adapter
  directly at all: it was "too flaky" against this device, so they run a
  dedicated ESP32-S3 as a pure Bluetooth proxy instead and disabled the
  onboard adapter entirely, citing Home Assistant's own guidance that
  overall reliability is limited to the worst-performing adapter in use.
  That's a Linux/BlueZ host, not Windows/WinRT, having similar
  connection-reliability problems with this chip family -- consistent
  with (though not proof of) what we saw testing from Windows. If the
  Raspberry Pi's onboard Bluetooth turns out to be similarly flaky once
  tested directly, an ESP32-S3 running ESPHome's stock Bluetooth-proxy
  firmware (no AC-specific code needed on the ESP32 itself) as a relay
  between the AC and the Pi is a proven fallback architecture, not a
  hypothetical one -- see `bobbaboui/outequip-ha`'s `proxy.yaml`.
- **Initialization handshake required.** Re-reading `protocol.md`'s
  Initialization section directly (rather than from memory) turned up a
  step neither `ble_client.py` nor the original `ble_scan_test.py` did: the
  real app queries key `66` (Active) immediately on connect and, if the
  reply is `2`, writes `1` back, *before* querying anything else. It also
  never queries key `1` (Power) directly at all -- only `2, 3, 7, 8, 18, 19`
  after that handshake. `dev-tools/ble_scan_test.py query` now does this
  handshake automatically before sending the requested query.
  `outequipac.py`'s poll loop does not yet do this handshake -- needs
  adding once `query` confirms it's actually necessary for responses (vs.
  just app bookkeeping) against this unit.
  **Update:** now implemented in `outequipac.py` (`_do_active_handshake`,
  called right after `connect()` and before the poll loop starts). While
  fixing this, also found and fixed the same `proto.RESPONSE_TIMEOUT_S`
  mistake here that was in the original `ble_scan_test.py` -- that
  constant lives in `ble_client.py`, not `ac_protocol.py`; the old code's
  `hasattr` check silently masked it by falling back to a hardcoded 3.0s,
  which happened to work but was dead, confusing code. Now imports and
  uses the real constant directly.
- **Executable bit lost in transit, confirmed on the Pi.** PackageManager's
  log showed `ERROR:setup file for VeOutEquipAC not executable` --
  `setup` (and the `services/OutEquipAc/run`/`log/run` scripts) lost their
  Unix executable bit somewhere in the Windows-checkout -> GitHub ->
  `git clone` on the Pi round-trip. This silently broke the install: the
  interactive MAC-address prompt never ran (so `mac.conf` never got
  created), and the service was never registered under `/service/` or
  `/opt/victronenergy/service/` despite the source `services/OutEquipAc/`
  folder existing under `/data/VeOutEquipAC/`. Fixed on-device with
  `chmod +x`; see README.md for the permanent `git update-index --chmod=+x`
  fix so future clones don't lose it again.
- **`endScript` keyword typo, found by reading the real installed source on
  the Pi.** `setup` called `endScript INSTALL_SERVICE` (singular) but
  `endScript`'s actual `case` statement (in
  `/data/SetupHelper/HelperResources/CommonResources`) only matches
  `INSTALL_SERVICES` (plural) -- a non-matching case falls through silently,
  so the script reported "complete - no errors" while never actually
  calling `installAllServices`/`installService` at all. This is why
  `mac.conf` got written correctly (that's our own code, ran fine) but the
  service was never registered under `/service/` or
  `/opt/victronenergy/service/` despite the source `services/OutEquipAc/`
  folder existing. Fixed: `setup` now calls `endScript INSTALL_SERVICES`.
  Confirmed against SetupHelper's real source, not documentation/memory --
  worth doing this for anything else `setup` calls before trusting it
  again.
- **Missing D-Bus main loop registration, found running directly on the
  Pi.** `outequipac.py` crashed immediately with `RuntimeError: ...D-Bus
  connections must be attached to a main loop...` the moment it tried to
  construct `VeDbusService`. python-dbus requires `DBusGMainLoop` to be
  registered as the default main loop *before* any D-Bus connection is
  made -- every other Venus OS driver using `velib_python` does this at
  startup; this one simply never did. Fixed: `from dbus.mainloop.glib
  import DBusGMainLoop; DBusGMainLoop(set_as_default=True)` now runs
  before `vedbus` is imported. Note this doesn't require ever calling
  `mainloop.run()` -- the driver's own polling loop with `time.sleep()`
  provides its own scheduling; the main loop object just needs to exist
  to satisfy python-dbus's requirement.
- **`ext/velib_python` symlink now created automatically by `setup`, not
  a manual step.** The manual `ln -s` done earlier during testing didn't
  survive a reinstall -- PackageManager re-extracts the package tarball
  fresh each time, which wipes any file not tracked in the git repo,
  symlinks included. `setup` now creates it itself on every `INSTALL`
  action (pointing at `/opt/victronenergy/dbus-systemcalc-py/ext/velib_python`),
  so it gets recreated every single install, scripted or automatic,
  rather than depending on someone remembering to redo it by hand.

## Before relying on this (remaining items)

1. **Confirm the GATT layout on your unit.** From the Pi:
   ```
   bluetoothctl scan on          # find the KT... name and MAC
   gatttool -b <MAC> --primary
   gatttool -b <MAC> --characteristics
   ```
   Confirm `0000ffe0-...` (service), `...ffe1...` (notify), `...ffe2...`
   (write) actually exist. If they don't, the module answered `AT+NAME?`
   differently than expected, or it's not the JDY-23/CC254x-class chip
   assumed here -- capture instead (next point).

2. **If UUIDs don't match:** capture a session between the OutEquip phone
   app and the unit (Android: enable Bluetooth HCI snoop log in Developer
   Options, use the app normally, pull `btsnoop_hci.log`, open in
   Wireshark). That will show the real service/characteristic UUIDs and
   let you diff the byte stream against `ac_protocol.py`'s frame decoder
   directly.

3. **Verify register 18 (voltage) endianness on your firmware.** Neither
   source has a worked example; `outequipac.py` guesses big-endian and
   falls back to little-endian only if the result is outside 5-60V. Watch
   the first few `/Ac/SupplyVoltage` values against a multimeter/known
   supply voltage.

4. **Confirm register 2 value 7** ("Wet" per OutEquip's own doc vs.
   "Dehumidify" per Velit's) and whether an 8 ("Vent") exists on your
   firmware -- doesn't affect the driver's correctness, just the
   `MODE_NAMES` label.

## Known firmware quirks already handled

- **Mismatched responses**: a *set* command's reply reflects the
  *last-queried* register, not the one just written. This driver never
  trusts an immediate write response as confirmation -- it only updates
  dbus values from frames matched against their own register number, and
  a follow-up poll cycle will pick up the real state.
- **Phantom heating**: `_on_power_write` always forces Cooling mode before
  turning power off, per the documented workaround.
- **Light register (28)** is not wired up to a dbus path in v0.1 -- it's
  inverted vs. every other on/off register *and* reads back garbage, so
  it's not worth exposing until there's a concrete reason to control it.

## Why `com.victronenergy.switch` + a grab-bag `/Ac/*` namespace

Victron doesn't have a device class for "rooftop air conditioner." Power
on/off is exposed as a proper `SwitchableOutput` under the `switch` service
because that's the one control the GX touchscreen / Remote Console can
render out of the box. Everything else (mode, fan speed, setpoint, swing,
temps, supply voltage) is published as extra, non-standard paths on the
same service -- ignored by the stock GUI, but they ride Venus's existing
dbus-to-MQTT bridge, so they show up for Node-RED/VRM automation the same
way any other dbus value does. If you want native-looking widgets later,
splitting the temperatures out into a proper `com.victronenergy.temperature`
service instance is the standard move (see how `dbus-mqtt-devices` does it)
-- deliberately left out of v0.1 to keep one service/one MAC/one poll loop
while the BLE layer itself is still unverified.

## Packaging: SetupHelper

This package now follows kwindrem/SetupHelper conventions (`version`,
`gitHubInfo`, `setup`, `services/OutEquipAc/{run,log/run}`) so it installs,
uninstalls, and reinstalls itself after a Venus OS firmware update through
PackageManager, instead of a hand-rolled runit drop-in.

- `setup` sources `/data/SetupHelper/HelperResources/IncludeHelpers`,
  prompts for the AC's BLE MAC address (the one thing this package needs
  beyond SetupHelper's automatic file/service handling), and calls
  `endScript INSTALL_SERVICE`.
- The MAC address is stored in `mac.conf` inside the package's own
  directory (`/data/VeOutEquipAC/mac.conf`) rather than through
  SetupHelper's `$setupOptionsDir` mechanism -- simpler, and the package
  directory itself already survives firmware updates and reinstalls the
  same way `$setupOptionsDir` would.
- `services/OutEquipAc/run` is what SetupHelper's `INSTALL_SERVICE`
  installs to `/service/OutEquipAc`.
- **`ext/velib_python` is still not wired up.** `outequipac.py` expects it
  at `<packageDir>/ext/velib_python`. SetupHelper ships a `velib_python`
  copy and a `makeVelib_python` helper for this -- check
  `SetupHelper/makeVelib_python` for the expected mechanism rather than
  hand-symlinking from `dbus-systemcalc-py`, since the SetupHelper-managed
  copy is the one PackageManager expects to stay in sync.
- I have **not** run this against a real Venus OS install or PackageManager
  -- the `setup` script is written to match
  `PackageDevelopmentGuidelines.md` closely, but small details (exact
  behavior of `standardActionPrompt`'s return, whether `scriptAction`
  comparisons need `[[ ]]` vs `[ ]` in this shell) are worth checking
  against `SetupHelper/genericSetupScript` or an existing simple package
  (e.g. GuiMods) before trusting it on your system.
- No `packageDependencies` or `raspberryPiOnly` files yet. This should
  work on any Venus OS device with a functioning BlueZ/BLE stack, not just
  Raspberry Pi, so `raspberryPiOnly` was deliberately left out -- add it if
  you find that's wrong in practice.

## Not yet done

- BLE layer verification (see above) -- do this before the SetupHelper
  packaging matters at all, since a wrong UUID means the service just
  fails to connect regardless of how it's installed.
- No systemd/serial-starter integration for auto-discovery -- MAC address
  is a manual value entered once during setup.
