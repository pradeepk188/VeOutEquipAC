"""
Frame codec for the OutEquipPro / Kingtec / Velit air-conditioner protocol.

Sources (cross-checked, not independently captured against this exact unit yet):
  - gongloo/OutEquipAC protocol.md   -> register semantics (wired UART, OutEquip-specific)
  - bobbaboui/outequip-ha PROTOCOL.md -> BLE transport (FFE0/FFE1/FFE2), framing, timing

Frame layout (8 bytes fixed + payload):
    5A 5A | LEN | DEV | REG | VAL | CHK | 0D 0A
    preamble(2) len(1) devtype(1) reg(1) val(1) checksum(1) postamble(2)

- LEN = bytes remaining after the length byte itself (dev + reg + val + chk = 4,
  so LEN is always 0x04 for this 1-byte-value protocol... but keep it general
  in case a register ever carries a wider value).
- DEV = 0x01 for air conditioners.
- CHK = 8-bit additive sum of every preceding byte (preamble..val), masked to 0xFF.
  No CRC, no auth -- trivial to forge, so no attempt is made to hide that here.

Reading a register: send it with value 0.
Writing a register: send it with the desired value; the board is documented to
reply with the *last queried* register's state, not necessarily the one just
written (firmware bug) -- callers should re-query rather than trust the
immediate reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

PREAMBLE = b"\x5a\x5a"
POSTAMBLE = b"\x0d\x0a"
DEVICE_TYPE_AC = 0x01

# --- Registers -------------------------------------------------------------

REG_POWER = 1
REG_MODE = 2
REG_SETPOINT = 3
REG_FAN_SPEED = 4
REG_UNDERVOLT_PROTECT = 5
REG_OVERVOLT_PROTECT = 6
REG_INTAKE_TEMP = 7
REG_OUTLET_TEMP = 8
REG_LCD = 10
REG_SWING = 16
REG_VOLTAGE = 18
REG_AMPERAGE = 19  # documented as always 0 on OutEquip boards
REG_LIGHT = 28
REG_ACTIVE = 66  # BLE handshake key, see Initialization notes

# Registers the stock app polls on its ~6s cycle. Good default poll list.
DEFAULT_POLL_REGISTERS = (
    REG_POWER,
    REG_MODE,
    REG_SETPOINT,
    REG_FAN_SPEED,
    REG_INTAKE_TEMP,
    REG_OUTLET_TEMP,
    REG_SWING,
    REG_VOLTAGE,
)

ON_OFF_OFF = 1
ON_OFF_ON = 2

MODE_COOL = 1
MODE_HEAT = 2
MODE_FAN = 3
MODE_ECO_COOL = 4
MODE_SLEEP_COOL = 5
MODE_TURBO_COOL = 6
MODE_WET = 7  # aka "Dehumidify" per velit-ble; unconfirmed on OutEquip

MODE_NAMES = {
    MODE_COOL: "Cool",
    MODE_HEAT: "Heat",
    MODE_FAN: "Fan",
    MODE_ECO_COOL: "Eco Cool",
    MODE_SLEEP_COOL: "Sleep Cool",
    MODE_TURBO_COOL: "Turbo Cool",
    MODE_WET: "Wet/Dehumidify",
}

# Registers that are inverted or otherwise unreliable -- see PROTOCOL notes.
# LCD: while unit is active, 1=Off, 0=On (opposite of every other on/off reg).
# Light: reads back ~always 1 regardless of actual state; treat write-only.
LCD_ACTIVE_OFF = 1
LCD_ACTIVE_ON = 0
LIGHT_ON = 1
LIGHT_OFF = 2


class ChecksumError(ValueError):
    pass


class FrameError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    device_type: int
    register: int
    value: int  # currently always a single byte (0-255); see note below

    def encode(self) -> bytes:
        # NOTE: every register documented so far uses a 1-byte value. If a
        # register needing a wider value ever turns up, extend `value` to
        # bytes and adjust `length` accordingly -- the framing already
        # supports variable-length values via the length byte.
        body = bytes([self.device_type, self.register, self.value & 0xFF])
        length = len(body) + 1  # +1 for the checksum byte itself is NOT
        # included per the spec ("Length: bytes remaining in this packet"
        # counts dev+reg+val+checksum = 4). Keep explicit rather than clever:
        length = len(body) + 1
        header = PREAMBLE + bytes([length])
        payload = header + body
        checksum = sum(payload) & 0xFF
        return payload + bytes([checksum]) + POSTAMBLE

    @staticmethod
    def decode(raw: bytes) -> "Frame":
        if len(raw) < 8:
            raise FrameError(f"frame too short: {raw!r}")
        if raw[0:2] != PREAMBLE:
            raise FrameError(f"bad preamble: {raw[0:2]!r}")
        length = raw[2]
        dev_type = raw[3]
        reg = raw[4]
        # value occupies bytes [5 : 5+value_len) where value_len = length - 3
        # (dev+reg+chk = 3 non-value bytes counted in the length byte)
        value_len = length - 3
        if value_len < 1:
            raise FrameError(f"implausible length byte {length} in {raw!r}")
        value_bytes = raw[5:5 + value_len]
        checksum_index = 5 + value_len
        checksum = raw[checksum_index]
        postamble = raw[checksum_index + 1: checksum_index + 3]
        if postamble != POSTAMBLE:
            raise FrameError(f"bad postamble: {postamble!r} in {raw!r}")
        computed = sum(raw[0:checksum_index]) & 0xFF
        if computed != checksum:
            raise ChecksumError(
                f"checksum mismatch: got {checksum:#x}, computed {computed:#x} in {raw!r}"
            )
        # Collapse multi-byte values big-endian by default; caller can
        # reinterpret raw bytes for registers where endianness is unconfirmed
        # (e.g. register 18/19 -- see driver-side handling).
        value = int.from_bytes(value_bytes, byteorder="big", signed=False)
        return Frame(device_type=dev_type, register=reg, value=value)


def make_query(register: int, device_type: int = DEVICE_TYPE_AC) -> bytes:
    """Build a frame that queries `register` (value=0 means 'read')."""
    return Frame(device_type=device_type, register=register, value=0).encode()


def make_write(register: int, value: int, device_type: int = DEVICE_TYPE_AC) -> bytes:
    """Build a frame that writes `value` to `register`."""
    return Frame(device_type=device_type, register=register, value=value).encode()


def try_decode(buf: bytearray) -> Optional[Frame]:
    """
    Attempt to pull one complete frame out of a growing receive buffer.
    Returns the decoded Frame and mutates `buf` to remove the consumed bytes,
    or returns None if no complete frame is available yet.

    Callers should keep feeding bytes from BLE notifications into `buf`
    and call this after each chunk arrives.
    """
    start = buf.find(PREAMBLE)
    if start == -1:
        # No preamble at all -- drop garbage to avoid unbounded growth.
        if len(buf) > 64:
            del buf[:-2]  # keep last couple bytes in case preamble is split
        return None
    if start > 0:
        del buf[:start]
    if len(buf) < 4:
        return None
    length = buf[2]
    total_len = 3 + length + 2  # preamble+len byte(3) + (dev+reg+val+chk) + postamble(2)
    if len(buf) < total_len:
        return None
    raw = bytes(buf[:total_len])
    del buf[:total_len]
    return Frame.decode(raw)
