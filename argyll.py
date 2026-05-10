#!/usr/bin/env -S uv run --script

import argparse
import asyncio
import datetime
import importlib.util
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import termios
import threading
import tty
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import colour

SAMPLE_SCRIPT = Path(__file__).with_name("hpcs-sample.py")
XYZ = tuple[float, float, float]
Sample = tuple[dict[str, float], list[float]]


class SampleModule(Protocol):
    async def capture_processed_sample(self) -> Sample | bool: ...
    def device_session(self) -> AbstractAsyncContextManager[object]: ...
    def instrument_name(self) -> str: ...


@dataclass
class ServerContext:
    loop: asyncio.AbstractEventLoop
    sample_mod: SampleModule
    lock: asyncio.Lock
    ready_event: asyncio.Event
    device_task: asyncio.Task[None]
    prompt_done: threading.Event
    request_fifo: Path
    response_fifo: Path
    stop_event: threading.Event
    errors: queue.SimpleQueue[BaseException]
    child_proc: subprocess.Popen[bytes] | None = None


@dataclass(frozen=True)
class Measurement:
    sample: Sample
    xyz: XYZ
    instrument_name: str


def load_sample_module() -> SampleModule:
    spec = importlib.util.spec_from_file_location("hpcs_sample", SAMPLE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {SAMPLE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(SampleModule, module)


def spectrum_to_xyz_absolute(
    spectrum_uw_cm2_nm: tuple[float, ...],
    start_nm: int,
    end_nm: int,
) -> XYZ:
    expected_bands = end_nm - start_nm + 1
    if len(spectrum_uw_cm2_nm) != expected_bands:
        raise ValueError(
            f"Expected {expected_bands} spectral bands for {start_nm}-{end_nm}nm, "
            f"got {len(spectrum_uw_cm2_nm)}"
        )

    spectral_data = {
        start_nm + offset: value * 1e-3
        for offset, value in enumerate(spectrum_to_argyll_emission(spectrum_uw_cm2_nm))
    }
    sd = colour.SpectralDistribution(spectral_data, name="HPCS sample")
    xyz = colour.sd_to_XYZ(sd, k=683, method="Integration")
    return tuple(float(component) for component in xyz)


def xyz_from_sample(sample: Sample) -> XYZ:
    start_nm, spectrum = extract_sample_spectrum(sample)
    end_nm = start_nm + len(spectrum) - 1
    return spectrum_to_xyz_absolute(spectrum, start_nm, end_nm)


def extract_sample_spectrum(sample: Sample) -> tuple[int, tuple[float, ...]]:
    fields, spectrum = sample
    start_nm = int(fields["StartTestWave"])
    end_nm = int(fields["EndTestWave"])
    used_spectrum = tuple(float(value) for value in spectrum[: end_nm - start_nm + 1])
    expected_bands = end_nm - start_nm + 1
    if len(used_spectrum) != expected_bands:
        raise ValueError(
            f"Expected {expected_bands} spectral bands for {start_nm}-{end_nm}nm, "
            f"got {len(used_spectrum)}"
        )
    return start_nm, used_spectrum


def spectrum_to_argyll_emission(spectrum_uw_cm2_nm: tuple[float, ...]) -> tuple[float, ...]:
    # Convert HPCS spectral values from uW/(cm^2*nm) to Argyll .sp emission
    # units of mW/(m^2*nm). That unit change is a straight 10x scale factor.
    return tuple(value * 10.0 for value in spectrum_uw_cm2_nm)


def format_sp(sample: Sample) -> str:
    start_nm, spectrum = extract_sample_spectrum(sample)
    end_nm = start_nm + len(spectrum) - 1
    field_names = " ".join(f"SPEC_{wavelength}" for wavelength in range(start_nm, end_nm + 1))
    values = " ".join(f"{value:.7f}" for value in spectrum_to_argyll_emission(spectrum))
    return "\n".join(
        (
            "SPECT",
            'DESCRIPTOR "Argyll Spectral power/reflectance information"',
            'ORIGINATOR "py-hpcs argyll.py"',
            f'CREATED "{datetime.datetime.now().astimezone().isoformat()}"',
            f'SPECTRAL_BANDS "{len(spectrum)}"',
            f'SPECTRAL_START_NM "{float(start_nm):.6f}"',
            f'SPECTRAL_END_NM "{float(end_nm):.6f}"',
            'SPECTRAL_NORM "1.000000"',
            "Emission",
            f"NUMBER_OF_FIELDS {len(spectrum) + 1}",
            "BEGIN_DATA_FORMAT",
            f"SampleID {field_names}",
            "END_DATA_FORMAT",
            "NUMBER_OF_SETS 1",
            "BEGIN_DATA",
            f"1 {values}",
            "END_DATA",
            "",
        )
    )


def write_sp_file(command_path: str, sample: Sample) -> None:
    Path(f"{command_path}.sp").write_text(format_sp(sample), encoding="ascii")


def write_name_file(command_path: str, instrument_name: str) -> None:
    Path(f"{command_path}.name").write_text(instrument_name, encoding="ascii")


async def capture_measurement(sample_mod: SampleModule) -> Measurement:
    while True:
        sample = await sample_mod.capture_processed_sample()
        if sample is not False:
            return Measurement(
                sample=sample,
                xyz=xyz_from_sample(sample),
                instrument_name=sample_mod.instrument_name(),
            )


def format_meas(xyz: XYZ) -> str:
    return f"{xyz[0]:.6f} {xyz[1]:.6f} {xyz[2]:.6f}\n"


def build_shell_script(request_fifo: Path, response_fifo: Path) -> str:
    return f"""#!/bin/sh
set -eu
request_fifo={shlex.quote(str(request_fifo))}
response_fifo={shlex.quote(str(response_fifo))}
meas_file="$0.meas"

printf 'measure\\t%s\\n' "$0" > "$request_fifo"
IFS= read -r measurement < "$response_fifo" || {{
    echo "failed to read measurement" >&2
    exit 1
}}
printf '%s\\n' "$measurement" > "$meas_file"
"""


async def measure_once(sample_mod: SampleModule, lock: asyncio.Lock) -> Measurement:
    async with lock:
        return await capture_measurement(sample_mod)


def prompt_before_first_sample() -> None:
    prompt = (
        "Place instrument on test window.\n"
        "Hit Esc or Q to give up, any other key to continue: "
    )
    print(prompt, end="", file=sys.stderr, flush=True)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = sys.stdin.buffer.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    print(file=sys.stderr, flush=True)
    if char in (b"\x1b", b"q", b"Q"):
        raise KeyboardInterrupt("Measurement aborted by user")


async def wait_for_device_ready(
    ready_event: asyncio.Event,
    device_task: asyncio.Task[None],
) -> None:
    if ready_event.is_set():
        if device_task.done():
            await device_task
        return

    ready_wait = asyncio.create_task(ready_event.wait())
    done, _ = await asyncio.wait({ready_wait, device_task}, return_when=asyncio.FIRST_COMPLETED)
    ready_wait.cancel()
    try:
        await ready_wait
    except asyncio.CancelledError:
        pass
    if device_task in done:
        await device_task


async def device_session_task(
    sample_mod: SampleModule,
    ready_event: asyncio.Event,
    stop_event: threading.Event,
) -> None:
    async with sample_mod.device_session():
        ready_event.set()
        await asyncio.to_thread(stop_event.wait)


def parse_request(line: str) -> tuple[str, str | None]:
    command, separator, payload = line.rstrip("\n").partition("\t")
    if not separator:
        return command, None
    return command, payload


def handle_measure_request(ctx: ServerContext, command_path: str | None) -> None:
    future = asyncio.run_coroutine_threadsafe(
        wait_for_device_ready(ctx.ready_event, ctx.device_task),
        ctx.loop,
    )
    future.result()
    if not ctx.prompt_done.is_set():
        prompt_before_first_sample()
        ctx.prompt_done.set()

    future = asyncio.run_coroutine_threadsafe(
        measure_once(ctx.sample_mod, ctx.lock),
        ctx.loop,
    )
    measurement = future.result()
    if command_path:
        write_sp_file(command_path, measurement.sample)
        write_name_file(command_path, measurement.instrument_name)
    with ctx.response_fifo.open("w", encoding="ascii") as response_stream:
        response_stream.write(format_meas(measurement.xyz))
        response_stream.flush()


def fifo_server_main(ctx: ServerContext) -> None:
    while not ctx.stop_event.is_set():
        try:
            with ctx.request_fifo.open("r", encoding="ascii") as request_stream:
                for line in request_stream:
                    if ctx.stop_event.is_set():
                        return
                    command, payload = parse_request(line)
                    if command != "measure":
                        continue
                    handle_measure_request(ctx, payload)
        except BaseException as exc:
            ctx.errors.put(exc)
            ctx.stop_event.set()
            if ctx.child_proc is not None:
                ctx.child_proc.terminate()
            return


def wake_fifo_reader(request_fifo: Path) -> None:
    try:
        with os.fdopen(os.open(request_fifo, os.O_WRONLY | os.O_NONBLOCK),
                       "w", encoding="ascii") as request_stream:
            request_stream.write("\n")
            request_stream.flush()
    except OSError:
        return


def take_first_error(errors: queue.SimpleQueue[BaseException]) -> BaseException | None:
    try:
        return errors.get_nowait()
    except queue.Empty:
        return None


async def run_argyll_server(request_fifo: Path, response_fifo: Path, command: list[str]) -> int:
    sample_mod = load_sample_module()
    ready_event = asyncio.Event()
    stop_event = threading.Event()
    context = ServerContext(
        loop=asyncio.get_running_loop(),
        sample_mod=sample_mod,
        lock=asyncio.Lock(),
        ready_event=ready_event,
        device_task=asyncio.create_task(device_session_task(sample_mod, ready_event, stop_event)),
        prompt_done=threading.Event(),
        request_fifo=request_fifo,
        response_fifo=response_fifo,
        stop_event=stop_event,
        errors=queue.SimpleQueue(),
    )

    server_thread = threading.Thread(
        target=fifo_server_main,
        args=(context,),
        daemon=True,
    )
    server_thread.start()
    return_code: int | None = None
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(command)
        context.child_proc = proc
        return_code = await asyncio.to_thread(proc.wait)
    finally:
        context.stop_event.set()
        wake_fifo_reader(request_fifo)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait)
        await asyncio.to_thread(server_thread.join, 1.0)
        if not context.device_task.done():
            context.device_task.cancel()
        try:
            await context.device_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if return_code in (None, 0):
                context.errors.put(exc)

    error = take_first_error(context.errors)
    if error is not None:
        raise error
    if return_code is None:
        raise RuntimeError("Argyll command did not produce an exit code")
    return return_code


def parse_wrapper_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wrap an Argyll command to measure with HPCS-310 (using -M)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Argyll command to run, for example: dispread",
    )
    return parser.parse_args(argv)


async def wrapper_main(argv: list[str]) -> int:
    args = parse_wrapper_args(argv)
    if not args.command:
        print("No Argyll command provided", file=sys.stderr)
        return 2
    if "-M" in args.command:
        print("Do not pass -M explicitly; argyll.py manages it", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="argyll-hpcs-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        request_fifo = tmp_path / "hpcs-request.fifo"
        response_fifo = tmp_path / "hpcs-response.fifo"
        shell_path = tmp_path / "hpcs.sh"
        os.mkfifo(request_fifo)
        os.mkfifo(response_fifo)
        shell_path.write_text(build_shell_script(request_fifo, response_fifo), encoding="ascii")
        shell_path.chmod(0o755)

        command = [args.command[0], "-M", str(shell_path), *args.command[1:]]
        print(f"Running: {' '.join(shlex.quote(part) for part in command)}", file=sys.stderr)
        return await run_argyll_server(request_fifo, response_fifo, command)


def main() -> int:
    return asyncio.run(wrapper_main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
