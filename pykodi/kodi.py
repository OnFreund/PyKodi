"""Implementation of a Kodi inteface."""
import aiohttp
import urllib

# import asyncio
import jsonrpc_base
import jsonrpc_async
import jsonrpc_websocket


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


class KodiConnection:
    """A connection to Kodi interface."""

    def __init__(self, host, port, username, password, ssl, timeout, session):
        """Initialize the object."""
        self._session = session
        self._created_session = False
        if self._session == None:
            self._session = aiohttp.ClientSession()
            self._created_session = True

        self._kwargs = {"timeout": timeout, "session": self._session}

        if username is not None:
            self._kwargs["auth"] = aiohttp.BasicAuth(username, password)
            image_auth_string = f"{username}:{password}@"
        else:
            image_auth_string = ""

        http_protocol = "https" if ssl else "http"

        self._image_url = f"{http_protocol}://{image_auth_string}{host}:{port}/image"

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
        raise NotImplementedError()

    @property
    def connected(self):
        """Is the server connected."""
        raise NotImplementedError()

    @property
    def can_subscribe(self):
        return False

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

        http_url = f"{http_protocol}://{host}:{port}/jsonrpc"

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
        ws_url = f"{ws_protocol}://{host}:{ws_port}/jsonrpc"

        self._ws_server = jsonrpc_websocket.Server(ws_url, **self._kwargs)

    @property
    def connected(self):
        """Return whether websocket is connected."""
        return self._ws_server and self._ws_server.connected

    @property
    def can_subscribe(self):
        return True

    async def connect(self):
        """Connect to kodi over websocket."""
        if self.connected:
            return
        try:
            await self._ws_server.ws_connect()
        except jsonrpc_base.jsonrpc.TransportError as error:
            raise CannotConnectError() from error

    async def close(self):
        """Close the connection."""
        await self._ws_server.close()
        self._ws_server = None
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
                raise InvalidAuthError() from error
            else:
                raise CannotConnectError() from error

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
                await self._server.Player.Seek(players[0]["playerid"], 0)

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

        time = {}

        time["milliseconds"] = int((position % 1) * 1000)
        position = int(position)

        time["seconds"] = int(position % 60)
        position /= 60

        time["minutes"] = int(position % 60)
        position /= 60

        time["hours"] = int(position)

        if players:
            await self._server.Player.Seek(players[0]["playerid"], time)

    async def _play_item(self, item):
        await self._server.Player.Open({"item": item})

    async def play_channel(self, channel_id):
        """Play the given channel."""
        await self._play_item({"channelid": channel_id})

    async def play_playlist(self, playlist_id):
        """Play the given playlist."""
        await self._play_item({"playlistid": playlist_id})

    async def play_directory(self, directory):
        """Play the given directory."""
        await self._play_item({"directory": directory})

    async def play_file(self, file):
        """Play the given file."""
        await self._play_item({"file": file})

    async def set_shuffle(self, shuffle):
        """Set shuffle mode, for the first player."""
        players = await self.get_players()
        if players:
            await self._server.Player.SetShuffle(
                {"playerid": players[0]["playerid"], "shuffle": shuffle}
            )

    async def call_method(self, method, **kwargs):
        """Run Kodi JSONRPC API method with params."""
        return await getattr(self._server, method)(**kwargs)

    async def _add_item_to_playlist(self, item):
        params = {"playlistid": 0, "item": item}
        self._server.Playlist.Add(params)

    async def add_song_to_playlist(self, song_id):
        """Add song to default playlist (i.e. playlistid=0)."""
        await self._add_item_to_playlist({"songid": song_id})

    async def add_album_to_playlist(self, album_id):
        """Add album to default playlist (i.e. playlistid=0)."""
        await self._add_item_to_playlist({"albumid": album_id})

    async def clear_playlist(self):
        """Clear default playlist (i.e. playlistid=0)."""
        await self._server.Playlist.Clear({"playlistid": 0})

    async def get_artists(self):
        """Get artists list."""
        return await self._server.AudioLibrary.GetArtists()

    async def get_albums(self, artist_id=None):
        """Get albums list."""
        if artist_id is None:
            return await self._server.AudioLibrary.GetAlbums()

        return await self._server.AudioLibrary.GetAlbums(
            {"filter": {"artistid": artist_id}}
        )

    async def get_songs(self, artist_id=None):
        """Get songs list."""
        if artist_id is None:
            return await self._server.AudioLibrary.GetSongs()

        return await self._server.AudioLibrary.GetSongs(
            {"filter": {"artistid": int(artist_id)}}
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


class CannotConnectError(Exception):
    """Exception to indicate an error in connection."""


class InvalidAuthError(Exception):
    """Exception to indicate an error in authentication."""
