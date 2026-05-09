from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Awaitable, Callable, Sequence

import numpy as np
import serial
from fluxgrip_interface import FluxGripConfig, FluxGripInterface
from force_sensor_interface import ForceSensorInterface
from setup_serial_links import FORCE_SENSOR_PORT, STEP_DRIVE_PORT
from step_drive_client import DRIVE_DIRECTION_IS_INVERTED
from step_drive_control import StepDriveControl

ProgressCallback = Callable[[str, float, float, float], None]
PhaseCallback = Callable[[str], None]


@dataclasses.dataclass(frozen=True)
class ForceRigConfig:
    force_port: str = FORCE_SENSOR_PORT
    drive_port: str = STEP_DRIVE_PORT
    touch_force_n: float = 0.3
    touch_timeout_s: float = 30.0
    pull_timeout_s: float = 15.0
    tare_samples: int = 50
    sample_period_s: float = 0.02
    max_pull_force_n: float = 10.0
    settle_after_touch_s: float = 0.5
    settle_after_demag_s: float = 1.0
    magnetized_hold_s: float = 1.0
    fluxgrip: FluxGripConfig = dataclasses.field(default_factory=FluxGripConfig)


@dataclasses.dataclass(frozen=True)
class RemainingForceResult:
    touchdown_force_n: float
    peak_remaining_force_n: float
    detached: bool
    pull_elapsed_s: float
    recovery_performed: bool = False


class PullForceSafetyError(RuntimeError):
    def __init__(self, peak_force_n: float, limit_n: float, elapsed_s: float) -> None:
        self.peak_force_n = peak_force_n
        self.limit_n = limit_n
        self.elapsed_s = elapsed_s
        super().__init__(
            f"Pull force safety limit exceeded: {peak_force_n:+.3f} N "
            f"(limit {limit_n:.3f} N)"
        )


