from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mutagen import File, MutagenError

from crate_digger.collection.models import LocalTrack


SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {
        ".aiff",
        ".aif",
        ".alac",
        ".flac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".opus",
        ".wav",
    }
)


def discover_audio_files(paths: Iterable[str | Path]) -> list[Path]:
    """Return supported audio files under the configured collection paths."""

    files: list[Path] = []
    for raw_path in paths:
        root = Path(raw_path).expanduser()
        if root.is_file() and root.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
            files.append(root)
            continue
        if not root.is_dir():
            continue

        files.extend(
            sorted(
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
            )
        )

    return sorted(files)


def scan_collection(paths: Iterable[str | Path]) -> list[LocalTrack]:
    """Read lightweight metadata for supported audio files."""

    return [read_track_metadata(path) for path in discover_audio_files(paths)]


def read_track_metadata(path: Path) -> LocalTrack:
    try:
        audio = File(path, easy=True)
    except MutagenError:
        audio = None
    if audio is None:
        return LocalTrack(
            path=path,
            title=None,
            artist=None,
            album=None,
            duration_seconds=None,
            bitrate=None,
            audio_format=path.suffix.removeprefix(".").upper() or None,
        )

    return LocalTrack(
        path=path,
        title=_first_tag(audio.tags, "title"),
        artist=_first_tag(audio.tags, "artist", "albumartist"),
        album=_first_tag(audio.tags, "album"),
        duration_seconds=getattr(audio.info, "length", None),
        bitrate=getattr(audio.info, "bitrate", None),
        audio_format=path.suffix.removeprefix(".").upper() or None,
    )


def _first_tag(tags: Any, *keys: str) -> str | None:
    if not tags:
        return None

    for key in keys:
        value = tags.get(key)
        if isinstance(value, list) and value:
            return str(value[0])
        if isinstance(value, str) and value:
            return value

    return None
