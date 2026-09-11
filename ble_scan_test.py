"""
ble_scan_test.py -- standalone Windows-side test tool for the OutEquipPro AC.

This is a DEV TOOL, not part of the Venus OS package: it doesn't get
installed by SetupHelper, and it uses `bleak` (cross-platform: Windows/
macOS/Linux) instead of `bluepy` (Linux/BlueZ only, what the real Pi driver
in ble_client.py uses). The goal here is just to confirm, from your Windows
machine's Bluetooth radio, whether the assumptions in ac_protocol.py and
ble_client.py actually hold against your real unit -- before trusting them
on the Pi.

Install:
    pip install bleak

Usage:
    # 1. Find the AC. Look for a name starting with KT, or containing
    #    OutEquip/Velit. Note its address.
    python ble_scan_test.py scan

    # 2. Confirm the GATT layout matches what ble_client.py assumes
    #    (service FFE0, notify char FFE1, write char FFE2).
    python ble_scan_test.py dump <ADDRESS>

    # 3. Send one query frame for a register and print whatever comes back,
    #    decoded through the real ac_protocol.py codec.
    python ble_scan_test.py query <ADDRESS> --register power
    python ble_scan_test.py query <ADDRESS> --register 7   # numeric also OK

`query` is a **read only** by design (value=0) -- it never sends a write
frame, so there's no risk of it changing the AC's actual state. Don't reuse
this for writes until `dump` has confirmed the characteristics you expect.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from bleak import BleakClient, BleakScanner

# Reuse the real codec so what this tool confirms is exactly what
# outequipac.py will use, not a reimplementation that could quietly diverge.
import ac_protocol as proto

EXPECTED_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
EXPECTED_NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
EXPECTED_WRITE_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"

# Standard GATT "Service Changed" characteristic. Not part of the AC
# protocol itself, but an ADB logcat capture of the real app connecting
# (see NOTES.md) showed it subscribing to this *before* FFE1, followed by
# a connection parameter update, then FFE1. Mirrored here since it's the
# one concrete behavioral difference that capture revealed -- cheap to
# try, unclear yet whether the module's firmware actually requires it or
# the app just does it as generic Android BLE hygiene.
SERVICE_CHANGED_UUID = "00002a05-0000-1000-8000-00805f9b34fb"

# Mirrors ble_client.RESPONSE_TIMEOUT_S. Duplicated rather than imported:
# ble_client.py imports bluepy (Linux/BlueZ only), which isn't installable
# on Windows, so this tool can't import that module at all. Keep this value
# in sync with ble_client.py by hand if that one ever changes.
QUERY_TIMEOUT_S = 3.0

REGISTER_NAMES = {
    "power": proto.REG_POWER,
    "mode": proto.REG_MODE,
    "setpoint": proto.REG_SETPOINT,
    "fan": proto.REG_FAN_SPEED,
    "intake_temp": proto.REG_INTAKE_TEMP,
    "outlet_temp": proto.REG_OUTLET_TEMP,
    "swing": proto.REG_SWING,
    "voltage": proto.REG_VOLTAGE,
}


async def cmd_scan(_args: argparse.Namespace) -> None:
    print("Scanning for 10s -- turn the AC on and make sure it's not already")
    print("connected to the phone app (BLE is exclusive: one central at a time).\n")
    devices = await BleakScanner.discover(timeout=10.0)
    if not devices:
        print("No BLE devices found at all. Check Windows Bluetooth is on and")
        print("the AC is powered and advertising.")
        return
    print(f"{'Address':<20} {'Name':<30} RSSI")
    for d in sorted(devices, key=lambda d: (d.name or "")):
        name = d.name or "(no name)"
        flag = ""
        if name.upper().startswith("KT") or "outequip" in name.lower() or "velit" in name.lower():
            flag = "  <-- looks like a candidate"
        print(f"{d.address:<20} {name:<30} {getattr(d, 'rssi', '?')}{flag}")


async def cmd_dump(args: argparse.Namespace) -> None:
    print(f"Connecting to {args.address} ...")
    async with BleakClient(args.address) as client:
        print(f"Connected: {client.is_connected}\n")
        found_service = False
        found_notify = False
        found_write = False
        for service in client.services:
            print(f"Service {service.uuid}")
            if service.uuid.lower() == EXPECTED_SERVICE_UUID:
                found_service = True
                print("  ^ matches EXPECTED_SERVICE_UUID (FFE0)")
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"  Characteristic {char.uuid}  [{props}]")
                if char.uuid.lower() == EXPECTED_NOTIFY_UUID:
                    found_notify = True
                    print("    ^ matches EXPECTED_NOTIFY_UUID (FFE1)")
                if char.uuid.lower() == EXPECTED_WRITE_UUID:
                    found_write = True
                    print("    ^ matches EXPECTED_WRITE_UUID (FFE2)")
                for desc in char.descriptors:
                    print(f"    Descriptor {desc.uuid}")
        print()
        print("=== Summary ===")
        print(f"Service FFE0 present: {found_service}")
        print(f"Notify  FFE1 present: {found_notify}")
        print(f"Write   FFE2 present: {found_write}")
        if found_service and found_notify and found_write:
            print("\nGATT layout matches ble_client.py's assumptions. Try `query` next.")
        else:
            print("\nDoes NOT match ble_client.py's assumptions -- ble_client.py's")
            print("SERVICE_UUID/NOTIFY_CHAR_UUID/WRITE_CHAR_UUID constants need to be")
            print("updated to whatever this dump actually shows before anything else")
            print("in this project is trustworthy.")


async def cmd_query(args: argparse.Namespace) -> None:
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    register = REGISTER_NAMES.get(args.register, None)
    if register is None:
        try:
            register = int(args.register)
        except ValueError:
            print(f"Unknown register {args.register!r}. Known names: {list(REGISTER_NAMES)}")
            sys.exit(1)

    write_response = args.with_response
    timeout = args.timeout
    settle_delay = args.settle_delay

    handshake_done = asyncio.Event()
    handshake_value = {}
    query_received = asyncio.Event()
    rx_buf = bytearray()
    decoded_frames = []

    def on_notify(_sender, data: bytearray) -> None:
        rx_buf.extend(bytes(data))
        print(f"  <- notification, {len(data)} bytes: {bytes(data).hex()}")
        while True:
            try:
                frame = proto.try_decode(rx_buf)
            except (proto.ChecksumError, proto.FrameError) as e:
                print(f"     decode error: {e}")
                rx_buf.clear()
                return
            if frame is None:
                return
            print(f"     decoded: register={frame.register} value={frame.value}")
            if frame.register == proto.REG_ACTIVE and not handshake_done.is_set():
                handshake_value["value"] = frame.value
                handshake_done.set()
            elif frame.register == register:
                decoded_frames.append(frame)
                query_received.set()

    print(f"Connecting to {args.address} ...")
    async with BleakClient(args.address) as client:
        # Windows' WinRT BLE backend occasionally fails the CCCD write that
        # enables notifications if it happens immediately after connect
        # (OSError -2147023673, "operation was canceled by the user").
        # Not protocol-related -- a brief settle delay + a couple of
        # retries clears it in practice. Only ever subscribe once per
        # connection (reused for both the handshake and the real query
        # below) to minimize how many chances there are to hit it.
        if settle_delay > 0:
            await asyncio.sleep(settle_delay)

        # Mirrors the real app: subscribe to Service Changed first (best
        # effort -- some servers reject this, harmless either way), then
        # give the module a moment before the FFE1 subscription that
        # actually matters, matching the ~80ms gap seen in the ADB capture.
        try:
            await client.start_notify(SERVICE_CHANGED_UUID, lambda *_: None)
            print("  subscribed to Service Changed (2A05), mirroring the real app")
            await asyncio.sleep(0.1)
        except Exception as e:  # noqa: BLE001 - best-effort, never fatal
            print(f"  Service Changed subscribe skipped ({e}) -- continuing")

        last_error = None
        for attempt in range(1, 4):
            try:
                await client.start_notify(EXPECTED_NOTIFY_UUID, on_notify)
                break
            except OSError as e:
                last_error = e
                print(f"  start_notify attempt {attempt} failed ({e}); retrying...")
                await asyncio.sleep(1.5)
        else:
            print(f"\nGiving up after 3 attempts: {last_error}")
            print("Check Windows Settings -> Bluetooth & devices: if this unit shows")
            print("as 'Paired', try removing it there (this protocol doesn't need")
            print("pairing) and also make sure the OutEquip phone app isn't connected.")
            return

        # Per protocol.md's Initialization section: the real app queries
        # key 66 (Active) immediately on connect, and if the reply is 2,
        # writes 1 back, before querying anything else. Whether this is
        # strictly required for the module to answer *other* queries is
        # undocumented, but the app's own query sequence never queries key
        # 1 (Power) directly either -- only 2/3/7/8/18/19 -- so doing this
        # handshake first is the closest match to real app behavior.
        print(f"-> handshake: querying Active (66) [response={write_response}]")
        await client.write_gatt_char(
            EXPECTED_WRITE_UUID, proto.make_query(proto.REG_ACTIVE), response=write_response
        )
        try:
            await asyncio.wait_for(handshake_done.wait(), timeout=timeout)
            print(f"   Active replied with value={handshake_value['value']}")
            if handshake_value["value"] == 2:
                print("-> handshake: Active==2, writing Active=1 per protocol.md")
                await client.write_gatt_char(
                    EXPECTED_WRITE_UUID, proto.make_write(proto.REG_ACTIVE, 1), response=write_response
                )
                await asyncio.sleep(0.5)
        except asyncio.TimeoutError:
            print("   no reply to Active handshake within timeout -- proceeding anyway")

        query_frame = proto.make_query(register)
        print(f"-> sending query for register {register}: {query_frame.hex()} [response={write_response}]")
        await client.write_gatt_char(EXPECTED_WRITE_UUID, query_frame, response=write_response)

        try:
            await asyncio.wait_for(query_received.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"\nNo response for register {register} within {timeout}s.")
            print("Either the write/notify characteristic UUIDs are wrong (run `dump`")
            print("again to double check), or this register isn't queried this way.")
            return

        await client.stop_notify(EXPECTED_NOTIFY_UUID)

    print(f"\nGot {len(decoded_frames)} frame(s) back. If register/value look sane,")
    print("ac_protocol.py's codec is confirmed working against real hardware.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="Discover nearby BLE devices").set_defaults(func=cmd_scan)

    p_dump = sub.add_parser("dump", help="List services/characteristics of one device")
    p_dump.add_argument("address", help="BLE address, e.g. AA:BB:CC:DD:EE:FF")
    p_dump.set_defaults(func=cmd_dump)

    p_query = sub.add_parser("query", help="Send one read-only query frame and print the decoded reply")
    p_query.add_argument("address", help="BLE address, e.g. AA:BB:CC:DD:EE:FF")
    p_query.add_argument(
        "--register", default="mode",
        help=(
            f"Register name ({', '.join(REGISTER_NAMES)}) or a numeric register id. "
            "Default 'mode' -- protocol.md's Initialization section shows the real "
            "app only ever querying keys 2/3/7/8/18/19 after the handshake, never "
            "key 1 (power) directly, so 'power' may just not respond to a query."
        ),
    )
    p_query.add_argument(
        "--with-response", action="store_true",
        help=(
            "Use a GATT Write Request (waits for a link-layer ACK) instead of "
            "Write Without Response. FFE2 advertises both properties; some modules "
            "silently drop Write Without Response despite advertising it. Worth "
            "trying if plain `query` gets zero response even to the handshake."
        ),
    )
    p_query.add_argument(
        "--timeout", type=float, default=QUERY_TIMEOUT_S,
        help=f"Seconds to wait for a notification before giving up (default {QUERY_TIMEOUT_S}).",
    )
    p_query.add_argument(
        "--settle-delay", type=float, default=1.0,
        help=(
            "Seconds to wait after connecting before subscribing to notifications "
            "(default 1.0, works around a flaky Windows WinRT CCCD-write error). "
            "Try 0 if the module disconnects you before start_notify even runs -- "
            "some cheap BLE-UART modules have a short patience window and drop "
            "the connection if nothing happens fast enough."
        ),
    )
    p_query.add_argument(
        "--debug", action="store_true",
        help="Enable bleak/BlueZ-backend verbose logging to see raw GATT operations.",
    )
    p_query.set_defaults(func=cmd_query)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
