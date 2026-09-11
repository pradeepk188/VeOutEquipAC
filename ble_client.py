"""
BLE transport for the OutEquipPro AC's built-in Bluetooth module.

Uses bluepy, matching the pattern already in use for the SOK battery
dbus-btbattery fork on this same Venus OS Pi -- one BLE stack to depend on,
one set of quirks to know.

Service/characteristic UUIDs (from bobbaboui/outequip-ha PROTOCOL.md,
cross-referenced against dwestcott/velit-ble -- NOT yet independently
confirmed against an OutEquip-branded module; see NOTES.md):

    Service:              0000FFE0-0000-1000-8000-00805F9B34FB
    Notify (device->us):  0000FFE1-0000-1000-8000-00805F9B34FB
    Write   (us->device): 0000FFE2-0000-1000-8000-00805F9B34FB

This is the standard HM-10 / JDY-class BLE-UART bridge shape. Access is
exclusive -- only one central (this driver, or the phone app) can hold the
connection at a time.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from bluepy import btle  # same dependency already used by dbus-btbattery

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"

MIN_COMMAND_SPACING_S = 0.4  # protocol requires >=400ms between commands
RESPONSE_TIMEOUT_S = 3.0
CONNECT_TIMEOUT_S = 10.0

logger = logging.getLogger(__name__)


class AcBleDelegate(btle.DefaultDelegate):
    def __init__(self, on_data: Callable[[bytes], None]):
        super().__init__()
        self._on_data = on_data

    def handleNotification(self, cHandle, data):  # noqa: N802 (bluepy API)
        self._on_data(bytes(data))


class AcBleClient:
    """
    Thread-owning BLE connection to one AC module.

    Not thread-safe for concurrent callers -- the driver should own one
    instance per AC and serialize all send()/poll_loop() access through it,
    which matches the protocol's own "one outstanding command at a time"
    requirement.
    """

    def __init__(self, mac_address: str, on_frame_bytes: Callable[[bytes], None]):
        self.mac_address = mac_address
        self._on_frame_bytes = on_frame_bytes
        self._peripheral: Optional[btle.Peripheral] = None
        self._write_char = None
        self._lock = threading.Lock()
        self._last_send_time = 0.0
        self.connected = False

    def connect(self) -> None:
        logger.info("Connecting to AC %s", self.mac_address)
        self._peripheral = btle.Peripheral(
            self.mac_address, addrType=btle.ADDR_TYPE_PUBLIC
        )
        self._peripheral.setDelegate(AcBleDelegate(self._on_frame_bytes))
        service = self._peripheral.getServiceByUUID(SERVICE_UUID)
        notify_char = service.getCharacteristics(NOTIFY_CHAR_UUID)[0]
        write_chars = service.getCharacteristics(WRITE_CHAR_UUID)
        self._write_char = write_chars[0]

        # Enable notifications via the standard CCCD (0x2902), same as any
        # other BLE-UART bridge.
        handle = notify_char.getHandle() + 1
        self._peripheral.writeCharacteristic(handle, b"\x01\x00", withResponse=True)

        self.connected = True
        logger.info("Connected to AC %s", self.mac_address)

    def disconnect(self) -> None:
        self.connected = False
        if self._peripheral is not None:
            try:
                self._peripheral.disconnect()
            except btle.BTLEException:
                pass
            self._peripheral = None

    def send(self, frame_bytes: bytes) -> None:
        """Write a frame, respecting the minimum inter-command spacing."""
        if not self.connected or self._write_char is None:
            raise RuntimeError("not connected")
        with self._lock:
            elapsed = time.monotonic() - self._last_send_time
            if elapsed < MIN_COMMAND_SPACING_S:
                time.sleep(MIN_COMMAND_SPACING_S - elapsed)
            self._write_char.write(frame_bytes, withResponse=False)
            self._last_send_time = time.monotonic()

    def wait_for_notifications(self, timeout: float) -> bool:
        """
        Pump the bluepy event loop so queued notifications get delivered to
        the delegate. Returns True if something was processed.
        """
        if self._peripheral is None:
            return False
        return self._peripheral.waitForNotifications(timeout)
