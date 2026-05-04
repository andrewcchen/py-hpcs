#!/usr/bin/env python3

import argparse
import asyncio
import datetime
import importlib.util
import shutil
import subprocess
import types
import sys
from pathlib import Path


SAMPLE_COLORS = (
    ("W", "WHITE", "#ffffff"),
    ("R", "RED", "#ff0000"),
    ("G", "GREEN", "#00ff00"),
    ("B", "BLUE", "#0000ff"),
)
SPECTRAL_START_NM = 380
SPECTRAL_END_NM = 730
SAMPLE_SCRIPT = Path(__file__).with_name("hpcs-sample.py")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Capture W/R/G/B spectra with hpcs-sample.py and write an Argyll .ccss file."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(f"HPCS_{datetime.datetime.now():%Y%m%d_%H%M%S}.ccss"),
        help="Output .ccss path",
    )
    parser.add_argument(
        "--display",
        default="Unknown display",
        help="DISPLAY field to write into the .ccss metadata",
    )
    parser.add_argument(
        "--technology",
        default="Unknown",
        help="TECHNOLOGY field to write into the .ccss metadata",
    )
    parser.add_argument(
        "--descriptor",
        default="Not specified",
        help="DESCRIPTOR field to write into the .ccss metadata",
    )
    parser.add_argument(
        "--reference",
        default="HPCS-310",
        help="REFERENCE field to write into the .ccss metadata",
    )
    parser.add_argument(
        "--originator",
        default="py-hpcs",
        help="ORIGINATOR field to write into the .ccss metadata",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Display the W/R/G/B patches locally",
    )
    return parser.parse_args()


def load_sample_module():
    spec = importlib.util.spec_from_file_location("hpcs_sample", SAMPLE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {SAMPLE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prompt_for_color(color: str):
    input(f"Set the display to {color} and press Enter to capture: ")


def prompt_for_local_sequence():
    input("Position the meter, then press Enter to start the local W/R/G/B sample sequence: ")


async def capture_processed_sample(sample_mod):
    while True:
        processed = await sample_mod.capture_processed_sample()
        if processed is not False:
            return processed


def extract_bands(sample_mod, processed: tuple[dict[str, float], list[float]]) -> list[float]:
    return sample_mod.extract_spectrum_range(
        processed,
        SPECTRAL_START_NM,
        SPECTRAL_END_NM,
    )


def format_created(dt: datetime.datetime) -> str:
    return dt.strftime("%a %b %d %H:%M:%S %Y")


def start_local_display(color_hex: str) -> subprocess.Popen[str]:
    display_bin = shutil.which("display")
    if display_bin is None:
        raise RuntimeError("`display` not found in PATH")
    return subprocess.Popen(
        [display_bin, "-size", "4000x4000", f"xc:{color_hex}"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_local_display(proc: subprocess.Popen[str] | None):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def write_ccss(
    output_path: Path,
    created: datetime.datetime,
    samples: dict[str, list[float]],
    args,
):
    band_names = " ".join(
        f"SPEC_{nm}" for nm in range(SPECTRAL_START_NM, SPECTRAL_END_NM + 1)
    )
    lines = [
        "CCSS   ",
        "",
        f'ORIGINATOR "{args.originator}"',
        f'CREATED "{format_created(created)}"',
        f'DISPLAY "{args.display}"',
        f'TECHNOLOGY "{args.technology}"',
        'DISPLAY_TYPE_REFRESH "NO"',
        'UI_SELECTORS "i"',
        f'REFERENCE "{args.reference}"',
        'OEM "YES"',
        f'SPECTRAL_BANDS "{SPECTRAL_END_NM - SPECTRAL_START_NM + 1}"',
        f'SPECTRAL_START_NM "{SPECTRAL_START_NM:.6f}"',
        f'SPECTRAL_END_NM "{SPECTRAL_END_NM:.6f}"',
        'SPECTRAL_NORM "1.000000"',
        f'DESCRIPTOR "{args.descriptor}"',
        "",
        f'NUMBER_OF_FIELDS {SPECTRAL_END_NM - SPECTRAL_START_NM + 2}',
        "BEGIN_DATA_FORMAT",
        f"SAMPLE_ID {band_names} ",
        "END_DATA_FORMAT",
        "",
        f"NUMBER_OF_SETS {len(SAMPLE_COLORS)}",
        "BEGIN_DATA",
    ]
    for index, (short_name, _long_name, _color_hex) in enumerate(SAMPLE_COLORS, start=1):
        values = " ".join(f"{value:.8f}" for value in samples[short_name])
        lines.append(f"{index} {values} ")
    lines.append("END_DATA")
    lines.append("")
    output_path.write_text("\n".join(lines), encoding="ascii")


async def capture_samples(sample_mod: types.ModuleType, args) -> dict[str, list[float]]:
    samples = {}
    local_display = None
    async with sample_mod.device_session():
        if args.local:
            prompt_for_local_sequence()
        try:
            for short_name, long_name, color_hex in SAMPLE_COLORS:
                if args.local:
                    stop_local_display(local_display)
                    local_display = start_local_display(color_hex)
                    await asyncio.sleep(1)
                else:
                    prompt_for_color(long_name)
                print(f"Capturing {long_name} sample...", file=sys.stderr)
                processed = await capture_processed_sample(sample_mod)
                samples[short_name] = extract_bands(sample_mod, processed)
        finally:
            stop_local_display(local_display)
    return samples


async def async_main():
    args = parse_args()
    sample_mod = load_sample_module()
    created = datetime.datetime.now()
    samples = await capture_samples(sample_mod, args)
    write_ccss(args.output, created, samples, args)
    print(f"Wrote {args.output}")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
