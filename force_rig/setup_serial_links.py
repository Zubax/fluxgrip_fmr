#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

FORCE_SENSOR_PORT = "/dev/fmr_force_sensor"
STEP_DRIVE_PORT = "/dev/fmr_step_drive"
HORIZONTAL_RULE = "──────────────────────────────────────────────"

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create stable FMR serial-port symlinks by identifying the force sensor while "
            "the step drive is disconnected, then detecting the newly connected step drive."
        )
    )
    parser.add_argument(
        "--force-link",
        type=Path,
        default=Path(FORCE_SENSOR_PORT),
        help="Force sensor symlink path.",
    )
    parser.add_argument(
        "--drive-link",
        type=Path,
        default=Path(STEP_DRIVE_PORT),
        help="Step-drive symlink path.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for the step drive to appear.",
    )
    args = parser.parse_args()

    if args.force_link == args.drive_link:
        raise SystemExit("force and drive symlink paths must be different")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")

    try:
        print_header()

        print("[1/3] Cleanup")
        print("  • Removing existing symlinks...")
        remove_previous_symlink(args.force_link)
        print(f"    {success('✓')} {args.force_link}")
        remove_previous_symlink(args.drive_link)
        print(f"    {success('✓')} {args.drive_link}")
        print()

        print(
            "[2/3] Detect Force Sensor\n"
            f"  {action('→')} Please disconnect the step-drive USB adapter\n"
            "    and leave ONLY the force sensor connected.\n"
        )
        input("    Press ENTER to continue...")
        print()
        print("  • Scanning devices...")

        force_candidates = current_usb_serial_ports()
        if len(force_candidates) != 1:
            raise RuntimeError(
                "Expected exactly one /dev/ttyUSB* device after disconnecting the step drive, "
                f"found {format_ports(force_candidates)}"
            )
        force_port = next(iter(force_candidates))
        create_symlink(force_port, args.force_link)
        print(f"    {success('✓')} Force sensor detected")
        print(f"      {args.force_link} → {force_port}")
        print()

        print(
            "[3/3] Detect Step Drive\n"
            f"  {action('→')} Plug the step-drive USB adapter back in.\n"
        )
        input("    Press ENTER to continue...")
        print()
        print("  • Scanning devices...")

        drive_candidates = wait_for_new_usb_serial_port(
            force_candidates, timeout_s=args.timeout
        )
        if len(drive_candidates) != 1:
            raise RuntimeError(
                "Expected exactly one new /dev/ttyUSB* device after plugging in the step drive, "
                f"found {format_ports(drive_candidates)}"
            )
        drive_port = next(iter(drive_candidates))
        create_symlink(drive_port, args.drive_link)
        print(f"    {success('✓')} Step drive detected")
        print(f"      {args.drive_link} → {drive_port}")
        print()
        print_footer()
        return 0
    except PermissionError as ex:
        print(
            f"{error('✗')} Permission denied while creating/removing symlinks: {ex}",
            file=sys.stderr,
        )
        print(
            "Run this script with sudo, or pass symlink paths in a writable directory.",
            file=sys.stderr,
        )
        return 1
    except (OSError, RuntimeError) as ex:
        print(f"{error('✗')} Error: {ex}", file=sys.stderr)
        return 1


def print_header() -> None:
    print(HORIZONTAL_RULE)
    print("  FORCE RIG — Serial Link Setup")
    print(HORIZONTAL_RULE)
    print()


def print_footer() -> None:
    print(HORIZONTAL_RULE)
    print(f"  {success('✓')} Setup complete — serial links ready")
    print(HORIZONTAL_RULE)


def success(text: str) -> str:
    return color(text, GREEN)


def action(text: str) -> str:
    return color(text, YELLOW)


def error(text: str) -> str:
    return color(text, RED)


def color(text: str, ansi_color: str) -> str:
    return f"{ansi_color}{text}{RESET}"


def current_usb_serial_ports() -> set[Path]:
    return {port for port in Path("/dev").glob("ttyUSB*") if port.exists()}


def wait_for_new_usb_serial_port(
    previous_ports: set[Path], timeout_s: float
) -> set[Path]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() <= deadline:
        current_ports = current_usb_serial_ports()
        new_ports = current_ports - previous_ports
        if new_ports:
            return new_ports
        time.sleep(0.2)
    return current_usb_serial_ports() - previous_ports


def remove_previous_symlink(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if not path.is_symlink():
        raise RuntimeError(f"Refusing to remove non-symlink path: {path}")
    path.unlink()


def create_symlink(target: Path, link: Path) -> None:
    if not target.exists():
        raise RuntimeError(f"Symlink target does not exist: {target}")
    if os.path.lexists(link):
        raise RuntimeError(f"Symlink path already exists after cleanup: {link}")
    os.symlink(target, link)


def format_ports(ports: set[Path]) -> str:
    if not ports:
        return "none"
    return ", ".join(str(port) for port in sorted(ports))


if __name__ == "__main__":
    raise SystemExit(main())
