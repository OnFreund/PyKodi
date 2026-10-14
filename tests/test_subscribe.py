"""Tests for subscribing to Kodi notifications."""

import asyncio
import logging
from typing import Any, List, Tuple
from unittest.mock import MagicMock

import aiohttp
from aiohttp import web
import jsonrpc_base
import pytest

from pykodi import get_kodi_connection

DATA = {"item": {"id": 1, "type": "movie"}, "player": {"playerid": 1, "speed": 1}}


def ws_connection():
    return get_kodi_connection("kodi", 8080, 9090, None, None, session=MagicMock())


async def notify(connection, method: str, data: Any = DATA) -> None:
    """Deliver a notification the way the websocket loop does."""
    request = jsonrpc_base.Request(method, {"sender": "xbmc", "data": data})
    await connection.server.async_receive_request(request)


def test_all_subscribers_are_called() -> None:
    calls: List[Tuple[str, str, Any]] = []

    def first(sender: str, data: Any) -> None:
        calls.append(("first", sender, data))

    async def second(sender: str, data: Any) -> None:
        calls.append(("second", sender, data))

    async def run() -> None:
        connection = ws_connection()
        connection.subscribe("Player.OnPlay", first)
        connection.subscribe("Player.OnPlay", second)
        await notify(connection, "Player.OnPlay")

    asyncio.run(run())

    assert calls == [("first", "xbmc", DATA), ("second", "xbmc", DATA)]


def test_only_the_notified_method_is_called() -> None:
    calls: List[str] = []

    async def run() -> None:
        connection = ws_connection()
        connection.subscribe("Player.OnPlay", lambda *_: calls.append("play"))
        connection.subscribe("Player.OnStop", lambda *_: calls.append("stop"))
        connection.subscribe("System.OnQuit", lambda *_: calls.append("quit"))
        await notify(connection, "Player.OnStop")

    asyncio.run(run())

    assert calls == ["stop"]


def test_unsubscribe() -> None:
    calls: List[str] = []

    async def run() -> None:
        connection = ws_connection()
        unsubscribe = connection.subscribe(
            "Player.OnPlay", lambda *_: calls.append("removed")
        )
        connection.subscribe("Player.OnPlay", lambda *_: calls.append("kept"))
        unsubscribe()
        # Calling it again is harmless
        unsubscribe()
        await notify(connection, "Player.OnPlay")

    asyncio.run(run())

    assert calls == ["kept"]


def test_failing_callback_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: List[str] = []

    def failing(sender: str, data: Any) -> None:
        raise ValueError("boom")

    async def run() -> None:
        connection = ws_connection()
        connection.subscribe("Player.OnPlay", failing)
        connection.subscribe("Player.OnPlay", lambda *_: calls.append("called"))
        with caplog.at_level(logging.ERROR, logger="pykodi.kodi"):
            await notify(connection, "Player.OnPlay")

    asyncio.run(run())

    assert calls == ["called"]
    assert "Error in callback for Player.OnPlay" in caplog.text


def test_notification_without_data() -> None:
    calls: List[Tuple[str, Any]] = []

    async def run() -> None:
        connection = ws_connection()
        connection.subscribe("System.OnQuit", lambda *args: calls.append(args))
        await notify(connection, "System.OnQuit", None)

    asyncio.run(run())

    assert calls == [("xbmc", None)]


def test_subscription_survives_reconnect() -> None:
    calls: List[Any] = []

    async def kodi(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "Player.OnPlay",
                "params": {"sender": "xbmc", "data": DATA},
            }
        )
        async for _ in websocket:
            pass
        return websocket

    async def wait_for_calls(count: int) -> None:
        for _ in range(100):
            if len(calls) >= count:
                return
            await asyncio.sleep(0.01)

    async def run() -> None:
        app = web.Application()
        app.router.add_get("/jsonrpc", kodi)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, None, None, session=session
                )
                connection.subscribe("Player.OnPlay", lambda *args: calls.append(args))
                await connection.connect()
                await wait_for_calls(1)
                await connection.close()
                await connection.connect()
                await wait_for_calls(2)
                await connection.close()
        finally:
            await runner.cleanup()

    asyncio.run(run())

    # One notification per connection, delivered once each
    assert calls == [("xbmc", DATA), ("xbmc", DATA)]


def test_http_connection_cannot_subscribe() -> None:
    async def run() -> None:
        connection = get_kodi_connection(
            "kodi", 8080, None, None, None, session=MagicMock()
        )
        with pytest.raises(NotImplementedError):
            connection.subscribe("Player.OnPlay", lambda *_: None)

    asyncio.run(run())
