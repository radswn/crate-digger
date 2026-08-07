import base64
import json
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree

from crate_digger.collection.matching import normalize_path
from crate_digger.collection.models import (
    BeatGridMarker,
    CanonicalTrackMetadata,
    ImportedPlaylist,
    TrackProfile,
    TrackTag,
)


PROFILE_MARKER_PREFIX = "[crate-digger-profile:"
PROFILE_MARKER_SUFFIX = "]"
REKORDBOX_CUE_TYPES = {
    "cue": "0",
    "fade-in": "1",
    "fade-out": "2",
    "load": "3",
    "loop": "4",
}
TRAKTOR_CUE_TYPES = {
    "cue": "0",
    "fade-in": "1",
    "fade-out": "2",
    "load": "3",
    "grid": "4",
    "loop": "5",
}


def render_rekordbox(
    path: Path,
    tracks: list[CanonicalTrackMetadata],
    playlists: list[ImportedPlaylist],
    *,
    source_paths: dict[str, str],
    source_ids: dict[str, str],
    profiles: dict[str, TrackProfile],
    tags: dict[str, list[TrackTag]],
) -> tuple[bytes, int]:
    root = _load_or_create(path, "rekordbox")
    collection = _first(root, "COLLECTION")
    if collection is None:
        collection = ElementTree.SubElement(root, "COLLECTION", Entries="0")
    entries_by_path = {
        normalize_path(location): element
        for element in collection
        if _name(element) == "TRACK"
        and (location := element.get("Location")) is not None
    }
    entries_by_id = {
        track_id: element
        for element in collection
        if _name(element) == "TRACK"
        and (track_id := element.get("TrackID")) is not None
    }
    changes = 0
    keys_by_path: dict[str, str] = {}
    used_ids = {int(value) for value in entries_by_id if value.isdigit()}
    next_id = max(used_ids, default=0) + 1
    for track in tracks:
        source_path = source_paths.get(track.track_path, track.track_path)
        source_id = source_ids.get(track.track_path)
        element = entries_by_id.get(source_id or "")
        if element is None:
            element = entries_by_path.get(normalize_path(source_path))
        if element is None:
            while next_id in used_ids:
                next_id += 1
            source_id = source_id if source_id and source_id.isdigit() else str(next_id)
            used_ids.add(int(source_id))
            next_id += 1
            element = ElementTree.SubElement(collection, "TRACK")
            changes += 1
        else:
            source_id = element.get("TrackID") or source_id
        if source_id is None:
            source_id = str(next_id)
            next_id += 1
        changes += _set(element, "TrackID", source_id)
        changes += _set(element, "Location", _rekordbox_location(source_path))
        changes += _set(element, "Name", track.title)
        changes += _set(element, "Artist", track.artist)
        changes += _set(element, "Album", track.album)
        changes += _set(element, "Genre", track.genre)
        changes += _set(element, "Label", track.label)
        changes += _set(element, "Rating", track.rating * 51 if track.rating else 0)
        changes += _set(element, "AverageBpm", track.bpm)
        changes += _set(element, "Tonality", track.musical_key)
        changes += _set(element, "Colour", track.color)
        changes += _set(element, "PlayCount", track.play_count)
        changes += _set(element, "DateAdded", track.date_added)
        comment = merge_profile_marker(
            track.comment,
            profiles.get(track.track_path),
            tags.get(track.track_path, []),
        )
        changes += _set(element, "Comments", comment)
        changes += _replace_rekordbox_markers(element, track)
        keys_by_path[track.track_path] = source_id
    changes += _update_rekordbox_playlists(root, playlists, keys_by_path)
    changes += _set(
        collection, "Entries", sum(_name(item) == "TRACK" for item in collection)
    )
    return _xml_bytes(root), changes


