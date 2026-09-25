"""Command-line interface for planning and converting one Flywheel subject."""

import argparse
import json
import os
from pathlib import Path
from typing import Callable

from .api import FlywheelBIDS, _require_pinned_deface_config
from .defacing import DefaceConfig
from .errors import DefacingError, NetworkFW2BIDSError


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download one Flywheel subject and convert its DICOM archives to BIDS"
    )
    parser.add_argument("--subject", required=True, help="Flywheel subject label")
    parser.add_argument(
        "--project", default="russpold/r01network", help="Flywheel group/project path"
    )
    parser.add_argument("--output", type=Path, help="new BIDS dataset directory")
    parser.add_argument("--inventory", type=Path, help="write the current acquisition selection as metadata-only JSON")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="download and convert; without this flag, only print the plan",
    )
    parser.add_argument("--pydeface-image", type=Path, metavar="PATH")
    parser.add_argument("--pydeface-version", metavar="VERSION")
    parser.add_argument("--pydeface-sha256", metavar="SHA256")
    return parser


def main(
    argv: list[str] | None = None,
    factory: Callable[..., FlywheelBIDS] = FlywheelBIDS.from_token,
) -> int:
    parser = get_parser()
    args = parser.parse_args(argv)
    if args.execute and args.output is None:
        parser.error("--execute requires --output")
    config: DefaceConfig | None = None
    values = (args.pydeface_image, args.pydeface_version, args.pydeface_sha256)
    if args.execute:
        if not all(values):
            parser.error(
                "--execute requires --pydeface-image, --pydeface-version, and --pydeface-sha256"
            )
        config = DefaceConfig(
            image=args.pydeface_image,
            version=args.pydeface_version,
            sha256=args.pydeface_sha256,
        )
        try:
            _require_pinned_deface_config(config)
        except DefacingError as exc:
            parser.error(str(exc))
    elif any(value is not None for value in values):
        parser.error("PyDeface options require --execute")
    token = os.environ.get("FLYWHEEL_API_TOKEN")
    if not token:
        raise SystemExit("FLYWHEEL_API_TOKEN is not set")
    try:
        converter = factory(token, project_path=args.project, deface_config=config)
        plans = converter.plan_subject(args.subject)
        if args.inventory:
            args.inventory.parent.mkdir(parents=True, exist_ok=True)
            args.inventory.write_text(json.dumps(converter.selection, indent=2) + "\n")
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
