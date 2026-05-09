#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import importlib
import logging
import os
import re
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pycyphal

LOGGER = logging.getLogger(__name__)

DEFAULT_CONTROLLER_NODE_ID = 1
DEFAULT_TARGET_NODE_ID: int | None = None
EXPECTED_FEEDBACK_SUBJECT_ID = 1000
EXPECTED_COMMAND_SUBJECT_ID = 1001
DEFAULT_COMMAND_SUBJECT_ID = EXPECTED_COMMAND_SUBJECT_ID
DEFAULT_FEEDBACK_SUBJECT_ID = EXPECTED_FEEDBACK_SUBJECT_ID
DEFAULT_BITRATE = 1_000_000
DEFAULT_NODE_NAME = "com.zubax.fluxgrip"
DEMAG_VALUE_COUNT = 64
DEMAG_REGISTER_NAME = "magnet.demagnetization.pulse_pct"
NODE_ID_REGISTER_NAME = "uavcan.node.id"
FEEDBACK_SUBJECT_REGISTER_NAME = "uavcan.pub.feedback.id"
COMMAND_SUBJECT_REGISTER_NAME = "uavcan.sub.command.id"
COMMAND_DEMAGNETIZE = 0
COMMAND_MAGNETIZE = 1
REMAGNETIZATION_IDLE = 0


@dataclasses.dataclass(frozen=True)
class FluxGripConfig:
    controller_node_id: int = DEFAULT_CONTROLLER_NODE_ID
    target_node_id: int | None = DEFAULT_TARGET_NODE_ID
    command_subject_id: int = DEFAULT_COMMAND_SUBJECT_ID
    feedback_subject_id: int = DEFAULT_FEEDBACK_SUBJECT_ID
    bitrate: int = DEFAULT_BITRATE
    can_iface: str | None = None
    can_iface_index: int = 0
    node_name: str = DEFAULT_NODE_NAME


@dataclasses.dataclass(frozen=True)
class ApplicationTypes:
    make_transport: Any
    make_node: Any
    NodeTracker: Any
    Natural16: Any
    Natural32: Any
    Value: Any
    ValueProxy: Any
    ValueProxyWithFlags: Any
    CentralizedAllocator: Any


@dataclasses.dataclass(frozen=True)
class DsdlTypes:
    GetInfo_1: Any
    ExecuteCommand_1: Any
    Integer32_1: Any
    Integer8_1: Any
    Access_1: Any
    List_1: Any
    Name_1: Any
    Feedback_0: Any


class RegisterProxy(collections.abc.Mapping[str, Any]):
    _VALID_NAME = re.compile(r"^[a-z_]+(\.\w+)+[<=>]?$")

    def __init__(self, local_node: Any, remote_node_id: int, dsdl: DsdlTypes, app: ApplicationTypes) -> None:
        self._cache: dict[str, Any] = {}
        self._dsdl = dsdl
        self._app = app
        self._access_client = local_node.make_client(dsdl.Access_1, remote_node_id)
        self._list_client = local_node.make_client(dsdl.List_1, remote_node_id)

    async def reload(self) -> None:
        names = await self._list_names()
        self._cache.clear()
        for name in sorted(names):
            await self.read(name)
        LOGGER.debug("Loaded %d FluxGrip registers", len(self._cache))

    async def read(self, name: str) -> Any:
        return await self.write(name, self._app.Value())

    async def write(self, name: str, value: Any) -> Any:
        if name in self._cache:
            value_with_flags = self._cache[name]
            value_with_flags.assign(value)
            register_value = value_with_flags.value
        else:
            register_value = self._app.ValueProxy(value).value

        request = self._dsdl.Access_1.Request(name=self._dsdl.Name_1(name), value=register_value)
        response = await self._access_client(request)
        if not response:
            raise TimeoutError(f"Register access timed out: {name}")

        result = self._app.ValueProxyWithFlags(response.value, response.mutable, response.persistent)
        if not result.value.empty:
            self._cache[name] = result
        return result

    async def _list_names(self) -> set[str]:
        names: set[str] = set()
        for index in range(2**16):
            response = await self._list_client(self._dsdl.List_1.Request(index))
            if not response:
                raise TimeoutError(f"Register list timed out at index {index}")

            name = response.name.name.tobytes().decode()
            if not name:
                return names
            if not self._VALID_NAME.match(name):
                raise RuntimeError(f"FluxGrip reported invalid register name: {name!r}")
            names.add(name)
        raise RuntimeError("FluxGrip register list did not terminate")

    def __getitem__(self, key: str) -> Any:
        return self._cache[key]

    def __len__(self) -> int:
        return len(self._cache)

    def __iter__(self) -> Iterator[str]:
        return iter(self._cache)


