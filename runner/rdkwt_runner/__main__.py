from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .main import configured_roots, install_signal_handlers, run_request_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RDK WebToolChain fixed Runner")
    parser.add_argument("--request", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    assets_root, runs_root = configured_roots()
    install_signal_handlers()
    try:
        run_request_file(args.request, assets_root, runs_root)
    except BaseException as exc:
        print(f"rdkwt-runner: {exc}", file=sys.stderr, flush=True)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
