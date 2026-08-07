from pathlib import Path
from typing import cast
from xml.etree import ElementTree

from crate_digger.collection.comments import extract_profile_tags
from crate_digger.collection.models import ImportedTrack, TagCategory


def parse_rekordbox(path: Path) -> list[ImportedTrack]:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise ValueError(f"Invalid Rekordbox XML: {error}") from error
    if _local_name(root.tag).upper() != "DJ_PLAYLISTS":
        raise ValueError("Invalid Rekordbox XML: expected DJ_PLAYLISTS root element")

    tracks: list[ImportedTrack] = []
    for element in root.iter():
        if _local_name(element.tag).upper() != "TRACK":
            continue
        comment = _optional(element.get("Comments") or element.get("Comment"))
        source_path = _optional(element.get("Location"))
        invalid_reason = None if source_path else "Missing track location"
        tracks.append(
            ImportedTrack(
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
            )
        )
    return tracks


def _rating(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        rating = int(value)
    except ValueError:
        return None
    return rating if 1 <= rating <= 5 else None


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]
