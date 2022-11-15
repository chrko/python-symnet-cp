import asyncio
import logging
import re
import typing
from datetime import datetime
from zoneinfo import ZoneInfo

import attr
from prometheus_client import Counter

UTC = ZoneInfo("UTC")

logger = logging.getLogger(__name__)


class SymNetRawProtocolCallback:
    DEFAULT_TIMEOUT = 5

    def __init__(
        self,
        protocol: "SymNetRawProtocol",
        callback: typing.Callable,
        expected_lines: int,
        regex: str = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.protocol = protocol
        self._callback = callback
        self.expected_lines = expected_lines
        self.regex = regex

        loop = asyncio.get_running_loop()

        self.future = loop.create_future()
        self.timeout = float(timeout)
        self.timeout_task = asyncio.create_task(
            self.timeouter(),
            name=f"SymNetRawProtocolCallback-timeouter-{datetime.now(UTC)}",
        )

    async def timeouter(self):
        await asyncio.sleep(self.timeout)
        logger.warning(f"Callback {self!r} timed out")
        try:
            self.future.set_exception(asyncio.TimeoutError())
        finally:
            self.protocol.callback_queue.remove(self)

    def callback(self, *args, **kwargs):
        logger.debug("raw protocol callback called")
        try:
            result = self._callback(*args, **kwargs)
            self.future.set_result(result)
        except Exception as e:
            try:
                self.future.set_exception(e)
            except Exception as e:
                logger.error(f"Error during setting exception on future: {self!r}", e)
        finally:
            try:
                self.timeout_task.cancel()
            except Exception as e:
                logger.error(f"Error canceling timout task: {self!r}", e)

    def __repr__(self):
        return (
            f"<{self.__class__.__qualname__} "
            f"callback={self._callback!r} "
            f"expected_lines={self.expected_lines} "
            f"regex={self.regex!r} "
            f"future={self.future!r} "
            f"timeout={self.timeout} "
            f"timeout_task={self.timeout_task}>"
        )


class SymNetRawProtocol(asyncio.DatagramProtocol):
    RECEIVED_DATAGRAMS = Counter(
        "symnet_received_datagrams", "Received datagrams", ["host", "port"]
    )
    RECEIVED_DATA_LINES = Counter(
        "symnet_received_data_lines",
        "Received lines",
        ["host", "port", "category"],
    )
    WRITTEN_DATAGRAMS = Counter(
        "symnet_written_datagrams", "Written datagrams", ["host", "port"]
    )

    def __init__(self, state_queue: asyncio.Queue):
        logger.debug("init a SymNetRawProtocol")
        self.transport: typing.Optional[asyncio.DatagramTransport] = None
        self.callback_queue: list[SymNetRawProtocolCallback] = []
        self.state_queue = state_queue
        self.address_labels = {"host": "UNKNOWN", "port": "UNKNOWN"}

    def connection_made(self, transport: asyncio.DatagramTransport):
        logger.debug("connection established")
        self.transport = transport

    def datagram_received(self, data: bytes, address):
        address_labels = {"host": address[0], "port": address[1]}
        self.address_labels = address_labels

        logger.debug("a datagram was received - %d bytes, from %s", len(data), address)
        self.RECEIVED_DATAGRAMS.labels(**address_labels).inc()
        data_str = data.decode()
        lines = data_str.split("\r")
        lines = [lines[i] for i in range(len(lines)) if len(lines[i]) > 0]

        logger.debug("%d non-empty lines received", len(lines))

        if len(self.callback_queue) > 0:
            logger.debug("iterate over callback queue")
            for callback_obj in self.callback_queue:
                if len(lines) == 1 and lines[0] == "NAK":
                    self.RECEIVED_DATA_LINES.labels(
                        category="callback_nak", **address_labels
                    ).inc()
                    logger.debug("got only a NAK - forwarding to the first callback")
                    callback_obj.callback(data_str)
                    self.callback_queue.remove(callback_obj)
                    return

                if callback_obj.regex is not None:
                    logger.debug(
                        "callback comes with a regex - try match on the whole received data string"
                    )
                    m = re.match(callback_obj.regex, data_str)
                    if m is not None:
                        self.RECEIVED_DATA_LINES.labels(
                            category="callback_regex", **address_labels
                        ).inc()
                        logger.debug("regex worked - deliver to callback and remove it")
                        callback_obj.callback(data_str, m=m)
                        self.callback_queue.remove(callback_obj)
                        return
                elif len(lines) == callback_obj.expected_lines:
                    self.RECEIVED_DATA_LINES.labels(
                        category="callback_expected_lines", **address_labels
                    ).inc()
                    logger.debug(
                        "callback has no regex, but the expected line count equals to the received one"
                    )
                    callback_obj.callback(data_str)
                    self.callback_queue.remove(callback_obj)
                    return

        if len(lines) == 1:
            if lines[0] == "NAK":
                self.RECEIVED_DATA_LINES.labels(category="nak", **address_labels).inc()
                logger.error("Uncaught NAK - this is probably a huge error")
                return
            if lines[0] == "ACK":
                self.RECEIVED_DATA_LINES.labels(category="ack", **address_labels).inc()
                logger.debug(
                    "got an ACK, but no callbacks waiting for input - just ignore it"
                )
                return

        logger.debug("no callbacks defined and not an ACK or NAK - must be pushed data")
        for line in lines:
            m = re.match("^#([0-9]{5})=(-?[0-9]{4,5})$", line)
            if m is None:
                self.RECEIVED_DATA_LINES.labels(
                    category="error", **address_labels
                ).inc()
                logger.error("error in in the received line <%s>", line)
                continue

            self.RECEIVED_DATA_LINES.labels(
                category="pushed_data", **address_labels
            ).inc()
            asyncio.ensure_future(
                self.state_queue.put(
                    SymNetRawControllerState(
                        controller_number=int(m.group(1)),
                        controller_value=int(m.group(2)),
                    )
                )
            )

    def error_received(self, exc):
        logger.error("Error received %s", exc)
        if isinstance(exc, ConnectionRefusedError):
            self.callback_queue.clear()
            logger.fatal("Unable to connect to remote endpoint")
            raise exc

    def write(self, data: str):
        logger.debug("send data to symnet %s", data)
        self.transport.sendto(data.encode())
        self.WRITTEN_DATAGRAMS.labels(**self.address_labels).inc()

    def __repr__(self):
        return (
            f"<{self.__class__.__qualname__} "
            f"callback_queue_len={len(self.callback_queue)} "
            f"address_labels={self.address_labels!r}>"
        )


@attr.s(frozen=True, slots=True)
class SymNetRawControllerState:
    controller_number: int = attr.ib(
        converter=int, validator=attr.validators.instance_of(int)
    )
    controller_value: int = attr.ib(
        converter=int, validator=attr.validators.instance_of(int)
    )