class FluxGripInterface:
    def __init__(self, config: FluxGripConfig | None = None) -> None:
        self.config = config or FluxGripConfig()
        self._app: ApplicationTypes | None = None
        self._dsdl: DsdlTypes | None = None
        self._controller_node: Any | None = None
        self._node_tracker: Any | None = None
        self._command_publisher: Any | None = None
        self._feedback_subscriber: Any | None = None
        self._registers: RegisterProxy | None = None
        self._target_node_id: int | None = None
        self._node_id_allocator: Any | None = None

    async def __aenter__(self) -> FluxGripInterface:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    async def start(self) -> None:
        if self._controller_node is not None:
            return

        self._app = load_application_types()
        self._dsdl = load_dsdl_types()
        can_iface = resolve_can_iface(self.config.can_iface, self.config.can_iface_index)
        LOGGER.info("Using Cyphal CAN interface: %s", can_iface)

        register_values = {
            "uavcan.can.iface": self._app.ValueProxy(can_iface),
            "uavcan.can.bitrate": self._app.ValueProxy(self._app.Natural32([self.config.bitrate, self.config.bitrate])),
            "uavcan.can.mtu": self._app.ValueProxy(self._app.Natural16([8])),
            "uavcan.node.id": self._app.ValueProxy(self._app.Natural16([self.config.controller_node_id])),
        }
        transport = self._app.make_transport(register_values)
        self._controller_node = self._app.make_node(
            info=self._dsdl.GetInfo_1.Response(name="org.opencyphal.force_rig.controller"),
            transport=transport,
            reconfigurable_transport=True,
        )
        self._node_tracker = self._app.NodeTracker(self._controller_node)
        self._node_tracker.get_info_timeout = 1.0
        self._controller_node.start()

        self._target_node_id = await self.wait_until_online_or_allocate(timeout_s=60.0)

        self._registers = RegisterProxy(self._controller_node, self._target_node_id, self._dsdl, self._app)
        await self._registers.reload()
        await self.ensure_node_id_register()
        await self.ensure_subject_registers()
        self._command_publisher = self._controller_node.make_publisher(
            self._dsdl.Integer8_1,
            self.config.command_subject_id,
        )
        self._feedback_subscriber = self._controller_node.make_subscriber(
            self._dsdl.Feedback_0,
            self.config.feedback_subject_id,
        )

    async def ensure_subject_registers(self, *, restart_if_updated: bool = True) -> bool:
        app = self._require_app()
        registers = self._require_registers()
        required_registers = (
            (FEEDBACK_SUBJECT_REGISTER_NAME, EXPECTED_FEEDBACK_SUBJECT_ID),
            (COMMAND_SUBJECT_REGISTER_NAME, EXPECTED_COMMAND_SUBJECT_ID),
        )
        updates: list[tuple[str, int, int]] = []

        if self.config.feedback_subject_id != EXPECTED_FEEDBACK_SUBJECT_ID:
            raise ValueError(
                f"feedback_subject_id must be {EXPECTED_FEEDBACK_SUBJECT_ID}, "
                f"got {self.config.feedback_subject_id}"
            )
        if self.config.command_subject_id != EXPECTED_COMMAND_SUBJECT_ID:
            raise ValueError(
                f"command_subject_id must be {EXPECTED_COMMAND_SUBJECT_ID}, "
                f"got {self.config.command_subject_id}"
            )

        for name, expected_value in required_registers:
            register_value = await registers.read(name)
            current_value = register_value_as_int(name, register_value)
            LOGGER.info("FluxGrip register %s=%d", name, current_value)
            if current_value == expected_value:
                continue
            if not register_value.mutable:
                raise RuntimeError(
                    f"FluxGrip register {name} is not mutable "
                    f"(current {current_value}, required {expected_value})"
                )
            await registers.write(name, app.Natural16([expected_value]))
            updates.append((name, current_value, expected_value))

        if not updates:
            return False

        LOGGER.info(
            "Updated FluxGrip subject register(s): %s",
            ", ".join(f"{name}: {old}->{new}" for name, old, new in updates),
        )
        if not restart_if_updated:
            return True

        LOGGER.info("Restarting FluxGrip after subject register update")
        await self.restart()

        registers = self._require_registers()
        for name, expected_value in required_registers:
            register_value = await registers.read(name)
            current_value = register_value_as_int(name, register_value)
            if current_value != expected_value:
                raise RuntimeError(
                    f"FluxGrip register {name} is still {current_value} after restart; "
                    f"expected {expected_value}"
                )
        return True

    async def ensure_node_id_register(self, *, restart_if_updated: bool = True) -> bool:
        app = self._require_app()
        registers = self._require_registers()
        expected_value = self._require_target_node_id()

        register_value = await registers.read(NODE_ID_REGISTER_NAME)
        current_value = register_value_as_int(NODE_ID_REGISTER_NAME, register_value)
        LOGGER.info("FluxGrip register %s=%d", NODE_ID_REGISTER_NAME, current_value)
        if current_value == expected_value:
            return False
        if not register_value.mutable:
            raise RuntimeError(
                f"FluxGrip register {NODE_ID_REGISTER_NAME} is not mutable "
                f"(current {current_value}, required {expected_value})"
            )

        await registers.write(NODE_ID_REGISTER_NAME, app.Natural16([expected_value]))
        LOGGER.info(
            "Updated FluxGrip node-ID register: %s: %d->%d",
            NODE_ID_REGISTER_NAME,
            current_value,
            expected_value,
        )
        if not restart_if_updated:
            return True

        LOGGER.info("Restarting FluxGrip after node-ID register update")
        await self.restart()
        register_value = await self._require_registers().read(NODE_ID_REGISTER_NAME)
        current_value = register_value_as_int(NODE_ID_REGISTER_NAME, register_value)
        if current_value != expected_value:
            raise RuntimeError(
                f"FluxGrip register {NODE_ID_REGISTER_NAME} is still {current_value} after restart; "
                f"expected {expected_value}"
            )
        return True

    def close(self) -> None:
        if self._controller_node is not None:
            self._controller_node.close()
        self._controller_node = None
        self._node_tracker = None
        self._command_publisher = None
        self._feedback_subscriber = None
        self._registers = None
        self._target_node_id = None
        self._node_id_allocator = None

    async def wait_until_online_or_allocate(self, *, timeout_s: float) -> int:
        self.start_node_id_allocator("FluxGrip discovery is starting")
        try:
            return await self.wait_until_online(timeout_s=timeout_s)
        except TimeoutError:
            raise

    def start_node_id_allocator(self, reason: str) -> None:
        if self._node_id_allocator is not None:
            return
        LOGGER.info(
            "Starting a temporary Cyphal PnP node-ID allocator because %s",
            reason,
        )
        self._node_id_allocator = self._require_app().CentralizedAllocator(self._require_controller_node())

    async def wait_until_online(self, *, timeout_s: float) -> int:
        node_tracker = self._require_node_tracker()
        deadline = time.monotonic() + timeout_s

        while time.monotonic() <= deadline:
            matching_node_ids: list[int] = []
            for node_id, node in node_tracker.registry.items():
                node_info = node.info
                if node_info is None:
                    continue
                node_name = node_info.name.tobytes().decode()
                if node_name != self.config.node_name:
                    continue
                matching_node_ids.append(int(node_id))

            if self.config.target_node_id is not None:
                if self.config.target_node_id in matching_node_ids:
                    LOGGER.info("FluxGrip online at node %d", self.config.target_node_id)
                    return self.config.target_node_id
                if matching_node_ids:
                    LOGGER.info(
                        "Ignoring FluxGrip node(s) %s while waiting for configured target node %d",
                        ", ".join(map(str, sorted(matching_node_ids))),
                        self.config.target_node_id,
                    )
            elif len(matching_node_ids) == 1:
                target_node_id = matching_node_ids[0]
                LOGGER.info("FluxGrip online at node %d", target_node_id)
                return target_node_id
            elif len(matching_node_ids) > 1:
                raise RuntimeError(
                    f"Found multiple {self.config.node_name} nodes: {', '.join(map(str, sorted(matching_node_ids)))}. "
                    "Pass --target-node-id to select one."
                )
            await asyncio.sleep(0.5)

        online = ", ".join(str(node_id) for node_id in sorted(node_tracker.registry)) or "none"
        target = self.config.target_node_id if self.config.target_node_id is not None else "any"
        raise TimeoutError(
            f"Timed out waiting for {self.config.node_name} at node {target}; "
            f"online node IDs: {online}"
        )

    async def set_demag_values(self, values: Sequence[int]) -> None:
        dsdl = self._require_dsdl()
        registers = self._require_registers()

        demag_values = normalize_demag_values(values)
        message = dsdl.Integer32_1(np.array(demag_values, dtype=np.int32))
        LOGGER.info("Writing %s (%d values)", DEMAG_REGISTER_NAME, len(demag_values))
        await registers.write(DEMAG_REGISTER_NAME, message)

        await self.restart()

    async def factory_reset(self) -> None:
        dsdl = self._require_dsdl()
        await self._execute_command(
            command=int(dsdl.ExecuteCommand_1.Request.COMMAND_FACTORY_RESET),
            name="factory reset",
        )
        await self._require_registers().reload()
        await self.ensure_node_id_register(restart_if_updated=False)
        await self.ensure_subject_registers(restart_if_updated=False)
        LOGGER.info("Restarting FluxGrip after factory reset register updates")
        await self.restart(allocate=True)
        await self.ensure_node_id_register()
        await self.ensure_subject_registers()

    async def restart(self, *, allocate: bool = False) -> None:
        dsdl = self._require_dsdl()
        await self._execute_command(
            command=int(dsdl.ExecuteCommand_1.Request.COMMAND_RESTART),
            name="restart",
        )
        if allocate:
            self.start_node_id_allocator("FluxGrip has just been factory reset")
        await asyncio.sleep(5.0)
        await self._refresh_target_after_restart(allocate=allocate)

    async def _execute_command(self, *, command: int, name: str) -> None:
        dsdl = self._require_dsdl()
        controller_node = self._require_controller_node()
        command_client = controller_node.make_client(dsdl.ExecuteCommand_1, self._require_target_node_id())
        response, _ = await command_client.call(dsdl.ExecuteCommand_1.Request(command=command))
        if response is None:
            raise TimeoutError(f"Timed out while asking FluxGrip to {name}")
        if response.status != dsdl.ExecuteCommand_1.Response.STATUS_SUCCESS:
            raise RuntimeError(f"FluxGrip {name} command failed with status {response.status}")

    async def _refresh_target_after_restart(self, *, allocate: bool = False) -> None:
        old_target_node_id = self._require_target_node_id()
        if allocate:
            self._target_node_id = await self.wait_until_online(timeout_s=60.0)
        else:
            self._target_node_id = await self.wait_until_online(timeout_s=30.0)
        if self._target_node_id != old_target_node_id or self._registers is None:
            self._registers = RegisterProxy(
                self._require_controller_node(),
                self._target_node_id,
                self._require_dsdl(),
                self._require_app(),
            )
        await self._registers.reload()

    async def magnetize(self, *, timeout_s: float = 10.0) -> None:
        await self._command_and_wait(
            command=COMMAND_MAGNETIZE,
            target_magnetized=True,
            timeout_s=timeout_s,
            action_name="magnetize",
        )

    async def demagnetize(self, *, timeout_s: float = 60.0) -> None:
        await self._command_and_wait(
            command=COMMAND_DEMAGNETIZE,
            target_magnetized=False,
            timeout_s=timeout_s,
            action_name="demagnetize",
        )

    async def _command_and_wait(
        self,
        *,
        command: int,
        target_magnetized: bool,
        timeout_s: float,
        action_name: str,
    ) -> None:
        dsdl = self._require_dsdl()
        publisher = self._require_command_publisher()

        await self._drain_feedback()
        feedback = await self._latest_feedback(timeout_s=20.0)
        if bool(feedback.magnetized) == target_magnetized and int(feedback.remagnetization_state) == REMAGNETIZATION_IDLE:
            LOGGER.info("FluxGrip is already %s", "magnetized" if target_magnetized else "demagnetized")
            return

        if not await publisher.publish(dsdl.Integer8_1(value=command)):
            raise RuntimeError(f"Failed to publish FluxGrip {action_name} command")

        deadline = time.monotonic() + timeout_s
        while time.monotonic() <= deadline:
            feedback = await self._latest_feedback(timeout_s=min(5.0, max(0.1, deadline - time.monotonic())))
            if bool(feedback.magnetized) == target_magnetized and int(feedback.remagnetization_state) == REMAGNETIZATION_IDLE:
                LOGGER.info("FluxGrip %s complete", action_name)
                return
            await self._drain_feedback()

        raise TimeoutError(f"Timed out while waiting for FluxGrip to {action_name}")

    async def _latest_feedback(self, *, timeout_s: float) -> Any:
        subscriber = self._require_feedback_subscriber()
        feedback = await subscriber.get(timeout_s)
        if feedback is None:
            raise TimeoutError("Timed out waiting for FluxGrip feedback")
        return feedback

    async def _drain_feedback(self) -> None:
        subscriber = self._require_feedback_subscriber()
        while await subscriber.get(0):
            pass

    def _require_dsdl(self) -> DsdlTypes:
        if self._dsdl is None:
            raise RuntimeError("FluxGripInterface.start() has not been called")
        return self._dsdl

    def _require_app(self) -> ApplicationTypes:
        if self._app is None:
            raise RuntimeError("FluxGripInterface.start() has not been called")
        return self._app

    def _require_controller_node(self) -> Any:
        if self._controller_node is None:
            raise RuntimeError("FluxGripInterface.start() has not been called")
        return self._controller_node

    def _require_node_tracker(self) -> Any:
        if self._node_tracker is None:
            raise RuntimeError("FluxGripInterface.start() has not been called")
        return self._node_tracker

    def _require_registers(self) -> RegisterProxy:
        if self._registers is None:
            raise RuntimeError("FluxGripInterface.start() has not completed")
        return self._registers

    def _require_target_node_id(self) -> int:
        if self._target_node_id is None:
            raise RuntimeError("FluxGripInterface.start() has not completed")
        return self._target_node_id

    def _require_command_publisher(self) -> Any:
        if self._command_publisher is None:
            raise RuntimeError("FluxGripInterface.start() has not completed")
        return self._command_publisher

    def _require_feedback_subscriber(self) -> Any:
        if self._feedback_subscriber is None:
            raise RuntimeError("FluxGripInterface.start() has not completed")
        return self._feedback_subscriber


