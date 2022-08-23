import asyncio
import typing

import pytest


async def test_device(
    unused_udp_port_factory: typing.Callable[[], int],
    symnet_device_provider_factory,
):
    symnet_device_provider = symnet_device_provider_factory(
        server_port=unused_udp_port_factory(),
        client_port=unused_udp_port_factory(),
        expected=[
            (b"GS2 1\r", b"1 0\r"),
            (b"CS 1 65535\r", b"ACK\r"),
            (b"GS2 1\r", b"1 0\r"),
            (b"GS2 1\r", None),
        ],
        delayed_data=[
            (1, b"#00001=00000\r"),
            (3, b"#00001=65535"),
        ],
    )

    async with symnet_device_provider as dev:
        sel = await dev.define_selector(1, 8)

        callback_args_expected = [
            ((sel,), {"old_value": 0, "new_value": 65535}),
            ((sel,), {"old_value": 65535, "new_value": 0}),
            ((sel,), {"old_value": 0, "new_value": 65535}),
        ]
        callback_args_received = []

        async def callback_expected(*args, **kwargs):
            callback_args_received.append((args, kwargs))

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

        assert callback_args_received == callback_args_expected
