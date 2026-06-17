#!/usr/bin/env python3

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from budget_tracker.dashboard import build_period_dashboard_html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the budget dashboard locally."
    )
    parser.add_argument(
        "--input-dir",
        default="input",
        help="Directory containing monthly CSV folders or a single CSV directory to analyze.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind.",
    )
    parser.add_argument(
        "--port",
        default=8000,
        type=int,
        help="Port to serve on.",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional dashboard title override.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in {"/", "/index.html"}:
                self.send_error(404, "Not found")
                return

            try:
                body = build_period_dashboard_html(args.input_dir, title=args.title).encode("utf-8")
            except ValueError as exc:
                self.send_error(500, str(exc))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Serving budget dashboard at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
