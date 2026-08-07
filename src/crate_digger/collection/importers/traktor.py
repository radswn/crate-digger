from math import ceil
from pathlib import Path
from typing import cast
from urllib.parse import unquote
from xml.etree import ElementTree

from crate_digger.collection.comments import extract_profile_tags
from crate_digger.collection.models import (
    BeatGridMarker,
    CuePoint,
    ImportedPlaylist,
    ImportedTrack,
    LibraryDocument,
    TagCategory,
)


TRAKTOR_CUE_TYPES = {
    "0": "cue",
    "1": "fade-in",
    "2": "fade-out",
    "3": "load",
    "4": "grid",
    "5": "loop",
}


def comment2_value(info: ElementTree.Element | None) -> str | None:
    """The Traktor Comment2 display field is INFO.RATING (RANKING is stars)."""
    return _attribute(info, "RATING")


def parse_traktor(path: Path) -> list[ImportedTrack]:
    return list(parse_traktor_document(path).tracks)


def parse_traktor_document(path: Path) -> LibraryDocument:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ValueError(f"Invalid Traktor NML: {error}") from error
    if _local_name(root.tag) != "NML":
        raise ValueError("Invalid Traktor NML: expected NML root element")

    tracks: list[ImportedTrack] = []
    collection = _child(root, "COLLECTION")
    if collection is None:
        raise ValueError("Invalid Traktor NML: missing COLLECTION")
    for entry in collection:
        if _local_name(entry.tag).upper() != "ENTRY":
            continue
        location = _child(entry, "LOCATION")
        info = _child(entry, "INFO")
        source_path = _location_path(location) if location is not None else None
        comment = _attribute(info, "COMMENT")
        comment2 = comment2_value(info)
        tags = set(extract_profile_tags(comment))
        tags.update(extract_profile_tags(comment2))
        title = _attribute(entry, "TITLE") or _attribute(info, "TITLE")
        artist = _attribute(entry, "ARTIST") or _attribute(info, "ARTIST")
        tempo = _child(entry, "TEMPO")
        bpm = _float(_attribute(tempo, "BPM"))
        raw_cues = _children(entry, "CUE_V2")
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
                album=_attribute(_child(entry, "ALBUM"), "TITLE"),
                label=_attribute(info, "LABEL"),
                bpm=bpm,
                musical_key=(
                    _attribute(info, "KEY")
                    or _attribute(_child(entry, "MUSICAL_KEY"), "VALUE")
                ),
                color=_attribute(info, "COLOR"),
                play_count=_integer(_attribute(info, "PLAYCOUNT")),
                date_added=(
                    (_attribute(info, "IMPORT_DATE") or "").replace("/", "-")
                    or _attribute(entry, "MODIFIED_DATE")
                ),
                cues=tuple(
                    _cue(cue) for cue in raw_cues if _attribute(cue, "TYPE") != "4"
                ),
                beatgrids=tuple(
                    BeatGridMarker(
                        start_ms=_float(_attribute(cue, "START")) or 0.0,
                        bpm=_float(_attribute(_child(cue, "GRID"), "BPM"))
                        or _float(_attribute(cue, "BPM"))
                        or bpm,
                        meter=_attribute(cue, "METRO") or "4/4",
                    )
                    for cue in raw_cues
                    if _attribute(cue, "TYPE") == "4" and bpm is not None
                ),
            )
        )
    return LibraryDocument(
        source="traktor",
        tracks=tuple(tracks),
        playlists=tuple(_traktor_playlists(root)),
    )


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


def _cue(element: ElementTree.Element) -> CuePoint:
    raw_type = _attribute(element, "TYPE") or "0"
    length = _float(_attribute(element, "LEN"))
    return CuePoint(
        name=_attribute(element, "NAME"),
        kind=TRAKTOR_CUE_TYPES.get(raw_type, f"traktor:{raw_type}"),
        start_ms=_float(_attribute(element, "START")) or 0.0,
        length_ms=length if length and length > 0 else None,
        hotcue=_integer(_attribute(element, "HOTCUE")),
    )


def _traktor_playlists(root: ElementTree.Element) -> list[ImportedPlaylist]:
    playlists_root = next(
        (item for item in root.iter() if _local_name(item.tag) == "PLAYLISTS"),
        None,
    )
    if playlists_root is None:
        return []
    playlists: list[ImportedPlaylist] = []

    def visit(node: ElementTree.Element, folders: tuple[str, ...]) -> None:
        if _local_name(node.tag) != "NODE":
            for child in node:
                visit(child, folders)
            return
        name = _attribute(node, "NAME") or "Unnamed"
        node_type = (_attribute(node, "TYPE") or "FOLDER").upper()
        if node_type == "PLAYLIST":
            playlist = _child(node, "PLAYLIST")
            entries = list(playlist) if playlist is not None else []
            paths = tuple(
                _primary_key_path(key)
                for item in entries
                if _local_name(item.tag) == "ENTRY"
                and (primary := _child(item, "PRIMARYKEY")) is not None
                and (key := _attribute(primary, "KEY")) is not None
            )
            playlists.append(
                ImportedPlaylist(name=name, folder_path=folders, track_paths=paths)
            )
            return
        next_folders = (
            folders if not folders and name in {"$ROOT", "ROOT"} else (*folders, name)
        )
        for child in node:
            visit(child, next_folders)

    visit(playlists_root, ())
    return playlists


def _primary_key_path(value: str) -> str:
    return unquote(value).replace("\\", "/").replace("/:", "/")


def _location_path(location: ElementTree.Element) -> str | None:
    filename = location.get("FILE")
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


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local_name(child.tag).upper() == name]


def _attribute(element: ElementTree.Element | None, name: str) -> str | None:
    if element is None:
        return None
    value = element.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _integer(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1].upper()
