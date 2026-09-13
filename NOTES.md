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
- **Root cause of connection failures on the Pi found (2026-09): a
  post-connection link-layer procedure fails against this AC module on the
  Pi's onboard Bluetooth controller -- not a competing Venus service.**
  First captured with `btmon -w` during a plain `bluetoothctl connect
  <MAC>` against the confirmed unit (`E6:87:25:EC:B3:84`,
  `KT2026050001550`) on a Raspberry Pi's onboard controller
  (Cypress/Broadcom chip, single `hci0`). Initial read of that capture
  wrongly blamed `vesmart_server` (Venus's VE.Smart Networking service)
  colliding on the radio via `MGMT Command: Add Advertising` around the
  same time -- **that was coincidental noise, not the cause.** Ruled out by
  directly testing, in order, with each stopped (`svc -d
  /service/<name>`) and the connection still failing identically every
  time: `vesmart-server`, `bluetoothd`'s own self-advertising/discoverable
  state (confirmed via `bluetoothctl show`: `Discoverable: no`, `Pairable:
  no`, `ActiveInstances: 0x00`), and `dbus-ble-sensors`. A second `btmon`
  capture with all of those stopped -- zero other HCI/mgmt activity on the
  adapter at all -- reproduced the exact same failure with nothing else
  touching the radio:
  ```
  LE Create Connection sent
  Command Status: Success
  LE Connection Complete: Status Success        <- connected
  LE Read Remote Used Features sent (automatic, kernel/BlueZ-initiated
    on every new LE connection, not something this driver controls)
  ~140ms later: LE Meta Event "LE Read Remote Used Features" completes
    with Status: Connection Failed to be Established (0x3e)
  Disconnect Complete: Reason: Connection Failed to be Established (0x3e)
  ```
  At the application layer this shows up as `bluetoothctl connect`
  reporting `Connected: yes` immediately followed by `Failed to connect:
  org.bluez.Error.Failed le-connection-abort-by-local` -- `-abort-by-local`
  confirms the local host tore it down, not the AC actively rejecting.
  Both `btmon` captures (with and without other services active) show the
  identical `LE Read Remote Used Features -> 0x3e` event as the actual
  failure point -- the AC's BLE module isn't completing that link-layer
  feature-exchange procedure properly, and the Pi's onboard controller
  firmware kills the connection as a result, ~140ms after "connecting",
  before this driver's code (or even `ble_scan_test.py`'s Active-register
  handshake) ever gets a chance to send anything. This is a firmware/
  link-layer incompatibility between this AC module and the RPi's onboard
  BT chip, not something fixable in `ac_protocol.py`/`ble_client.py`.
  **Next thing to try:** a cheap external USB Bluetooth 4.0/5.0 dongle
  (shows up as `hci1`) in place of the onboard adapter. If that connects
  cleanly, it confirms the onboard Cypress/Broadcom chip's BLE firmware as
  the culprit and gives a real fix (use `hci1` for this driver going
  forward -- `ble_client.py`/`bluepy` needs to target that adapter
  explicitly). If a different adapter fails the same way, that points at
  the AC module's own BLE firmware not handling the feature-exchange
  procedure correctly regardless of host, and the ESP32-S3 Bluetooth-proxy
  fallback described above becomes the path forward instead of continued
  direct-host debugging.
  **Also ruled out (2026-09-13):** `/etc/bluetooth/ble.conf` (the config
  file Venus launches `bluetoothd` with -- `bluetoothd -E -f
  /etc/bluetooth/ble.conf`, confirmed via `ps`; there is no
  `/etc/bluetooth/main.conf` on this device, the default BlueZ looks for --
  Venus overrides the path) had `Privacy = device` active, enabling LE
  Privacy (rotating resolvable private address for the local/central side).
  Commented it out, rebooted (bluetoothd isn't under a runit `/service/`
  entry on this device, so a full reboot was the reliable way to force a
  config reload -- confirmed via `ps w | grep bluetoothd` showing the same
  `-f /etc/bluetooth/ble.conf` invocation post-reboot), confirmed via
  `bluetoothctl`'s `hci0 new_settings:` line no longer listing `privacy`.
  Retried the connection: identical failure
  (`le-connection-abort-by-local`). LE Privacy is not the cause either.
  USB-dongle test (above) is still the next concrete step on the host-side
  investigation; not done yet as of this note -- paused to instead capture
  the real app's traffic (below), which independently confirms the AC
  module itself holds a stable connection fine, strengthening the case that
  this is specific to the Pi's onboard controller.

