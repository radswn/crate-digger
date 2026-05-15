from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LocalTrack:
    """A lightweight view of one audio file in the local collection."""

    path: Path
    title: str | None
    artist: str | None
    album: str | None
    duration_seconds: float | None
    bitrate: int | None
    audio_format: str | None
    artwork_mime: str | None = None
    artwork_data: bytes | None = None
    spotify_uri: str | None = None

    @property
    def display_title(self) -> str:
        return self.title or self.path.stem

    @property
    def display_artist(self) -> str:
        return self.artist or "Unknown artist"
