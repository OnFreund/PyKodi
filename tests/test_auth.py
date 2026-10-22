"""Tests for HTTP Basic authentication."""

import asyncio
import base64
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
import warnings

import aiohttp
from aiohttp import web
import pytest

from pykodi import Kodi, get_kodi_connection


def basic(credentials: str) -> str:
    return "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii")


async def start_server(app: web.Application) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner


def http_authorization(username: Optional[str], password: Optional[str]) -> Any:
    """Ping over HTTP and return the Authorization header Kodi received."""
    received: List[Optional[str]] = []

    async def jsonrpc(request: web.Request) -> web.Response:
        received.append(request.headers.get("Authorization"))
        message = await request.json()
        return web.json_response(
            {"jsonrpc": "2.0", "id": message["id"], "result": "pong"}
        )

    async def run() -> None:
        app = web.Application()
        app.router.add_post("/jsonrpc", jsonrpc)
        runner = await start_server(app)
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", port, None, username, password, session=session
                )
                assert await Kodi(connection).ping()
        finally:
            await runner.cleanup()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(run())

    auth_warnings = [w for w in caught if "auth" in str(w.message).lower()]
    assert auth_warnings == []
    assert len(received) == 1
    return received[0]


def test_http_sends_authorization_header() -> None:
    assert http_authorization("kodi", "secret") == basic("kodi:secret")


def test_http_without_username_sends_no_header() -> None:
    assert http_authorization(None, None) is None


def test_password_is_encoded_as_utf8() -> None:
    assert http_authorization("kodi", "jelszó") == basic("kodi:jelszó")


def test_missing_password_is_sent_empty() -> None:
    assert http_authorization("kodi", None) == basic("kodi:")


def test_websocket_sends_authorization_header() -> None:
    received: Dict[str, Optional[str]] = {}

    async def jsonrpc(request: web.Request) -> web.WebSocketResponse:
        received["authorization"] = request.headers.get("Authorization")
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        async for _ in websocket:
            pass
        return websocket

    async def run() -> None:
        app = web.Application()
        app.router.add_get("/jsonrpc", jsonrpc)
        runner = await start_server(app)
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                connection = get_kodi_connection(
                    "127.0.0.1", 8080, port, "kodi", "secret", session=session
                )
                await connection.connect()
                await connection.close()
        finally:
            await runner.cleanup()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(run())

    assert [w for w in caught if "auth" in str(w.message).lower()] == []
    assert received["authorization"] == basic("kodi:secret")


def test_colon_in_username_is_rejected() -> None:
    with pytest.raises(ValueError):
        get_kodi_connection("kodi", 8080, None, "ko:di", "secret", session=MagicMock())


def test_missing_password_in_image_url() -> None:
    connection = get_kodi_connection(
        "kodi", 8080, None, "kodi", None, session=MagicMock()
    )
    assert connection.thumbnail_url("image://cover.jpg/").startswith(
        "http://kodi:@kodi:8080/image/"
    )
