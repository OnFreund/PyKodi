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
```
