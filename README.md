# PyKodi

An async python interface for [Kodi](https://kodi.tv/) over JSON-RPC.
This is mostly designed to integrate with HomeAssistant. If you have other needs, there might be better packages available.

## Installation

You can install PyKodi from [PyPI](https://pypi.org/project/pykodi/):

    pip3 install pykodi

Python 3.7 and above are supported.


## How to use

```python
from pykodi import get_kodi_connection, Kodi
kc = get_kodi_connection(<host>, <port>, <ws_port>, <username>, <password>, <ssl>, <timeout>, <session>)
# if ws_port is None the connection will be over HTTP, otherwise over WebSocket.
# ssl defaults to False (only relevant if you have a proxy), timeout to 5 (seconds)
# session is generated if not passed in

# you can also pass in your own session
await kc.connect()

kodi = Kodi(kc)

await kodi.ping()
properties = await kodi.get_application_properties(["name", "version"])

await kodi.play()
await kodi.volume_up()
await kodi.pause()
...

# over WebSocket you can subscribe to Kodi notifications
def on_play(sender, data):
    print(data["item"])

unsubscribe = kc.subscribe("Player.OnPlay", on_play)
...
unsubscribe()
```

### Stream and HDR details

```python
players = await kodi.get_players()
info = await kodi.get_stream_info(players[0])

info["currentvideostream"]                  # {"codec": "hevc", "width": 3840, "height": 2160, ...}
info["infolabels"]["VideoPlayer.HdrType"]   # "dolbyvision", "hdr10", "hlg" or "" ("hdr10plus" from Kodi 22)
info["infolabels"]["System.ScreenResolution"]
info["hdrhints"]                            # ["dolbyvision", "hdr10"], taken from the file name
```

The info labels are returned as Kodi reports them. Labels that the running Kodi version does not know are empty strings. `hdrhints` lists the HDR formats named in the file name, because Kodi 21 reports HDR10+ as `hdr10` and does not report the HDR10 base layer of a Dolby Vision stream.
