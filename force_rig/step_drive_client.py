#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable

import serial

from setup_serial_links import STEP_DRIVE_PORT
from step_drive_control import StepDriveControl

LOGGER = logging.getLogger(__name__)
PROGRESS_BAR_WIDTH = 32

# The installed drive wiring is inverted relative to the firmware's UP/DOWN labels.
DRIVE_DIRECTION_IS_INVERTED = True


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
    parser = argparse.ArgumentParser(
        description="Manual step-drive control for jogging the force-rig arm.",
    )
    parser.add_argument(
        "--port",
        "-P",
        default=STEP_DRIVE_PORT,
        help="Serial port or URI for the step-drive controller.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    add_move_command(subparsers, "up", "Move the arm upward for a fixed duration.")
    add_move_command(subparsers, "down", "Move the arm downward for a fixed duration.")
    subparsers.add_parser("stop", help="Send STOP to the step drive.")
    return parser


def add_move_command(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
) -> None:
    parser = subparsers.add_parser(name, help=help_text, description=help_text)
    parser.add_argument(
        "--duration",
        "-t",
        type=positive_float,
        default=1.0,
        help="Run the motor for this many seconds before sending STOP.",
    )
    parser.add_argument(
        "--speed",
        "-s",
        choices=("slow", "fast"),
        default="slow",
        help="Motor speed.",
    )


async def run(args: argparse.Namespace) -> None:
    port = serial.serial_for_url(
        args.port,
        baudrate=StepDriveControl.BAUD,
        dsrdtr=None,
        rtscts=None,
    )
    control = StepDriveControl(port)
    try:
        if args.command == "stop":
            print(f"■ Sending STOP via {args.port}")
            await control.stop()
            print("✓ Step drive stopped")
            return

        move = physical_move(control, args.command)
        print(f"→ Moving arm {args.command} for {args.duration:.3f} s at {args.speed} speed via {args.port}")
        await move(args.speed)
        await show_progress(args.duration)
        print("✓ Move complete")
    finally:
        try:
            await control.stop()
        finally:
            control.close()


def physical_move(
    control: StepDriveControl,
    direction: str,
) -> Callable[[str], Awaitable[None]]:
    if direction not in {"up", "down"}:
        raise ValueError(f"Unsupported move direction: {direction}")
    if not DRIVE_DIRECTION_IS_INVERTED:
        return control.up if direction == "up" else control.down
    return control.down if direction == "up" else control.up


def positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


async def show_progress(duration_s: float) -> None:
    start_time = time.monotonic()
    while True:
        elapsed_s = time.monotonic() - start_time
        progress = min(elapsed_s / duration_s, 1.0)
        print_progress(progress, elapsed_s, duration_s)
        if progress >= 1.0:
            print()
            return
        await asyncio.sleep(min(0.05, duration_s - elapsed_s))


def print_progress(progress: float, elapsed_s: float, duration_s: float) -> None:
    filled = round(PROGRESS_BAR_WIDTH * progress)
    bar = "█" * filled + "░" * (PROGRESS_BAR_WIDTH - filled)
    percent = progress * 100.0
    print(f"\r  [{bar}] {percent:6.2f}%  {elapsed_s:6.2f}/{duration_s:.2f}s", end="", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
