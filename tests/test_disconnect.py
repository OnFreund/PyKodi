"""Tests for the disconnect callback and concurrent connects."""

import asyncio
import gc
import logging
import sys
from typing import Any, Awaitable, Callable, List, Optional
from unittest.mock import MagicMock

import aiohttp
from aiohttp import web
import jsonrpc_base
import pytest

from pykodi import CannotConnectError, get_kodi_connection

Handler = Callable[[web.WebSocketResponse], Awaitable[None]]


async def keep_open(websocket: web.WebSocketResponse) -> None:
    async for _ in websocket:
        pass


async def close_now(websocket: web.WebSocketResponse) -> None:
    await websocket.close()


async def send_invalid_json(websocket: web.WebSocketResponse) -> None:
    await websocket.send_str("not json")
    await keep_open(websocket)


async def start_kodi(handlers: List[Handler], autoclose: bool = True) -> web.AppRunner:
    """Start a websocket server that runs the next handler for each connection."""

    async def jsonrpc(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse(autoclose=autoclose)
        await websocket.prepare(request)
        await handlers.pop(0)(websocket)
        return websocket

    app = web.Application()
    app.router.add_get("/jsonrpc", jsonrpc)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner


def connect_to(runner: web.AppRunner, session: aiohttp.ClientSession, **kwargs: Any):
    port = runner.addresses[0][1]
    return get_kodi_connection(
        "127.0.0.1", 8080, port, None, None, session=session, **kwargs
    )


async def wait_until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


def test_callback_when_kodi_closes_the_connection() -> None:
    errors: List[Optional[BaseException]] = []

    async def run() -> None:
        runner = await start_kodi([close_now])
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)
                connection.on_disconnect(errors.append)
                await connection.connect()
                await wait_until(lambda: bool(errors))
                assert not connection.connected
                await connection.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())

    assert errors == [None]


def test_callback_gets_the_error() -> None:
    errors: List[Optional[BaseException]] = []
    unhandled: List[Any] = []

    async def run() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: unhandled.append(context)
        )
        runner = await start_kodi([send_invalid_json])
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)
                connection.on_disconnect(errors.append)
                await connection.connect()
                await wait_until(lambda: bool(errors))
                await connection.close()
        finally:
            await runner.cleanup()
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(run())

    assert len(errors) == 1
    assert isinstance(errors[0], jsonrpc_base.jsonrpc.TransportError)
    assert unhandled == []


def test_no_callback_on_close() -> None:
    errors: List[Optional[BaseException]] = []

    async def run() -> None:
        runner = await start_kodi([keep_open])
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)
                connection.on_disconnect(errors.append)
                await connection.connect()
                await asyncio.sleep(0)
                await connection.close()
                assert not connection.connected
                await asyncio.sleep(0.05)
        finally:
            await runner.cleanup()

    asyncio.run(run())

    assert errors == []


def test_no_callback_when_close_aborts_the_connection() -> None:
    errors: List[Optional[BaseException]] = []
    unhandled: List[Any] = []

    async def run() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: unhandled.append(context)
        )
        release = asyncio.Event()

        async def keep_sending(websocket: web.WebSocketResponse) -> None:
            # Keep sending notifications and never answer the closing handshake
            try:
                while not release.is_set():
                    await websocket.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "Player.OnSeek",
                            "params": {"sender": "xbmc", "data": None},
                        }
                    )
                    await asyncio.sleep(0.1)
            except ConnectionResetError:
                pass

        runner = await start_kodi([keep_sending], autoclose=False)
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session, timeout=0.3)
                connection.on_disconnect(errors.append)
                await connection.connect()
                await asyncio.sleep(0)
                await asyncio.wait_for(connection.close(), 5)
                assert not connection.connected
                await asyncio.sleep(0.05)
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(run())

    assert errors == []
    assert unhandled == []


def test_no_callback_when_connect_aborts_a_closing_connection() -> None:
    errors: List[Optional[BaseException]] = []
    unhandled: List[Any] = []

    async def run() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: unhandled.append(context)
        )
        release = asyncio.Event()

        async def break_and_hang(websocket: web.WebSocketResponse) -> None:
            # Break the connection and never answer the closing handshake
            await websocket.send_str("not json")
            await release.wait()

        async def keep_open_and_answer_close(websocket: web.WebSocketResponse) -> None:
            await keep_open(websocket)
            await websocket.close()

        runner = await start_kodi(
            [break_and_hang, keep_open_and_answer_close], autoclose=False
        )
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)
                connection.on_disconnect(errors.append)
                await connection.connect()
                await wait_until(lambda: not connection.connected)
                # The first connection is still closing, connect() aborts it
                await asyncio.wait_for(connection.connect(), 5)
                assert connection.connected
                # Let the first connection end, it must not report a loss now
                release.set()
                await asyncio.sleep(0.2)
                await connection.close()
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(run())

    assert errors == []
    assert unhandled == []


def test_no_callback_when_connect_fails() -> None:
    errors: List[Optional[BaseException]] = []

    async def run() -> None:
        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.read()
            writer.close()

        # Accepts connections but never answers the websocket handshake
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, None, None, timeout=0.2, session=session
                )
                connection.on_disconnect(errors.append)
                with pytest.raises(CannotConnectError):
                    await asyncio.wait_for(connection.connect(), 5)
                await asyncio.sleep(0.05)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())

    assert errors == []