def render_traktor(
    path: Path,
    tracks: list[CanonicalTrackMetadata],
    playlists: list[ImportedPlaylist],
    *,
    source_paths: dict[str, str],
    source_ids: dict[str, str],
    profiles: dict[str, TrackProfile],
    tags: dict[str, list[TrackTag]],
    preserve_existing: bool = True,
) -> tuple[bytes, int]:
    root = (
        _load_or_create(path, "traktor") if preserve_existing else _new_traktor_root()
    )
    collection = _first(root, "COLLECTION")
    if collection is None:
        collection = ElementTree.SubElement(root, "COLLECTION", ENTRIES="0")
    entries_by_path: dict[str, ElementTree.Element] = {}
    entries_by_id: dict[str, ElementTree.Element] = {}
    for element in collection:
        if _name(element) != "ENTRY":
            continue
        location = _first(element, "LOCATION")
        if location is not None:
            parsed_path = _traktor_location_path(location)
            if parsed_path:
                entries_by_path[normalize_path(parsed_path)] = element
        source_id = element.get("AUDIO_ID")
        if source_id:
            entries_by_id[source_id] = element
    changes = 0
    keys_by_path: dict[str, str] = {}
    for track in tracks:
        source_path = source_paths.get(track.track_path, track.track_path)
        source_id = source_ids.get(track.track_path)
        element = entries_by_id.get(source_id or "")
        if element is None:
            element = entries_by_path.get(normalize_path(source_path))
        if element is None:
            element = ElementTree.SubElement(collection, "ENTRY")
            changes += 1
        if source_id:
            changes += _set(element, "AUDIO_ID", source_id)
        changes += _set(element, "TITLE", track.title)
        changes += _set(element, "ARTIST", track.artist)
        location = _ensure_child(element, "LOCATION")
        for key, value in _traktor_location(source_path).items():
            changes += _set(location, key, value)
        album = _ensure_child(element, "ALBUM")
        changes += _set(album, "TITLE", track.album)
        info = _ensure_child(element, "INFO")
        changes += _set(info, "GENRE", track.genre)
        changes += _set(info, "LABEL", track.label)
        changes += _set(info, "RANKING", track.rating * 51 if track.rating else None)
        changes += _set(info, "PLAYCOUNT", track.play_count)
        changes += _set(
            info,
            "IMPORT_DATE",
            track.date_added.replace("-", "/") if track.date_added else None,
        )
        changes += _set(info, "KEY", track.musical_key)
        changes += _set(info, "COLOR", track.color)
        comment = merge_profile_marker(
            track.comment,
            profiles.get(track.track_path),
            tags.get(track.track_path, []),
        )
        changes += _set(info, "COMMENT", comment)
        tempo = _ensure_child(element, "TEMPO")
        changes += _set(tempo, "BPM", track.bpm)
        changes += _replace_traktor_markers(element, track)
        keys_by_path[track.track_path] = _traktor_primary_key(source_path)
    changes += _update_traktor_playlists(root, playlists, keys_by_path)
    changes += _set(
        collection, "ENTRIES", sum(_name(item) == "ENTRY" for item in collection)
    )
    return _xml_bytes(root), changes


def merge_profile_marker(
    comment: str | None,
    profile: TrackProfile | None,
    tags: list[TrackTag],
) -> str | None:
    base = strip_profile_marker(comment)
    manual_tags = sorted(
        f"{tag.category}:{tag.value}" for tag in tags if tag.source == "manual"
    )
    if profile is None and not manual_tags:
        return base or None
    payload = {
        "energy": profile.energy if profile else None,
        "personal_rating": profile.personal_rating if profile else None,
        "set_role": profile.set_role if profile else None,
        "notes": profile.notes if profile else None,
        "tags": manual_tags,
    }
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    marker = f"{PROFILE_MARKER_PREFIX}{encoded}{PROFILE_MARKER_SUFFIX}"
    return f"{base}\n{marker}".strip() if base else marker


