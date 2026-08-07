from math import ceil
from pathlib import Path
from typing import cast
from urllib.parse import unquote
from xml.etree import ElementTree

from crate_digger.collection.comments import extract_profile_tags
from crate_digger.collection.models import ImportedTrack, TagCategory


def parse_traktor(path: Path) -> list[ImportedTrack]:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ValueError(f"Invalid Traktor NML: {error}") from error
    if _local_name(root.tag) != "NML":
        raise ValueError("Invalid Traktor NML: expected NML root element")

    tracks: list[ImportedTrack] = []
    for entry in root.iter():
        if _local_name(entry.tag).upper() != "ENTRY":
            continue
        location = _child(entry, "LOCATION")
        info = _child(entry, "INFO")
        source_path = _location_path(location) if location is not None else None
        comment = _attribute(info, "COMMENT")
        comment2 = _attribute(info, "COMMENT2")
        tags = set(extract_profile_tags(comment))
        tags.update(extract_profile_tags(comment2))
        title = _attribute(entry, "TITLE") or _attribute(info, "TITLE")
        artist = _attribute(entry, "ARTIST") or _attribute(info, "ARTIST")
        tracks.append(
            ImportedTrack(
                source="traktor",
                source_path=source_path,
                source_track_id=_source_id(entry, source_path),
                title=title,
                artist=artist,
                genre=_attribute(info, "GENRE"),
                comment=comment,
                comment2=comment2,
                legacy_rating=convert_traktor_rating(_attribute(info, "RANKING")),
                tags=tuple(
                    (cast(TagCategory, category), value)
                    for category, value in sorted(tags)
                ),
                invalid_reason=None if source_path else "Missing track location",
            )
        )
    return tracks


def convert_traktor_rating(value: str | None) -> int | None:
    """Convert Traktor's 0..255 ranking to stars using 51 points per star.

    Some exports contain literal 1..5 values; those are retained as-is. Zero means
    unrated. Invalid and out-of-range values are ignored.
    """

    if value is None or not value.strip():
        return None
    try:
        raw = int(value)
    except ValueError:
        return None
    if raw == 0:
        return None
    if 1 <= raw <= 5:
        return raw
    if 6 <= raw <= 255:
        return min(5, ceil(raw / 51))
    return None


def _location_path(location: ElementTree.Element) -> str | None:
    filename = _attribute(location, "FILE")
    if not filename:
        return None
    directory = unquote(location.get("DIR", "")).replace("\\", "/")
    if "/:" in directory:
        directory = "/".join(part for part in directory.split("/:") if part)
    directory = directory.rstrip("/:")
    volume = unquote(location.get("VOLUME", "")).strip()

    if volume and len(volume) == 2 and volume[1] == ":":
        prefix = volume
    elif volume == "/":
        prefix = ""
        directory = f"/{directory.lstrip('/')}"
    elif directory.startswith("/"):
        prefix = ""
    else:
        # macOS NML volumes name the disk; indexed paths are rooted at /.
        prefix = "" if volume else ""
        if volume:
            directory = f"/{directory}"

    parts = [part for part in (prefix, directory, unquote(filename)) if part]
    path = "/".join(part.strip("/") for part in parts)
    if directory.startswith("/") and not prefix:
        path = f"/{path}"
    return path or None


def _source_id(entry: ElementTree.Element, source_path: str | None) -> str | None:
    for key in ("AUDIO_ID", "TRACKID", "UNIQUE_ID", "ID"):
        value = _attribute(entry, key)
        if value:
            return value
    audio_id = _child(entry, "AUDIO_ID")
    if audio_id is not None:
        value = _attribute(audio_id, "VALUE") or _attribute(audio_id, "ID")
        if value:
            return value
    return source_path


def _child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for child in element:
        if _local_name(child.tag).upper() == name:
            return child
    return None


def _attribute(element: ElementTree.Element | None, name: str) -> str | None:
    if element is None:
        return None
    value = element.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1].upper()
