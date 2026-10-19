#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import dataclasses
import logging
import struct
from typing import Generic, TypeVar

import numpy as np
import serial
from numpy.typing import NDArray

from step_drive_control import Packet

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class MovingAverage(Generic[T]):
    def __init__(self, depth: int, initial: T) -> None:
        if depth <= 0:
            raise ValueError("Moving average depth must be positive")
        self._values = [initial] * depth
        self._sum = sum(self._values)  # type: ignore[arg-type]
        self._index = 0

    def __call__(self, value: T) -> T:
        self._sum -= self._values[self._index]  # type: ignore[operator]
        self._values[self._index] = value
        self._sum += value  # type: ignore[operator]
        self._index = (self._index + 1) % len(self._values)
        return self._sum * (1 / len(self._values))  # type: ignore[operator]


@dataclasses.dataclass(frozen=True)
class ForceSensorReading:
    seq_num: int
    raw_adc: NDArray[np.int32]
    calibration: NDArray[np.float64]

    ACTIVE_CHANNEL_COUNT = 2
    RAW_CHANNEL_COUNT = 4
    CALIBRATION_COEFFICIENT_COUNT = 2

    @property
    def active_adc(self) -> NDArray[np.int32]:
        return self.raw_adc[: self.ACTIVE_CHANNEL_COUNT]


class ForceSensorInterface:
    BAUD = 38400
    READING_STRUCT = struct.Struct("< Q 8x 8x 16s 40s")
    CALIBRATION_SHAPE = (
        ForceSensorReading.CALIBRATION_COEFFICIENT_COUNT,
        ForceSensorReading.ACTIVE_CHANNEL_COUNT,
    )

    def __init__(self, port: serial.SerialBase) -> None:
        self._port = port
        self._backlog = b""
        self._zero_bias = np.zeros(ForceSensorReading.ACTIVE_CHANNEL_COUNT, dtype=np.float64)
        if not self._port.is_open:
            self._port.open()

    def close(self) -> None:
        self._port.close()

    async def fetch(self, *, flush: bool = False, timeout_s: float = 10.0) -> ForceSensorReading:
        if flush:
            await self.flush()
        reading = await self.read(timeout_s=timeout_s)
        if reading is None:
            raise TimeoutError(f"Timed out waiting for force-sensor data after {timeout_s:.3f} s")
        return reading

    async def read(self, *, timeout_s: float) -> ForceSensorReading | None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            packet = await self._read_one_packet()
            if packet is not None:
                return self.parse_reading(packet.payload)

            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.001)

    async def flush(self) -> None:
        await self._read_serial_data()
        self._backlog = b""

    async def tare(self, *, sample_count: int = 50) -> NDArray[np.float64]:
        if sample_count <= 0:
            raise ValueError("sample_count must be positive")

        total = np.zeros(ForceSensorReading.ACTIVE_CHANNEL_COUNT, dtype=np.float64)
        for sample_index in range(sample_count):
            reading = await self.fetch(flush=sample_index == 0)
            total += self.compute_forces(reading)
        self._zero_bias = total / sample_count
        LOGGER.info("Zero bias: %s N", self._zero_bias)
        return self._zero_bias.copy()

    async def get_forces(self, *, flush: bool = True) -> NDArray[np.float64]:
        reading = await self.fetch(flush=flush)
        return self.compute_forces(reading) - self._zero_bias

    async def write_calibration(
        self,
        calibration: NDArray[np.float64],
        *,
        confirmation_timeout_s: float = 10.0,
    ) -> bool:
        calibration = normalize_calibration(calibration)
        payload = calibration.astype("<f4").tobytes()
        packet = Packet(memoryview(payload)).compile()

        LOGGER.debug("Writing calibration packet: %s", packet.hex())
        await asyncio.to_thread(self._port.write, packet)
        await asyncio.sleep(1.0)
        await self.flush()

        reading = await self.read(timeout_s=confirmation_timeout_s)
        if reading is None:
            return False
        return np.allclose(reading.calibration, calibration, atol=1e-3, rtol=1e-3, equal_nan=True)

    async def _read_one_packet(self) -> Packet | None:
        await self._read_serial_data()
        self._backlog, packet = Packet.parse(self._backlog)
        return packet

    async def _read_serial_data(self) -> None:
        self._port.timeout = 0
        chunk = await asyncio.to_thread(self._port.read_all)
        if chunk:
            self._backlog += chunk

    @classmethod
    def parse_reading(cls, payload: memoryview) -> ForceSensorReading:
        if len(payload) < cls.READING_STRUCT.size:
            raise ValueError(f"Malformed force-sensor payload: {len(payload)} bytes")

        seq_num, raw_adc_bytes, calibration_bytes = cls.READING_STRUCT.unpack_from(payload)
        raw_adc = np.frombuffer(raw_adc_bytes, dtype="<i4", count=ForceSensorReading.RAW_CHANNEL_COUNT).copy()
        calibration = np.frombuffer(
            calibration_bytes,
            dtype="<f4",
            count=ForceSensorReading.ACTIVE_CHANNEL_COUNT * ForceSensorReading.CALIBRATION_COEFFICIENT_COUNT,
        ).astype(np.float64)
        return ForceSensorReading(
            seq_num=int(seq_num),
            raw_adc=raw_adc,
            calibration=calibration.reshape(cls.CALIBRATION_SHAPE),
        )

    @staticmethod
    def compute_forces(reading: ForceSensorReading) -> NDArray[np.float64]:
        calibration = normalize_calibration(reading.calibration)
        slopes = calibration[0]
        offsets = calibration[1]
        return slopes * reading.active_adc.astype(np.float64) + offsets


def normalize_calibration(calibration: NDArray[np.float64]) -> NDArray[np.float64]:
    calibration = np.asarray(calibration, dtype=np.float64)
    expected_shape = ForceSensorInterface.CALIBRATION_SHAPE
    if calibration.shape != expected_shape:
        raise ValueError(f"Calibration shape must be {expected_shape}, got {calibration.shape}")
    return calibration
