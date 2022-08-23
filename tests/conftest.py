import asyncio
import typing
from asyncio import transports

import pytest

from symnet_cp.device import SymNetDevice


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


class SymNetDeviceProvider(UDPExpectedServer):
    def __init__(
        self,
        *,
        server_port: int,
        client_port: int,
        expected: list[tuple[bytes, bytes]],
        delayed_data: list[tuple[float, bytes]] = None,
    ) -> None:
        super().__init__(
            server_port=server_port,
            client_port=client_port,
            expected=expected,
            delayed_data=delayed_data,
        )
        self._device: typing.Optional[SymNetDevice] = None

    async def __aenter__(self) -> SymNetDevice:
        await super().__aenter__()

        self._device = await SymNetDevice.create(
            local_address=self.client_address,
            remote_address=self.server_address,
        )
        return self._device

    async def __aexit__(self, exc_type, exc, tb):
        if self._device is not None:
            await self._device.cleanup()
        await super().__aexit__(exc_type, exc, tb)


@pytest.fixture
def symnet_device_provider_factory():
    return SymNetDeviceProvider