@pytest.mark.parametrize(
    "eager",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                sys.version_info < (3, 12), reason="needs eager_task_factory"
            ),
        ),
    ],
)
def test_no_callback_on_close_from_a_notification_handler(eager: bool) -> None:
    calls: List[Any] = []

    async def send_quit(websocket: web.WebSocketResponse) -> None:
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "System.OnQuit",
                "params": {"sender": "xbmc", "data": None},
            }
        )
        await keep_open(websocket)

    async def run() -> None:
        if eager:
            # Home Assistant runs with eager tasks, then the read loop starts
            # and handles the notification before connect() returns
            asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
        runner = await start_kodi([send_quit])
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)

                async def on_quit(sender: str, data: Any) -> None:
                    calls.append("quit")
                    await connection.close()

                connection.subscribe("System.OnQuit", on_quit)
                connection.on_disconnect(calls.append)
                await connection.connect()
                await wait_until(lambda: bool(calls))
                await asyncio.sleep(0.1)
                assert not connection.connected
        finally:
            await runner.cleanup()

    asyncio.run(run())

    assert calls == ["quit"]


def lose_connection(register: Callable[[Any], None]) -> None:
    """Connect, let Kodi close the connection and wait for the callbacks."""

    async def run() -> None:
        runner = await start_kodi([close_now])
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)
                register(connection)
                await connection.connect()
                await wait_until(lambda: not connection.connected)
                await asyncio.sleep(0.1)
                await connection.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_async_callback_is_awaited() -> None:
    calls: List[str] = []

    async def slow(error: Optional[BaseException]) -> None:
        await asyncio.sleep(0.01)
        calls.append("slow")

    def register(connection: Any) -> None:
        connection.on_disconnect(slow)
        connection.on_disconnect(lambda error: calls.append("next"))

    lose_connection(register)

    # The next callback runs only after the slow one finished
    assert calls == ["slow", "next"]


def test_failing_callback_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    errors: List[Optional[BaseException]] = []

    def failing(error: Optional[BaseException]) -> None:
        raise ValueError("boom")

    def register(connection: Any) -> None:
        connection.on_disconnect(failing)
        connection.on_disconnect(errors.append)

    with caplog.at_level(logging.ERROR, logger="pykodi.kodi"):
        lose_connection(register)

    assert errors == [None]
    assert "Error in disconnect callback" in caplog.text


def test_remove_callback() -> None:
    calls: List[str] = []

    def register(connection: Any) -> None:
        remove = connection.on_disconnect(lambda error: calls.append("removed"))
        connection.on_disconnect(lambda error: calls.append("kept"))
        remove()
        # Calling it again is harmless
        remove()

    lose_connection(register)

    assert calls == ["kept"]


def test_close_from_a_callback_skips_the_rest() -> None:
    calls: List[str] = []

    async def run() -> None:
        runner = await start_kodi([close_now])
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)

                async def first(error: Optional[BaseException]) -> None:
                    calls.append("first")
                    await asyncio.sleep(0)
                    await connection.close()
                    # close() does not stop the callback that called it
                    calls.append("first done")

                connection.on_disconnect(first)
                connection.on_disconnect(lambda error: calls.append("second"))
                await connection.connect()
                await wait_until(lambda: bool(calls))
                await asyncio.sleep(0.05)
        finally:
            await runner.cleanup()

    asyncio.run(run())

    assert calls == ["first", "first done"]


def test_reconnect_from_a_callback() -> None:
    errors: List[Optional[BaseException]] = []
    reconnected = []

    async def run() -> None:
        runner = await start_kodi([close_now, keep_open])
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)

                async def reconnect(error: Optional[BaseException]) -> None:
                    errors.append(error)
                    await connection.connect()
                    reconnected.append(connection.connected)

                connection.on_disconnect(reconnect)
                await connection.connect()
                await asyncio.wait_for(wait_until(lambda: bool(reconnected)), 5)
                assert connection.connected
                await connection.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())

    assert errors == [None]
    assert reconnected == [True]


def test_callback_survives_reconnect() -> None:
    errors: List[Optional[BaseException]] = []

    async def run() -> None:
        runner = await start_kodi([close_now, close_now])
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)
                connection.on_disconnect(errors.append)
                await connection.connect()
                await wait_until(lambda: len(errors) == 1)
                await connection.connect()
                await wait_until(lambda: len(errors) == 2)
                await connection.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())

    assert errors == [None, None]


def test_callback_after_close_and_reconnect() -> None:
    errors: List[Optional[BaseException]] = []

    async def run() -> None:
        runner = await start_kodi([keep_open, close_now])
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)
                connection.on_disconnect(errors.append)
                await connection.connect()
                await connection.close()
                await connection.connect()
                await wait_until(lambda: bool(errors))
                await connection.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())

    assert errors == [None]


def test_http_connection_has_no_disconnect_callback() -> None:
    connection = get_kodi_connection(
        "kodi", 8080, None, None, None, session=MagicMock()
    )
    with pytest.raises(NotImplementedError):
        connection.on_disconnect(lambda error: None)


def test_concurrent_connect_opens_one_connection() -> None:
    opened: List[int] = []

    async def count(websocket: web.WebSocketResponse) -> None:
        opened.append(1)
        await keep_open(websocket)

    async def run() -> None:
        runner = await start_kodi([count, count])
        try:
            async with aiohttp.ClientSession() as session:
                connection = connect_to(runner, session)
                await asyncio.gather(connection.connect(), connection.connect())
                await asyncio.sleep(0.05)
                assert connection.connected
                await connection.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())

    assert opened == [1]
