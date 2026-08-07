from pathlib import Path
from typing import cast
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


REKORDBOX_CUE_TYPES = {
    "0": "cue",
    "1": "fade-in",
    "2": "fade-out",
    "3": "load",
    "4": "loop",
}


def parse_rekordbox(path: Path) -> list[ImportedTrack]:
    return list(parse_rekordbox_document(path).tracks)


def parse_rekordbox_document(path: Path) -> LibraryDocument:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ValueError(f"Invalid Rekordbox XML: {error}") from error
    if _local_name(root.tag).upper() != "DJ_PLAYLISTS":
        raise ValueError("Invalid Rekordbox XML: expected DJ_PLAYLISTS root element")

    tracks: list[ImportedTrack] = []
    paths_by_id: dict[str, str] = {}
    collection = next(
        (item for item in root if _local_name(item.tag).upper() == "COLLECTION"), None
    )
    if collection is None:
        raise ValueError("Invalid Rekordbox XML: missing COLLECTION")
    for element in collection:
        if _local_name(element.tag).upper() != "TRACK":
            continue
        comment = _optional(element.get("Comments") or element.get("Comment"))
        source_path = _optional(element.get("Location"))
        invalid_reason = None if source_path else "Missing track location"
        track = ImportedTrack(
            source="rekordbox",
            source_path=source_path,
            source_track_id=_optional(element.get("TrackID")),
            title=_optional(element.get("Name") or element.get("Title")),
            artist=_optional(element.get("Artist")),
            genre=_optional(element.get("Genre")),
            comment=comment,
            comment2=None,
            legacy_rating=_rating(element.get("Rating")),
            tags=tuple(
                (cast(TagCategory, category), value)
                for category, value in extract_profile_tags(comment)
            ),
            invalid_reason=invalid_reason,
            album=_optional(element.get("Album")),
            label=_optional(element.get("Label")),
            bpm=_float(element.get("AverageBpm")),
            musical_key=_optional(element.get("Tonality")),
            color=_optional(element.get("Colour") or element.get("Color")),
            play_count=_integer(element.get("PlayCount")),
            date_added=_optional(element.get("DateAdded")),
            cues=tuple(_cue(marker) for marker in _children(element, "POSITION_MARK")),
            beatgrids=tuple(
                marker
                for tempo in _children(element, "TEMPO")
                if (marker := _beatgrid(tempo)) is not None
            ),
        )
        tracks.append(track)
        if track.source_track_id and track.source_path:
            paths_by_id[track.source_track_id] = track.source_path
    playlists_root = next(
        (item for item in root.iter() if _local_name(item.tag).upper() == "PLAYLISTS"),
        None,
    )
    playlists = (
        tuple(_rekordbox_playlists(playlists_root, paths_by_id))
        if playlists_root is not None
        else ()
    )
    return LibraryDocument(
        source="rekordbox", tracks=tuple(tracks), playlists=playlists
    )


def _rating(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        rating = int(value)
    except ValueError:
        return None
    if 1 <= rating <= 5:
        return rating
    return rating // 51 if 51 <= rating <= 255 and rating % 51 == 0 else None


def _cue(element: ElementTree.Element) -> CuePoint:
    raw_type = element.get("Type", "0")
    start_ms = (_float(element.get("Start")) or 0.0) * 1000
    end = _float(element.get("End"))
    length_ms = max(0.0, end * 1000 - start_ms) if end is not None else None
    return CuePoint(
        name=_optional(element.get("Name")),
        kind=REKORDBOX_CUE_TYPES.get(raw_type, f"rekordbox:{raw_type}"),
        start_ms=start_ms,
        length_ms=length_ms,
        hotcue=_integer(element.get("Num")),
    )


def _beatgrid(element: ElementTree.Element) -> BeatGridMarker | None:
    bpm = _float(element.get("Bpm"))
    if bpm is None:
        return None
    return BeatGridMarker(
        start_ms=(_float(element.get("Inizio")) or 0.0) * 1000,
        bpm=bpm,
        meter=_optional(element.get("Metro")),
        beat=_integer(element.get("Battito")) or 1,
    )


def _rekordbox_playlists(
    playlists_root: ElementTree.Element,
    paths_by_id: dict[str, str],
) -> list[ImportedPlaylist]:
    playlists: list[ImportedPlaylist] = []

    def visit(node: ElementTree.Element, folders: tuple[str, ...]) -> None:
        if _local_name(node.tag).upper() != "NODE":
            for child in node:
                visit(child, folders)
            return
        name = _optional(node.get("Name")) or "Unnamed"
        node_type = node.get("Type", "0")
        if node_type == "1":
            paths = tuple(
                paths_by_id[key]
                for item in node
                if _local_name(item.tag).upper() == "TRACK"
                and (key := item.get("Key")) in paths_by_id
            )
            playlists.append(
                ImportedPlaylist(name=name, folder_path=folders, track_paths=paths)
            )
            return
        next_folders = (
            folders if not folders and name.upper() == "ROOT" else (*folders, name)
        )
        for child in node:
            visit(child, next_folders)

    visit(playlists_root, ())
    return playlists


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [
        child for child in element if _local_name(child.tag).upper() == name.upper()
    ]


def _float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _integer(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]