def normalize_demag_values(values: Sequence[int]) -> list[int]:
    if len(values) != DEMAG_VALUE_COUNT:
        raise ValueError(f"Demag sequence must contain exactly {DEMAG_VALUE_COUNT} values, got {len(values)}")

    normalized: list[int] = []
    for index, value in enumerate(values):
        integer = int(value)
        if integer < np.iinfo(np.int32).min or integer > np.iinfo(np.int32).max:
            raise ValueError(f"Demag value at index {index} is outside int32 range: {value}")
        normalized.append(integer)
    return normalized


def register_value_as_int(name: str, register_value: Any) -> int:
    if register_value.value.empty:
        raise RuntimeError(f"FluxGrip register {name} is missing")
    try:
        return int(register_value)
    except (TypeError, ValueError) as ex:
        raise RuntimeError(f"FluxGrip register {name} is not numeric: {register_value.value!r}") from ex


def resolve_can_iface(can_iface: str | None, can_iface_index: int) -> str:
    if can_iface:
        return can_iface if ":" in can_iface else f"slcan:{can_iface}"

    candidates = sorted(Path("/dev/serial/by-id").glob("usb-*Zubax*Babel*"), key=lambda path: str(path))
    if not candidates:
        raise RuntimeError("No Zubax Babel CAN interface found under /dev/serial/by-id")
    if can_iface_index < 0 or can_iface_index >= len(candidates):
        raise ValueError(f"CAN interface index {can_iface_index} is out of range; found {len(candidates)} interface(s)")
    return f"slcan:{candidates[can_iface_index]}"


