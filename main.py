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


class SM:
    Idle = auto()
    GetDeviceInfo = auto()
    StartSamping = auto()
    ReadSampleState = auto()
    ReadTestResult = auto()
    Stop = auto()


class State:
    loop: asyncio.AbstractEventLoop
    client: bleak.BleakClient
    state_machine: SM
    device_info: DeviceInfo
    response: bytearray
    response_length: int
    response_future: asyncio.Future


s = State()


async def notify_handler(sender: bleak.BleakGATTCharacteristic, data: bytearray):
    match s.state_machine:

        case SM.GetDeviceInfo:
            assert data[:2] == bytes.fromhex("8CEE")
            i = DeviceInfo()
            i.version = int.from_bytes(data[16:18], "little")
            i.name = data[2:12].decode("ascii", errors="ignore").rstrip("\x00")
            i.serial = int.from_bytes(data[12:16], "little")
            i.battery = int(data[18])
            i.status = int(data[19])
            s.device_info = i
            s.state_machine = SM.Idle
            s.response_future.set_result(True)

        case SM.StartSamping:
            assert data[:2] == bytes.fromhex("8C0E")
            s.state_machine = SM.Idle
            s.response_future.set_result(True)

        case SM.ReadSampleState:
            assert data[:2] == bytes.fromhex("8C03")
            assert len(data) >= 9
            if data[3] != 1:
                await asyncio.sleep(0.1)
                await send_command(SM.ReadSampleState, "8C03")
            else:
                s.state_machine = SM.Idle
                s.response_future.set_result(True)

        case SM.ReadTestResult:
            if not s.response:
                assert data[:2] == bytes.fromhex("8C13")
                s.response = data.copy()
                s.response_length = int.from_bytes(data[2:4], "big") + 4
            else:
                s.response += data
            if len(s.response) >= s.response_length:
                whole_response = s.response.copy()
                s.response.clear()
                s.response_length = 0
                s.state_machine = SM.Idle
                s.response_future.set_result(whole_response)

        case SM.Stop:
            s.state_machine = SM.Idle
            s.response_future.set_result(True)

        case _:
            assert False



async def send_command(sm: SM, hex: str):
    s.state_machine = sm
    s.response_future = s.loop.create_future()
    await s.client.write_gatt_char(CHAR_UUID, bytes.fromhex(hex), response=False)



def process_result(data: bytearray):
    field_names = "fLx fEfc cct duv x y u v u2 v2 fSDCM Ra R1 R2 R3 R4 R5 R6 R7 R8 R9 R10 R11 R12 R13 R14 R15 fSP DominantWave Purity HalfWidth PeakWave CentreWave CentroidWave fRratio fGratio fBratio fEML fEeml fEmlRatio fEDI_lx IntegTime0 VPeak VDark VDarkDAC"
    field_list = [name.strip() for name in field_names.split(' ') if name.strip()]

    offset = 40

    field_count = len(field_list)
    field_bytes = 4 * field_count
    field_values = struct.unpack(f"<{field_count}f", data[offset:offset+field_bytes])
    processed = dict(zip(field_list, field_values))

    offset += field_bytes
    offset += 20

    spectrum_length = 671
    spectrum_bytes = 4 * spectrum_length
    spectrum_chunk = data[offset:offset+spectrum_bytes]
    spectrum = list(struct.unpack(f"<{spectrum_length}f", spectrum_chunk))

    start_test_wave, end_test_wave = struct.unpack("<2f", data[-8:])
    processed["StartTestWave"] = start_test_wave
    processed["EndTestWave"] = end_test_wave

    processed["spectrum"] = [v * 0.1 for v in spectrum]

    assert offset + spectrum_bytes == len(data) - 8

    return processed


async def main():
    s.loop = asyncio.get_event_loop()
    s.response = bytearray()

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

        s.state_machine = SM.Idle
        await client.start_notify(CHAR_UUID, notify_handler)

        await asyncio.sleep(2)

        await send_command(SM.GetDeviceInfo, "8CEE")
        await asyncio.wait_for(s.response_future, 1)
        print(s.device_info, file=sys.stderr)

        await asyncio.sleep(0.1)
        await send_command(SM.StartSamping, "8C0E01")
        await asyncio.wait_for(s.response_future, 1)

        await asyncio.sleep(2)
        await send_command(SM.ReadSampleState, "8C03")
        await asyncio.wait_for(s.response_future, 60)

        await send_command(SM.ReadTestResult, "8C1331")
        result = await asyncio.wait_for(s.response_future, 10)
        procesed  = process_result(result)
        pprint(procesed, sort_dicts=False)

        await send_command(SM.Stop, "8C25")
        await asyncio.wait_for(s.response_future, 1)


if __name__ == "__main__":
    asyncio.run(main())
