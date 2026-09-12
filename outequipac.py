#!/usr/bin/env python3
"""
VeOutEquipAC: Venus OS driver bridging an OutEquipPro AC's built-in BLE
module onto the D-Bus (and from there, onto Venus's local/VRM MQTT bridge,
Node-RED, and the GX device list).

Modeled after the existing Venus driver conventions used by
dbus-serialbattery / dbus-btbattery (velib_python's VeDbusService, a
poll-and-publish loop), packaged via kwindrem/SetupHelper's
`services/<name>/run` convention so it fits the same
install/update/troubleshoot workflow already in place on this Pi.

Because there's no first-class Victron device class for "rooftop AC",
this exposes:
  - Power as a `com.victronenergy.switch` SwitchableOutput (on/off, and it's
    the one thing worth controlling from the GX touchscreen / Remote Console).
  - Everything else (mode, fan speed, setpoint, swing, intake/outlet temp,
    supply voltage) as extra, non-standard paths under /Ac/ on the same
    service. The stock GUI won't render those, but they ride along on
    Venus's dbus->MQTT bridge for Node-RED/automation, same as any other
    dbus value.

Known unknowns (see NOTES.md): FFE1/FFE2 characteristic UUIDs and the
register map are carried over from Velit-branded units on the same
Kingtec-ish platform, not yet confirmed against an OutEquip-branded BLE
module by direct capture. Confirm handle/UUID discovery logs on first run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "ext" / "velib_python"))

# Must happen before any D-Bus connection is created (vedbus creates one on
# import-time construction of VeDbusService below) -- python-dbus requires
# a main loop registered up front even for a driver like this one that
# never actually calls mainloop.run() itself, since the polling loop below
# provides its own scheduling via time.sleep(). Every other Venus OS driver
# using velib_python does this; missing it fails with:
#   RuntimeError: ...D-Bus connections must be attached to a main loop...
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
DBusGMainLoop(set_as_default=True)

from vedbus import VeDbusService  # noqa: E402  (Venus OS provided library)

import ac_protocol as proto  # noqa: E402
from ble_client import AcBleClient, RESPONSE_TIMEOUT_S  # noqa: E402

logger = logging.getLogger("VeOutEquipAC")

POLL_INTERVAL_S = 6.0  # matches the stock app's ~6s cycle
RECONNECT_BACKOFF_S = 5.0


class OutEquipAcDriver:
    def __init__(self, mac_address: str, device_instance: int):
        self.mac_address = mac_address
        self._rx_buf = bytearray()
        self._ble = AcBleClient(mac_address, on_frame_bytes=self._on_bytes)
        self._service = self._build_service(device_instance)
        self._last_poll_index = 0
        self._active_handshake_event = threading.Event()
        self._active_handshake_value: Optional[int] = None

    # -- D-Bus service setup -------------------------------------------------

    def _build_service(self, device_instance: int) -> VeDbusService:
        svc_name = f"com.victronenergy.switch.outequipac_{device_instance:02d}"
        service = VeDbusService(svc_name, register=False)

        service.add_path("/Mgmt/ProcessName", os.path.basename(__file__))
        service.add_path("/Mgmt/ProcessVersion", "0.1.0")
        service.add_path("/Mgmt/Connection", f"BLE {self.mac_address}")
        service.add_path("/DeviceInstance", device_instance)
        service.add_path("/ProductId", 0xFFFF)  # placeholder: no Victron PID for this
        service.add_path("/ProductName", "OutEquipPro AC (BLE)")
        service.add_path("/FirmwareVersion", None)
        service.add_path("/Connected", 0)
        service.add_path("/CustomName", "OutEquip AC", writeable=True)

        # The one control the stock switch panel understands: on/off.
        service.add_path(
            "/SwitchableOutput/output_1/Name", "AC Power"
        )
        service.add_path(
            "/SwitchableOutput/output_1/Settings/Type", 1
        )  # 1 = toggle, per Cerbo GX relay convention
        service.add_path(
            "/SwitchableOutput/output_1/State",
            0,
            writeable=True,
            onchangecallback=self._on_power_write,
        )
        service.add_path("/SwitchableOutput/output_1/Status", 0)

        # Everything else: informational + settable where useful, grouped
        # under /Ac/ since there's no matching standard Victron path set.
        service.add_path("/Ac/Mode", None, writeable=True, onchangecallback=self._on_mode_write)
        service.add_path("/Ac/ModeName", None)
        service.add_path("/Ac/FanSpeed", None, writeable=True, onchangecallback=self._on_fan_write)
        service.add_path(
            "/Ac/SetpointTemperature", None, writeable=True,
            onchangecallback=self._on_setpoint_write,
        )
        service.add_path("/Ac/Swing", None, writeable=True, onchangecallback=self._on_swing_write)
        service.add_path("/Ac/IntakeTemperature", None)
        service.add_path("/Ac/OutletTemperature", None)
        service.add_path("/Ac/SupplyVoltage", None)
        service.add_path("/Ac/LastUpdate", None)

        service.register()
        return service

    # -- BLE lifecycle --------------------------------------------------------

    def connect_and_run_forever(self) -> None:
        while True:
            try:
                self._ble.connect()
                self._do_active_handshake()
                self._service["/Connected"] = 1
                self._run_poll_loop()
            except Exception:  # noqa: BLE001 - top-level supervisor loop
                logger.exception("BLE session failed, will reconnect")
                self._service["/Connected"] = 0
                self._ble.disconnect()
                time.sleep(RECONNECT_BACKOFF_S)

    def _do_active_handshake(self) -> None:
        """
        Per protocol.md's Initialization section: query key 66 (Active)
        immediately on connect, and if the reply is 2, write 1 back,
        before querying anything else. Confirmed necessary against real
        hardware via dev-tools/ble_scan_test.py and the ESP32 test sketch
        -- without this, the module does not answer other queries either.
        """
        self._active_handshake_event.clear()
        self._active_handshake_value = None
        logger.info("Sending Active (66) handshake query")
        self._ble.send(proto.make_query(proto.REG_ACTIVE))
        deadline = time.monotonic() + RESPONSE_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._ble.wait_for_notifications(0.5):
                if self._active_handshake_event.is_set():
                    break
        if not self._active_handshake_event.is_set():
            logger.warning("No reply to Active handshake within timeout; proceeding anyway")
            return
        logger.info("Active handshake replied with value=%s", self._active_handshake_value)
        if self._active_handshake_value == 2:
            logger.info("Active==2, writing Active=1 per protocol.md")
            self._ble.send(proto.make_write(proto.REG_ACTIVE, 1))
            time.sleep(0.5)

    def _run_poll_loop(self) -> None:
        registers = proto.DEFAULT_POLL_REGISTERS
        while self._ble.connected:
            reg = registers[self._last_poll_index % len(registers)]
            self._last_poll_index += 1
            self._ble.send(proto.make_query(reg))
            # Pump notifications until either a frame arrives or we time out;
            # the protocol guarantees at most one outstanding command.
            deadline = time.monotonic() + RESPONSE_TIMEOUT_S
            while time.monotonic() < deadline:
                if self._ble.wait_for_notifications(0.5):
                    break
            time.sleep(max(0.0, POLL_INTERVAL_S / len(registers) - 0.4))

    # -- Frame handling --------------------------------------------------------

    def _on_bytes(self, chunk: bytes) -> None:
        self._rx_buf.extend(chunk)
        while True:
            try:
                frame = proto.try_decode(self._rx_buf)
            except (proto.ChecksumError, proto.FrameError):
                logger.warning("dropping malformed frame data: %r", chunk)
                self._rx_buf.clear()
                return
            if frame is None:
                return
            self._apply_frame(frame)

    def _apply_frame(self, frame: proto.Frame) -> None:
        reg, val = frame.register, frame.value
        with self._service as s:
            if reg == proto.REG_ACTIVE:
                self._active_handshake_value = val
                self._active_handshake_event.set()
            elif reg == proto.REG_POWER:
                s["/SwitchableOutput/output_1/State"] = 1 if val == proto.ON_OFF_ON else 0
                s["/SwitchableOutput/output_1/Status"] = 1 if val == proto.ON_OFF_ON else 0
            elif reg == proto.REG_MODE:
                s["/Ac/Mode"] = val
                s["/Ac/ModeName"] = proto.MODE_NAMES.get(val, f"Unknown ({val})")
            elif reg == proto.REG_SETPOINT:
                s["/Ac/SetpointTemperature"] = val
            elif reg == proto.REG_FAN_SPEED:
                s["/Ac/FanSpeed"] = val
            elif reg == proto.REG_SWING:
                s["/Ac/Swing"] = 1 if val == proto.ON_OFF_ON else 0
            elif reg == proto.REG_INTAKE_TEMP:
                s["/Ac/IntakeTemperature"] = _to_signed_byte(val)
            elif reg == proto.REG_OUTLET_TEMP:
                s["/Ac/OutletTemperature"] = _to_signed_byte(val)
            elif reg == proto.REG_VOLTAGE:
                # Endianness for reg 18 is unconfirmed for this protocol
                # revision (see PROTOCOL notes) -- decivolts either way;
                # sanity-check against a plausible 12V/24V system range and
                # flip if garbage. Left explicit rather than "clever".
                decivolts = val
                volts = decivolts / 10.0
                if not (5.0 <= volts <= 60.0):
                    swapped = int.from_bytes(
                        decivolts.to_bytes(2, "big"), "little"
                    )
                    volts = swapped / 10.0
                s["/Ac/SupplyVoltage"] = round(volts, 1)
            # REG_AMPERAGE intentionally ignored: documented as always 0.
            s["/Ac/LastUpdate"] = int(time.time())

    # -- Write handlers (dbus -> device) ---------------------------------------

    def _on_power_write(self, path, value):
        reg_value = proto.ON_OFF_ON if value else proto.ON_OFF_OFF
        if not value:
            # Firmware quirk: always switch to Cooling before powering off,
            # or heat mode phantom-engages every few minutes while "off".
            self._ble.send(proto.make_write(proto.REG_MODE, proto.MODE_COOL))
        self._ble.send(proto.make_write(proto.REG_POWER, reg_value))
        return True

    def _on_mode_write(self, path, value):
        self._ble.send(proto.make_write(proto.REG_MODE, int(value)))
        return True

    def _on_fan_write(self, path, value):
        speed = max(1, min(5, int(value)))
        self._ble.send(proto.make_write(proto.REG_FAN_SPEED, speed))
        return True

    def _on_setpoint_write(self, path, value):
        self._ble.send(proto.make_write(proto.REG_SETPOINT, int(value)))
        return True

    def _on_swing_write(self, path, value):
        reg_value = proto.ON_OFF_ON if value else proto.ON_OFF_OFF
        self._ble.send(proto.make_write(proto.REG_SWING, reg_value))
        return True


def _to_signed_byte(val: int) -> int:
    val &= 0xFF
    return val - 256 if val >= 128 else val


def main() -> None:
    parser = argparse.ArgumentParser(description="Venus OS driver for an OutEquipPro AC over BLE")
    parser.add_argument("--mac", required=True, help="BLE MAC address of the AC's KT module")
    parser.add_argument("--device-instance", type=int, default=100)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    driver = OutEquipAcDriver(args.mac, args.device_instance)
    driver.connect_and_run_forever()


if __name__ == "__main__":
    main()
