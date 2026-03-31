from __future__ import annotations

import argparse
from pathlib import Path

from clearfeed_dashboard.config import load_config
from clearfeed_dashboard.service import XAgentService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import an unzipped X archive into Clearfeed.")
    parser.add_argument("--archive-dir", required=True, help="Path to the unzipped X archive root folder.")
    parser.add_argument(
        "--run-voice-build",
        action="store_true",
        help="Build an archive-derived Voice.md proposal after importing.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    service = XAgentService(load_config())
    service.bootstrap()
    result = service.import_x_archive(args.archive_dir)
    print(result["message"])
    print(f"Summary file: {result['summary_path']}")
    if args.run_voice_build:
        build = service.maybe_run_archive_voice_build()
        print(build["message"])


if __name__ == "__main__":
    main()
