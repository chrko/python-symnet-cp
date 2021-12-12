import asyncio
import logging
from asyncio import CancelledError

from symnet_cp.controller import (
    SymNetButtonController,
    SymNetController,
    SymNetRawProtocol,
    SymNetSelectorController,
)
from symnet_cp.protocol import SymNetRawControllerState


class SymNetDevice:
    def __init__(
        self,
        local_address: tuple[str, int],
        remote_address: tuple[str, int],
        state_queue: asyncio.Queue,
        transport: asyncio.DatagramTransport,
        protocol: SymNetRawProtocol,
    ):
        self.logger = logging.getLogger(self.__class__.__qualname__)

        self.local_address = local_address
        self.remote_address = remote_address

        self._state_queue: asyncio.Queue = state_queue

        self.controllers: dict[int, SymNetController] = {}

        self.transport, self.protocol = transport, protocol

        self._process_task = asyncio.create_task(
            self._process_push_messages(), name=f"{self!r}-process_push_messages"
        )

    @classmethod
    async def create(
        cls, local_address: tuple[str, int], remote_address: tuple[str, int]
    ) -> "SymNetDevice":
        logger = logging.getLogger(cls.__qualname__)
        logger.debug("setup new SymNet device")

        state_queue = asyncio.Queue()

        def create_protocol() -> asyncio.DatagramProtocol:
            return SymNetRawProtocol(state_queue=state_queue)

        transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            create_protocol, local_addr=local_address, remote_addr=remote_address
        )  # type: asyncio.DatagramTransport, SymNetRawProtocol

        return cls(
            local_address=local_address,
            remote_address=remote_address,
            state_queue=state_queue,
            transport=transport,
            protocol=protocol,
        )

    async def _process_push_messages(self):
        while True:
            cs: SymNetRawControllerState = await self._state_queue.get()
            self.logger.debug(
                "received some pushed data - handover to the controller object"
            )
            if cs.controller_number in self.controllers:
                # noinspection PyProtectedMember
                self.controllers[cs.controller_number]._set_raw_value(
                    cs.controller_value
                )

    async def define_controller(self, controller_number: int) -> SymNetController:
        self.logger.debug(
            "create new controller %d on SymNet device", controller_number
        )
        controller_number = int(controller_number)
        controller = SymNetController(controller_number, self.protocol)
        self.controllers[controller_number] = controller

        await controller.retrieve_current_state()

        return controller

    async def define_selector(
        self,
        controller_number: int,
        position_count: int,
    ) -> SymNetSelectorController:
        self.logger.debug("create new selector %d on SymNet device", controller_number)
        controller_number = int(controller_number)
        controller = SymNetSelectorController(
            controller_number, position_count, self.protocol
        )
        self.controllers[controller_number] = controller

        await controller.retrieve_current_state()

        return controller

    async def define_button(self, controller_number: int) -> SymNetButtonController:
        self.logger.debug("create new button %d on SymNet device", controller_number)
        controller_number = int(controller_number)
        controller = SymNetButtonController(controller_number, self.protocol)
        self.controllers[controller_number] = controller

        await controller.retrieve_current_state()

        return controller

    async def cleanup(self):
        self.logger.debug("SymNetDevice cancel process_task")
        self._process_task.cancel()
        try:
            await self._process_task
        except CancelledError:
            pass
        self.logger.debug("SymNetDevice close transport")
        self.transport.close()

    def __repr__(self):
        return (
            f"{self.__class__.__qualname__} "
            f"local_address={self.local_address!r} "
            f"remote_address={self.remote_address!r} "
            f"controller_count={len(self.controllers)} "
            f"transport={self.transport} "
            f"protocol={self.protocol} "
            f"state_queue_depth={self._state_queue.qsize()}>"
        )
