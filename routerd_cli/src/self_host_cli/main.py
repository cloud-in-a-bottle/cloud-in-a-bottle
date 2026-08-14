"""Cloud in a Bottle CLI entry point.

Usage:
    openhost up [--domain DOMAIN] [--zone-domain DOMAIN]
                [--email EMAIL] [--port PORT] [--foreground]
    openhost down
    openhost doctor
    openhost update
"""

import argparse
import sys

from self_host_cli.doctor import run_doctor
from self_host_cli.down import run_down
from self_host_cli.up import run_up
from self_host_cli.update import run_update


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openhost",
        description="Cloud in a Bottle -- run open-source apps on your own compute.",
    )
    sub = parser.add_subparsers(dest="command")

    # --- openhost up ---
    up_parser = sub.add_parser(
        "up",
        help="Start Cloud in a Bottle.",
        description="Launches the Cloud in a Bottle router directly on this machine.",
    )
    up_parser.add_argument(
        "--domain",
        type=str,
        default="",
        help="Domain name for TLS mode.",
    )
    up_parser.add_argument(
        "--zone-domain",
        type=str,
        default="",
        help=(
            "Domain used for host-based app subdomain routing (for example, "
            "example.com). Does not enable TLS by itself."
        ),
    )
    up_parser.add_argument(
        "--email",
        type=str,
        default="",
        help="Contact email for ACME certificate registration (used with --domain).",
    )
    up_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Host port for the router HTTP interface (default: 8080).",
    )
    up_parser.add_argument(
        "--foreground",
        action="store_true",
        default=False,
        help="Run in foreground instead of daemonizing.",
    )
    up_parser.add_argument(
        "--claim-token",
        type=str,
        default=None,
        help=(
            "Secret required to claim this Cloud in a Bottle on first boot. If omitted, "
            "a URL-safe random token is generated and printed. The password-"
            "setup page rejects every caller that doesn't supply this token, "
            "preventing a MITM from claiming the instance. Must be URL-safe "
            "(letters, digits, '-', '_')."
        ),
    )

    # --- openhost down ---
    sub.add_parser(
        "down",
        help="Stop Cloud in a Bottle.",
        description="Stops the running router cleanly.",
    )

    # --- openhost doctor ---
    sub.add_parser(
        "doctor",
        help="Check prerequisites and common misconfigurations.",
        description="Validates that Podman, ports, and other prerequisites are correctly configured.",
    )

    # --- openhost update ---
    sub.add_parser(
        "update",
        help="Update Cloud in a Bottle code.",
        description="Pulls latest code (git pull) and syncs dependencies.",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "up":
        run_up(args)
    elif args.command == "down":
        run_down(args)
    elif args.command == "doctor":
        run_doctor()
    elif args.command == "update":
        run_update(args)
    else:
        parser.print_help()
        sys.exit(1)
