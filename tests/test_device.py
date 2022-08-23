import asyncio
import typing
from asyncio import transports

import pytest

from symnet_cp import SymNetDevice


class UDPExpectedServer:
    def __init__(
        self,
        *,
        server_port: int,
        client_port: int,
        expected: list[tuple[bytes, bytes]],
        delayed_data: list[tuple[float, bytes]] = None,
    ) -> None:
        self._host = "127.0.0.1"
        self._server_port = server_port
        self._client_port = client_port

        self._expected = expected
        self._delayed_data = delayed_data or []
        self._delayed_data_tasks: list[asyncio.Task] = []

        class ServerProtocol(asyncio.DatagramProtocol):
            def __init__(self):
                self.transport: typing.Optional[asyncio.DatagramTransport] = None
                self.address: typing.Optional[tuple[str, int]] = None

            def connection_made(self, transport: transports.DatagramTransport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, address: tuple[str, int]) -> None:
                if len(expected) == 0:
                    raise AssertionError("No more datagrams expected")
                req, resp = expected.pop(0)
                if not self.address:
                    self.address = address
                assert data == req
                if len(resp) > 0:
                    self.transport.sendto(resp, self.address)

        self._protocol_factory = ServerProtocol
        self._transport = None

    @property
    def client_address(self) -> tuple[str, int]:
        return self._host, self._client_port

    @property
    def server_address(self) -> tuple[str, int]:
        return self._host, self._server_port

    @staticmethod
    async def delayed_sender(timeout: float, data: bytes, protocol):
        await asyncio.sleep(timeout)
        if protocol.address:
            protocol.transport.sendto(data, protocol.address)

    async def __aenter__(self):
        listen = asyncio.get_running_loop().create_datagram_endpoint(
            self._protocol_factory, local_addr=self.server_address
        )
        self._transport, protocol = await listen
        for delayed_data in self._delayed_data:
            self._delayed_data_tasks.append(
                asyncio.create_task(
                    self.delayed_sender(*delayed_data, protocol=protocol),
                    name=f"Delayed task {delayed_data}",
                )
            )

    async def __aexit__(self, exc_type, exc, tb):
        self._transport.close()
        self._transport = None
        assert len(self._expected) == 0, "Not all expected datagrams received"
        for delayed_data_task in self._delayed_data_tasks:
            assert delayed_data_task.done() is True, delayed_data_task


@pytest.fixture
def udp_expected_factory_server():
    return UDPExpectedServer


async def test_device(
    unused_udp_port_factory: typing.Callable[[], int],
    udp_expected_factory_server: typing.Type[UDPExpectedServer],
):
    local_port = unused_udp_port_factory()
    remote_port = unused_udp_port_factory()

    expected = [
        (b"GS2 1\r", b"1 0\r"),
        (b"CS 1 65535\r", b"ACK\r"),
        (b"GS2 1\r", b"1 0\r"),
        (b"GS2 1\r", None),
    ]
    delayed_data = [(1, b"#00001=00000\r"), (3, b"#00001=65535")]

    udp_server = udp_expected_factory_server(
        server_port=remote_port,
        client_port=local_port,
        expected=expected,
        delayed_data=delayed_data,
    )

    async with udp_server:
        dev = await SymNetDevice.create(
            local_address=udp_server.client_address,
            remote_address=udp_server.server_address,
        )
        try:
            sel = await dev.define_selector(1, 8)

            callback_args_expected = [
                (sel, {"old_value": 0, "new_value": 65535}),
                (sel, {"old_value": 65535, "new_value": 0}),
                (sel, {"old_value": 0, "new_value": 65535}),
            ]

            async def callback_expected(*args, **kwargs):
                exp = callback_args_expected.pop(0)
                assert exp == (args, kwargs)

            sel.add_observer(callback_expected)

            assert await sel.get_position() == 1

            await sel.set_position(8)
            assert await sel.get_position() == 8

            await asyncio.sleep(2)
            assert await sel.get_position() == 1
            assert await sel.get_raw_value(True) == 0

            await asyncio.sleep(15)
            assert sel.raw_value == 65535
            with pytest.raises(asyncio.TimeoutError):
                assert await sel.get_position() == 8

            assert len(dev.protocol.callback_queue) == 0

            assert len(callback_args_expected) == 0
        finally:
            await dev.cleanup()
