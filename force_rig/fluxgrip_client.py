#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from fluxgrip_interface import DEMAG_VALUE_COUNT, FluxGripConfig, FluxGripInterface, normalize_demag_values

LOGGER = logging.getLogger(__name__)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        LOGGER.info("Interrupted")
        return 130
    except Exception as ex:
        print(f"✗ {type(ex).__name__}: {ex}", file=sys.stderr)
        LOGGER.debug("Command failed", exc_info=True)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FluxGrip test client for the force rig.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    parser.add_argument("--can-iface", help="Cyphal CAN interface. Use /dev/... for SLCAN or pass a full URI.")
    parser.add_argument(
        "--can-iface-index",
        type=non_negative_int,
        default=0,
        help="Zubax Babel index under /dev/serial/by-id when --can-iface is not set.",
    )
    parser.add_argument("--controller-node-id", type=node_id, default=1, help="Local Cyphal controller node-ID.")
    parser.add_argument(
        "--target-node-id",
        type=node_id,
        default=None,
        help="FluxGrip target node-ID. Omit to auto-detect the available FluxGrip node.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("magnetize", help="Command FluxGrip to magnetize.")
    subparsers.add_parser("demagnetize", help="Command FluxGrip to demagnetize.")

    set_demag = subparsers.add_parser("set-demag", help="Write demag values and restart FluxGrip.")
    add_demag_value_args(set_demag)

    cycle = subparsers.add_parser("cycle", help="Optionally set demag values, then magnetize and demagnetize.")
    add_demag_value_args(cycle, required=False)
    cycle.add_argument(
        "--settle",
        type=non_negative_float,
        default=1.0,
        help="Seconds to wait between magnetize and demagnetize.",
    )
    return parser


def add_demag_value_args(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(
        "--values",
        nargs="+",
        help=(
            f"Demag sequence as {DEMAG_VALUE_COUNT} integers. "
            "Use either comma-separated text or space-separated values."
        ),
    )
    group.add_argument(
        "--file",
        type=Path,
        help="Text file containing comma/space/newline-separated demag integers.",
    )


async def run(args: argparse.Namespace) -> None:
    interface = FluxGripInterface(
        FluxGripConfig(
            controller_node_id=args.controller_node_id,
            target_node_id=args.target_node_id,
            can_iface=args.can_iface,
            can_iface_index=args.can_iface_index,
        )
    )

    print("→ Connecting to FluxGrip...", flush=True)
    async with interface:
        print("✓ FluxGrip connected")

        if args.command == "magnetize":
            print("→ Magnetizing...")
            await interface.magnetize()
            print("✓ Magnetized")
            return

        if args.command == "demagnetize":
            print("→ Demagnetizing...")
            await interface.demagnetize()
            print("✓ Demagnetized")
            return

        if args.command == "set-demag":
            values = read_demag_values(args)
            print(f"→ Writing {len(values)} demag values...")
            await interface.set_demag_values(values)
            print("✓ Demag values written")
            return

        if args.command == "cycle":
            if args.values is not None or args.file is not None:
                values = read_demag_values(args)
                print(f"→ Writing {len(values)} demag values...")
                await interface.set_demag_values(values)
                print("✓ Demag values written")

            print("→ Magnetizing...")
            await interface.magnetize()
            print("✓ Magnetized")
            if args.settle > 0:
                print(f"→ Waiting {args.settle:.3f} s...")
                await asyncio.sleep(args.settle)
            print("→ Demagnetizing...")
            await interface.demagnetize()
            print("✓ Demagnetized")
            return

        raise ValueError(f"Unsupported command: {args.command}")


def read_demag_values(args: argparse.Namespace) -> list[int]:
    if args.file is not None:
        raw = args.file.read_text(encoding="utf-8")
        return parse_demag_values([raw])
    if args.values is not None:
        return parse_demag_values(args.values)
    raise ValueError("Demag values are required")


def parse_demag_values(raw_values: list[str]) -> list[int]:
    text = " ".join(raw_values).replace(",", " ")
    try:
        values = [int(part) for part in text.split()]
    except ValueError as ex:
        raise ValueError("Demag values must be integers") from ex
    return normalize_demag_values(values)


def node_id(raw: str) -> int:
    value = int(raw)
    if value < 0 or value > 127:
        raise argparse.ArgumentTypeError("node-ID must be in range 0..127")
    return value


def non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def non_negative_float(raw: str) -> float:
    value = float(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
