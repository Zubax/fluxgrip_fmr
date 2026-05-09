#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from fluxgrip_interface import DEMAG_VALUE_COUNT, FluxGripConfig, normalize_demag_values
from force_rig_interface import ForceRigConfig, ForceRigInterface, RemainingForceResult
from setup_serial_links import FORCE_SENSOR_PORT, STEP_DRIVE_PORT

LOGGER = logging.getLogger(__name__)
SLCAN_REMOVE_COMMAND = "sudo ./scripts/setup_slcan --remove-all"

DEFAULT_DEMAGNETIZATION_SEQUENCE = [
    -100,
    +100,
    -100,
    +100,
    -99,
    -92,
    -88,
    +80,
    +74,
    -67,
    -61,
    +56,
    +51,
    -46,
    -42,
    +38,
    +35,
    -32,
    -29,
    +27,
    +25,
    -22,
    -20,
    +18,
    -16,
    +14,
    -11,
    +10,
    -8,
    +7,
    -6,
    +5,
    -4,
    +4,
    -3,
    +3,
    -2,
    +2,
    -1,
    +2,
    -1,
    +2,
    -1,
    +1,
    -1,
    +1,
    -1,
    +1,
    -1,
    +1,
    -1,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
]

CUSTOM_DEMAGNETIZATION_SEQUENCE = [
    -60,
    +100,
    -100,
    +100,
    -99,
    -92,
    -88,
    +80,
    +74,
    -67,
    -61,
    +56,
    +51,
    -46,
    -42,
    +38,
    +35,
    -32,
    -29,
    +27,
    +25,
    -22,
    -20,
    +18,
    -16,
    +14,
    -11,
    +10,
    -8,
    +7,
    -6,
    +5,
    -4,
    +4,
    -3,
    +3,
    -2,
    +2,
    -1,
    +2,
    -1,
    +2,
    -1,
    +1,
    -1,
    +1,
    -1,
    +1,
    -1,
    +1,
    -1,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
]


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    slcan_interfaces = setup_slcan_interfaces()
    if slcan_interfaces:
        print(
            f"✗ Existing SLCAN interface(s) detected: {', '.join(slcan_interfaces)}",
            file=sys.stderr,
        )
        print("Please remove them before running this test:", file=sys.stderr)
        print(f"  {SLCAN_REMOVE_COMMAND}", file=sys.stderr)
        return 1

    try:
        result = asyncio.run(run(args))
    except KeyboardInterrupt:
        print()
        LOGGER.info("Interrupted")
        return 130
    except Exception as ex:
        print(f"✗ {type(ex).__name__}: {ex}", file=sys.stderr)
        LOGGER.debug("Touchdown test failed", exc_info=True)
        return 1

    print_summary(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Move the plate onto FluxGrip, magnetize/demagnetize, then measure the remaining "
            "negative force while lifting the plate away."
        )
    )
    parser.add_argument(
        "--force-port",
        default=FORCE_SENSOR_PORT,
        help="Force-sensor serial port or URI.",
    )
    parser.add_argument(
        "--drive-port", default=STEP_DRIVE_PORT, help="Step-drive serial port or URI."
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging."
    )
    parser.add_argument(
        "--touch-force", type=float, default=0.3, help="Touchdown threshold in newtons."
    )
    parser.add_argument(
        "--touch-timeout",
        type=positive_float,
        default=30.0,
        help="Max seconds to search downward.",
    )
    parser.add_argument(
        "--pull-timeout",
        type=positive_float,
        default=15.0,
        help="Max seconds to pull upward.",
    )
    parser.add_argument(
        "--tare-samples",
        type=positive_int,
        default=50,
        help="Samples used for the pre-touchdown force tare.",
    )
    parser.add_argument(
        "--sample-period",
        type=positive_float,
        default=0.02,
        help="Force polling period in seconds.",
    )
    parser.add_argument(
        "--max-pull-force",
        type=positive_float,
        default=10.0,
        help="Safety limit: stop and fail if upward pull force magnitude reaches this value.",
    )
    parser.add_argument("--settle-after-touch", type=non_negative_float, default=0.5)
    parser.add_argument("--settle-after-demag", type=non_negative_float, default=1.0)
    parser.add_argument("--magnetized-hold", type=non_negative_float, default=1.0)
    parser.add_argument(
        "--custom",
        action="store_true",
        help="Write CUSTOM_DEMAGNETIZATION_SEQUENCE before magnetizing.",
    )
    parser.add_argument(
        "--can-iface",
        help="Cyphal CAN interface. Use /dev/... for SLCAN or pass a full URI.",
    )
    parser.add_argument("--can-iface-index", type=non_negative_int, default=0)
    parser.add_argument("--controller-node-id", type=node_id, default=1)
    parser.add_argument("--target-node-id", type=node_id, default=None)
    return parser


def setup_slcan_interfaces() -> list[str]:
    return sorted(path.name for path in Path("/sys/class/net").glob("slcan*"))