class ForceRigInterface:
    def __init__(
        self,
        config: ForceRigConfig | None = None,
        *,
        progress: ProgressCallback | None = None,
        phase: PhaseCallback | None = None,
    ) -> None:
        self.config = config or ForceRigConfig()
        self._progress = progress
        self._phase = phase
        self._force_sensor: ForceSensorInterface | None = None
        self._step_drive: StepDriveControl | None = None
        self._fluxgrip: FluxGripInterface | None = None

    async def __aenter__(self) -> ForceRigInterface:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._force_sensor is not None:
            return

        force_port = serial.serial_for_url(
            self.config.force_port,
            baudrate=ForceSensorInterface.BAUD,
            dsrdtr=None,
            rtscts=None,
        )
        drive_port = serial.serial_for_url(
            self.config.drive_port,
            baudrate=StepDriveControl.BAUD,
            dsrdtr=None,
            rtscts=None,
        )
        self._force_sensor = ForceSensorInterface(force_port)
        self._step_drive = StepDriveControl(drive_port)
        self._fluxgrip = FluxGripInterface(self.config.fluxgrip)
        await self._step_drive.stop()

    async def close(self) -> None:
        try:
            if self._step_drive is not None:
                await self._step_drive.stop()
        finally:
            if self._force_sensor is not None:
                self._force_sensor.close()
            if self._step_drive is not None:
                self._step_drive.close()
            if self._fluxgrip is not None:
                self._fluxgrip.close()
            self._force_sensor = None
            self._step_drive = None
            self._fluxgrip = None

    async def measure_remaining_force(
        self, demag_values: Sequence[int] | None = None
    ) -> RemainingForceResult:
        force_sensor = self._require_force_sensor()
        fluxgrip = self._require_fluxgrip()

        self._emit_phase("tare-before-touchdown")
        await self.tare_force_sensors()
        self._emit_phase("touchdown")
        touchdown_force = await self.move_down_until_touch()

        if self.config.settle_after_touch_s > 0:
            self._emit_phase("settle-after-touchdown")
            await asyncio.sleep(self.config.settle_after_touch_s)

        self._emit_phase("fluxgrip-connect")
        await fluxgrip.start()
        if demag_values is not None:
            self._emit_phase("set-demag-values")
            await fluxgrip.set_demag_values(demag_values)

        self._emit_phase("magnetize")
        await fluxgrip.magnetize()

        if self.config.magnetized_hold_s > 0:
            self._emit_phase("magnetized-hold")
            await asyncio.sleep(self.config.magnetized_hold_s)

        self._emit_phase("demagnetize")
        await fluxgrip.demagnetize()

        if self.config.settle_after_demag_s > 0:
            self._emit_phase("settle-after-demag")
            await asyncio.sleep(self.config.settle_after_demag_s)

        self._emit_phase("tare-before-pull")
        await force_sensor.tare(sample_count=self.config.tare_samples)
        self._emit_phase("pull-up")
        recovery_performed = False
        try:
            peak_force, detached, elapsed_s = await self.move_up_and_record_peak()
        except PullForceSafetyError:
            recovery_performed = True
            await self.recover_from_excess_pull_force()
            self._emit_phase("pull-up-retry")
            peak_force, detached, elapsed_s = await self.move_up_and_record_peak()
        return RemainingForceResult(
            touchdown_force_n=touchdown_force,
            peak_remaining_force_n=peak_force,
            detached=detached,
            pull_elapsed_s=elapsed_s,
            recovery_performed=recovery_performed,
        )

    async def recover_from_excess_pull_force(self) -> None:
        force_sensor = self._require_force_sensor()
        fluxgrip = self._require_fluxgrip()

        self._emit_phase("pull-force-safety")
        self._emit_phase("factory-reset")
        await fluxgrip.factory_reset()

        self._emit_phase("recovery-magnetize")
        await fluxgrip.magnetize()

        if self.config.magnetized_hold_s > 0:
            self._emit_phase("recovery-magnetized-hold")
            await asyncio.sleep(self.config.magnetized_hold_s)

        self._emit_phase("recovery-demagnetize")
        await fluxgrip.demagnetize()

        if self.config.settle_after_demag_s > 0:
            self._emit_phase("recovery-settle-after-demag")
            await asyncio.sleep(self.config.settle_after_demag_s)

        self._emit_phase("tare-before-pull-retry")
        await force_sensor.tare(sample_count=self.config.tare_samples)

    async def factory_reset_fluxgrip(self) -> None:
        fluxgrip = self._require_fluxgrip()
        self._emit_phase("fluxgrip-connect")
        await fluxgrip.start()
        self._emit_phase("factory-reset")
        await fluxgrip.factory_reset()

    async def tare_force_sensors(self) -> None:
        await self._require_force_sensor().tare(sample_count=self.config.tare_samples)

    async def move_down_until_touch(self) -> float:
        step_drive = self._require_step_drive()
        start_time = time.monotonic()
        await self._physical_move("down")("slow")
        try:
            while True:
                total_force = await self.total_force_n()
                elapsed_s = time.monotonic() - start_time
                self._emit_progress("down", elapsed_s, total_force, 0.0)
                if force_reached_threshold(total_force, self.config.touch_force_n):
                    return total_force
                if elapsed_s >= self.config.touch_timeout_s:
                    raise TimeoutError(
                        f"Touchdown force threshold not reached within {self.config.touch_timeout_s:.3f} s "
                        f"(last force {total_force:+.3f} N)"
                    )
                await asyncio.sleep(self.config.sample_period_s)
        finally:
            await step_drive.stop()

    async def move_up_and_record_peak(self) -> tuple[float, bool, float]:
        step_drive = self._require_step_drive()
        start_time = time.monotonic()
        peak_negative_force = 0.0

        await self._physical_move("up")("slow")
        try:
            while True:
                total_force = await self.total_force_n()
                elapsed_s = time.monotonic() - start_time
                if total_force < peak_negative_force:
                    peak_negative_force = total_force

                self._emit_progress("up", elapsed_s, total_force, peak_negative_force)

                if abs(peak_negative_force) >= self.config.max_pull_force_n:
                    raise PullForceSafetyError(
                        peak_force_n=peak_negative_force,
                        limit_n=self.config.max_pull_force_n,
                        elapsed_s=elapsed_s,
                    )
                if elapsed_s >= self.config.pull_timeout_s:
                    return peak_negative_force, True, elapsed_s
                await asyncio.sleep(self.config.sample_period_s)
        finally:
            await step_drive.stop()

    async def total_force_n(self) -> float:
        return float(np.sum(await self._require_force_sensor().get_forces(flush=True)))

    def _physical_move(self, direction: str) -> Callable[[str], Awaitable[None]]:
        control = self._require_step_drive()
        if direction not in {"up", "down"}:
            raise ValueError(f"Unsupported move direction: {direction}")
        if not DRIVE_DIRECTION_IS_INVERTED:
            return control.up if direction == "up" else control.down
        return control.down if direction == "up" else control.up

    def _emit_progress(
        self, phase: str, elapsed_s: float, force_n: float, peak_force_n: float
    ) -> None:
        if self._progress is not None:
            self._progress(phase, elapsed_s, force_n, peak_force_n)

    def _emit_phase(self, phase: str) -> None:
        if self._phase is not None:
            self._phase(phase)

    def _require_force_sensor(self) -> ForceSensorInterface:
        if self._force_sensor is None:
            raise RuntimeError("ForceRigInterface.start() has not been called")
        return self._force_sensor

    def _require_step_drive(self) -> StepDriveControl:
        if self._step_drive is None:
            raise RuntimeError("ForceRigInterface.start() has not been called")
        return self._step_drive

    def _require_fluxgrip(self) -> FluxGripInterface:
        if self._fluxgrip is None:
            raise RuntimeError("ForceRigInterface.start() has not been called")
        return self._fluxgrip


def force_reached_threshold(force_n: float, threshold_n: float) -> bool:
    if threshold_n >= 0:
        return force_n >= threshold_n
    return force_n <= threshold_n
