#!/usr/bin/env python3
"""
field-audit - profile a CRM export before you build anything on it.

The problem: someone hands you a 200-column Salesforce or HubSpot export and
asks whether you can segment on it, score with it, or migrate it. Half the
columns are empty, a third are constants left over from a 2019 campaign, and
two of them are the same field under different names. You find this out three
days in.

This prints the shape of the file in one pass: fill rate, cardinality, inferred
type, constants, empties, free-text columns, and columns that look like
duplicates of each other.

Standard library only. Copy this file and run it.

Usage:
    python field_audit.py contacts.csv
    python field_audit.py contacts.csv --min-fill 0.5
    python field_audit.py contacts.csv --sort fill
    python field_audit.py contacts.csv --delimiter ';' --encoding latin-1

Exit codes:
    0  audit completed
    1  could not read the file

Nothing is written. The file is opened read-only.
"""

import argparse
import csv
import re
import sys
from collections import Counter

# Values that mean "empty" in exports even though they are not blank. CRM
# exports are full of these; treating them as populated inflates every fill
# rate in the report and is the single most common reason an audit lies.
NULLISH = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "nil",
    "unknown",
    "not applicable",
    "#n/a",
}

BOOL_TRUE = {"true", "yes", "y", "1", "t"}
BOOL_FALSE = {"false", "no", "n", "0", "f"}

# Deliberately loose. The goal is "this column looks like a date", not parsing.
DATE_PATTERNS = [
    re.compile(r"^\d{4}-\d{2}-\d{2}"),  # 2026-09-09, ISO timestamps
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$"),  # 9/9/2026
    re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{2,4}$"),  # 9-Sep-2026
]

# Columns whose values are usually long prose. Flagged separately because they
# are almost never usable as-is for grouping, filtering, or as model features.
FREE_TEXT_MIN_AVG_LEN = 60


def is_null(value):
    return value.strip().lower() in NULLISH


def looks_like_date(value):
    return any(p.match(value) for p in DATE_PATTERNS)


def infer_type(values):
    """Best-effort type for a column, from its non-null values.

    Checked most specific first. 'categorical' means short strings with low
    cardinality relative to volume; 'text' means everything else.
    """
    if not values:
        return "empty"

    sample = values[:2000]

    if all(v.strip().lower() in BOOL_TRUE | BOOL_FALSE for v in sample):
        return "boolean"

    if all(looks_like_date(v.strip()) for v in sample):
        return "date"

    ints = 0
    floats = 0
    for v in sample:
        cleaned = v.strip().replace(",", "").replace("$", "").replace("%", "")
        try:
            int(cleaned)
            ints += 1
            continue
        except ValueError:
            pass
        try:
            float(cleaned)
            floats += 1
        except ValueError:
            pass
    if ints == len(sample):
        return "integer"
    if ints + floats == len(sample):
        return "number"

    avg_len = sum(len(v) for v in sample) / len(sample)
    if avg_len >= FREE_TEXT_MIN_AVG_LEN:
        return "text (long)"

    distinct = len(set(sample))
    if distinct <= max(50, len(sample) * 0.02):
        return "categorical"

    return "text"


def profile_column(name, values, total_rows):
    """Return a dict of stats for one column."""
    populated = [v for v in values if not is_null(v)]
    distinct = Counter(v.strip() for v in populated)

    lengths = [len(v) for v in populated] or [0]

    return {
        "name": name,
        "fill": len(populated) / total_rows if total_rows else 0.0,
        "populated": len(populated),
        "distinct": len(distinct),
        "type": infer_type(populated),
        "max_len": max(lengths),
        "avg_len": sum(lengths) / len(lengths),
        "top": distinct.most_common(3),
        # Fingerprint used to spot columns carrying identical data under
        # different names. Order-independent, so a reordered export still
        # matches. Only meaningful for populated columns.
        "fingerprint": frozenset(distinct.items()) if populated else None,
    }


def find_duplicate_columns(profiles):
    """Group columns whose populated values are exactly identical."""
    by_fingerprint = {}
    for p in profiles:
        if p["fingerprint"] is None or p["distinct"] <= 1:
            continue
        by_fingerprint.setdefault(p["fingerprint"], []).append(p["name"])
    return [names for names in by_fingerprint.values() if len(names) > 1]


