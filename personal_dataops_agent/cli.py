"""CLI for the personal data download agent team."""

from __future__ import annotations

import argparse
import json

from .team import ComfortDownloadTeam


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Agent team that helps you comfortably download data."
    )
    parser.add_argument("url", help="Source URL of the data to download")
    parser.add_argument("output", help="Output file path")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = ComfortDownloadTeam().download_data(args.url, args.output)
    print(
        json.dumps(
            {
                "success": result.success,
                "message": result.message,
                "output_path": result.output_path,
                "size_bytes": result.size_bytes,
            }
        )
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
