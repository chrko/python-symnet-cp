import asyncio
import logging
import typing
from datetime import datetime
from zoneinfo import ZoneInfo

from symnet_cp.protocol import SymNetRawProtocol, SymNetRawProtocolCallback

UTC = ZoneInfo("UTC")


class SymNetController:
    value_timeout = 10  # in seconds

    def __init__(self, controller_number: int, protocol: SymNetRawProtocol):
        self.logger = logging.getLogger(self.__class__.__qualname__)

        self.logger.debug("create new SymNetController with %d", controller_number)
        self.controller_number = int(controller_number)
        self.protocol = protocol

        self.raw_value = 0
        self.raw_value_time: float = 0

        self.observer: list[typing.Callable] = []

    def add_observer(self, callback: typing.Callable):
        self.logger.debug(
            "add a observer (%s) to controller %d", callback, self.controller_number
        )
        return self.observer.append(callback)

    def remove_observer(self, callback: typing.Callable):
        self.logger.debug(
            "remove a observer (%s) to controller %d", callback, self.controller_number
        )
        return self.observer.remove(callback)

    async def get_raw_value(self, force_load: bool = False) -> int:
        self.logger.debug(
            "retrieve current value for controller %d", self.controller_number
        )
        if (
            force_load
            or asyncio.get_running_loop().time() - self.raw_value_time
            > self.value_timeout
        ):
            self.logger.debug("value timeout - refresh")
            await self.retrieve_current_state()
        return self.raw_value

    async def set_raw_value(self, value: int):
        self._set_raw_value(value)
        await self.assure_current_state()

    def _set_raw_value(self, value: int):
        self.logger.debug(
            "set_raw_value called on %d with %d", self.controller_number, value
        )
        old_value = self.raw_value
        self.raw_value = value
        self.raw_value_time = asyncio.get_running_loop().time()
        if old_value != value:
            self.logger.debug("value has changed - notify observers")
            for clb in self.observer:
                asyncio.create_task(
                    clb(self, old_value=old_value, new_value=value),
                    name=f"{self!r}-{datetime.now(UTC)}-callback-{clb}",
                )

    async def assure_current_state(self):
        self.logger.debug(
            "assure current controller %d state to set on the symnet device",
            self.controller_number,
        )
        callback_obj = SymNetRawProtocolCallback(
            callback=self._assure_callback, expected_lines=1, regex="^(ACK)|(NAK)\r$"
        )
        self.protocol.callback_queue.append(callback_obj)
        self.protocol.write(
            "CS {cn:d} {cv:d}\r".format(cn=self.controller_number, cv=self.raw_value)
        )
        await callback_obj.future

    def _assure_callback(self, _, m=None):
        if m is None or m.group(1) == "NAK":
            raise Exception(
                f"Unknown error occurred awaiting the acknowledge of setting controller number {self.controller_number:d}"
            )

    async def retrieve_current_state(self):
        self.logger.debug(
            "request current value from the symnet device for controller %d",
            self.controller_number,
        )
        callback_obj = SymNetRawProtocolCallback(
            callback=self._retrieve_callback,
            expected_lines=1,
            regex=f"^{self.controller_number} ([0-9]{{1,5}})\r$",
        )
        self.protocol.callback_queue.append(callback_obj)
        self.protocol.write(f"GS2 {self.controller_number:d}\r")
        await callback_obj.future

    def _retrieve_callback(self, _, m=None):
        if m is None:
            raise Exception(
                f"Error executing GS2 command, controller {self.controller_number}"
            )
        self._set_raw_value(int(m.group(1)))

    def __repr__(self):
        return (
            f"<{self.__class__.__qualname__} "
            f"controller_number={self.controller_number} "
            f"raw_value={self.raw_value} "
            f"raw_value_time={self.raw_value_time} "
            f"observer_count={len(self.observer)} "
            f"protocol={self.protocol}>"
        )


class SymNetSelectorController(SymNetController):
    def __init__(
        self, controller_number: int, position_cont: int, protocol: SymNetRawProtocol
    ):
        super().__init__(controller_number, protocol)

        self._position_count = int(position_cont)

    @property
    def position_count(self) -> int:
        return self._position_count

    def _raw_to_position(self, value: int) -> int:
        return int(round(value / 65535 * (self.position_count - 1) + 1))

    async def get_position(self):
        raw_value = await self.get_raw_value()
        return self._raw_to_position(raw_value)

    async def set_position(self, position: int):
        assert 1 <= position <= self.position_count
        await self.set_raw_value(
            int(round((position - 1) / (self.position_count - 1) * 65535))
        )

    def __repr__(self):
        return (
            f"<{self.__class__.__qualname__} "
            f"controller_number={self.controller_number} "
            f"position={self._raw_to_position(self.raw_value)} "
            f"raw_value={self.raw_value} "
            f"raw_value_time={self.raw_value_time} "
            f"observer_count={len(self.observer)} "
            f"protocol={self.protocol}>"
        )


class SymNetButtonController(SymNetController):
    async def on(self):
        await self.set_raw_value(65535)

    async def off(self):
        await self.set_raw_value(0)

    async def pressed(self):
        return await self.get_raw_value() > 0

    async def set(self, state: bool):
        if state:
            await self.on()
        else:
            await self.off()

    def __repr__(self):
        return (
            f"<{self.__class__.__qualname__} "
            f"controller_number={self.controller_number} "
            f"raw_value={self.raw_value} "
            f"raw_value_time={self.raw_value_time} "
            f"observer_count={len(self.observer)} "
            f"protocol={self.protocol}>"
        )
