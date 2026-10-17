"""Implementation of a Kodi inteface."""
import aiohttp
import urllib

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List
import jsonrpc_base
import jsonrpc_async
import jsonrpc_websocket
import os
import re

_STREAM_INFO_PROPERTIES = [
    "currentvideostream",
    "videostreams",
    "currentaudiostream",
    "audiostreams",
]

_STREAM_INFO_LABELS = [
    "VideoPlayer.HdrType",
    "VideoPlayer.HdrDetail",
    "VideoPlayer.VideoResolution",
    "VideoPlayer.VideoAspect",
    "System.SupportedHDRTypes",
    "System.ScreenResolution",
    "System.ScreenWidth",
    "System.ScreenHeight",
    "Player.Filename",
]

# Cover art embedded in a container shows up as a video stream with these codecs.
_COVER_ART_CODECS = ("mjpeg", "png")

# Release tags in file names. A tag starts after anything but a letter or digit
# and ends before one, except "HDR10+", which ends with the plus sign.
_HDR_HINT_PATTERNS = (
    ("dolbyvision", r"(?:dovi|dv|dolby[ ._-]?vision)(?![a-z0-9])"),
    ("hdr10plus", r"hdr10(?:(?:plus|p)(?![a-z0-9])|\+)"),
    ("hdr10", r"hdr(?:10)?(?![a-z0-9+])"),
    ("hlg", r"hlg(?![a-z0-9])"),
)
_HDR_HINTS = tuple(
    (hint, re.compile(r"(?<![a-z0-9])" + pattern, re.IGNORECASE))
    for hint, pattern in _HDR_HINT_PATTERNS
)


def _hdr_hints(filename):
    """Return the HDR formats named in a file name.

    Kodi 21 reports HDR10+ as hdr10 and does not report the HDR10 base layer
    of a Dolby Vision stream, but release names usually contain both. The
    extension is ignored, so a raw DV camera file (.dv) gives no hint.
    """
    name = os.path.splitext(filename or "")[0]
    return [hint for hint, pattern in _HDR_HINTS if pattern.search(name)]

_LOGGER = logging.getLogger(__name__)

NotificationCallback = Callable[[str, Any], Any]


def get_kodi_connection(
    host, port, ws_port, username, password, ssl=False, timeout=5, session=None
):
    """Returns a Kodi connection."""
    if ws_port is None:
        return KodiHTTPConnection(host, port, username, password, ssl, timeout, session)
    else:
        return KodiWSConnection(
            host, port, ws_port, username, password, ssl, timeout, session
        )


def _url_host(host):
    """Return a host formatted for use in a URL.

    Bare IPv6 literals must be bracketed in URLs (RFC 3986 section 3.2.2),
    otherwise their colons are parsed as port separators.
    """
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


class KodiConnection:
    """A connection to Kodi interface."""

    def __init__(self, host, port, username, password, ssl, timeout, session):
        """Initialize the object."""
        self._session = session
        self._created_session = False
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._created_session = True

        self._kwargs = {"timeout": timeout, "session": self._session}

        if username is not None:
            self._kwargs["auth"] = aiohttp.BasicAuth(username, password)
            image_auth_string = f"{username}:{password}@"
        else:
            image_auth_string = ""

        http_protocol = "https" if ssl else "http"

        self._image_url = f"{http_protocol}://{image_auth_string}{_url_host(host)}:{port}/image"

    async def connect(self):
        """Connect to kodi."""
        pass

    async def close(self):
        """Close the connection."""
        if self._created_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._created_session = False

    @property
    def server(self):
        raise NotImplementedError

    @property
    def connected(self):
        """Is the server connected."""
        raise NotImplementedError

    @property
    def can_subscribe(self):
        return False

    def subscribe(
        self, method: str, callback: NotificationCallback
    ) -> Callable[[], None]:
        """Subscribe to a Kodi notification."""
        raise NotImplementedError("This connection does not receive notifications")

    def thumbnail_url(self, thumbnail):
        """Get the URL for a thumbnail."""
        if thumbnail is None:
            return None

        url_components = urllib.parse.urlparse(thumbnail)
        if url_components.scheme == "image":
            return f"{self._image_url}/{urllib.parse.quote_plus(thumbnail)}"


