from typing import List, TypedDict


class SpotifyArtist(TypedDict):
    """Minimal representation of an artist returned by Spotify APIs."""

    name: str


class SpotifyAlbum(TypedDict):
    """Album metadata as returned by album and search endpoints."""

    uri: str
    name: str
    label: str
    release_date: str


class SpotifyTrack(TypedDict):
    """Track metadata used throughout the project."""

    name: str
    uri: str
    artists: List[SpotifyArtist]
    album: SpotifyAlbum


class TrackInfo(TypedDict):
    """Tracks grouped for a specific album, ready for messaging or display."""

    album_name: str
    tracks: List[SpotifyTrack]


__all__ = [
    "SpotifyArtist",
    "SpotifyAlbum",
    "SpotifyTrack",
    "TrackInfo",
]