def decode_profile_marker(comment: str | None) -> dict[str, object] | None:
    if not comment or PROFILE_MARKER_PREFIX not in comment:
        return None
    encoded = comment.split(PROFILE_MARKER_PREFIX, 1)[1].split(
        PROFILE_MARKER_SUFFIX, 1
    )[0]
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def strip_profile_marker(comment: str | None) -> str:
    if not comment:
        return ""
    before, marker, after = comment.partition(PROFILE_MARKER_PREFIX)
    if not marker:
        return comment.strip()
    _encoded, suffix, remaining = after.partition(PROFILE_MARKER_SUFFIX)
    return f"{before}{remaining if suffix else after}".strip()


def _replace_rekordbox_markers(
    element: ElementTree.Element, track: CanonicalTrackMetadata
) -> int:
    old = [child for child in element if _name(child) in {"POSITION_MARK", "TEMPO"}]
    for child in old:
        element.remove(child)
    for grid in track.beatgrids:
        attributes = {
            "Inizio": _number(grid.start_ms / 1000),
            "Bpm": _number(grid.bpm),
            "Metro": grid.meter or "4/4",
            "Battito": str(grid.beat),
        }
        ElementTree.SubElement(element, "TEMPO", attributes)
    for cue in track.cues:
        cue_type = REKORDBOX_CUE_TYPES.get(
            cue.kind, cue.kind.removeprefix("rekordbox:")
        )
        attributes = {
            "Name": cue.name or "",
            "Type": cue_type,
            "Start": _number(cue.start_ms / 1000),
            "Num": str(cue.hotcue if cue.hotcue is not None else -1),
        }
        if cue.length_ms is not None:
            attributes["End"] = _number((cue.start_ms + cue.length_ms) / 1000)
        ElementTree.SubElement(element, "POSITION_MARK", attributes)
    return len(old) + len(track.cues) + len(track.beatgrids)


def _replace_traktor_markers(
    element: ElementTree.Element, track: CanonicalTrackMetadata
) -> int:
    old = [child for child in element if _name(child) == "CUE_V2"]
    for child in old:
        element.remove(child)
    for index, grid in enumerate(sorted(track.beatgrids, key=_traktor_grid_start)):
        marker = ElementTree.SubElement(
            element,
            "CUE_V2",
            {
                "NAME": "Grid",
                "DISPL_ORDER": str(index),
                "TYPE": "4",
                "START": _number(_traktor_grid_start(grid)),
                "LEN": "0",
                "REPEATS": "-1",
                "HOTCUE": "-1",
            },
        )
        ElementTree.SubElement(marker, "GRID", BPM=_number(grid.bpm))
    offset = len(track.beatgrids)
    for index, cue in enumerate(track.cues, start=offset):
        cue_type = TRAKTOR_CUE_TYPES.get(cue.kind, cue.kind.removeprefix("traktor:"))
        ElementTree.SubElement(
            element,
            "CUE_V2",
            {
                "NAME": cue.name or "",
                "DISPL_ORDER": str(index),
                "TYPE": cue_type,
                "START": _number(cue.start_ms),
                "LEN": _number(cue.length_ms or 0),
                "REPEATS": "-1",
                "HOTCUE": str(cue.hotcue if cue.hotcue is not None else -1),
            },
        )
    return len(old) + len(track.cues) + len(track.beatgrids)


def _traktor_grid_start(grid: BeatGridMarker) -> float:
    # Traktor grid anchors are downbeats. Rekordbox's Battito is one-based.
    numerator, denominator = (int(part) for part in (grid.meter or "4/4").split("/"))
    if (
        grid.bpm <= 0
        or numerator <= 0
        or denominator <= 0
        or not 1 <= grid.beat <= numerator
    ):
        raise ValueError("Invalid beat-grid tempo, meter, or beat number")
    beats_to_downbeat = (1 - grid.beat) % numerator
    return grid.start_ms + beats_to_downbeat * 60000 / grid.bpm * 4 / denominator