def load_application_types() -> ApplicationTypes:
    install_dsdl_import_hook()
    try:
        application = importlib.import_module("pycyphal.application")
        node_tracker = importlib.import_module("pycyphal.application.node_tracker")
        plug_and_play = importlib.import_module("pycyphal.application.plug_and_play")
        register = importlib.import_module("pycyphal.application.register")
    except ModuleNotFoundError as ex:
        raise RuntimeError(
            "Could not import PyCyphal application support because generated Cyphal DSDL types are unavailable. "
            f"{format_dsdl_diagnostic()}"
        ) from ex

    return ApplicationTypes(
        make_transport=application.make_transport,
        make_node=application.make_node,
        NodeTracker=node_tracker.NodeTracker,
        Natural16=register.Natural16,
        Natural32=register.Natural32,
        Value=register.Value,
        ValueProxy=register.ValueProxy,
        ValueProxyWithFlags=register.ValueProxyWithFlags,
        CentralizedAllocator=plug_and_play.CentralizedAllocator,
    )


def load_dsdl_types() -> DsdlTypes:
    install_dsdl_import_hook()
    try:
        uavcan_node = importlib.import_module("uavcan.node")
        uavcan_array = importlib.import_module("uavcan.primitive.array")
        uavcan_scalar = importlib.import_module("uavcan.primitive.scalar")
        uavcan_register = importlib.import_module("uavcan.register")
        fluxgrip = importlib.import_module("zubax.fluxgrip")
    except ModuleNotFoundError as ex:
        raise RuntimeError(
            f"Could not import Cyphal DSDL types. {format_dsdl_diagnostic()}"
        ) from ex

    return DsdlTypes(
        GetInfo_1=uavcan_node.GetInfo_1,
        ExecuteCommand_1=uavcan_node.ExecuteCommand_1,
        Integer32_1=uavcan_array.Integer32_1,
        Integer8_1=uavcan_scalar.Integer8_1,
        Access_1=uavcan_register.Access_1,
        List_1=uavcan_register.List_1,
        Name_1=uavcan_register.Name_1,
        Feedback_0=fluxgrip.Feedback_0,
    )