class KodiHTTPConnection(KodiConnection):
    """An HTTP connection to Kodi."""

    def __init__(self, host, port, username, password, ssl, timeout, session):
        """Initialize the object."""
        super().__init__(host, port, username, password, ssl, timeout, session)

        http_protocol = "https" if ssl else "http"

        http_url = f"{http_protocol}://{_url_host(host)}:{port}/jsonrpc"

        self._http_server = jsonrpc_async.Server(http_url, **self._kwargs)

    @property
    def connected(self):
        """Is the server connected."""
        return True

    async def close(self):
        """Close the connection."""
        self._http_server = None
        await super().close()

    @property
    def server(self):
        """Active server for json-rpc requests."""
        return self._http_server


class KodiWSConnection(KodiConnection):
    """A WS connection to Kodi."""

    def __init__(self, host, port, ws_port, username, password, ssl, timeout, session):
        """Initialize the object."""
        super().__init__(host, port, username, password, ssl, timeout, session)

        ws_protocol = "wss" if ssl else "ws"
        ws_url = f"{ws_protocol}://{_url_host(host)}:{ws_port}/jsonrpc"

        self._ws_server = jsonrpc_websocket.Server(ws_url, **self._kwargs)
        self._subscriptions: Dict[str, List[NotificationCallback]] = {}

    @property
    def connected(self):
        """Return whether websocket is connected."""
        return self._ws_server.connected

    @property
    def can_subscribe(self):
        return True

    def subscribe(
        self, method: str, callback: NotificationCallback
    ) -> Callable[[], None]:
        """Subscribe to a Kodi notification, for example "Player.OnPlay".

        The callback is called with the sender and the data of the
        notification, and it can be a coroutine function. More than one
        callback can subscribe to the same notification. Returns a function
        that removes the subscription.

        Subscriptions stay active across reconnects, so subscribe only once
        per callback. Do not assign a handler on ``server`` for a
        notification that is also subscribed here, because the two would
        replace each other. Notification names with a part starting with an
        underscore cannot be subscribed to.
        """
        callbacks = self._subscriptions.get(method)
        if callbacks is None:
            callbacks = self._subscriptions[method] = []
            *path, name = method.split(".")
            target = self._ws_server
            for part in path:
                target = getattr(target, part)
            setattr(target, name, self._dispatcher(method))
        callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in callbacks:
                callbacks.remove(callback)

        return unsubscribe

    def _dispatcher(self, method: str) -> Callable[..., Any]:
        """Return a handler that calls every callback subscribed to method."""

        async def dispatch(sender: str, data: Any) -> None:
            for callback in list(self._subscriptions[method]):
                try:
                    result = callback(sender, data)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    _LOGGER.exception("Error in callback for %s", method)

        return dispatch

    async def connect(self):
        """Connect to kodi over websocket."""
        if self.connected:
            return
        try:
            await self._ws_server.ws_connect()
        except (
            jsonrpc_base.jsonrpc.TransportError,
            asyncio.exceptions.CancelledError,
        ) as error:
            raise CannotConnectError from error

    async def close(self):
        """Close the connection."""
        await self._ws_server.close()
        await super().close()

    @property
    def server(self):
        """Active server for json-rpc requests."""
        return self._ws_server


