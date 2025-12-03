#!/usr/bin/env python3

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Iterable


NM_COLUMN = re.compile(r"^\d+nm$", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read a CSV file with a `time` column and wavelength columns such as "
            "`380nm`, then emit a tab-separated file with the selected data transposed."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        help="Path to the input CSV file, or omit/pass '-' to read from stdin.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output TSV file. Default: stdout.",
    )
    return parser.parse_args()


def select_columns(header: Iterable[str]) -> list[int]:
    indices: list[int] = []
    seen_time = False
    for idx, name in enumerate(header):
        lowered = name.lower()
        if lowered == "time":
            indices.append(idx)
            seen_time = True
        elif NM_COLUMN.match(lowered):
            indices.append(idx)
    if not seen_time:
        raise ValueError("Input CSV must include a `time` column.")
    if len(indices) == 1:
        raise ValueError("No wavelength columns (like `380nm`) were found.")
    return indices


def read_selected_rows(handle) -> list[list[str]]:
    reader = csv.reader(handle)
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("Input CSV is empty.") from None
    indices = select_columns(header)
    selected = [[header[i] for i in indices]]
    for row in reader:
        selected.append([row[i] for i in indices])
    return selected


def transpose(matrix: list[list[str]]) -> list[list[str]]:
    return [list(group) for group in zip(*matrix)]


def write_tsv(rows: list[list[str]], handle) -> None:
    writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
    writer.writerows(rows)


def main() -> None:
    args = parse_args()
    if args.input is None or str(args.input) == "-":
        matrix = read_selected_rows(sys.stdin)
    else:
        with args.input.open(newline="") as handle:
            matrix = read_selected_rows(handle)
    transposed = transpose(matrix)
    if not transposed:
        raise ValueError("No data found in selected columns.")
    transposed[0][0] = "nm"
    for row in transposed[1:]:
        label = row[0]
        if label.lower().endswith("nm"):
            row[0] = label[:-2]
    if args.output:
        with args.output.open("w", newline="") as handle:
            write_tsv(transposed, handle)
    else:
        write_tsv(transposed, sys.stdout)


if __name__ == "__main__":
    main()
