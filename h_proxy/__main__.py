"""Command-line entry point: ``python -m h_proxy [config.yaml]``."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import ConfigError, load
from .server import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="h_proxy",
        description="A small pure-standard-library TCP/HTTP proxy (haproxy-lite).",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="config.yaml",
        help="path to the YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity (default: INFO)",
    )
    parser.add_argument("--version", action="version", version=f"h_proxy {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    run(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