def read_csv(path, delimiter, encoding):
    with open(path, "r", encoding=encoding, newline="") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            return [], []
        columns = {name: [] for name in header}
        # Duplicate header names are common in exports. Keep the first and
        # suffix the rest so nothing is silently dropped.
        seen = Counter()
        resolved = []
        for name in header:
            seen[name] += 1
            resolved.append(name if seen[name] == 1 else f"{name}__{seen[name]}")
        columns = {name: [] for name in resolved}

        rows = 0
        for row in reader:
            rows += 1
            for i, name in enumerate(resolved):
                columns[name].append(row[i] if i < len(row) else "")
        return columns, rows


def main():
    parser = argparse.ArgumentParser(
        description="Profile a CRM export: fill rates, cardinality, types, duplicates."
    )
    parser.add_argument("csvfile", help="Path to the export")
    parser.add_argument(
        "--min-fill",
        type=float,
        default=0.0,
        help="Only show columns with fill rate at or above this (0.0-1.0)",
    )
    parser.add_argument(
        "--sort",
        choices=["name", "fill", "distinct"],
        default="fill",
        help="Sort order for the column table (default: fill)",
    )
    parser.add_argument("--delimiter", default=",", help="Field delimiter (default: ,)")
    parser.add_argument(
        "--encoding",
        default="utf-8-sig",
        help="File encoding (default: utf-8-sig, which tolerates Excel's BOM)",
    )
    args = parser.parse_args()

    try:
        columns, total_rows = read_csv(args.csvfile, args.delimiter, args.encoding)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"Could not read {args.csvfile}: {exc}", file=sys.stderr)
        return 1

    if not columns:
        print("File has no header row.", file=sys.stderr)
        return 1

    profiles = [profile_column(n, v, total_rows) for n, v in columns.items()]

    print(f"\n{args.csvfile}")
    print(f"{total_rows:,} rows x {len(profiles)} columns\n")

    empties = [p["name"] for p in profiles if p["populated"] == 0]
    constants = [
        (p["name"], p["top"][0][0])
        for p in profiles
        if p["distinct"] == 1 and p["populated"] > 0
    ]
    dupes = find_duplicate_columns(profiles)

    # Findings first. If you read nothing else, read these.
    if empties:
        print(f"EMPTY ({len(empties)}) - no populated values, safe to drop")
        for name in empties:
            print(f"  {name}")
        print()

    if constants:
        print(f"CONSTANT ({len(constants)}) - one value throughout, carries no signal")
        for name, value in constants:
            shown = value if len(value) <= 40 else value[:37] + "..."
            print(f"  {name} = {shown}")
        print()

    if dupes:
        print(f"IDENTICAL ({len(dupes)} group(s)) - same values under different names")
        for group in dupes:
            print(f"  {' == '.join(group)}")
        print()

    shown = [p for p in profiles if p["fill"] >= args.min_fill]
    if args.sort == "name":
        shown.sort(key=lambda p: p["name"].lower())
    elif args.sort == "distinct":
        shown.sort(key=lambda p: -p["distinct"])
    else:
        shown.sort(key=lambda p: -p["fill"])

    name_w = min(40, max((len(p["name"]) for p in shown), default=10))
    print(f"{'COLUMN'.ljust(name_w)}  {'FILL':>6}  {'DISTINCT':>9}  {'TYPE':<14}  TOP VALUE")
    print("-" * (name_w + 60))
    for p in shown:
        top = p["top"][0][0] if p["top"] else ""
        if len(top) > 28:
            top = top[:25] + "..."
        name = p["name"] if len(p["name"]) <= name_w else p["name"][: name_w - 3] + "..."
        print(
            f"{name.ljust(name_w)}  "
            f"{p['fill'] * 100:5.1f}%  "
            f"{p['distinct']:>9,}  "
            f"{p['type']:<14}  "
            f"{top}"
        )

    if args.min_fill > 0:
        hidden = len(profiles) - len(shown)
        if hidden:
            print(f"\n{hidden} column(s) below the {args.min_fill:.0%} fill threshold not shown.")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
