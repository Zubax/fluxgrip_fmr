#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import numpy as np
import serial
from numpy.typing import NDArray

from force_sensor_interface import ForceSensorInterface, ForceSensorReading, MovingAverage
from setup_serial_links import FORCE_SENSOR_PORT

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
        print()
        LOGGER.info("Interrupted")
        return 130
    except Exception as ex:
        print(f"✗ {type(ex).__name__}: {ex}", file=sys.stderr)
        LOGGER.debug("Command failed", exc_info=True)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Force-sensor client for displaying readings and writing calibration data.",
    )
    parser.add_argument(
        "--port",
        "-P",
        default=FORCE_SENSOR_PORT,
        help="Serial port or URI for the force-sensor controller.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    display_parser = subparsers.add_parser("display", help="Display live force readings.")
    display_parser.add_argument(
        "--filter-depth",
        "-f",
        type=positive_int,
        default=2,
        help="Moving-average depth for displayed force values.",
    )
    display_parser.add_argument(
        "--no-tare",
        action="store_true",
        help="Skip zero-bias tare at startup.",
    )
    display_parser.add_argument(
        "--tare-samples",
        type=positive_int,
        default=50,
        help="Number of samples used for zero-bias tare.",
    )

    calibrate_parser = subparsers.add_parser("calibrate", help="Interactively calibrate the force sensors.")
    calibrate_parser.add_argument(
        "--samples",
        "-n",
        type=positive_int,
        default=100,
        help="Number of raw ADC samples to average for each calibration datapoint.",
    )
    return parser


async def run(args: argparse.Namespace) -> None:
    port = serial.serial_for_url(
        args.port,
        baudrate=ForceSensorInterface.BAUD,
        dsrdtr=None,
        rtscts=None,
    )
    sensor = ForceSensorInterface(port)
    try:
        if args.command == "display":
            await display(sensor, args)
        elif args.command == "calibrate":
            await calibrate(sensor, args)
        else:
            raise ValueError(f"Unsupported command: {args.command}")
    finally:
        sensor.close()


async def display(sensor: ForceSensorInterface, args: argparse.Namespace) -> None:
    print("→ Reading force sensors")
    if not args.no_tare:
        print(f"→ Taring zero bias from {args.tare_samples} samples...")
        zero_bias = await sensor.tare(sample_count=args.tare_samples)
        print(f"✓ Zero bias: {format_channels(zero_bias)} N")

    initial_forces = await sensor.get_forces(flush=True)
    moving_average = MovingAverage(args.filter_depth, initial_forces)
    peak_force = 0.0
    counter = 0

    while True:
        forces = moving_average(await sensor.get_forces(flush=True))
        total_force = float(np.sum(forces))
        if abs(total_force) > abs(peak_force):
            peak_force = total_force

        print(
            f"\r#{counter:06d}  "
            f"F={total_force:+08.2f} N  "
            f"channels=[{format_channels(forces)}]  "
            f"peak={peak_force:+08.2f} N",
            end="",
            flush=True,
        )
        counter += 1


async def calibrate(sensor: ForceSensorInterface, args: argparse.Namespace) -> None:
    reading = await sensor.fetch(flush=True)
    calibration = reading.calibration.copy()

    print("Current calibration:")
    print(calibration)
    print()

    new_calibration = await calibrate_all_channels(sensor, args.samples)
    if new_calibration is None:
        print("• Calibration unchanged")
        return

    for channel_index in range(ForceSensorReading.ACTIVE_CHANNEL_COUNT):
        new_coefficients = new_calibration[:, channel_index]
        calibration[:, channel_index] = new_coefficients
        print(f"✓ Channel {channel_index}: slope={new_coefficients[0]:+.9e}, offset={new_coefficients[1]:+.6f}")

    calibration = replace_invalid_coefficients(calibration)
    print()
    print("New calibration:")
    print(calibration)
    print()
    print("→ Writing calibration data...")

    while not await sensor.write_calibration(calibration):
        print("✗ Calibration write confirmation failed, retrying...")

    print("✓ Calibration data written successfully")


async def calibrate_all_channels(
    sensor: ForceSensorInterface,
    sample_count: int,
) -> NDArray[np.float64] | None:
    min_datapoints = 2
    datapoints: list[tuple[NDArray[np.float64], float]] = []

    print("Place each calibration load on the shared platform.")
    print("Both force-sensor channels will be sampled at the same time.")
    print("Use at least two datapoints: one known load and one zero-force/no-load datapoint.")
    print("Press ENTER without a value to finish calibration.")

    while True:
        raw_force = (await async_input(f"Platform force for datapoint {len(datapoints)} [N]: ")).strip()
        if not raw_force:
            break

        try:
            force_n = float(raw_force)
        except ValueError:
            print(f"✗ Invalid force value: {raw_force!r}")
            continue

        adc = await average_adc(sensor, sample_count)
        datapoints.append((adc, force_n))
        print(f"  ✓ ADC [{format_adc_channels(adc)}] → {force_n:+.3f} N")

    if len(datapoints) < min_datapoints:
        print(f"  • Not enough datapoints; need at least {min_datapoints}")
        print()
        return None

    adc_values = np.vstack([adc for adc, _ in datapoints])
    force_values = np.array([force for _, force in datapoints], dtype=np.float64)
    coefficients = np.zeros(ForceSensorInterface.CALIBRATION_SHAPE, dtype=np.float64)
    for channel_index in range(ForceSensorReading.ACTIVE_CHANNEL_COUNT):
        coefficients[:, channel_index] = np.polyfit(adc_values[:, channel_index], force_values, deg=1)
    print()
    return coefficients


async def average_adc(sensor: ForceSensorInterface, sample_count: int) -> NDArray[np.float64]:
    total = np.zeros(ForceSensorReading.ACTIVE_CHANNEL_COUNT, dtype=np.float64)
    for sample_index in range(sample_count):
        reading = await sensor.fetch(flush=sample_index == 0)
        total += reading.active_adc.astype(np.float64)
        print(
            f"\r  Sampling {sample_index + 1:04d}/{sample_count}: "
            f"ADC average [{format_adc_channels(total / (sample_index + 1))}]",
            end="",
            flush=True,
        )
    print()
    return total / sample_count


def replace_invalid_coefficients(calibration: NDArray[np.float64]) -> NDArray[np.float64]:
    calibration = calibration.copy()
    for channel_index in range(calibration.shape[1]):
        if not np.isfinite(calibration[:, channel_index]).all():
            print(f"• Channel {channel_index}: replacing invalid calibration coefficients with zeros")
            calibration[:, channel_index] = 0.0
    return calibration


async def async_input(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


def format_channels(values: NDArray[np.float64]) -> str:
    return ", ".join(f"{value:+08.2f}" for value in values)


def format_adc_channels(values: NDArray[np.float64]) -> str:
    return ", ".join(f"{value:.1f}" for value in values)


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
