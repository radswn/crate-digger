"""Typed dictionary definitions for Spotify entities used across crate-digger.

Keeping these in one place makes data shapes explicit and helps static checkers.
"""

from __future__ import annotations

from typing import List, Optional, TypedDict


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
    is_added: Optional[bool]


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
