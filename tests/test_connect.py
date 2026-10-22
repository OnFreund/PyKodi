"""Tests for connecting over websocket."""

import asyncio
import gc
import logging
from typing import Any, List

import aiohttp
from aiohttp import web
import pytest

from pykodi import CannotConnectError, get_kodi_connection


async def start_silent_server() -> asyncio.Server:
    """Accept connections but never answer the websocket handshake."""

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.read()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def test_connect_times_out() -> None:
    async def run() -> None:
        server = await start_silent_server()
        port = server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, None, None, timeout=0.2, session=session
                )
                start = asyncio.get_running_loop().time()
                with pytest.raises(CannotConnectError):
                    await asyncio.wait_for(connection.connect(), 5)
                assert asyncio.get_running_loop().time() - start < 2
                assert not connection.connected
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_connect_can_be_cancelled() -> None:
    async def run() -> None:
        server = await start_silent_server()
        port = server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, None, None, timeout=30, session=session
                )
                task = asyncio.ensure_future(connection.connect())
                await asyncio.sleep(0.1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert task.cancelled()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_read_loop_error_is_retrieved(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="pykodi.kodi")
    unhandled: List[Any] = []

    async def jsonrpc(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await websocket.send_str("not json")
        async for _ in websocket:
            pass
        return websocket

    async def run() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: unhandled.append(context)
        )
        app = web.Application()
        app.router.add_get("/jsonrpc", jsonrpc)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, None, None, session=session
                )
                await connection.connect()
                for _ in range(200):
                    if not connection.connected:
                        break
                    await asyncio.sleep(0.01)
                assert not connection.connected
                await connection.close()
        finally:
            await runner.cleanup()
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(run())

    assert unhandled == []
    lost = [
        record
        for record in caplog.records
        if record.getMessage() == "Websocket connection to Kodi lost"
    ]
    assert len(lost) == 1
    assert lost[0].exc_info is not None


def test_close_stops_read_loop() -> None:
    async def jsonrpc(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        async for _ in websocket:
            pass
        return websocket

    async def run() -> None:
        app = web.Application()
        app.router.add_get("/jsonrpc", jsonrpc)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, None, None, session=session
                )
                before = asyncio.all_tasks()
                await connection.connect()
                await asyncio.sleep(0)
                assert connection.connected
                await connection.close()
                assert not connection.connected
                assert asyncio.all_tasks() - before == set()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_close_from_callback() -> None:
    async def run() -> None:
        quit_sent = asyncio.Event()

        async def jsonrpc(request: web.Request) -> web.WebSocketResponse:
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await quit_sent.wait()
            await websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "method": "System.OnQuit",
                    "params": {"sender": "xbmc", "data": None},
                }
            )
            async for _ in websocket:
                pass
            return websocket

        app = web.Application()
        app.router.add_get("/jsonrpc", jsonrpc)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, None, None, session=session
                )
                closed = asyncio.Event()

                async def on_quit(sender: str, data: Any) -> None:
                    await connection.close()
                    closed.set()

                connection.subscribe("System.OnQuit", on_quit)
                await connection.connect()
                quit_sent.set()
                await asyncio.wait_for(closed.wait(), 2)
                assert not connection.connected
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_close_from_callback_right_after_connect() -> None:
    async def run() -> None:
        release = asyncio.Event()
        open_connections = 0

        async def jsonrpc(request: web.Request) -> web.WebSocketResponse:
            nonlocal open_connections
            open_connections += 1
            websocket = web.WebSocketResponse(autoclose=False)
            await websocket.prepare(request)
            # Keep sending notifications and never answer the closing handshake
            try:
                while not release.is_set():
                    await websocket.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "System.OnQuit",
                            "params": {"sender": "xbmc", "data": None},
                        }
                    )
                    await asyncio.sleep(0.1)
            except ConnectionResetError:
                pass
            finally:
                open_connections -= 1
            return websocket

        app = web.Application()
        app.router.add_get("/jsonrpc", jsonrpc)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, None, None, timeout=0.5, session=session
                )
                closed = asyncio.Event()

                async def on_quit(sender: str, data: Any) -> None:
                    if not closed.is_set():
                        await connection.close()
                        closed.set()

                connection.subscribe("System.OnQuit", on_quit)
                await connection.connect()
                await asyncio.wait_for(closed.wait(), 2)
                assert not connection.connected
                # The read loop started by connect() is aborted as well
                for _ in range(200):
                    if open_connections == 0:
                        break
                    await asyncio.sleep(0.01)
                assert open_connections == 0
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(run())


def test_connect_after_cancelled_close() -> None:
    async def run() -> None:
        release = asyncio.Event()
        open_connections = 0

        async def jsonrpc(request: web.Request) -> web.WebSocketResponse:
            nonlocal open_connections
            open_connections += 1
            websocket = web.WebSocketResponse(autoclose=False)
            await websocket.prepare(request)
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
            finally:
                open_connections -= 1
            return websocket

        app = web.Application()
        app.router.add_get("/jsonrpc", jsonrpc)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, None, None, timeout=0.5, session=session
                )
                await connection.connect()
                await asyncio.sleep(0)
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(connection.close(), 0.2)
                assert not connection.connected
                start = asyncio.get_running_loop().time()
                await asyncio.wait_for(connection.connect(), 5)
                assert asyncio.get_running_loop().time() - start < 2
                assert connection.connected
                # The read loop of the first connection is aborted
                for _ in range(200):
                    if open_connections == 1:
                        break
                    await asyncio.sleep(0.01)
                assert open_connections == 1
                release.set()
                await asyncio.wait_for(connection.close(), 5)
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(run())


# Before Python 3.12 the read loop has already started when connect() returns
@pytest.mark.parametrize("read_loop_started", [True, False])
def test_close_is_bounded(read_loop_started: bool) -> None:
    async def run() -> None:
        release = asyncio.Event()
        open_connections = 0

        async def jsonrpc(request: web.Request) -> web.WebSocketResponse:
            nonlocal open_connections
            open_connections += 1
            websocket = web.WebSocketResponse(autoclose=False)
            await websocket.prepare(request)
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
            finally:
                open_connections -= 1
            return websocket

        app = web.Application()
        app.router.add_get("/jsonrpc", jsonrpc)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, None, None, timeout=0.5, session=session
                )
                await connection.connect()
                if read_loop_started:
                    await asyncio.sleep(0)
                start = asyncio.get_running_loop().time()
                await asyncio.wait_for(connection.close(), 5)
                assert asyncio.get_running_loop().time() - start < 2
                assert not connection.connected
                # The connection is aborted, not left to the next connect()
                for _ in range(200):
                    if open_connections == 0:
                        break
                    await asyncio.sleep(0.01)
                assert open_connections == 0
                await asyncio.wait_for(connection.connect(), 5)
                assert connection.connected
                release.set()
                await asyncio.wait_for(connection.close(), 5)
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(run())
