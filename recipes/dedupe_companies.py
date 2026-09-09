#!/usr/bin/env python3
"""
dedupe-companies - find likely duplicate company records in a CSV.

The problem: "Acme Inc.", "Acme, Inc", "ACME Incorporated" and "acme.com" are
one account. Your CRM thinks they are four. Native dedupe rules match on exact
strings and miss all of it, and the paid tools want a seat license before they
will tell you how bad it is.

This clusters records using three signals, strongest first:

  1. same normalized domain      (acme.com == www.Acme.com/careers)
  2. same normalized name        (Acme, Inc. == ACME INCORPORATED)
  3. fuzzy name similarity       (Acme Industries ~ Acme Industrys)

Clusters are transitive: if A matches B by domain and B matches C by name,
all three land in one cluster.

Standard library only. Copy this file and run it.

Read-only. It prints a report and optionally writes a CSV of the clusters.
It never touches your CRM and never modifies the input.

Usage:
    python dedupe_companies.py accounts.csv --name-col "Account Name"
    python dedupe_companies.py accounts.csv --name-col Name --domain-col Website
    python dedupe_companies.py accounts.csv --name-col Name --threshold 0.92
    python dedupe_companies.py accounts.csv --name-col Name --out clusters.csv

Tuning:
    --threshold controls fuzzy matching (0.0-1.0, default 0.88). Raise it if
    you get false pairs, lower it if you are missing obvious ones. Start by
    reading the output at the default before changing anything.

Exit codes:
    0  no duplicate clusters found
    1  could not read the file / bad arguments
    2  duplicate clusters found  (useful in CI or a scheduled hygiene check)
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher

# Legal-form suffixes stripped before comparison. Longest forms first so
# "incorporated" is removed before "inc" can partially match it.
LEGAL_SUFFIXES = [
    "incorporated", "corporation", "limited", "holdings", "holding",
    "company", "companies", "group", "gmbh", "s.a.r.l", "sarl", "pty ltd",
    "pty", "plc", "llc", "l.l.c", "ltda", "ltd", "inc", "corp", "co",
    "bv", "b.v", "nv", "n.v", "ag", "srl", "s.r.l", "spa", "s.p.a",
    "oy", "ab", "as", "a/s", "kk", "k.k", "sa", "s.a", "pvt", "pte",
]

# Multi-part public suffixes common enough to matter. Not exhaustive; this is
# a hygiene tool, not a PSL implementation. Add yours if you work a region.
MULTI_PART_TLDS = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "or.jp", "ne.jp",
    "com.au", "net.au", "org.au", "co.nz", "com.br", "com.mx", "co.za",
    "com.sg", "com.hk", "co.in", "co.kr", "com.tr", "com.ar",
}

# Free and disposable mail providers. A shared domain here means "both people
# use Gmail", not "same company", so these are excluded from domain matching.
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "aol.com", "icloud.com", "me.com", "mac.com", "msn.com",
    "protonmail.com", "proton.me", "gmx.com", "yandex.com", "mail.com",
    "qq.com", "163.com", "126.com", "comcast.net", "verizon.net", "att.net",
}

PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
WHITESPACE = re.compile(r"\s+")


def normalize_name(raw):
    """Lowercase, strip punctuation and legal suffixes, collapse whitespace."""
    if not raw:
        return ""
    name = raw.lower().strip()
    name = PUNCT.sub(" ", name)
    name = WHITESPACE.sub(" ", name).strip()

    # Strip suffixes repeatedly: "Acme Holdings Ltd" -> "acme"
    changed = True
    while changed:
        changed = False
        for suffix in LEGAL_SUFFIXES:
            token = " " + suffix
            if name.endswith(token):
                name = name[: -len(token)].strip()
                changed = True
                break
    return name


def normalize_domain(raw):
    """Reduce a URL or email to a registrable domain, or '' if not usable."""
    if not raw:
        return ""
    value = raw.strip().lower()
    if not value:
        return ""

    if "@" in value:  # an email address
        value = value.rsplit("@", 1)[1]

    value = re.sub(r"^[a-z]+://", "", value)  # scheme
    value = value.split("/")[0].split("?")[0].split("#")[0]  # path/query
    value = value.split(":")[0]  # port
    if value.startswith("www."):
        value = value[4:]

    if "." not in value:
        return ""

    parts = value.split(".")
    if len(parts) >= 3:
        tail = ".".join(parts[-2:])
        if tail in MULTI_PART_TLDS:
            value = ".".join(parts[-3:])
        else:
            value = ".".join(parts[-2:])

    return "" if value in GENERIC_DOMAINS else value


def block_key(normalized_name):
    """Cheap bucket so we compare thousands of pairs instead of millions.

    Records only get fuzzy-compared against others in the same bucket. First
    three characters is crude but catches the overwhelming majority of real
    duplicates, which differ in suffix, punctuation, or a trailing typo, not
    in their opening letters.
    """
    return normalized_name[:3]


class UnionFind:
    """Merges pairwise matches into transitive clusters."""

    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def clusters(self):
        groups = defaultdict(list)
        for i in range(len(self.parent)):
            groups[self.find(i)].append(i)
        return [g for g in groups.values() if len(g) > 1]


def main():
    parser = argparse.ArgumentParser(
        description="Find likely duplicate company records. Report only, never writes to a CRM."
    )
    parser.add_argument("csvfile", help="Path to the export")
    parser.add_argument("--name-col", required=True, help="Company name column")
    parser.add_argument(
        "--domain-col",
        help="Website or email column (optional, but the strongest signal by far)",
    )
    parser.add_argument("--id-col", help="Record id column, shown in the report")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.88,
        help="Fuzzy name similarity, 0.0-1.0 (default: 0.88)",
    )
    parser.add_argument("--out", help="Write clusters to this CSV")
    parser.add_argument(
        "--encoding",
        default="utf-8-sig",
        help="File encoding (default: utf-8-sig)",
    )
    args = parser.parse_args()

    if not 0.0 < args.threshold <= 1.0:
        print("--threshold must be between 0 and 1", file=sys.stderr)
        return 1

    try:
        with open(args.csvfile, "r", encoding=args.encoding, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, UnicodeDecodeError) as exc:
        print(f"Could not read {args.csvfile}: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("No rows found.", file=sys.stderr)
        return 1

    if args.name_col not in rows[0]:
        print(f"Column '{args.name_col}' not in file.", file=sys.stderr)
        print(f"Available: {', '.join(rows[0].keys())}", file=sys.stderr)
        return 1

    names = [normalize_name(r.get(args.name_col, "")) for r in rows]
    domains = (
        [normalize_domain(r.get(args.domain_col, "")) for r in rows]
        if args.domain_col
        else [""] * len(rows)
    )

    uf = UnionFind(len(rows))
    reasons = {}

    # Signal 1: identical domain.
    by_domain = defaultdict(list)
    for i, domain in enumerate(domains):
        if domain:
            by_domain[domain].append(i)
    for group in by_domain.values():
        for other in group[1:]:
            uf.union(group[0], other)
            reasons[other] = "domain"

    # Signal 2: identical normalized name.
    by_name = defaultdict(list)
    for i, name in enumerate(names):
        if name:
            by_name[name].append(i)
    for group in by_name.values():
        for other in group[1:]:
            uf.union(group[0], other)
            reasons.setdefault(other, "exact name")

    # Signal 3: fuzzy name similarity, within blocks only.
    blocks = defaultdict(list)
    for i, name in enumerate(names):
        if name:
            blocks[block_key(name)].append(i)

    comparisons = 0
    for members in blocks.values():
        if len(members) < 2:
            continue
        for pos, a in enumerate(members):
            for b in members[pos + 1 :]:
                if uf.find(a) == uf.find(b):
                    continue
                comparisons += 1
                if SequenceMatcher(None, names[a], names[b]).ratio() >= args.threshold:
                    uf.union(a, b)
                    reasons.setdefault(b, "fuzzy name")

    clusters = uf.clusters()
    clusters.sort(key=len, reverse=True)

    label = args.id_col if args.id_col and args.id_col in rows[0] else None
    duplicate_rows = sum(len(c) for c in clusters)

    print(f"\n{args.csvfile}")
    print(f"{len(rows):,} rows  |  {comparisons:,} fuzzy comparisons")
    print(
        f"{len(clusters):,} duplicate cluster(s) covering {duplicate_rows:,} rows "
        f"({duplicate_rows - len(clusters):,} redundant)\n"
    )

    if not clusters:
        print("No duplicates found at the current threshold.\n")
        return 0

    for n, cluster in enumerate(clusters, 1):
        print(f"Cluster {n}  ({len(cluster)} records)")
        for i in cluster:
            ident = f"[{rows[i][label]}] " if label else ""
            domain = f"  |  {domains[i]}" if domains[i] else ""
            why = reasons.get(i, "seed")
            print(f"  {ident}{rows[i][args.name_col]}{domain}   ({why})")
        print()

    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                header = ["cluster_id", "match_reason"]
                if label:
                    header.append(label)
                header += [args.name_col, "normalized_name", "normalized_domain"]
                writer.writerow(header)
                for n, cluster in enumerate(clusters, 1):
                    for i in cluster:
                        row = [n, reasons.get(i, "seed")]
                        if label:
                            row.append(rows[i][label])
                        row += [rows[i][args.name_col], names[i], domains[i]]
                        writer.writerow(row)
            print(f"Wrote {args.out}\n")
        except OSError as exc:
            print(f"Could not write {args.out}: {exc}", file=sys.stderr)
            return 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
