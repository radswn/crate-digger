"""One-way Rekordbox export, independent of the dashboard index and sync state."""

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from xml.etree import ElementTree

from crate_digger.collection.exporters import render_traktor
from crate_digger.collection.importers.rekordbox import parse_rekordbox_document
from crate_digger.collection.library_sync import (
    _copy_audio_files,
    _plan_audio_copies,
    _write_outputs,
)
from crate_digger.collection.matching import PathMap, apply_path_maps, normalize_path
from crate_digger.collection.models import CanonicalTrackMetadata
from crate_digger.collection.timing import rekordbox_to_traktor_offset_ms


@dataclass(frozen=True)
class MigrationTrack:
    source_id: str | None
    title: str | None
    source_path: str
    local_path: str
    target_path: str
    error: str | None
    timing_offset_ms: float = 0
    cues: int = 0
    loops: int = 0
    grids: int = 0
    phase_adjusted_grids: int = 0


@dataclass(frozen=True)
class MigrationReport:
    dry_run: bool
    source_file: str
    target_file: str
    source_tracks: int
    playlists: int
    tracks: tuple[MigrationTrack, ...]
    backups: tuple[str, ...] = ()
    audio_files_copied: int = 0
    cover_art_references: int = 0

    @property
    def blocked(self) -> bool:
        return any(track.error for track in self.tracks) or not self.source_tracks

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "blocked": self.blocked}


