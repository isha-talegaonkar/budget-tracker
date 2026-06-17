#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path

from budget_tracker.analyzer import generate_report, load_transactions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a budget and spending analysis report from CSV account activity."
    )
    parser.add_argument(
        "--input-dir",
        default="input/may-2026",
        help="Directory containing CSV files to analyze.",
    )
    parser.add_argument(
        "--output",
        default="output/may-2026-report.md",
        help="Markdown report file to create.",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional report title override.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    transactions = load_transactions(args.input_dir)
    if not transactions:
        raise SystemExit(f"No supported CSV files found in {args.input_dir}")

    report = generate_report(transactions, title=args.title)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"Wrote report to {output_path}")


if __name__ == "__main__":
    main()