def _update_rekordbox_playlists(
    root: ElementTree.Element,
    playlists: list[ImportedPlaylist],
    keys_by_path: dict[str, str],
) -> int:
    playlists_root = _first(root, "PLAYLISTS")
    if playlists_root is None:
        playlists_root = ElementTree.SubElement(root, "PLAYLISTS")
    root_node = next(
        (child for child in playlists_root if _name(child) == "NODE"), None
    )
    if root_node is None:
        root_node = ElementTree.SubElement(
            playlists_root, "NODE", Type="0", Name="ROOT", Count="0"
        )
    changes = 0
    for playlist in playlists:
        parent = root_node
        for folder in playlist.folder_path:
            parent = _ensure_rb_folder(parent, folder)
        node = next(
            (
                child
                for child in parent
                if _name(child) == "NODE"
                and child.get("Type") == "1"
                and child.get("Name") == playlist.name
            ),
            None,
        )
        if node is None:
            node = ElementTree.SubElement(parent, "NODE", Type="1", Name=playlist.name)
            changes += 1
        old_keys = [child.get("Key") for child in node if _name(child) == "TRACK"]
        new_keys = [
            keys_by_path[path] for path in playlist.track_paths if path in keys_by_path
        ]
        if old_keys != new_keys:
            for child in list(node):
                if _name(child) == "TRACK":
                    node.remove(child)
            for key in new_keys:
                ElementTree.SubElement(node, "TRACK", Key=key)
            changes += 1
        changes += _set(node, "Entries", len(new_keys))
    return changes


def _update_traktor_playlists(
    root: ElementTree.Element,
    playlists: list[ImportedPlaylist],
    keys_by_path: dict[str, str],
) -> int:
    playlists_root = _first(root, "PLAYLISTS")
    if playlists_root is None:
        playlists_root = ElementTree.SubElement(root, "PLAYLISTS")
    root_node = next(
        (child for child in playlists_root if _name(child) == "NODE"), None
    )
    if root_node is None:
        root_node = ElementTree.SubElement(
            playlists_root, "NODE", TYPE="FOLDER", NAME="$ROOT"
        )
        ElementTree.SubElement(root_node, "SUBNODES", COUNT="0")
    changes = 0
    for playlist in playlists:
        parent = root_node
        for folder in playlist.folder_path:
            parent = _ensure_traktor_folder(parent, folder)
        subnodes = _ensure_child(parent, "SUBNODES")
        node = next(
            (
                child
                for child in subnodes
                if _name(child) == "NODE"
                and child.get("TYPE") == "PLAYLIST"
                and child.get("NAME") == playlist.name
            ),
            None,
        )
        if node is None:
            node = ElementTree.SubElement(
                subnodes, "NODE", TYPE="PLAYLIST", NAME=playlist.name
            )
            changes += 1
        playlist_element = _ensure_child(node, "PLAYLIST")
        if not playlist_element.get("UUID"):
            playlist_element.set(
                "UUID",
                uuid5(
                    NAMESPACE_URL, json.dumps([*playlist.folder_path, playlist.name])
                ).hex,
            )
        changes += _set(playlist_element, "TYPE", "LIST")
        old_keys = [
            primary.get("KEY")
            for entry in playlist_element
            if _name(entry) == "ENTRY"
            and (primary := _first(entry, "PRIMARYKEY")) is not None
        ]
        new_keys = [
            keys_by_path[path] for path in playlist.track_paths if path in keys_by_path
        ]
        if old_keys != new_keys:
            for child in list(playlist_element):
                playlist_element.remove(child)
            for key in new_keys:
                entry = ElementTree.SubElement(playlist_element, "ENTRY")
                ElementTree.SubElement(entry, "PRIMARYKEY", TYPE="TRACK", KEY=key)
            changes += 1
        changes += _set(playlist_element, "ENTRIES", len(new_keys))
    for subnodes in root.iter("SUBNODES"):
        changes += _set(
            subnodes, "COUNT", sum(_name(child) == "NODE" for child in subnodes)
        )
    return changes


def _ensure_rb_folder(parent: ElementTree.Element, name: str) -> ElementTree.Element:
    existing = next(
        (
            child
            for child in parent
            if _name(child) == "NODE"
            and child.get("Type", "0") == "0"
            and child.get("Name") == name
        ),
        None,
    )
    if existing is not None:
        return existing
    return ElementTree.SubElement(parent, "NODE", Type="0", Name=name)


