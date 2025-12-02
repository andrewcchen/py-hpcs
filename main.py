#!/usr/bin/env -S uv run --script

# /// script
# dependencies = [
#   "bleak==2.*",
# ]
# ///

import argparse
import collections
from dataclasses import dataclass
from enum import Enum, auto
from pprint import pprint
import asyncio
import struct
import sys

import bleak

CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


@dataclass
class DeviceInfo:
    version: int = 0
    name: str = ""
    serial: int = 0
    battery: int = 0
    status: int = 0


class SM(Enum):
    Idle = auto()
    GetDeviceInfo = auto()
    StartSamping = auto()
    ReadIntegTime = auto()
    ReadSampleState = auto()
    ReadTestResult = auto()
    Stop = auto()


class State:
    loop: asyncio.AbstractEventLoop
    client: bleak.BleakClient
    state_machine: SM
    device_info: DeviceInfo
    integration_time: float
    response_data: bytearray
    response_length: int
    response_future: asyncio.Future
    header_printed: bool
    def __init__(self):
        self.header_printed = False
s = State()


async def notify_handler(sender: bleak.BleakGATTCharacteristic, data: bytearray):
    match s.state_machine:

        case SM.GetDeviceInfo:
            assert data[:2] == bytes.fromhex("8CEE")
            info = DeviceInfo()
            info.version = int.from_bytes(data[16:18], "little")
            info.name = data[2:12].decode("ascii", errors="ignore").rstrip("\x00")
            info.serial = int.from_bytes(data[12:16], "little")
            info.battery = int(data[18])
            info.status = int(data[19])
            s.state_machine = SM.Idle
            s.response_future.set_result(info)

        case SM.StartSamping:
            assert data[:2] == bytes.fromhex("8C0E")
            s.state_machine = SM.Idle
            s.response_future.set_result(None)

        case SM.ReadIntegTime:
            assert data[:2] == bytes.fromhex("8C05")
            s.state_machine = SM.Idle
            time = int.from_bytes(data[2:6], "little") / 1e3
            s.response_future.set_result(time)

        case SM.ReadSampleState:
            assert data[:2] == bytes.fromhex("8C03")
            assert len(data) >= 9
            s.state_machine = SM.Idle
            s.response_future.set_result(data[3] == 1)

        case SM.ReadTestResult:
            if not s.response_data:
                assert data[:2] == bytes.fromhex("8C13")
                s.response_data = data.copy()
                s.response_length = int.from_bytes(data[2:4], "big") + 4
            else:
                s.response_data += data
            # print(f'got {len(data)} total {len(s.response_data)} expect {s.response_length}', file=sys.stderr)
            if len(s.response_data) >= s.response_length:
                whole_response = s.response_data.copy()
                s.response_data.clear()
                s.response_length = 0
                s.state_machine = SM.Idle
                s.response_future.set_result(whole_response)

        case SM.Stop:
            s.state_machine = SM.Idle
            s.response_future.set_result(None)

        case _:
            assert False



async def command(sm: SM, hex: str, timeout=1):
    s.state_machine = sm
    s.response_future = s.loop.create_future()
    await s.client.write_gatt_char(CHAR_UUID, bytes.fromhex(hex), response=False)
    return await asyncio.wait_for(s.response_future, timeout)


def process_result(data: bytearray) -> tuple[dict[str, float], list[float]]:
    name = s.device_info.name
    iver = s.device_info.version

    if (name.endswith('310') or name.endswith('330')) and iver > 2005:
        field_names = "fLx fEfc cct duv x y u v u2 v2 fSDCM Ra R1 R2 R3 R4 R5 R6 R7 R8 R9 R10 R11 R12 R13 R14 R15 fSP DominantWave Purity HalfWidth PeakWave CentreWave CentroidWave fRratio fGratio fBratio fEML fEeml fEmlRatio fEDI_lx IntegTime0 VPeak VDark VDarkDAC"
    else:
        raise RuntimeError("TODO add support for device")
    field_list = [name.strip() for name in field_names.split(' ') if name.strip()]

    offset = 40

    field_count = len(field_list)
    field_bytes = 4 * field_count
    field_values = struct.unpack(f"<{field_count}f", data[offset:offset+field_bytes])
    fields = collections.OrderedDict(zip(field_list, field_values))

    offset += field_bytes
    offset += 20

    spectrum_length = 671
    spectrum_bytes = 4 * spectrum_length
    spectrum_chunk = data[offset:offset+spectrum_bytes]
    spectrum = [v * 0.1 for v in struct.unpack(f"<{spectrum_length}f", spectrum_chunk)]

    start_test_wave, end_test_wave = struct.unpack("<2f", data[-8:])
    fields["StartTestWave"] = int(start_test_wave)
    fields["EndTestWave"] = int(end_test_wave)

    assert offset + spectrum_bytes == len(data) - 8

    return fields, spectrum


def print_result(processed: tuple[dict[str, float], list[float]]):
    fields, spectrum = processed
    if not s.header_printed:
        for name in fields:
            print(name, end=',')
        for i in range(fields["StartTestWave"], fields["EndTestWave"] + 1):
            print(i, end='nm,')
        print()
        s.header_printed = True
    for value in fields.values():
        print(value, end=',')
    for i in range(fields["EndTestWave"] - fields["StartTestWave"] + 1):
        print(spectrum[i], end=',')
    print()


async def capture_sample():
    await command(SM.StartSamping, "8C0E01")

    wait_time = s.integration_time + 500 if s.integration_time > 500 else 1000
    await asyncio.sleep(wait_time / 1e3)
    for _ in range(50):
        try:
            if await command(SM.ReadSampleState, "8C03", 0.1):
                break
            await asyncio.sleep(0.1)
        except TimeoutError:
            pass
    else:
        raise TimeoutError

    s.response_data.clear()
    try:
        raw_result = await command(SM.ReadTestResult, "8C1331", 3)
    except TimeoutError:
        print("Failed to read test result", file=sys.stderr)
        return False
    result = process_result(raw_result)
    print_result(result)

    s.integration_time = result[0]["IntegTime0"]
    return True


async def main(continuous: bool):
    s.loop = asyncio.get_event_loop()
    s.response_data = bytearray()

    print("Searching for HPCS devices", file=sys.stderr)
    device = await bleak.BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and d.name.startswith("HPCS")
    )
    if not device:
        print("No HPCS device found", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to {device.name}", file=sys.stderr)
    async with bleak.BleakClient(device) as client:
        s.client = client
        print(f"Connected to {device.name}", file=sys.stderr)
        await asyncio.sleep(1)

        s.state_machine = SM.Idle
        await client.start_notify(CHAR_UUID, notify_handler)
        await asyncio.sleep(1)

        s.device_info = await command(SM.GetDeviceInfo, "8CEE")
        print(s.device_info, file=sys.stderr)

        s.integration_time = await command(SM.ReadIntegTime, "8C05")
        # print(f'integration time estimate: {s.integration_time}')

        while True:
            got = await capture_sample()
            if got and not continuous:
                break

        await command(SM.Stop, "8C25")


def parse_args():
    parser = argparse.ArgumentParser(description="Capture spectrum from HPCS-310 and HPCS-330")
    parser.add_argument("-c", "--continuous", action="store_true", help="Continue sampling until interrupted")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.continuous))