def install_dsdl_import_hook() -> None:
    lookup_directories = dsdl_lookup_directories()
    output_directory = dsdl_output_directory()
    LOGGER.debug("Installing DSDL import hook; lookup=%s output=%s", lookup_directories, output_directory)
    pycyphal.dsdl.install_import_hook(
        lookup_directories=lookup_directories,
        output_directory=output_directory,
    )


def dsdl_lookup_directories() -> list[Path] | None:
    cyphal_path = os.environ.get("CYPHAL_PATH")
    if cyphal_path:
        # None lets PyCyphal source the lookup list from CYPHAL_PATH exactly as documented.
        return None

    project_root = Path(__file__).resolve().parents[1]
    fallback_directories = [
        project_root / "force_rig_client" / "lib" / "public_regulated_data_types",
        project_root / "force_rig_client" / "lib" / "zubax_dsdl",
    ]
    existing_directories = [path for path in fallback_directories if path.exists() and any(path.iterdir())]
    return existing_directories or None


def dsdl_output_directory() -> Path | None:
    pycyphal_path = os.environ.get("PYCYPHAL_PATH")
    if pycyphal_path:
        return None

    output_directory = Path.home() / ".pycyphal"
    output_directory.mkdir(parents=True, exist_ok=True)
    return output_directory


def format_dsdl_diagnostic() -> str:
    cyphal_path = os.environ.get("CYPHAL_PATH")
    pycyphal_path = os.environ.get("PYCYPHAL_PATH")
    return (
        "PyCyphal expects DSDL root namespaces via CYPHAL_PATH and writes generated packages to "
        "PYCYPHAL_PATH or ~/.pycyphal. "
        f"Current python: {sys.executable}; "
        f"CYPHAL_PATH={cyphal_path or '<unset>'}; "
        f"PYCYPHAL_PATH={pycyphal_path or '<unset>'}."
    )
