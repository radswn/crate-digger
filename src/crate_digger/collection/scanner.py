from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mutagen import File, MutagenError
from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.wave import WAVE

from crate_digger.collection.comments import read_comment
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
    file_created_at = _file_created_at(path)
    try:
        audio = File(path, easy=True)
    except MutagenError:
        audio = None
    artwork_mime, artwork_data = read_embedded_artwork(path)
    if audio is None:
        return LocalTrack(
            path=path,
            title=None,
            artist=None,
            album=None,
            duration_seconds=None,
            bitrate=None,
            audio_format=path.suffix.removeprefix(".").upper() or None,
            file_created_at=file_created_at,
            artwork_mime=artwork_mime,
            artwork_data=artwork_data,
        )

    return LocalTrack(
        path=path,
        title=_first_tag(audio.tags, "title"),
        artist=_first_tag(audio.tags, "artist", "albumartist"),
        album=_first_tag(audio.tags, "album"),
        comment=read_comment(path)
        or _first_tag(audio.tags, "comment", "comments", "description"),
        genre=_first_tag(audio.tags, "genre"),
        release_date=_first_tag(
            audio.tags,
            "date",
            "originaldate",
            "releasedate",
            "year",
        ),
        file_created_at=file_created_at,
        duration_seconds=getattr(audio.info, "length", None),
        bitrate=getattr(audio.info, "bitrate", None),
        audio_format=path.suffix.removeprefix(".").upper() or None,
        artwork_mime=artwork_mime,
        artwork_data=artwork_data,
    )


def read_embedded_artwork(path: Path) -> tuple[str | None, bytes | None]:
    try:
        audio = File(path, easy=False)
    except MutagenError:
        return None, None
    if audio is None:
        return None, None

    flac_picture = _first_picture(getattr(audio, "pictures", None))
    if flac_picture is not None:
        return getattr(flac_picture, "mime", None), getattr(flac_picture, "data", None)

    tags = getattr(audio, "tags", None)
    if not tags:
        return None, None

    mp4_cover = _first_cover(tags)
    if isinstance(mp4_cover, bytes):
        mime = _mp4_cover_mime(mp4_cover)
        return mime, bytes(mp4_cover)

    for key, value in tags.items():
        if str(key).startswith("APIC"):
            return getattr(value, "mime", None), getattr(value, "data", None)

    return None, None


def overwrite_embedded_artwork(path: Path, *, mime: str, data: bytes) -> bool:
    """Replace embedded cover art for formats Mutagen can safely update."""

    suffix = path.suffix.lower()
    try:
        if suffix == ".flac":
            _overwrite_flac_artwork(path, mime=mime, data=data)
        elif suffix == ".mp3":
            _overwrite_mp3_artwork(path, mime=mime, data=data)
        elif suffix in {".m4a", ".mp4", ".alac"}:
            _overwrite_mp4_artwork(path, mime=mime, data=data)
        elif suffix == ".wav":
            _overwrite_wav_artwork(path, mime=mime, data=data)
        elif suffix in {".aiff", ".aif"}:
            _overwrite_aiff_artwork(path, mime=mime, data=data)
        else:
            return False
    except (MutagenError, OSError):
        return False

    return True


def _overwrite_flac_artwork(path: Path, *, mime: str, data: bytes) -> None:
    audio = FLAC(path)
    picture = Picture()
    picture.type = 3
    picture.mime = mime
    picture.desc = "Cover"
    picture.data = data
    audio.clear_pictures()
    audio.add_picture(picture)
    audio.save()


def _overwrite_mp3_artwork(path: Path, *, mime: str, data: bytes) -> None:
    audio = MP3(path)
    if audio.tags is None:
        try:
            audio.add_tags()
        except ID3NoHeaderError:
            pass
    if audio.tags is None:
        return

    audio.tags.delall("APIC")
    audio.tags.add(
        APIC(
            encoding=3,
            mime=mime,
            type=3,
            desc="Cover",
            data=data,
        )
    )
    audio.save()


def _overwrite_mp4_artwork(path: Path, *, mime: str, data: bytes) -> None:
    audio = MP4(path)
    image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
    audio["covr"] = [MP4Cover(data, imageformat=image_format)]
    audio.save()


def _overwrite_wav_artwork(path: Path, *, mime: str, data: bytes) -> None:
    audio = WAVE(path)
    _overwrite_id3_artwork(audio, mime=mime, data=data)
    audio.save()


def _overwrite_aiff_artwork(path: Path, *, mime: str, data: bytes) -> None:
    audio = AIFF(path)
    _overwrite_id3_artwork(audio, mime=mime, data=data)
    audio.save()


def _overwrite_id3_artwork(audio: Any, *, mime: str, data: bytes) -> None:
    if audio.tags is None:
        audio.add_tags()
    if audio.tags is None:
        return

    audio.tags.delall("APIC")
    audio.tags.add(
        APIC(
            encoding=3,
            mime=mime,
            type=3,
            desc="Cover",
            data=data,
        )
    )


def _first_picture(pictures: object) -> object | None:
    if isinstance(pictures, list) and pictures:
        return pictures[0]
    return None


def _first_cover(tags: Any) -> object | None:
    covers = tags.get("covr")
    if isinstance(covers, list) and covers:
        return covers[0]
    return None


def _mp4_cover_mime(cover: object) -> str:
    imageformat = getattr(cover, "imageformat", None)
    if imageformat == getattr(cover, "FORMAT_PNG", 14):
        return "image/png"
    return "image/jpeg"


def _file_created_at(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    timestamp = getattr(stat, "st_birthtime", stat.st_ctime)
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


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