def migrate_rekordbox_to_traktor(
    source: Path,
    target: Path,
    *,
    dry_run: bool = True,
    path_maps: tuple[PathMap, ...] = (),
    target_path_maps: tuple[PathMap, ...] = (),
    backup_dir: Path | None = None,
    copy_audio_to: Path | None = None,
    playlist_folder: str = "Rekordbox",
    correct_timing: bool = True,
    traktor_reference: Path | None = None,
) -> MigrationReport:
    """Produce a complete import file, leaving both live DJ libraries untouched.

    Source paths are retained in NML. Path maps (or mounted Windows drives in
    WSL) are used only to locate and inspect the audio. Missing or invalid
    tracks block the entire write and are returned in the diagnostic report.
    """
    if source.resolve() == target.resolve():
        raise ValueError("Source and destination must be different files")
    document = parse_rekordbox_document(source)
    raw_root = ElementTree.parse(source).getroot()
    raw_tracks = raw_root.findall("COLLECTION/TRACK")
    known_ids = {track.source_track_id for track in document.tracks}
    for reference in raw_root.findall(".//NODE/TRACK"):
        if reference.get("Key") not in known_ids:
            raise ValueError(
                f"Playlist references unknown TrackID {reference.get('Key')}"
            )

    metadata: list[CanonicalTrackMetadata] = []
    diagnostics: list[MigrationTrack] = []
    seen: set[str] = set()
    for track in document.tracks:
        source_path = normalize_path(track.source_path or "")
        local = _local_audio_path(source_path, path_maps)
        error = track.invalid_reason
        if not error and not local.is_file():
            error = "Audio file not found"
        if source_path in seen:
            error = "Duplicate audio location in Rekordbox collection"
        seen.add(source_path)
        offset = 0.0
        if not error and correct_timing and (track.cues or track.beatgrids):
            try:
                offset = rekordbox_to_traktor_offset_ms(local)
            except (OSError, ValueError) as exc:
                error = f"Cannot determine cue timing: {exc}"
        if any(cue.hotcue is not None and cue.hotcue > 7 for cue in track.cues):
            error = "Traktor supports hot cue slots 0–7; this track uses a higher slot"
        if any(
            cue.kind not in {"cue", "loop", "load", "fade-in", "fade-out"}
            for cue in track.cues
        ):
            error = "Unsupported cue type"
        diagnostics.append(
            MigrationTrack(
                source_id=track.source_track_id,
                title=track.title,
                source_path=source_path,
                local_path=str(local),
                target_path=apply_path_maps(source_path, target_path_maps),
                error=error,
                timing_offset_ms=offset,
                cues=sum(cue.kind != "loop" for cue in track.cues),
                loops=sum(cue.kind == "loop" for cue in track.cues),
                grids=len(track.beatgrids),
                phase_adjusted_grids=sum(grid.beat != 1 for grid in track.beatgrids),
            )
        )
        metadata.append(
            CanonicalTrackMetadata(
                track_path=source_path,
                title=track.title,
                artist=track.artist,
                album=track.album,
                genre=track.genre,
                label=track.label,
                comment=track.comment,
                rating=track.legacy_rating,
                bpm=track.bpm,
                musical_key=track.musical_key,
                color=track.color,
                play_count=track.play_count,
                date_added=track.date_added,
                cues=tuple(
                    replace(cue, start_ms=cue.start_ms + offset) for cue in track.cues
                ),
                beatgrids=tuple(
                    replace(grid, start_ms=grid.start_ms + offset)
                    for grid in track.beatgrids
                ),
                updated_at="",
            )
        )

    report = MigrationReport(
        dry_run=dry_run,
        source_file=str(source),
        target_file=str(target),
        source_tracks=len(document.tracks),
        playlists=len(document.playlists),
        tracks=tuple(diagnostics),
    )
    if report.blocked:
        return report

    audio_plan = _plan_audio_copies(
        {
            item.local_path: replace(track, track_path=item.local_path)
            for item, track in zip(diagnostics, metadata, strict=True)
        },
        copy_audio_to,
    )
    if audio_plan:
        diagnostics = [
            replace(
                item,
                target_path=apply_path_maps(
                    _destination_audio_path(audio_plan[item.local_path]),
                    target_path_maps,
                ),
            )
            for item in diagnostics
        ]
    targets = [normalize_path(item.target_path).casefold() for item in diagnostics]
    if len(set(targets)) != len(targets):
        raise ValueError(
            "Multiple source tracks map to the same destination audio path"
        )
    playlists = [
        replace(
            playlist,
            folder_path=((playlist_folder,) if playlist_folder else ())
            + playlist.folder_path,
            track_paths=tuple(normalize_path(path) for path in playlist.track_paths),
        )
        for playlist in document.playlists
    ]
    content, _ = render_traktor(
        target,
        metadata,
        playlists,
        source_paths={item.source_path: item.target_path for item in diagnostics},
        source_ids={},
        profiles={},
        tags={},
        preserve_existing=False,
    )
    root = ElementTree.fromstring(content)
    for entry, raw, track in zip(
        root.findall("COLLECTION/ENTRY"), raw_tracks, metadata, strict=True
    ):
        _complete_traktor_metadata(entry, raw, track)
    reference = traktor_reference or (target if target.is_file() else None)
    covers = restore_traktor_artwork(root, reference) if reference else 0
    ElementTree.indent(root, space="  ")
    content = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    backups: list[str] = []
    copied: list[Path] = []
    if not dry_run:
        try:
            copied = _copy_audio_files(audio_plan)
            backups = _write_outputs([(target, content)], backup_dir=backup_dir)
        except (OSError, ValueError):
            for path in reversed(copied):
                path.unlink(missing_ok=True)
            raise
    return replace(
        report,
        tracks=tuple(diagnostics),
        backups=tuple(backups),
        cover_art_references=covers,
        audio_files_copied=len(copied)
        if not dry_run
        else sum(not p.exists() for p in audio_plan.values()),
    )


