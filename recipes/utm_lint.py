#!/usr/bin/env python3
"""
utm-lint - validate campaign URLs against your own tagging convention.

The problem: your team agreed on utm_source=li. Six months later the warehouse
has li, linkedin, LinkedIn, Linkedin, and linkedin.com, all pointing at the
same channel, and every dashboard that groups by source is quietly wrong. The
fix is cheap at tagging time and expensive at reporting time.

This checks a list of URLs against a conventions file you control and prints
what is wrong before the links ship.

Standard library only. Copy this file and run it.

Usage:
    python utm_lint.py links.csv --url-col "Landing URL" --conventions conventions.json
    python utm_lint.py links.txt --conventions conventions.json
    python utm_lint.py --url "https://ax1om.ai/?utm_source=LinkedIn" --conventions conventions.json

Conventions file (JSON, all keys optional):

    {
      "required": ["utm_source", "utm_medium", "utm_campaign"],
      "allowed": {
        "utm_source": ["li", "rd", "em", "gh"],
        "utm_medium": ["social", "email", "referral", "cpc"]
      },
      "lowercase": ["utm_source", "utm_medium", "utm_campaign"],
      "patterns": {
        "utm_campaign": "^[a-z0-9]+(-[a-z0-9]+)*$"
      },
      "max_length": { "utm_campaign": 60 }
    }

See conventions.example.json next to this file.

Exit codes:
    0  clean
    1  could not read a file / bad arguments
    2  findings (errors or warnings) present
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from urllib.parse import parse_qsl, urlparse

UTM_KEYS = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id"]

# Characters that survive a copy-paste but break attribution downstream.
SUSPICIOUS = re.compile(r"[\s<>\"'{}|\\^`\[\]]")


class Finding:
    __slots__ = ("level", "url", "message")

    def __init__(self, level, url, message):
        self.level = level
        self.url = url
        self.message = message


def load_conventions(path):
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def check_url(url, conventions):
    """Return a list of Findings for one URL."""
    out = []
    raw = url.strip()
    if not raw:
        return out

    parsed = urlparse(raw)

    if not parsed.scheme:
        out.append(Finding("error", raw, "no scheme (missing https://)"))
    elif parsed.scheme != "https":
        out.append(Finding("warning", raw, f"scheme is {parsed.scheme}, expected https"))

    if not parsed.netloc:
        out.append(Finding("error", raw, "no host"))
        return out

    # keep_blank_values so '?utm_source=' is caught rather than silently dropped
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))

    for key in conventions.get("required", []):
        if key not in params:
            out.append(Finding("error", raw, f"missing {key}"))
        elif not params[key].strip():
            out.append(Finding("error", raw, f"{key} is empty"))

    for key, allowed in conventions.get("allowed", {}).items():
        value = params.get(key)
        if value and value not in allowed:
            close = [a for a in allowed if a.lower() == value.lower()]
            hint = f" (did you mean '{close[0]}'?)" if close else ""
            out.append(
                Finding("error", raw, f"{key}='{value}' not in allowed list{hint}")
            )

    for key in conventions.get("lowercase", []):
        value = params.get(key)
        if value and value != value.lower():
            out.append(
                Finding("error", raw, f"{key}='{value}' should be lowercase")
            )

    for key, pattern in conventions.get("patterns", {}).items():
        value = params.get(key)
        if value and not re.match(pattern, value):
            out.append(
                Finding("error", raw, f"{key}='{value}' does not match {pattern}")
            )

    for key, limit in conventions.get("max_length", {}).items():
        value = params.get(key)
        if value and len(value) > limit:
            out.append(
                Finding("warning", raw, f"{key} is {len(value)} chars, limit {limit}")
            )

    for key in UTM_KEYS:
        value = params.get(key)
        if value and SUSPICIOUS.search(value):
            out.append(
                Finding("error", raw, f"{key}='{value}' contains an unencoded character")
            )

    # A tagged link that survived a redirect chain often picks up a second copy.
    for key in UTM_KEYS:
        if parsed.query.count(key + "=") > 1:
            out.append(Finding("error", raw, f"{key} appears more than once"))

    if parsed.fragment and any(k in parsed.fragment for k in UTM_KEYS):
        out.append(
            Finding(
                "error",
                raw,
                "utm params are after the # fragment, where analytics cannot read them",
            )
        )

    return out


def read_urls(path, url_col, encoding):
    """Read URLs from a CSV column, or one per line from a text file."""
    if path.lower().endswith(".csv"):
        with open(path, "r", encoding=encoding, newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            return []
        if url_col:
            if url_col not in rows[0]:
                raise KeyError(
                    f"Column '{url_col}' not in file. Available: {', '.join(rows[0].keys())}"
                )
            return [r[url_col] for r in rows if r.get(url_col)]
        # No column named: take the first column that looks like URLs.
        for candidate in rows[0]:
            if any(str(r.get(candidate, "")).startswith("http") for r in rows[:20]):
                return [r[candidate] for r in rows if r.get(candidate)]
        raise KeyError("No URL-looking column found. Pass --url-col.")

    with open(path, "r", encoding=encoding) as fh:
        return [line for line in (l.strip() for l in fh) if line and not line.startswith("#")]


def main():
    parser = argparse.ArgumentParser(
        description="Validate campaign URLs against a tagging convention."
    )
    parser.add_argument("source", nargs="?", help="CSV or text file of URLs")
    parser.add_argument("--url", help="Check a single URL instead of a file")
    parser.add_argument("--url-col", help="URL column name, if the CSV has several")
    parser.add_argument("--conventions", help="Path to the conventions JSON")
    parser.add_argument(
        "--encoding", default="utf-8-sig", help="File encoding (default: utf-8-sig)"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Only print findings, not the summary"
    )
    args = parser.parse_args()

    if not args.source and not args.url:
        parser.error("pass a file or --url")

    try:
        conventions = load_conventions(args.conventions)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read conventions: {exc}", file=sys.stderr)
        return 1

    if not conventions:
        print(
            "No conventions file given. Checking structure only "
            "(scheme, encoding, duplicate params).\n",
            file=sys.stderr,
        )

    try:
        urls = [args.url] if args.url else read_urls(args.source, args.url_col, args.encoding)
    except (OSError, UnicodeDecodeError, KeyError) as exc:
        print(f"Could not read URLs: {exc}", file=sys.stderr)
        return 1

    findings = []
    for url in urls:
        findings.extend(check_url(url, conventions))

    # Same landing page tagged two different ways is invisible per-URL and
    # obvious across the set, so it is checked here rather than in check_url.
    by_path = defaultdict(set)
    for url in urls:
        parsed = urlparse(url.strip())
        if not parsed.netloc:
            continue
        params = dict(parse_qsl(parsed.query))
        source = params.get("utm_source")
        if source:
            by_path[(parsed.netloc + parsed.path)].add(source)
    for page, sources in by_path.items():
        if len(sources) > 1 and len({s.lower() for s in sources}) == 1:
            findings.append(
                Finding(
                    "error",
                    page,
                    f"same page tagged with case-variant sources: {sorted(sources)}",
                )
            )

    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]

    by_url = defaultdict(list)
    for f in findings:
        by_url[f.url].append(f)

    for url, items in by_url.items():
        print(f"\n{url}")
        for f in items:
            marker = "ERROR  " if f.level == "error" else "WARNING"
            print(f"  {marker}  {f.message}")

    if not args.quiet:
        print(
            f"\n{len(urls):,} URL(s) checked  |  "
            f"{len(errors)} error(s)  |  {len(warnings)} warning(s)\n"
        )

    return 2 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
