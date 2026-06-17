#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path

from budget_tracker.dashboard import build_period_dashboard_html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a browser dashboard from CSV account activity."
    )
    parser.add_argument(
        "--input-dir",
        default="input",
        help="Directory containing monthly CSV folders or a single CSV directory to analyze.",
    )
    parser.add_argument(
        "--output",
        default="output/budget-dashboard.html",
        help="HTML dashboard file to create.",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional dashboard title override.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dashboard_html = build_period_dashboard_html(args.input_dir, title=args.title)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(dashboard_html, encoding="utf-8")
    print(f"Wrote dashboard to {output_path}")


if __name__ == "__main__":
    main()
