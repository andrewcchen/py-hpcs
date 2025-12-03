#!/usr/bin/env python3

import argparse
import csv
import re
import sys
from pathlib import Path


NM_COLUMN = re.compile(r"^\d+nm$", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert captured spectrum to a txt file for ColorCalculator")
    parser.add_argument("input", nargs="?", type=Path, help="Input file")
    parser.add_argument("-o", "--output", type=Path, help="Output file")
    return parser.parse_args()


def select_columns(header: list[str]) -> list[int]:
    assert 'timestamp' in header
    indices: list[int] = [-1]
    for idx, name in enumerate(header):
        if name == "timestamp":
            indices[0] = idx
        elif NM_COLUMN.match(name):
            indices.append(idx)
    assert indices[0] >= 0, "Missing timestamp column in input"
    return indices


def read_selected_rows(file) -> list[list[str]]:
    reader = csv.reader(file)
    header = next(reader)
    indices = select_columns(header)
    selected = [[header[i] for i in indices]]
    for row in reader:
        selected.append([row[i] for i in indices])
    return selected


def transpose(matrix: list[list[str]]) -> list[list[str]]:
    return [list(group) for group in zip(*matrix)]


def write_tsv(rows: list[list[str]], handle) -> None:
    writer = csv.writer(handle, delimiter="\t")
    writer.writerows(rows)


def main() -> None:
    args = parse_args()
    if args.input is None:
        with args.input.open(newline="") as handle:
            matrix = read_selected_rows(handle)
    else:
        matrix = read_selected_rows(sys.stdin)
    transposed = transpose(matrix)
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