def restore_traktor_artwork(root: ElementTree.Element, reference: Path) -> int:
    """Restore missing cache references by exact file location, preserving other data.

    COVERARTID points into the destination Traktor installation's Coverart folder;
    the reference collection must come from that installation. Never replace an
    existing reference, since it may represent a more recent manual cover edit.
    """
    original = ElementTree.parse(reference).getroot()
    if original.tag != "NML" or original.find("COLLECTION") is None:
        raise ValueError("Artwork reference must be a Traktor NML collection")

    def location_key(entry: ElementTree.Element) -> tuple[str, ...] | None:
        location = entry.find("LOCATION")
        if location is None:
            return None
        # Leading filename spaces are significant; don't pass through tag trimming.
        return tuple(location.get(name, "") for name in ("VOLUME", "DIR", "FILE"))

    covers = {}
    for entry in original.findall("COLLECTION/ENTRY"):
        info = entry.find("INFO")
        key = location_key(entry)
        if key is not None and info is not None and info.get("COVERARTID"):
            covers[key] = info.attrib["COVERARTID"]
    restored = 0
    for entry in root.findall("COLLECTION/ENTRY"):
        cover = covers.get(location_key(entry))
        if cover:
            info = entry.find("INFO")
            if info is None:
                info = ElementTree.SubElement(entry, "INFO")
            if not info.get("COVERARTID"):
                info.set("COVERARTID", cover)
                restored += 1
    return restored


def _local_audio_path(source_path: str, path_maps: tuple[PathMap, ...]) -> Path:
    mapped = apply_path_maps(source_path, path_maps)
    path = Path(mapped).expanduser()
    if mapped == source_path and len(source_path) > 2 and source_path[1:3] == ":/":
        mount = Path("/mnt") / source_path[0].lower()
        if mount.is_dir():
            return mount / source_path[3:]
    return path


def _destination_audio_path(path: Path) -> str:
    parts = path.resolve().parts
    if (
        len(parts) > 3
        and parts[1] == "mnt"
        and len(parts[2]) == 1
        and parts[2].isalpha()
    ):
        return f"{parts[2].upper()}:/" + "/".join(parts[3:])
    return str(path.resolve())


def _complete_traktor_metadata(
    entry: ElementTree.Element,
    raw: ElementTree.Element,
    track: CanonicalTrackMetadata,
) -> None:
    info = entry.find("INFO")
    assert info is not None
    # Keep existing comment tags verbatim; no profile markers or new tags.
    info.set("COMMENT", raw.get("Comments", raw.get("Comment", "")))
    info.set("RANKING", str((track.rating or 0) * 51))
    for rb, tk in (("TotalTime", "PLAYTIME"), ("BitRate", "BITRATE")):
        value = raw.get(rb)
        if value:
            info.set(tk, str(int(float(value) * (1000 if rb == "BitRate" else 1))))
    if raw.get("Size"):
        info.set("FILESIZE", str(int(raw.get("Size", "0")) // 1024))
    if raw.get("Year") not in (None, "", "0"):
        info.set("RELEASE_DATE", f"{raw.get('Year')}/1/1")
    album = entry.find("ALBUM")
    if album is not None and raw.get("TrackNumber"):
        album.set("TRACK", raw.get("TrackNumber", "0"))
    ElementTree.SubElement(entry, "MODIFICATION_INFO", AUTHOR_TYPE="user")
    if track.beatgrids:
        entry.set("LOCK", "1")
    if track.musical_key:
        value = _traktor_key(track.musical_key)
        if value is not None:
            ElementTree.SubElement(entry, "MUSICAL_KEY", VALUE=str(value))


def _traktor_key(key: str) -> int | None:
    # NML pitch classes: C major = 0 ... B major = 11; minor = pitch + 12.
    if key[-1:].upper() in {"A", "B"} and key[:-1].isdigit():
        number = int(key[:-1])
        if 1 <= number <= 12:
            major = (11, 6, 1, 8, 3, 10, 5, 0, 7, 2, 9, 4)[number - 1]
            return (major + 9) % 12 + 12 if key[-1:].upper() == "A" else major
    pitches = {
        "C": 0,
        "C#": 1,
        "Db": 1,
        "D": 2,
        "D#": 3,
        "Eb": 3,
        "E": 4,
        "F": 5,
        "F#": 6,
        "Gb": 6,
        "G": 7,
        "G#": 8,
        "Ab": 8,
        "A": 9,
        "A#": 10,
        "Bb": 10,
        "B": 11,
    }
    minor = key.endswith("m")
    pitch = pitches.get(key[:-1] if minor else key)
    return pitch + (12 if minor else 0) if pitch is not None else None
