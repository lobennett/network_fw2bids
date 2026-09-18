"""Command-line interface for planning and converting one Flywheel subject."""

import argparse
import os
from pathlib import Path
from typing import Callable

from .api import FlywheelBIDS
from .errors import NetworkFW2BIDSError


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download one Flywheel subject and convert its DICOM archives to BIDS"
    )
    parser.add_argument("--subject", required=True, help="Flywheel subject label")
    parser.add_argument(
        "--project", default="russpold/r01network", help="Flywheel group/project path"
    )
    parser.add_argument("--output", type=Path, help="new BIDS dataset directory")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="download and convert; without this flag, only print the plan",
    )
    return parser


def main(
    argv: list[str] | None = None,
    factory: Callable[..., FlywheelBIDS] = FlywheelBIDS.from_token,
) -> int:
    parser = get_parser()
    args = parser.parse_args(argv)
    if args.execute and args.output is None:
        parser.error("--execute requires --output")
    token = os.environ.get("FLYWHEEL_API_TOKEN")
    if not token:
        raise SystemExit("FLYWHEEL_API_TOKEN is not set")
    try:
        converter = factory(token, project_path=args.project)
        plans = converter.plan_subject(args.subject)
        for plan in plans:
            print(f"{plan.acquisition.label}: {plan.dicom_file.name} -> {plan.relative_prefix}")
        print(f"{len(plans)} DICOM archives planned for {args.subject}")
        if args.execute:
            converter.convert_subject(args.subject, args.output, plans=plans)
            print(f"BIDS dataset written to {args.output}")
        else:
            print("Dry run only; pass --execute --output DIR to download and convert")
    except NetworkFW2BIDSError as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 1
    return 0