async def run(args: argparse.Namespace) -> RemainingForceResult:
    demag_values = custom_demag_values() if args.custom else None
    config = ForceRigConfig(
        force_port=args.force_port,
        drive_port=args.drive_port,
        touch_force_n=args.touch_force,
        touch_timeout_s=args.touch_timeout,
        pull_timeout_s=args.pull_timeout,
        tare_samples=args.tare_samples,
        sample_period_s=args.sample_period,
        max_pull_force_n=args.max_pull_force,
        settle_after_touch_s=args.settle_after_touch,
        settle_after_demag_s=args.settle_after_demag,
        magnetized_hold_s=args.magnetized_hold,
        fluxgrip=FluxGripConfig(
            controller_node_id=args.controller_node_id,
            target_node_id=args.target_node_id,
            can_iface=args.can_iface,
            can_iface_index=args.can_iface_index,
        ),
    )

    async with ForceRigInterface(
        config, progress=print_progress, phase=print_phase(config)
    ) as rig:
        result = await rig.measure_remaining_force(demag_values)
        if result.detached:
            print("\n✓ Pull timeout reached; plate should be clear")
        else:
            print("\n• Pull timeout reached")
        return result


def custom_demag_values() -> list[int]:
    try:
        return normalize_demag_values(CUSTOM_DEMAGNETIZATION_SEQUENCE)
    except ValueError as ex:
        missing_count = DEMAG_VALUE_COUNT - len(CUSTOM_DEMAGNETIZATION_SEQUENCE)
        if missing_count > 0:
            raise ValueError(
                "CUSTOM_DEMAGNETIZATION_SEQUENCE contains "
                f"{len(CUSTOM_DEMAGNETIZATION_SEQUENCE)} values; expected {DEMAG_VALUE_COUNT}. "
                f"Add {missing_count} more value(s) before using --custom."
            ) from ex
        raise


def print_progress(
    phase: str, elapsed_s: float, force_n: float, peak_force_n: float
) -> None:
    if phase == "down":
        print(f"\r  down {elapsed_s:6.2f}s  F={force_n:+08.3f} N", end="", flush=True)
        return
    print(
        f"\r  up   {elapsed_s:6.2f}s  F={force_n:+08.3f} N  peak={peak_force_n:+08.3f} N",
        end="",
        flush=True,
    )


def print_phase(config: ForceRigConfig):
    messages = {
        "tare-before-touchdown": "→ Taring force sensors before touchdown...",
        "touchdown": f"→ Moving arm down slowly until total force reaches {config.touch_force_n:+.3f} N...",
        "settle-after-touchdown": f"\n→ Settling for {config.settle_after_touch_s:.3f} s...",
        "fluxgrip-connect": "→ Connecting to FluxGrip...",
        "set-demag-values": "→ Writing demagnetization pulse_pct values...",
        "magnetize": "→ Magnetizing...",
        "magnetized-hold": f"→ Holding magnetized state for {config.magnetized_hold_s:.3f} s...",
        "demagnetize": "→ Demagnetizing...",
        "settle-after-demag": f"→ Settling for {config.settle_after_demag_s:.3f} s...",
        "pull-up": "→ Moving arm up and recording negative detach force...",
        "pull-force-safety": "\n✗ Pull force safety limit reached; stopping arm and recovering...",
        "factory-reset": "→ Factory resetting FluxGrip...",
        "recovery-magnetize": "→ Recovery magnetize...",
        "recovery-magnetized-hold": f"→ Holding recovery magnetized state for {config.magnetized_hold_s:.3f} s...",
        "recovery-demagnetize": "→ Recovery demagnetize...",
        "recovery-settle-after-demag": f"→ Settling after recovery demag for {config.settle_after_demag_s:.3f} s...",
        "pull-up-retry": "→ Retrying arm-up movement after recovery...",
    }

    def callback(phase: str) -> None:
        if phase in {
            "settle-after-touchdown",
            "fluxgrip-connect",
            "factory-reset",
            "pull-up-retry",
        }:
            print()
        print(messages.get(phase, f"→ {phase}"))

    return callback


def print_summary(result: RemainingForceResult) -> None:
    print()
    print("──────────────────────────────────────────────")
    print("  ✓ Touchdown test complete")
    print(f"  Touchdown force: {result.touchdown_force_n:+.3f} N")
    print(f"  Peak remaining magnetic force: {result.peak_remaining_force_n:+.3f} N")
    print(f"  Pull elapsed: {result.pull_elapsed_s:.3f} s")
    print(f"  Detached: {result.detached}")
    print(f"  Recovery performed: {result.recovery_performed}")
    print("──────────────────────────────────────────────")


def positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def non_negative_float(raw: str) -> float:
    value = float(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def node_id(raw: str) -> int:
    value = int(raw)
    if value < 0 or value > 127:
        raise argparse.ArgumentTypeError("node-ID must be in range 0..127")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
