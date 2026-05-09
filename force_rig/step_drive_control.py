#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import dataclasses
import logging
import struct
from enum import IntEnum
from typing import Self

import serial

LOGGER = logging.getLogger(__name__)


class StepDirection(IntEnum):
    UP = -1
    STOP = 0
    DOWN = 1


class StepSpeed(IntEnum):
    SLOW = 0
    FAST = 1


@dataclasses.dataclass(frozen=True)
class StepDriveCommand:
    step: StepDirection
    speed: StepSpeed = StepSpeed.SLOW

    @classmethod
    def from_payload(cls, payload: memoryview) -> Self | None:
        if len(payload) >= StepDriveControl.COMMAND_STRUCT.size:
            step, speed = StepDriveControl.COMMAND_STRUCT.unpack_from(payload)
        elif len(payload) >= StepDriveControl.LEGACY_COMMAND_STRUCT.size:
            (step,) = StepDriveControl.LEGACY_COMMAND_STRUCT.unpack_from(payload)
            speed = StepSpeed.SLOW
        else:
            return None

        try:
            return cls(step=StepDirection(step), speed=StepSpeed(speed))
        except ValueError:
            LOGGER.warning("Ignoring invalid step-drive echo: step=%s speed=%s", step, speed)
            return None

    def to_payload(self) -> bytes:
        return StepDriveControl.COMMAND_STRUCT.pack(int(self.step), int(self.speed))


@dataclasses.dataclass(frozen=True)
class Packet:
    payload: memoryview

    MAGIC_INT = 0xF2EC4CB4
    MAGIC_BYTES = MAGIC_INT.to_bytes(4, "little")
    HEADER_STRUCT = struct.Struct("< L B 3x")
    CRC_SIZE = 2
    MAX_PAYLOAD_SIZE = 255

    @classmethod
    def parse(cls, data: bytes | bytearray | memoryview) -> tuple[bytes, Packet | None]:
        buffer = bytes(data)
        magic_size = len(cls.MAGIC_BYTES)

        while len(buffer) >= cls.HEADER_STRUCT.size:
            magic_index = buffer.find(cls.MAGIC_BYTES)
            if magic_index < 0:
                return buffer[-(magic_size - 1) :], None
            if magic_index:
                buffer = buffer[magic_index:]
            if len(buffer) < cls.HEADER_STRUCT.size:
                return buffer, None

            _, payload_size = cls.HEADER_STRUCT.unpack_from(buffer)
            packet_size = cls.HEADER_STRUCT.size + payload_size + cls.CRC_SIZE
            if len(buffer) < packet_size:
                return buffer, None

            payload_start = cls.HEADER_STRUCT.size
            payload_end = payload_start + payload_size
            payload = buffer[payload_start:payload_end]
            crc = buffer[payload_end:packet_size]
            remainder = buffer[packet_size:]

            if crc16_ccitt_false(payload) == int.from_bytes(crc, "big"):
                return remainder, cls(memoryview(payload))

            LOGGER.debug("Discarding packet with invalid CRC")
            buffer = buffer[magic_size:]

        return buffer, None

    def compile(self) -> bytes:
        if len(self.payload) > self.MAX_PAYLOAD_SIZE:
            raise ValueError(f"Payload too large: {len(self.payload)} > {self.MAX_PAYLOAD_SIZE} bytes")
        payload = bytes(self.payload)
        return b"".join(
            (
                self.HEADER_STRUCT.pack(self.MAGIC_INT, len(payload)),
                payload,
                crc16_ccitt_false(payload).to_bytes(2, "big"),
            )
        )


class StepDriveControl:
    BAUD = 38400
    COMMAND_STRUCT = struct.Struct("< i i")
    LEGACY_COMMAND_STRUCT = struct.Struct("< i")

    def __init__(self, port: serial.SerialBase) -> None:
        self._port = port
        self._backlog = b""
        if not self._port.is_open:
            self._port.open()

    def close(self) -> None:
        self._port.close()

    async def up(self, speed: str | StepSpeed = StepSpeed.SLOW) -> None:
        await self.send(StepDriveCommand(StepDirection.UP, normalize_speed(speed)))

    async def down(self, speed: str | StepSpeed = StepSpeed.SLOW) -> None:
        await self.send(StepDriveCommand(StepDirection.DOWN, normalize_speed(speed)))

    async def stop(self) -> None:
        await self.send(StepDriveCommand(StepDirection.STOP, StepSpeed.SLOW))

    async def send(
        self,
        command: StepDriveCommand,
        *,
        timeout_s: float = 2.0,
        retry_interval_s: float = 0.25,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if retry_interval_s <= 0:
            raise ValueError("retry_interval_s must be positive")

        await self.flush()
        deadline = asyncio.get_running_loop().time() + timeout_s
        packet = Packet(memoryview(command.to_payload())).compile()

        while True:
            await asyncio.to_thread(self._port.write, packet)
            retry_deadline = min(deadline, asyncio.get_running_loop().time() + retry_interval_s)

            while asyncio.get_running_loop().time() <= retry_deadline:
                echoed = await self.fetch(timeout_s=0.05)
                if echoed == command:
                    return

            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for step-drive acknowledgement: {command}")

    async def fetch(self, timeout_s: float) -> StepDriveCommand | None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            packet = await self._read_one_packet()
            if packet is not None:
                command = StepDriveCommand.from_payload(packet.payload)
                if command is not None:
                    return command

            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.001)

    async def flush(self) -> None:
        await self._read_serial_data()
        self._backlog = b""

    async def _read_one_packet(self) -> Packet | None:
        await self._read_serial_data()
        self._backlog, packet = Packet.parse(self._backlog)
        return packet

    async def _read_serial_data(self) -> None:
        self._port.timeout = 0
        chunk = await asyncio.to_thread(self._port.read_all)
        if chunk:
            self._backlog += chunk


def normalize_speed(speed: str | StepSpeed) -> StepSpeed:
    if isinstance(speed, StepSpeed):
        return speed
    try:
        return StepSpeed[speed.upper()]
    except KeyError as ex:
        valid = ", ".join(value.name.lower() for value in StepSpeed)
        raise ValueError(f"Invalid speed {speed!r}; expected one of: {valid}") from ex


def crc16_ccitt_false(data: bytes | bytearray | memoryview) -> int:
    value = 0xFFFF
    for byte in data:
        value ^= byte << 8
        for _ in range(8):
            if value & 0x8000:
                value = ((value << 1) ^ 0x1021) & 0xFFFF
            else:
                value = (value << 1) & 0xFFFF
    return value