def _ensure_traktor_folder(
    parent: ElementTree.Element, name: str
) -> ElementTree.Element:
    subnodes = _ensure_child(parent, "SUBNODES")
    existing = next(
        (
            child
            for child in subnodes
            if _name(child) == "NODE"
            and child.get("TYPE", "FOLDER") == "FOLDER"
            and child.get("NAME") == name
        ),
        None,
    )
    if existing is not None:
        return existing
    folder = ElementTree.SubElement(subnodes, "NODE", TYPE="FOLDER", NAME=name)
    ElementTree.SubElement(folder, "SUBNODES", COUNT="0")
    return folder


def _load_or_create(path: Path, source: str) -> ElementTree.Element:
    if path.is_file():
        try:
            return ElementTree.parse(path).getroot()
        except ElementTree.ParseError as error:
            raise ValueError(f"Invalid {source.title()} collection: {error}") from error
    if source == "rekordbox":
        return ElementTree.Element("DJ_PLAYLISTS", Version="1.0.0")
    return _new_traktor_root()


def _new_traktor_root() -> ElementTree.Element:
    root = ElementTree.Element("NML", VERSION="20")
    ElementTree.SubElement(
        root, "HEAD", COMPANY="www.native-instruments.com", PROGRAM="Traktor Pro 4"
    )
    ElementTree.SubElement(root, "COLLECTION", ENTRIES="0")
    ElementTree.SubElement(root, "SETS", ENTRIES="0")
    return root


def _xml_bytes(root: ElementTree.Element) -> bytes:
    ElementTree.indent(root, space="  ")
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _first(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next((child for child in element if _name(child) == name), None)


def _ensure_child(element: ElementTree.Element, name: str) -> ElementTree.Element:
    existing = _first(element, name)
    return existing if existing is not None else ElementTree.SubElement(element, name)


def _name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", maxsplit=1)[-1].upper()


def _set(element: ElementTree.Element, key: str, value: object | None) -> int:
    if value is None or value == "":
        if key in element.attrib:
            del element.attrib[key]
            return 1
        return 0
    text = _number(value) if isinstance(value, float) else str(value)
    if element.get(key) == text:
        return 0
    element.set(key, text)
    return 1


def _number(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _rekordbox_location(path: str) -> str:
    normalized = normalize_path(path)
    quoted = quote(normalized.replace("\\", "/"), safe="/:")
    prefix = "/" if len(normalized) >= 2 and normalized[1] == ":" else ""
    return f"file://localhost{prefix}{quoted}"


def _traktor_location(path: str) -> dict[str, str]:
    normalized = normalize_path(path)
    pure = PurePosixPath(normalized)
    filename = pure.name
    parent = str(pure.parent)
    if len(normalized) >= 2 and normalized[1] == ":":
        volume = normalized[:2]
        relative_parent = parent[2:].strip("/")
        directory = "/:" + "/:".join(relative_parent.split("/")) + "/:"
    else:
        volume = "/"
        directory = "/:" + "/:".join(parent.strip("/").split("/")) + "/:"
    if directory == "/:/:":
        directory = "/:"
    return {"DIR": directory, "FILE": filename, "VOLUME": volume}


def _traktor_primary_key(path: str) -> str:
    location = _traktor_location(path)
    prefix = location["VOLUME"] if location["VOLUME"] != "/" else ""
    return f"{prefix}{location['DIR']}{location['FILE']}"


def _traktor_location_path(element: ElementTree.Element) -> str | None:
    filename = element.get("FILE")
    if not filename:
        return None
    directory = element.get("DIR", "").replace("/:", "/").rstrip("/")
    volume = element.get("VOLUME", "")
    if len(volume) == 2 and volume[1] == ":":
        return f"{volume}/{directory.strip('/')}/{filename}".replace("//", "/")
    return f"/{directory.strip('/')}/{filename}".replace("//", "/")
