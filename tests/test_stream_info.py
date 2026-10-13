"""Tests for reading stream and HDR details of the playing item."""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from pykodi import Kodi

PLAYER = {"playerid": 1, "type": "video"}

# Kodi 21.3, Dolby Vision movie with embedded cover art.
PROPERTIES = {
    "currentvideostream": {
        "codec": "hevc",
        "height": 2160,
        "index": 0,
        "language": "eng",
        "name": "Movietitle.2160p.BluRay.REMUX.DoVi.HDR10.HEVC",
        "width": 3840,
    },
    "videostreams": [
        {
            "codec": "hevc",
            "height": 2160,
            "index": 0,
            "language": "eng",
            "name": "Movietitle.2160p.BluRay.REMUX.DoVi.HDR10.HEVC",
            "width": 3840,
        },
        {
            "codec": "mjpeg",
            "height": 360,
            "index": 1,
            "language": "",
            "name": "",
            "width": 640,
        },
    ],
    "currentaudiostream": {
        "channels": 6,
        "codec": "eac3",
        "index": 0,
        "language": "eng",
    },
    "audiostreams": [{"channels": 6, "codec": "eac3", "index": 0, "language": "eng"}],
}

LABELS = {
    "VideoPlayer.HdrType": "dolbyvision",
    "VideoPlayer.HdrDetail": "",
    "VideoPlayer.VideoResolution": "4K",
    "VideoPlayer.VideoAspect": "1.78",
    "System.SupportedHDRTypes": "",
    "System.ScreenResolution": "1920x1080 @ 23.98 Hz - Full screen",
    "System.ScreenWidth": "1920",
    "System.ScreenHeight": "1080",
    "Player.Filename": "Movietitle.2160p.BluRay.REMUX.DoVi.HDR10.HEVC.mkv",
}


def fake_kodi(properties: Dict[str, Any], labels: Dict[str, str]) -> Kodi:
    async def get_properties(playerid: int, requested: List[str]) -> Dict[str, Any]:
        assert playerid == PLAYER["playerid"]
        return {key: properties[key] for key in requested if key in properties}

    async def get_info_labels(requested: List[str]) -> Dict[str, str]:
        return {label: labels.get(label, "") for label in requested}

    server = SimpleNamespace(
        Player=SimpleNamespace(GetProperties=get_properties),
        XBMC=SimpleNamespace(GetInfoLabels=get_info_labels),
    )
    return Kodi(SimpleNamespace(server=server))


def stream_info(properties: Dict[str, Any], labels: Dict[str, str]) -> Dict[str, Any]:
    return asyncio.run(fake_kodi(properties, labels).get_stream_info(PLAYER))


def test_stream_info() -> None:
    info = stream_info(PROPERTIES, LABELS)

    assert info["currentvideostream"] == PROPERTIES["currentvideostream"]
    assert info["videostreams"] == PROPERTIES["videostreams"][:1]
    assert info["currentaudiostream"] == PROPERTIES["currentaudiostream"]
    assert info["audiostreams"] == PROPERTIES["audiostreams"]
    assert info["infolabels"] == LABELS
    assert info["hdrhints"] == ["dolbyvision", "hdr10"]


def test_cover_art_kept_when_current() -> None:
    mjpeg = {
        "codec": "mjpeg",
        "height": 480,
        "index": 0,
        "language": "",
        "name": "",
        "width": 640,
    }
    properties = {
        "currentvideostream": mjpeg,
        "videostreams": [mjpeg],
        "currentaudiostream": {},
        "audiostreams": [],
    }

    info = stream_info(properties, {"Player.Filename": "camera.avi"})

    assert info["videostreams"] == [mjpeg]
    assert info["hdrhints"] == []


def test_no_video_stream() -> None:
    properties = {
        "currentvideostream": None,
        "videostreams": [],
        "currentaudiostream": {"channels": 2, "codec": "flac", "index": 0},
        "audiostreams": [{"channels": 2, "codec": "flac", "index": 0}],
    }

    info = stream_info(properties, {"Player.Filename": "song.flac"})

    assert info["videostreams"] == []
    assert info["infolabels"]["VideoPlayer.HdrType"] == ""
    assert info["hdrhints"] == []


@pytest.mark.parametrize(
    ("filename", "hints"),
    [
        (
            "Movietitle.2160p.BluRay.DoVi-HDR.x265.mkv",
            ["dolbyvision", "hdr10"],
        ),
        (
            "Movietitle.2160p.BluRay.HDR.x265.mkv",
            ["hdr10"],
        ),
        (
            "Movietitle.2160p.BluRay.HDR10Plus.REMUX.HEVC.mkv",
            ["hdr10plus"],
        ),
        ("Movietitle.1080p.BluRay.x264.mkv", []),
        ("Movietitle.2160p.HDR10+.mkv", ["hdr10plus"]),
        ("Movietitle.2160p.hdr10p.mkv", ["hdr10plus"]),
        ("Movietitle 2160p Dolby Vision.mkv", ["dolbyvision"]),
        ("Movietitle_2160p_DV_HDR10.mkv", ["dolbyvision", "hdr10"]),
        ("Movietitle.2160p.DolbyVision.HDR10Plus.mkv", ["dolbyvision", "hdr10plus"]),
        ("Broadcast.2160p.HLG.ts", ["hlg"]),
        ("Movietitle.2160p.HDR10+DV.mkv", ["dolbyvision", "hdr10plus"]),
        ("Movietitle.2160p.DV.HDR10+.mkv", ["dolbyvision", "hdr10plus"]),
        ("Movietitle.1999.DVDRip.HDRip.avi", []),
        ("clip.dv", []),
        ("", []),
    ],
)
def test_hdr_hints(filename: str, hints: List[str]) -> None:
    info = stream_info(PROPERTIES, {"Player.Filename": filename})

    assert info["hdrhints"] == hints
