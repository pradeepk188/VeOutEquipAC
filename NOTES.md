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