- **Sniffing the real OutEquipPro Android app's BLE traffic against the AC
  (in progress, 2026-09-13) -- confirms the AC holds a connection fine from
  a phone; wire-format decoding blocked on Android's HCI log redaction, fix
  identified but not yet re-captured.** Goal: get real protocol payload
  bytes from a working session to check against `ac_protocol.py`'s assumed
  frame format, since the Pi can't sustain a connection at all (above).
  Method and exact repro, so this doesn't need re-deriving:
  1. Phone: Developer Options -> "Bluetooth HCI snoop log" -- this device
     has a **3-way picker**: `Disabled` / `Enabled (Filtered)` / `Enabled`
     (not a simple on/off toggle -- don't assume it is on other phones).
     `Enabled (Filtered)` is Android's privacy-redacted mode and is *not*
     what we want.
  2. Phone connected via USB, `adb devices` from
     `tools/platform-tools-latest-windows/platform-tools/adb.exe` (already
     in this repo's dev tools, gitignored) to confirm it's authorized.
     Note: adb drops the device from `devices` if the phone's screen
     locks/USB debugging re-prompts -- re-check `adb devices` if a
     subsequent command reports "no devices/emulators found".
  3. Used the OutEquipPro app normally against the confirmed AC
     (`E6:87:25:EC:B3:84`): connect, power on, change fan speed/mode,
     power off, disconnect. AC visibly responded to all of it -- this
     session's phone-to-AC BLE connection is solid proof the AC module
     itself isn't the problem; whatever's failing on the Pi is specific to
     the Pi's host/controller.
  4. Pulled the log via `adb bugreport bugreport_outequip.zip` (reliable,
     no root needed) -- the Bluetooth HCI snoop log is at
     `FS/data/misc/bluetooth/logs/btsnooz_hci.log` inside that zip.
     Despite the `btsnooz` filename (Android's *compressed* format used
     elsewhere), extracting it directly (`unzip -p bugreport_outequip.zip
     FS/data/misc/bluetooth/logs/btsnooz_hci.log > btsnooz_hci.log`) gave a
     file starting with the literal `btsnoop\0` magic header -- i.e. it's
     already standard uncompressed btsnoop format on this device/Android
     version, no `btsnooz.py`-style conversion needed. Just copy/rename to
     a `.btsnoop` extension and it opens directly.
  5. Analyzed with `tshark` (Wireshark CLI, found at `C:\Program
     Files\Wireshark\tshark.exe` on this Windows dev machine -- not on
     PATH, must use the full path). `tshark -r <file>.btsnoop -q -z
     io,phs` gives a protocol hierarchy summary; filtering
     `bthci_evt.code == 0x3e` (LE Meta) for connection-complete subevents
     and reading `bthci_evt.bd_addr`/`bthci_evt.connection_handle` finds
     which HCI connection handle is the AC's (was `0x0041` in this
     capture, spanning frames ~340-1200, roughly t=11.9s-54.4s, exactly
     one connect and one disconnect -- confirmed no reconnects by checking
     every `bthci_evt.code == 0x05` Disconnect Complete in the whole
     file). Filtering `bthci_acl.chandle == 0x0041 && btatt` then shows
     the actual GATT traffic: write characteristic value-handle `0x0010`,
     notify value-handle `0x0012` (both showed as `(Unknown)` to
     Wireshark's GATT dissector, expected for FFE1/FFE2-style vendor
     16-bit UUIDs that aren't in the SIG database -- consistent with, not
     contradicting, the already-confirmed FFE0/FFE1/FFE2 GATT layout).
  6. **Initial read looked alarming: every write and notification payload
     on that connection was only 3 bytes (`5A 5A <byte>`), constant `5A 5A
     06` on every single write (109 of them) regardless of what button was
     pressed in the app, with the notify side occasionally blipping to
     `07`/`08`.** This looked like it might mean the real BLE wire format
     for this OutEquip-branded module is a minimal 3-byte frame, nothing
     like `ac_protocol.py`'s assumed 9-byte
     `5A 5A LEN DEV REG VAL CHK 0D 0A` framing (which makes some sense on
     its face -- BLE's ATT/L2CAP layer already delimits and CRCs each
     packet, so a UART-style length/checksum/postamble that
     `protocol.md`'s wired-UART framing needs would be redundant, and the
     BLE variant of the same logical protocol could legitimately be
     shorter). **This turned out to be wrong** -- see next point --
     flagging it here only because it's a plausible-looking dead end worth
     not re-deriving/re-falling-for.
  7. **Root cause of the too-short payloads: Android's HCI snoop log
     truncates/redacts GATT payload bytes even in the plain `Enabled`
     mode on this device, not just `Enabled (Filtered)`.** Proved via
     `tshark -T fields -e frame.len -e frame.cap_len`: e.g. frame 487 had
     `frame.len` (real/original packet length) of 21 bytes but
     `frame.cap_len` (what was actually logged) of only 15 -- 6 bytes
     silently cut. Working back from the L2CAP declared payload length (12
     bytes = 1-byte ATT opcode + 2-byte handle + **9-byte value**), the
     real write value is 9 bytes -- which matches `ac_protocol.py`'s
     assumed frame format exactly. The "constant 3-byte heartbeat" was
     just the unredacted first 3 bytes of every real (longer, presumably
     varying) frame; everything after byte 3 was zeroed/stripped by the
     phone's own OS before it ever reached the log file. This is why the
     write payload never appeared to change even though the AC visibly
     responded to real button presses -- the actual command bytes were
     there on the wire, just not in what got logged.
  8. Tried to force full/unredacted logging via
     `adb shell setprop persist.bluetooth.btsnooplogmode full` --
     **failed, "Failed to set property... See dmesg for error reason"**
     (this needs root; this phone is unrooted). `adb shell settings get
     secure bluetooth_hci_log` also returned `null` (not the right
     setting key on this Android version either).
  9. Re-selecting plain `Enabled` (it was already selected, but a
     Bluetooth off/on toggle was needed to actually apply it -- the prior
     capture's truncation was stale state, not `Enabled` itself being
     redacted) plus setting the separate, unrelated "Bluetooth" log
     *verbosity* picker (Info/Debug/Warn/Error/Verbose -- a different
     Developer Options entry, general Bluetooth-stack logging, nothing to
     do with HCI snoop redaction) to `Verbose` for good measure, then
     redoing the app interaction, fixed it: the fresh `adb bugreport`'s
     `FS/data/misc/bluetooth/logs/btsnoop_hci.log` (note: genuinely named
     `btsnoop_hci.log` this time, not `btsnooz_hci.log` -- another sign the
     unredacted mode was active) showed **zero** `frame.len` !=
     `frame.cap_len` mismatches across all 1997 frames.
  10. **Bug found and fixed in `ac_protocol.py`'s `Frame.encode()`/
      `decode()`: the length-byte math was wrong, off by the 2-byte
      postamble.** Decoding the real captured frames through the actual
      `Frame.decode()` (not a reimplementation -- this matters, see
      CLAUDE.md's note about `ble_scan_test.py` sharing the real codec for
      the same reason) failed on **every single real frame** with `bad
      postamble` -- e.g. a real 1-byte-value frame
      `5a 5a 06 01 12 00 cd 0d 0a` has length byte `0x06`, but the old code
      computed `value_len = length - 3` (assuming length counts only
      dev+reg+val+checksum), consuming the checksum and half the postamble
      as if they were value bytes. Confirmed from two independent frame
      shapes in the capture (a 1-byte-value frame with length=0x06, a
      2-byte-value voltage-register frame with length=0x07) that length
      actually counts dev+reg+val+checksum+**postamble** = value_len+5, not
      value_len+3. Fixed: `decode()` now uses `value_len = length - 5`;
      `encode()` now computes `length = len(body) + 1 + len(POSTAMBLE)`
      instead of `len(body) + 1`. This means **every frame this driver has
      ever sent to real hardware had a wrong length byte** (4 instead of 6
      for the common 1-byte-value case) -- worth keeping in mind if any
      past on-device testing behaved strangely; the AC's firmware may have
      been silently misparsing or ignoring malformed writes rather than the
      BLE layer being at fault.
  11. **Full validation after the fix, against the *previous* (rotated-out)
      BLE session in the same bugreport.** Android keeps one rotated-out
      prior snoop log alongside the current one:
      `FS/data/misc/bluetooth/logs/btsnoop_hci.log.last` in the same zip.
      The *current* log's captured AC session (`chandle 0x0041`,
      ~162-176s relative) turned out to be a later, idle reconnect that
      only ever polled (42 write/42 notify frames, every write value=0 --
      i.e. pure reads, no real commands) -- worth remembering if a future
      capture looks like "nothing but polling," check `.log.last` too
      before concluding the app isn't sending real commands. The *real*
      interaction session was in `.log.last` (`chandle 0x0041` again,
      ~764-806s relative, 252 write/notify frames, clean disconnect at the
      end this time). **All 252 frames decoded with zero errors** through
      the fixed `Frame.decode()`, and the actual command frames matched
      the real button presses exactly:
      `POWER=2` (power on) -> `SETPOINT=67,68,69` (three temperature taps)
      -> `FAN_SPEED=2` (fan speed change) -> `POWER=1` (power off).
      `ON_OFF_ON = 2` / `ON_OFF_OFF = 1` in `ac_protocol.py` match exactly.
      This confirms the frame format, checksum algorithm (`sum(payload) &
      0xFF` over preamble+length+dev+reg+value, matching `encode()`
      exactly), `DEVICE_TYPE_AC = 0x01`, and the whole `REG_*` map end to
      end against real hardware, not just circumstantially from two other
      reverse-engineering projects.
  12. **New/undocumented registers seen in the real app's idle poll cycle**
      (not in `ac_protocol.py`'s `REG_*` map -- meanings unknown, noting
      the raw numbers in case they matter later): register `9` (seen
      value `1`), register `11` (value `0`), register `22` (value `3`),
      and register `26` queried but the *reply* came back tagged as
      register `27` (value `0`) -- consistent with the documented
      "mismatched response reflects the last-queried register" firmware
      quirk applying even to plain reads, not just writes.
  13. **Register 66 (`REG_ACTIVE`) never appeared in either real captured
      app session.** The real OutEquipPro app did not query it before, or
      at any point during, ~14s and ~42s of normal use on this unit. This
      contradicts the `protocol.md`-sourced assumption (recorded earlier
      in this file) that the app always does the Active-handshake first.
      Not removing `outequipac.py`'s `_do_active_handshake()` over this
      alone -- it may still be harmless/a no-op, or may matter for a fresh
      (never-before-paired) connection specifically, which this phone's
      session wasn't -- but flagging the discrepancy rather than treating
      the handshake as confirmed-necessary.
  13b. **A second, independent copy of the same length-byte bug was found
      in `try_decode()` (2026-09-13), after the fix above -- this one
      matters more for live operation.** `try_decode()` is what
      `outequipac.py`'s `_on_bytes()` actually calls on every real BLE
      notification; it does its own frame-boundary math rather than
      sharing `Frame.decode()`'s. It had `total_len = 3 + length + 2`,
      double-counting the postamble the same way the old `encode()`/
      `decode()` did. Fixed to `total_len = 3 + length`. Verified by
      feeding all 252 real frames from the `.log.last` capture through
      `try_decode()` as a simulated notification stream (one frame's worth
      of bytes appended at a time, matching how `_on_bytes` receives BLE
      notifications): 252 extracted, 0 errors, 0 bytes left over. Before
      this fix, the live driver would have kept working for exactly one
      frame after any reconnect and then silently broken frame-boundary
      detection for everything after that (waiting for 2 bytes that never
      arrive, then eating the start of the next frame's preamble trying to
      satisfy that wait) -- so this was a real, live-operation-breaking bug
      independent of the BLE-connection-layer issue above, not just a
      decode-path issue confined to `ble_scan_test.py`/offline analysis.
  14. **Practical note for reproducing this on a Pixel (and possibly other
      Android 12+ phones):** Developer Options' "Bluetooth HCI snoop log"
      is a 3-way picker here -- `Disabled` / `Enabled (Filtered)` /
      `Enabled` -- not a simple toggle; don't assume otherwise on a
      different phone. `Enabled (Filtered)` is the redacted mode and
      useless for protocol reverse-engineering. Setting `Enabled` requires
      a Bluetooth off/on toggle to actually take effect, not just
      reselecting it in the menu. `adb shell setprop
      persist.bluetooth.btsnooplogmode full` does **not** work on an
      unrooted phone ("Failed to set property... See dmesg", needs root) --
      don't bother trying it again, use the in-menu picker instead.
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