class Kodi:
    """A high level Kodi interface."""

    def __init__(self, connection):
        """Initialize the object."""
        self._conn = connection
        self._server = connection.server

    async def ping(self):
        """Ping the server."""
        try:
            response = await self._server.JSONRPC.Ping()
            return response == "pong"
        except jsonrpc_base.jsonrpc.TransportError as error:
            if "401" in str(error):
                raise InvalidAuthError from error
            else:
                raise CannotConnectError from error

    async def get_application_properties(self, properties):
        """Get value of given properties."""
        return await self._server.Application.GetProperties(properties)

    async def get_player_properties(self, player, properties):
        """Get value of given properties."""
        return await self._server.Player.GetProperties(player["playerid"], properties)

    async def get_playing_item_properties(self, player, properties):
        """Get value of given properties."""
        return (await self._server.Player.GetItem(player["playerid"], properties))[
            "item"
        ]

    async def get_stream_info(self, player):
        """Get the video and audio streams, HDR and display details.

        Returns the stream properties of the player, the raw HDR, resolution
        and display info labels under "infolabels" and the HDR formats named
        in the file name under "hdrhints". Info labels unknown to the Kodi
        version are returned as empty strings. Embedded cover art is left out
        of "videostreams" unless it is the current video stream.
        """
        info, labels = await asyncio.gather(
            self.get_player_properties(player, _STREAM_INFO_PROPERTIES),
            self._server.XBMC.GetInfoLabels(_STREAM_INFO_LABELS),
        )
        current = info.get("currentvideostream") or {}
        info["videostreams"] = [
            stream
            for stream in info.get("videostreams") or []
            if stream.get("codec") not in _COVER_ART_CODECS
            or stream.get("index") == current.get("index")
        ]
        info["infolabels"] = labels
        info["hdrhints"] = _hdr_hints(labels.get("Player.Filename"))
        return info

    async def volume_up(self):
        """Send volume up command."""
        await self._server.Input.ExecuteAction("volumeup")

    async def volume_down(self):
        """Send volume down command."""
        await self._server.Input.ExecuteAction("volumedown")

    async def set_volume_level(self, volume):
        """Set volume level, range 0-100."""
        await self._server.Application.SetVolume(volume)

    async def mute(self, mute):
        """Send (un)mute command."""
        await self._server.Application.SetMute(mute)

    async def _set_play_state(self, state):
        players = await self.get_players()

        if players:
            await self._server.Player.PlayPause(players[0]["playerid"], state)

    async def play_pause(self):
        """Send toggle command command."""
        await self._set_play_state("toggle")

    async def play(self):
        """Send play command."""
        await self._set_play_state(True)

    async def pause(self):
        """Send pause command."""
        await self._set_play_state(False)

    async def stop(self):
        """Send stop command."""
        players = await self.get_players()

        if players:
            await self._server.Player.Stop(players[0]["playerid"])

    async def _goto(self, direction):
        players = await self.get_players()

        if players:
            if direction == "previous":
                # First seek to position 0. Kodi goes to the beginning of the
                # current track if the current track is not at the beginning.
                await self._server.Player.Seek(
                    players[0]["playerid"], {"percentage": 0}
                )

            await self._server.Player.GoTo(players[0]["playerid"], direction)

    async def next_track(self):
        """Send next track command."""
        await self._goto("next")

    async def previous_track(self):
        """Send previous track command."""
        await self._goto("previous")

    async def media_seek(self, position):
        """Send seek command."""
        players = await self.get_players()

        time = {"milliseconds": int((position % 1) * 1000)}

        position = int(position)

        time["seconds"] = int(position % 60)
        position /= 60

        time["minutes"] = int(position % 60)
        position /= 60

        time["hours"] = int(position)

        if players:
            await self._server.Player.Seek(players[0]["playerid"], {"time": time})

    async def play_item(self, item):
        await self._server.Player.Open(**{"item": item})

    async def play_channel(self, channel_id):
        """Play the given channel."""
        await self.play_item({"channelid": channel_id})

    async def play_playlist(self, playlist_id):
        """Play the given playlist."""
        await self.play_item({"playlistid": playlist_id})

    async def play_directory(self, directory):
        """Play the given directory."""
        await self.play_item({"directory": directory})

    async def play_file(self, file):
        """Play the given file."""
        await self.play_item({"file": file})

    async def set_shuffle(self, shuffle):
        """Set shuffle mode, for the first player."""
        players = await self.get_players()
        if players:
            await self._server.Player.SetShuffle(
                **{"playerid": players[0]["playerid"], "shuffle": shuffle}
            )

    async def call_method(self, method, **kwargs):
        """Run Kodi JSONRPC API method with params."""
        if "." not in method or len(method.split(".")) != 2:
            raise ValueError(f"Invalid method: {method}")
        return await getattr(self._server, method)(**kwargs)

    async def _add_item_to_playlist(self, item):
        await self._server.Playlist.Add(**{"playlistid": 0, "item": item})

    async def add_song_to_playlist(self, song_id):
        """Add song to default playlist (i.e. playlistid=0)."""
        await self._add_item_to_playlist({"songid": song_id})

    async def add_album_to_playlist(self, album_id):
        """Add album to default playlist (i.e. playlistid=0)."""
        await self._add_item_to_playlist({"albumid": album_id})

    async def add_artist_to_playlist(self, artist_id):
        """Add album to default playlist (i.e. playlistid=0)."""
        await self._add_item_to_playlist({"artistid": artist_id})

    async def clear_playlist(self):
        """Clear default playlist (i.e. playlistid=0)."""
        await self._server.Playlist.Clear(**{"playlistid": 0})

    async def get_artists(self, properties=None):
        """Get artists list."""
        return await self._server.AudioLibrary.GetArtists(
            **_build_query(properties=properties)
        )

    async def get_artist_details(self, artist_id=None, properties=None):
        """Get artist details."""
        return await self._server.AudioLibrary.GetArtistDetails(
            **_build_query(artistid=artist_id, properties=properties)
        )

    async def get_albums(self, artist_id=None, album_id=None, properties=None):
        """Get albums list."""
        filter = {}
        if artist_id:
            filter["artistid"] = artist_id
        if album_id:
            filter["albumid"] = album_id

        return await self._server.AudioLibrary.GetAlbums(
            **_build_query(filter=filter, properties=properties)
        )

    async def get_album_details(self, album_id, properties=None):
        """Get album details."""
        return await self._server.AudioLibrary.GetAlbumDetails(
            **_build_query(albumid=album_id, properties=properties)
        )

    async def get_songs(self, artist_id=None, album_id=None, properties=None):
        """Get songs list."""
        filter = {}
        if artist_id:
            filter["artistid"] = artist_id
        if album_id:
            filter["albumid"] = album_id

        return await self._server.AudioLibrary.GetSongs(
            **_build_query(filter=filter, properties=properties)
        )

    async def get_movies(self, properties=None):
        """Get movies list."""
        return await self._server.VideoLibrary.GetMovies(
            **_build_query(properties=properties)
        )

    async def get_movie_details(self, movie_id, properties=None):
        """Get movie details."""
        return await self._server.VideoLibrary.GetMovieDetails(
            **_build_query(movieid=movie_id, properties=properties)
        )

    async def get_seasons(self, tv_show_id, properties=None):
        """Get seasons list."""
        return await self._server.VideoLibrary.GetSeasons(
            **_build_query(tvshowid=tv_show_id, properties=properties)
        )

    async def get_season_details(self, season_id, properties=None):
        """Get songs list."""
        return await self._server.VideoLibrary.GetSeasonDetails(
            **_build_query(seasonid=season_id, properties=properties)
        )

    async def get_episodes(self, tv_show_id, season_id, properties=None):
        """Get episodes list."""

        return await self._server.VideoLibrary.GetEpisodes(
            **_build_query(tvshowid=tv_show_id, season=season_id, properties=properties)
        )

    async def get_tv_shows(self, properties=None):
        """Get tv shows list."""
        return await self._server.VideoLibrary.GetTVShows(
            **_build_query(properties=properties)
        )

    async def get_tv_show_details(self, tv_show_id=None, properties=None):
        """Get songs list."""
        return await self._server.VideoLibrary.GetTVShowDetails(
            **_build_query(tvshowid=tv_show_id, properties=properties)
        )

    async def get_channels(self, channel_group_id, properties=None):
        """Get channels list."""
        return await self._server.PVR.GetChannels(
            **_build_query(channelgroupid=channel_group_id, properties=properties)
        )

    async def get_players(self):
        """Return the active player objects."""
        return await self._server.Player.GetActivePlayers()

    async def send_notification(self, title, message, icon="info", displaytime=10000):
        """Display on-screen message."""
        await self._server.GUI.ShowNotification(title, message, icon, displaytime)

    def thumbnail_url(self, thumbnail):
        """Get the URL for a thumbnail."""
        return self._conn.thumbnail_url(thumbnail)


def _build_query(**kwargs):
    """Build query."""
    return {key: val for key, val in kwargs.items() if val is not None}


class CannotConnectError(Exception):
    """Exception to indicate an error in connection."""


class InvalidAuthError(Exception):
    """Exception to indicate an error in authentication."""
